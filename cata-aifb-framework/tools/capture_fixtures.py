"""Capture REAL mainnet-beta pump.fun transactions as test fixtures.

This exists because of a standard this repo holds and this package inherits:
a decoder tested only against hand-written payloads has not been verified. A
synthetic fixture is written to match the decoder's own assumptions, so the
pair agrees with itself and disagrees with the chain -- which is precisely the
failure a decoder test is supposed to catch.

So the fixtures under pumpfun/fixtures/ are unmodified `getTransaction`
responses, and this tool is how they were obtained and how they get refreshed.
Committing the tool alongside them means the next person can re-capture after
a protocol change instead of trusting a file of unknown provenance.

WHAT IT LOOKS FOR. One transaction per interesting event: CreateEvent (a new
launch), TradeEvent (a buy and a sell), CompleteEvent (curve filled) and
CompletePumpAmmMigrationEvent (pool created). The last two are RARE relative
to trades -- the overwhelming majority of pump.fun traffic is buys and sells
-- so it pages back through signature history until it finds them or hits a
budget, rather than assuming a single page will contain one.

Usage:
  NON_EVM_SOL_RPC_URL=... python tools/capture_fixtures.py
  python tools/capture_fixtures.py --limit-pages=40 --out=<dir>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.data.solana_rpc_client import SolanaRpcClient  # noqa: E402
from src.adapters.launchpads.pumpfun import constants as C  # noqa: E402
from src.adapters.launchpads.pumpfun.anchor_codec import (  # noqa: E402
    BorshError, decode_event, parse_program_data_logs,
)
from src.adapters.launchpads.pumpfun.events import (  # noqa: E402
    iter_event_payloads,
)

DEFAULT_OUT = (Path(__file__).resolve().parents[1] / "src" / "adapters" /
               "launchpads" / "pumpfun" / "fixtures")

# One fixture per event we ingest, named for what it proves.
WANTED = {
    C.EVENT_CREATE: "create_event",
    C.EVENT_TRADE: "trade_event",
    C.EVENT_COMPLETE: "complete_event",
    C.EVENT_MIGRATED: "migration_event",
}


def endpoints() -> list[str]:
    urls = [os.environ.get("NON_EVM_SOL_RPC_URL", ""),
            os.environ.get("SOLANA_RPC_HTTP_URL", ""),
            "https://api.mainnet-beta.solana.com"]
    return [u for u in urls if u]


async def main() -> int:
    args = {a.split("=", 1)[0][2:]: a.split("=", 1)[1]
            for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    out_dir = Path(args.get("out", DEFAULT_OUT))
    out_dir.mkdir(parents=True, exist_ok=True)
    max_pages = int(args.get("limit-pages", "25"))
    page_size = int(args.get("page-size", "200"))

    client = SolanaRpcClient(urls=endpoints())
    found: dict[str, str] = {}
    before: str | None = None
    scanned = 0

    try:
        for page in range(max_pages):
            sigs = await client.get_signatures_for_address(
                C.PUMPFUN_PROGRAM_ID, before=before, limit=page_size)
            if not sigs:
                print("  signature history exhausted")
                break
            before = sigs[-1]["signature"]
            for entry in sigs:
                if entry.get("err") is not None:
                    continue           # a failed tx emits no events
                if set(WANTED) <= set(found):
                    break
                sig = entry["signature"]
                tx = await client.get_transaction(sig)
                scanned += 1
                if not tx:
                    continue
                names = set()
                for disc, payload in iter_event_payloads(tx):
                    try:
                        name, _ = decode_event(disc, payload, C.IDL)
                    except BorshError:
                        continue
                    names.add(name)
                for name, stem in WANTED.items():
                    if name in names and name not in found:
                        path = out_dir / f"{stem}.json"
                        path.write_text(json.dumps(
                            {"_fixture": stem,
                             "_captured_at_slot": entry.get("slot"),
                             "_signature": sig,
                             "_source": "mainnet-beta getTransaction, unmodified",
                             "transaction_response": tx},
                            indent=1))
                        found[name] = sig
                        print(f"  captured {name:<32} -> {path.name} "
                              f"(slot {entry.get('slot')})")
            print(f"  page {page + 1}: scanned {scanned} tx, "
                  f"have {len(found)}/{len(WANTED)}")
            if set(WANTED) <= set(found):
                break
    finally:
        await client.aclose()

    missing = [n for n in WANTED if n not in found]
    if missing:
        print(f"\nNOT FOUND after {scanned} transactions: {', '.join(missing)}")
        print("  Graduations are rare relative to trades; raise --limit-pages.")
    (out_dir / "MANIFEST.json").write_text(json.dumps(
        {"captured": found, "transactions_scanned": scanned,
         "idl_vendored_at": C.IDL_VENDORED_AT}, indent=1))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
