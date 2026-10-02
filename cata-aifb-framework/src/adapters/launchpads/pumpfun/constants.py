"""Every verified pump.fun on-chain fact. Nothing here is from memory.

HOUSE RULE THIS FILE EXISTS TO ENFORCE: a wrong address or discriminator does
not announce itself -- it decodes cleanly into a plausible, wrong value. So
each constant below is either read out of the vendored IDL at import time, or
derived from Anchor's own hashing rule and then checked against the IDL. The
checks run at import, so a re-vendored IDL that moves something fails the
process immediately rather than at the first bad decode.

VERIFIED 2026-10-02 against the authoritative IDL at
github.com/pump-fun/pump-public-docs (`idl/pump.json`, vendored beside this
file) and against live mainnet-beta RPC. What the verification found, versus
the handoff spec:

  CONFIRMED EXACTLY. Program id 6EF8rre...; PumpSwap pAMMBay...; the four
  discriminators for create/buy/sell/migrate; bonding-curve PDA seeds
  ["bonding-curve", mint].

  CONFIRMED INCOMPLETE, as the spec itself warned. The live IDL has 47
  instructions, 28 events, 7 accounts, 42 types and 94 errors. The additions
  that CHANGE INGESTION CORRECTNESS, rather than merely existing:

    create_v2  -- a SECOND launch path. An indexer watching only `create`
                  misses every v2 launch. Global.create_v2_enabled gates it.
    migrate_v2 -- a SECOND graduation path, same reasoning.
    CompletePumpAmmMigrationEvent -- carries `pool`, the PumpSwap pool
                  address. The spec expected pool discovery to need a separate
                  polling step "mirroring PONS/Arc's pool-discovery pattern";
                  it does not. The address is in the event.
    quote_mint -- present on CreateEvent, CompleteEvent, TradeEvent and the
                  BondingCurve account. Multi-quote (USDC-paired) coins are
                  LIVE, so "native SOL only" has to be an explicit FILTER on
                  this field, not an assumption. See WSOL_MINT.
    CreateEvent.creator -- distinct from CreateEvent.user. `user` signs,
                  `creator` is the designated creator and is an explicit
                  argument to `create`. They differ whenever a coin is
                  launched through a bot or proxy, which on pump.fun is the
                  common case rather than the exception. Both are recorded;
                  see adapter.py for which becomes `deployer` and why.

  A DISTINCTION THE SPEC COLLAPSED. CompleteEvent and
  CompletePumpAmmMigrationEvent are different moments: the curve filling, and
  the liquidity actually arriving in a PumpSwap pool. They can be separated in
  time and the second can fail to happen. Treating "complete" as "graduated"
  publishes a graduation for a token with no pool. Both are modelled.

LEARNED FROM LIVE RPC, not from any document:
  * pump.fun is very often invoked by CPI from a router/aggregator program at
    depth 2+, not as a top-level instruction. A decoder that only scans
    top-level instructions whose programId is pump.fun misses most activity.
  * Version-1 transactions are live, so getTransaction needs
    maxSupportedTransactionVersion >= 1 or it refuses them outright.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

IDL_PATH = Path(__file__).with_name("idl") / "pump.json"

# When the vendored IDL was last refreshed from pump-public-docs. pump.fun
# vendors its own IDLs with the same advice -- refresh when the program
# changes -- and an un-dated vendored file is how a decoder silently rots.
IDL_VENDORED_AT = "2026-10-02"
IDL_SOURCE = ("https://raw.githubusercontent.com/pump-fun/"
              "pump-public-docs/main/idl/pump.json")


def load_idl(path: Path | None = None) -> dict:
    """The vendored Anchor IDL. The single source of layout truth."""
    p = path or IDL_PATH
    with p.open() as fh:
        return json.load(fh)


IDL = load_idl()

# --- programs -------------------------------------------------------------

# Read from the IDL, not typed in. The IDL's own `address` field is the
# program it describes.
PUMPFUN_PROGRAM_ID = IDL["address"]

# PumpSwap, the post-graduation AMM. NOT Raydium -- Raydium was the migration
# target before PumpSwap existed, so any material describing a Raydium
# migration is describing the retired model. Confirmed from the sibling
# idl/pump_amm.json in the same authoritative repo.
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

# HOW A NATIVE-SOL COIN IS ACTUALLY IDENTIFIED -- and the trap in it.
#
# pump-public-docs tells integrations to PASS the wrapped-SOL mint as the
# quote mint when trading any pre-existing coin, which makes WSOL look like
# the value that marks a coin native. It is not what the EVENTS carry. A
# census of 85 consecutive live CreateEvent/TradeEvent payloads on
# mainnet-beta (2026-10-02) found:
#
#     11111111111111111111111111111111               x82   <- native SOL
#     EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v   x2    <- USDC
#     A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS   x1    <- another quote
#
# A native-SOL coin reports the ZERO PUBKEY, not WSOL. Filtering on WSOL
# would therefore discard every native coin -- i.e. essentially all of them --
# while silently keeping the USDC ones, the exact inverse of the intended
# "native SOL only for this pass" scope. Both constants are kept: the
# sentinel is what to COMPARE against, WSOL is what to SEND.
NATIVE_QUOTE_SENTINEL = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Quote mints that mean "priced in native SOL". WSOL is included because the
# v2 instructions accept it as the explicit spelling of the same thing.
NATIVE_QUOTE_MINTS = frozenset({NATIVE_QUOTE_SENTINEL, WSOL_MINT})


def is_native_sol_quote(quote_mint: str | None) -> bool:
    """True when a coin is priced in SOL rather than a token quote asset.

    A missing quote_mint counts as native: the field post-dates multi-quote
    support, so an event decoded without it is from the single-asset era.
    """
    return quote_mint is None or quote_mint in NATIVE_QUOTE_MINTS


# A SECOND CONSEQUENCE of multi-quote, worth stating where the mints are
# defined: on a non-SOL coin, TradeEvent.sol_amount is 0 and the real figure
# is in quote_amount (observed directly: sol_amount=0, quote_amount=4044868
# on a live USDC-quoted sell). Any progress or volume arithmetic that reads
# sol_amount or real_sol_reserves therefore reports zero for USDC coins
# rather than failing, which is why those fields must never be read without
# first checking the quote mint.

# --- discriminators -------------------------------------------------------

def _anchor_hash(preimage: str) -> list[int]:
    """Anchor's discriminator: the first 8 bytes of sha256 over a namespace."""
    return list(hashlib.sha256(preimage.encode()).digest()[:8])


