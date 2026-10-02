"""PumpfunAdapter -- turn a raw Solana transaction into AIFB domain events.

DELIBERATELY NOT A `LaunchpadAdapter`. The ABC's `decode(log)`,
`watched_addresses()` and `watched_topics()` are typed around an EVM log dict
(`topics`, `data`, `address`), and Solana has no such object: what arrives is
a program-invocation tree plus a flat log array, and one transaction can carry
several events of different kinds at once. Forcing that through the ABC would
be a fiction -- `watched_topics()` would return an empty list forever and
`decode()` would take something that is not a log.

The ABC earns its keep where one loop drives several launchpads generically
(Arc's main.py driving Tolly and CircleWarp). Nothing drives an EVM and a
non-EVM launchpad from one loop, so there is no generic dispatch to preserve
here, and widening a working contract for a single outlier leaves a seam
nobody uses. This is recorded as an open decision in
design/solana/PUMPFUN_ARCHITECTURE.md; if the owner prefers the ABC, the fix
is to widen `decode()`'s parameter type, not to reshape this class.

WHAT ONE TRANSACTION CAN CONTAIN, learned from real captured mainnet data
rather than assumed -- this is why `decode()` returns a RESULT OBJECT with
lists rather than a single Optional[TokenLaunch]:

  * a launch and its first buy together (create_v2 + buy_v2 in one tx),
  * a launch AND its graduation in the same transaction -- the DISNEY fixture
    is `create_v2` + `buy` whose CreateEvent, TradeEvent and CompleteEvent all
    share one timestamp. A decoder returning at most one event per
    transaction would drop the graduation of a token it had just discovered.
  * a graduation with no launch (migrate_v2 on a curve created long before).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.adapters.launchpads.pumpfun import constants as C
from src.adapters.launchpads.pumpfun.anchor_codec import BorshError, decode_event
from src.adapters.launchpads.pumpfun.events import (
    fee_payer_of, invoked_instruction_names, iter_event_payloads,
    mentions_pumpfun, signature_of,
)
from src.domain.chains import SOLANA_MAINNET_CHAIN_ID
from src.domain.graduation_signal import GraduationSignal
from src.domain.token_launch import TokenLaunch

log = logging.getLogger(__name__)

LAUNCHPAD = "pumpfun"


def to_wire(value: Any) -> Any:
    """Make a decoded field safe to put in JSON.

    u64 reserve values routinely exceed 2**53, and JSON numbers are IEEE-754
    doubles: round-tripping one through a JSON number loses low-order digits
    silently. Large ints therefore become strings, which is also what the
    existing NUMERIC columns want. bytes become hex for the same reason.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > 2 ** 53 else value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(v) for v in value]
    return value


@dataclass
class DecodeResult:
    """Everything one transaction yielded. Any list may be empty."""
    launches: list[TokenLaunch] = field(default_factory=list)
    graduations: list[GraduationSignal] = field(default_factory=list)
    # Launches whose quote asset is not SOL. Counted and dropped rather than
    # half-supported, because their sol_amount/real_sol_reserves fields read
    # 0 and would silently corrupt any progress arithmetic. See the non-goals
    # ledger in the design doc.
    skipped_non_native: list[str] = field(default_factory=list)
    # Events decoded but not acted on, by name. Useful for noticing that the
    # protocol started emitting something new.
    ignored_events: list[str] = field(default_factory=list)
    undecodable: int = 0

    @property
    def empty(self) -> bool:
        return not self.launches and not self.graduations


