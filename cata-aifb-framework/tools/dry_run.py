"""Run the whole ingestion pipeline against live mainnet WITHOUT Redis.

Why this is worth committing rather than being a throwaway: the unit tests
prove the decoder against captured fixtures and the provider against fakes,
but nothing in the suite proves the pieces work together on live data -- and
"each part passes its own tests" is exactly how an integration stays broken.
This wires the real RPC client, the real adapter and the real gap-safe
provider to an IN-MEMORY position store and publisher, so an operator can
confirm a config and credentials end to end before enabling the collector.

It writes nothing and publishes nothing outside this process. Read-only.

Usage:
  NON_EVM_SOL_RPC_URL=... python tools/dry_run.py
  python tools/dry_run.py --signatures=400
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.data.solana_rpc_client import SolanaRpcClient  # noqa: E402
from src.adapters.launchpads.pumpfun import constants as C  # noqa: E402
from src.adapters.launchpads.pumpfun.adapter import PumpfunAdapter  # noqa: E402
from src.adapters.launchpads.pumpfun.config import load_config  # noqa: E402
from src.adapters.launchpads.pumpfun.position_store import (  # noqa: E402
    PositionStore,
)
from src.adapters.launchpads.pumpfun.solana_provider import (  # noqa: E402
    GapSafeSolanaProvider,
)


class MemoryRedis:
    """Just enough of the Redis surface the position store touches."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def eval(self, script, numkeys, key, slot, signature):
        cur = int(self.store.get(key, {}).get("slot", -1))
        if int(slot) > cur:
            self.store[key] = {"slot": str(int(slot)), "signature": signature}
            return [1, str(slot)]
        return [0, str(cur)]


async def main() -> int:
    args = {a.split("=", 1)[0][2:]: a.split("=", 1)[1]
            for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    budget = int(args.get("signatures", "250"))

    cfg = load_config()
    print(f"config: {cfg.describe()}")
    print(f"program={C.PUMPFUN_PROGRAM_ID} idl_vendored_at={C.IDL_VENDORED_AT}")
    print(f"\nDRY RUN: read-only, nothing is published. budget={budget} signatures\n")

    client = SolanaRpcClient(urls=cfg.rpc_urls)
    adapter = PumpfunAdapter(chain_id=cfg.chain_id,
                             native_quote_only=cfg.native_quote_only)
    redis = MemoryRedis()
    store = PositionStore(redis=redis, chain_id=cfg.chain_id)

    launches: list = []
    graduations: list = []
    skipped: list[str] = []

    async def handler(tx: dict) -> None:
        result = await adapter.decode(tx)
        for launch in result.launches:
            launches.append(launch)
            print(f"  LAUNCH  {launch.token_symbol!r:<18} {launch.token}")
            print(f"          curve={launch.curve} deployer={launch.deployer}")
        for grad in result.graduations:
            graduations.append(grad)
            print(f"  GRAD    {getattr(grad, '_event')} {grad.token_address}")
            print(f"          pool={getattr(grad, '_pool')}")
        skipped.extend(result.skipped_non_native)

    alerts: list[str] = []

    async def on_alert(kind: str, detail: dict) -> None:
        alerts.append(kind)
        print(f"  ALERT   {kind}: {str(detail)[:120]}")

    provider = GapSafeSolanaProvider(
        client=client, store=store, handler=handler,
        program_id=C.PUMPFUN_PROGRAM_ID, feed=None,
        cold_start_signatures=budget, on_alert=on_alert)

    try:
        slot = await client.get_slot()
        print(f"chain head slot: {slot:,}\n")
        done = await provider.backfill(until_signature=None)
    finally:
        await client.aclose()

    pos = await store.read()
    print(f"\n--- dry run complete")
    print(f"transactions handled : {done}")
    print(f"launches decoded     : {len(launches)}")
    print(f"graduations decoded  : {len(graduations)}")
    print(f"non-SOL skipped      : {len(skipped)}")
    print(f"undecodable skipped  : {provider.stats.skipped_undecodable}")
    print(f"alerts raised        : {alerts or 'none'}")
    print(f"position would be    : slot={pos.slot} signature={pos.signature}")
    print(f"rpc                  : {client.stats()}")
    # A dry run that decoded nothing is not a pass: it means the pipeline is
    # wired to a program that is not producing, which is the exact symptom
    # this whole design is built to make impossible to miss.
    if done == 0:
        print("\nWARNING: no transactions were handled. Check the endpoint.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