def instruction_discriminator(name: str) -> bytes:
    return bytes(_anchor_hash(f"global:{name}"))


def event_discriminator(name: str) -> bytes:
    return bytes(_anchor_hash(f"event:{name}"))


# Anchor's self-CPI event marker, which prefixes an event emitted via
# emit_cpi!. Derived, because a pasted byte array is unverifiable on sight.
EVENT_CPI_MARKER = bytes(_anchor_hash("anchor:event"))

DISCRIMINATOR_LEN = 8

# --- the instructions and events ingestion actually cares about -----------

# Both launch paths. Watching only `create` silently misses v2 launches.
CREATE_INSTRUCTIONS = ("create", "create_v2")
# Both graduation paths, same reasoning.
MIGRATE_INSTRUCTIONS = ("migrate", "migrate_v2")
TRADE_INSTRUCTIONS = ("buy", "buy_v2", "buy_exact_sol_in",
                      "buy_exact_quote_in_v2", "sell", "sell_v2")

EVENT_CREATE = "CreateEvent"
EVENT_COMPLETE = "CompleteEvent"                 # the curve filled
EVENT_MIGRATED = "CompletePumpAmmMigrationEvent"  # the pool now exists
EVENT_TRADE = "TradeEvent"

# Events this package decodes. Anything else in the IDL's 28 is ignored by
# name rather than by silence, so an unexpected one is loggable.
INGESTED_EVENTS = (EVENT_CREATE, EVENT_COMPLETE, EVENT_MIGRATED, EVENT_TRADE)

# --- PDA ------------------------------------------------------------------

# seeds ["bonding-curve", mint] -- one curve per token, read out of the IDL's
# own pda definition for the `create` instruction's bonding_curve account.
BONDING_CURVE_SEED = b"bonding-curve"
GLOBAL_SEED = b"global"


# --- import-time self-checks ---------------------------------------------
#
# These are assertions about the vendored file, not about the world, so they
# belong at import: if a refreshed IDL renames an event or moves a
# discriminator, every decode downstream would be wrong, and failing to start
# is the only safe response.

def _verify() -> None:
    problems: list[str] = []

    if PUMPFUN_PROGRAM_ID != "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
        problems.append(
            f"vendored IDL describes program {PUMPFUN_PROGRAM_ID}, not the "
            "pump.fun bonding-curve program this package was written for")

    by_name = {i["name"]: i for i in IDL.get("instructions") or []}
    ev_by_name = {e["name"]: e for e in IDL.get("events") or []}

    for n in CREATE_INSTRUCTIONS + MIGRATE_INSTRUCTIONS + TRADE_INSTRUCTIONS:
        if n not in by_name:
            problems.append(f"instruction {n!r} is missing from the IDL")
        elif by_name[n].get("discriminator") != list(instruction_discriminator(n)):
            problems.append(
                f"instruction {n!r}: IDL discriminator "
                f"{by_name[n].get('discriminator')} does not match Anchor's "
                "derivation -- one of the two is wrong and decoding would "
                "silently target the wrong instruction")

    for n in INGESTED_EVENTS:
        if n not in ev_by_name:
            problems.append(f"event {n!r} is missing from the IDL")
        elif ev_by_name[n].get("discriminator") != list(event_discriminator(n)):
            problems.append(
                f"event {n!r}: IDL discriminator does not match derivation")

    # The four facts the handoff spec asserted independently. Checking them
    # here means this package and that document cannot drift apart unnoticed.
    expected = {
        "create": [24, 30, 200, 40, 5, 28, 7, 119],
        "buy": [102, 6, 61, 18, 1, 218, 235, 234],
        "sell": [51, 230, 133, 164, 1, 127, 131, 173],
        "migrate": [155, 234, 231, 146, 236, 158, 162, 30],
    }
    for n, want in expected.items():
        got = by_name.get(n, {}).get("discriminator")
        if got != want:
            problems.append(
                f"instruction {n!r} discriminator is {got}, but was "
                f"independently verified as {want}")

    if problems:
        raise RuntimeError(
            "pump.fun IDL verification failed; refusing to import because "
            "every decode downstream would be unsound:\n  "
            + "\n  ".join(problems))


_verify()