@dataclass
class PumpfunAdapter:
    """Stateless decoder. One instance can serve the whole process."""
    chain_id: int = SOLANA_MAINNET_CHAIN_ID
    launchpad: str = LAUNCHPAD
    # Native-SOL only for this pass. Set False to stop filtering, but read
    # constants.NATIVE_QUOTE_MINTS' comment first: the downstream reserve
    # fields are SOL-denominated and are zero on a token-quoted coin.
    native_quote_only: bool = True

    @property
    def program_id(self) -> str:
        return C.PUMPFUN_PROGRAM_ID

    def watched_programs(self) -> tuple[str, ...]:
        """The Solana analog of watched_addresses(): programs to subscribe to.

        Named differently from the EVM ABC on purpose -- these are programs,
        not contract addresses, and there is no topic filter to pair with them.
        """
        return (C.PUMPFUN_PROGRAM_ID,)

    # --- decoding ---------------------------------------------------------

    async def decode(self, tx: dict) -> DecodeResult:
        """Decode one `getTransaction` response into domain events.

        Async to match every other adapter's interface even though no I/O
        happens here: the caller is an async ingestion loop, and a sync
        decode in that loop is how the EVM side once starved its own
        websocket.
        """
        out = DecodeResult()
        if not tx or not mentions_pumpfun(tx):
            return out

        # A failed transaction emits no state change. Its logs can still
        # contain data lines, so this check must come before decoding or a
        # reverted create would be published as a real launch.
        if (tx.get("meta") or {}).get("err") is not None:
            return out

        slot = int(tx.get("slot") or 0)
        block_time = tx.get("blockTime")
        signature = signature_of(tx) or ""
        signer = fee_payer_of(tx)
        instructions = sorted(invoked_instruction_names(tx))

        for disc, payload in iter_event_payloads(tx):
            try:
                name, fields = decode_event(disc, payload, C.IDL)
            except BorshError as e:
                out.undecodable += 1
                log.debug("pumpfun: undecodable event in %s: %s", signature, e)
                continue

            if name == C.EVENT_CREATE:
                self._on_create(out, fields, slot, block_time, signature,
                                signer, instructions)
            elif name in (C.EVENT_COMPLETE, C.EVENT_MIGRATED):
                self._on_graduation(out, name, fields, signature, block_time)
            elif name != C.EVENT_TRADE:
                out.ignored_events.append(name)

        return out

    def _on_create(self, out: DecodeResult, f: dict, slot: int,
                   block_time: Optional[int], signature: str,
                   signer: Optional[str], instructions: list[str]) -> None:
        mint = f.get("mint")
        if not mint:
            out.undecodable += 1
            return

        quote_mint = f.get("quote_mint")
        if self.native_quote_only and not C.is_native_sol_quote(quote_mint):
            out.skipped_non_native.append(mint)
            return

        # DEPLOYER: `creator`, falling back to `user`.
        #
        # These are different fields and on pump.fun they routinely differ:
        # `user` signs the transaction, `creator` is an explicit argument to
        # `create`/`create_v2` and is who the protocol itself treats as the
        # coin's creator (it is who collects the creator fee). Launching
        # through a bot or a proxy signer is the norm, so `user` is often the
        # bot and `creator` the human. Downstream deployer-reputation work
        # wants the latter, so `creator` is authoritative and `user` is the
        # fallback for pre-`creator` history. Both are kept in `raw`.
        deployer = f.get("creator") or f.get("user") or signer or ""

        # block_timestamp: the event's own on-chain timestamp is preferred
        # over blockTime because it is what the program recorded. Neither is
        # ever replaced with "now" -- a launch backdated to whenever the
        # indexer ran is worse than one with a missing timestamp, because the
        # error is invisible and poisons every downstream age calculation.
        ts = f.get("timestamp") or block_time or 0

        out.launches.append(TokenLaunch(
            chain_id=self.chain_id,
            launchpad=self.launchpad,
            token=mint,
            deployer=deployer,
            block_number=slot,
            block_timestamp=int(ts),
            tx_hash=signature,
            token_name=f.get("name"),
            token_symbol=f.get("symbol"),
            # curve, never pool: this is a bonding-curve launchpad.
            curve=f.get("bonding_curve"),
            metadata_uri=f.get("uri"),
            graduation_threshold=None,   # see the design doc's non-goals
            raw={
                "event": C.EVENT_CREATE,
                "instructions": instructions,
                "signer": signer,
                "slot": slot,
                "block_time": block_time,
                "idl_vendored_at": C.IDL_VENDORED_AT,
                "fields": to_wire(f),
            },
        ))

    def _on_graduation(self, out: DecodeResult, name: str, f: dict,
                       signature: str, block_time: Optional[int]) -> None:
        """Both graduation moments, which are NOT the same event.

        CompleteEvent fires when the curve fills. CompletePumpAmmMigrationEvent
        fires when liquidity actually lands in a PumpSwap pool, and only the
        latter carries `pool`. They can be separated in time, and the second
        can fail to happen at all, so collapsing them would publish a
        graduation for a token that has no pool to trade on.

        Both are published, because `launches.graduated` means "has left the
        curve" and CompleteEvent is the honest first evidence of that. The
        consumer de-duplicates on (chain_id, token_address); the pool address
        rides in the migration signal's own payload.
        """
        mint = f.get("mint")
        if not mint:
            out.undecodable += 1
            return
        ts = f.get("timestamp") or block_time
        iso = (datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
               if ts else None)
        sig = GraduationSignal(
            chain_id=self.chain_id,
            token_address=mint,
            graduated_at_onchain=iso,
            tx_hash=signature,
        )
        # The dataclass is the shared contract and is not widened here. The
        # extra facts a migration carries -- which event, and the pool -- are
        # attached for the publisher to put in the stream envelope.
        setattr(sig, "_event", name)
        setattr(sig, "_pool", f.get("pool"))
        out.graduations.append(sig)
