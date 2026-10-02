"""Pull Anchor event payloads out of a raw `getTransaction` response.

Solana has nothing resembling `eth_getLogs`: there is no indexed topic to
filter on and no flat per-event record. What a transaction gives you is a
program-invocation tree plus a list of log strings, and an Anchor event may
appear in either. This module is the whole of that extraction, kept separate
from the adapter so the adapter deals in decoded events and never in wire
shapes.

THREE THINGS HERE ARE LOAD-BEARING, each learned by reading real mainnet
transactions rather than documentation:

1. INNER INSTRUCTIONS ARE NOT OPTIONAL. pump.fun is frequently invoked by CPI
   from a router or aggregator at stack depth 2 or more -- the first live
   transaction inspected while building this was a `sell` reached through
   another program, with pump.fun never appearing as a top-level instruction.
   An extractor that scans only `message.instructions` for a matching
   programId sees a fraction of real activity and reports a quiet program.
   Anchor's own emit_cpi! events are themselves inner instructions, so this is
   doubly true.

2. PROGRAM IDS MUST BE RESOLVED THROUGH THE LOOKUP TABLES. Instructions
   reference programs by INDEX into the account-key list. For a versioned
   transaction that list is the static keys PLUS the addresses loaded from
   address lookup tables, in a defined order: static, then loaded writable,
   then loaded readonly. Resolving against the static keys alone makes indices
   past the static range either miss or -- worse -- land on the wrong program,
   and versioned transactions are the norm in this traffic.

3. BOTH EVENT TRANSPORTS. `Program data:` log lines (Anchor's emit!) and
   self-CPI event instructions (emit_cpi!). Logs can be truncated by the
   runtime, which is why Anchor moved to CPI events; CPI events are absent
   from older history. Reading only one transport loses events, so both are
   read and the results de-duplicated.
"""
from __future__ import annotations

from typing import Any, Iterator

from src.adapters.launchpads.pumpfun import constants as C
from src.adapters.launchpads.pumpfun.anchor_codec import (
    b58decode, parse_program_data_logs,
)
from src.adapters.launchpads.pumpfun.constants import DISCRIMINATOR_LEN


def account_keys(tx: dict) -> list[str]:
    """Every account key an instruction index can refer to, in wire order.

    Order is fixed by the runtime: static message keys, then lookup-table
    writable, then lookup-table readonly. Getting this order wrong silently
    attributes an instruction to the wrong program.
    """
    msg = ((tx.get("transaction") or {}).get("message") or {})
    keys: list[str] = []
    for k in msg.get("accountKeys") or []:
        # jsonParsed encoding gives dicts; plain json gives strings.
        keys.append(k["pubkey"] if isinstance(k, dict) else k)
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def _program_of(ix: dict, keys: list[str]) -> str | None:
    idx = ix.get("programIdIndex")
    if idx is None:
        # jsonParsed form names the program directly.
        return ix.get("programId")
    if not isinstance(idx, int) or not 0 <= idx < len(keys):
        return None
    return keys[idx]


def iter_instructions(tx: dict) -> Iterator[tuple[str | None, dict]]:
    """Every instruction in the transaction, top-level and inner alike.

    Yields (program_id, instruction). Flattened deliberately: the stack depth
    of a pump.fun invocation is not something ingestion should care about, and
    caring about it is how CPI-reached activity gets dropped.
    """
    keys = account_keys(tx)
    msg = ((tx.get("transaction") or {}).get("message") or {})
    for ix in msg.get("instructions") or []:
        yield _program_of(ix, keys), ix
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            yield _program_of(ix, keys), ix


def _ix_data(ix: dict) -> bytes:
    """Instruction data as bytes. `json` encoding gives base58."""
    raw = ix.get("data")
    if not raw or not isinstance(raw, str):
        return b""
    try:
        return b58decode(raw)
    except Exception:  # noqa: BLE001 -- a malformed instruction is not fatal
        return b""


def iter_event_payloads(tx: dict) -> Iterator[tuple[bytes, bytes]]:
    """Yield (8-byte event discriminator, borsh payload) for every event.

    Both transports are read and the pair is de-duplicated, because an event
    emitted via emit_cpi! also appears in the logs on some program versions
    and double-counting a CreateEvent would publish a launch twice.
    """
    seen: set[tuple[bytes, bytes]] = set()

    # Transport 2: self-CPI event instructions addressed to pump.fun itself.
    for program_id, ix in iter_instructions(tx):
        if program_id != C.PUMPFUN_PROGRAM_ID:
            continue
        data = _ix_data(ix)
        if len(data) < 2 * DISCRIMINATOR_LEN:
            continue
        if data[:DISCRIMINATOR_LEN] != C.EVENT_CPI_MARKER:
            continue
        pair = (data[DISCRIMINATOR_LEN:2 * DISCRIMINATOR_LEN],
                data[2 * DISCRIMINATOR_LEN:])
        if pair not in seen:
            seen.add(pair)
            yield pair

    # Transport 1: `Program data:` log lines, attributed to pump.fun only --
    # the fee program and PumpSwap write their own lines into the same array.
    logs = (tx.get("meta") or {}).get("logMessages") or []
    for blob in parse_program_data_logs(logs, C.PUMPFUN_PROGRAM_ID):
        if len(blob) < DISCRIMINATOR_LEN:
            continue
        pair = (blob[:DISCRIMINATOR_LEN], blob[DISCRIMINATOR_LEN:])
        if pair not in seen:
            seen.add(pair)
            yield pair


def mentions_pumpfun(tx: dict) -> bool:
    """Cheap pre-filter: does this transaction touch pump.fun at all?

    Checks the account-key list rather than the instruction tree, because the
    program id must appear as a key for any invocation, at any depth, and this
    avoids walking the tree for the many transactions that are irrelevant.
    """
    return C.PUMPFUN_PROGRAM_ID in account_keys(tx)


def invoked_instruction_names(tx: dict) -> set[str]:
    """Which pump.fun instructions this transaction invoked, by name.

    Used to tell the two launch paths (create / create_v2) and the two
    graduation paths (migrate / migrate_v2) apart for auditing. Events alone
    cannot distinguish them: create and create_v2 both emit CreateEvent.
    """
    by_disc = {bytes(i["discriminator"]): i["name"]
               for i in C.IDL.get("instructions") or []
               if i.get("discriminator")}
    out: set[str] = set()
    for program_id, ix in iter_instructions(tx):
        if program_id != C.PUMPFUN_PROGRAM_ID:
            continue
        data = _ix_data(ix)
        if len(data) < DISCRIMINATOR_LEN:
            continue
        name = by_disc.get(data[:DISCRIMINATOR_LEN])
        if name:
            out.add(name)
    return out


def signature_of(tx: dict) -> str | None:
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else None


def fee_payer_of(tx: dict) -> str | None:
    """The first account key: the fee payer, i.e. the transaction signer."""
    keys = account_keys(tx)
    return keys[0] if keys else None
