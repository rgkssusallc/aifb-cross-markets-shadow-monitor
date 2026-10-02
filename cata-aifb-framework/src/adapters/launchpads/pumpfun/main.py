"""pump.fun ingestion entrypoint. Standalone, like arc/main.py.

NEVER imports src.bridge or the pons/arc launchpad packages. Shares only
genuinely generic infra: the domain dataclasses, src.config helpers, and the
Solana RPC client under adapters/data. static_rules_test.py enforces this
rather than trusting it, because an import added in a hurry is exactly how an
isolation rule quietly stops being true.

KILL SWITCH FIRST, before any network call. PUMPFUN_COLLECTOR_ENABLED must be
exactly "true"; anything else means idle. An idle process logs its effective
config once and then sleeps, making zero RPC calls -- so this can be deployed
and left dormant, which is the only safe way to ship a new chain's ingestion
into a running system.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from src.adapters.data.solana_rpc_client import SolanaRpcClient
from src.adapters.launchpads.pumpfun import constants as C
from src.adapters.launchpads.pumpfun.adapter import PumpfunAdapter
from src.adapters.launchpads.pumpfun.config import COMPONENT, load_config
from src.adapters.launchpads.pumpfun.ops_reporter import OperationsReporter
from src.adapters.launchpads.pumpfun.position_store import PositionStore
from src.adapters.launchpads.pumpfun.publisher import RedisStreamPublisher
from src.adapters.launchpads.pumpfun.solana_provider import (
    GapSafeSolanaProvider,
)

log = logging.getLogger("pumpfun")

IDLE_SLEEP_S = 300


async def _redis(cfg) -> object:
    import redis.asyncio as aioredis
    return aioredis.Redis(
        host=cfg.redis_host, port=cfg.redis_port,
        password=cfg.redis_password or None, decode_responses=False)


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()

    # One startup line with the effective config -- hosts only, never full
    # provider URLs, which carry API keys in the path.
    log.info("pumpfun collector starting: %s", cfg.describe())
    log.info("pump.fun program=%s pumpswap=%s idl_vendored_at=%s",
             C.PUMPFUN_PROGRAM_ID, C.PUMPSWAP_PROGRAM_ID, C.IDL_VENDORED_AT)

    if not cfg.enabled:
        log.warning(
            "PUMPFUN_COLLECTOR_ENABLED is not exactly 'true' -- idling with "
            "zero RPC calls. Flip it to enable ingestion.")
        while True:
            await asyncio.sleep(IDLE_SLEEP_S)

    redis = await _redis(cfg)
    client = SolanaRpcClient(urls=cfg.rpc_urls)
    adapter = PumpfunAdapter(chain_id=cfg.chain_id,
                             native_quote_only=cfg.native_quote_only)
    publisher = RedisStreamPublisher(redis=redis)
    store = PositionStore(redis=redis, chain_id=cfg.chain_id)
    reporter = OperationsReporter(base_url=cfg.operations_url,
                                 component=COMPONENT,
                                 interval_seconds=cfg.heartbeat_interval_s)

    async def handle(tx: dict) -> None:
        """Decode and publish. Raising here prevents the position commit.

        That is deliberate: a publish failure must become a retry, never a
        silently skipped transaction.
        """
        result = await adapter.decode(tx)
        for launch in result.launches:
            await publisher.publish_launch(launch)
        for grad in result.graduations:
            await publisher.publish_graduation(grad)
        if result.skipped_non_native:
            log.info("pumpfun: skipped %d non-SOL-quoted launch(es): %s",
                     len(result.skipped_non_native),
                     ",".join(result.skipped_non_native[:3]))
        if result.ignored_events:
            log.debug("pumpfun: ignored events %s", result.ignored_events)

    async def alert(kind: str, detail: dict) -> None:
        await reporter.alert(kind, detail)

    # NOTE: feed=None. No Geyser/gRPC provider is configured, so this runs in
    # the REST polling fallback -- correct and gap-safe, but slower to notice
    # a launch than the production design. Which Geyser provider to buy is an
    # owner decision; see the design doc's "Real-time feed" section.
    if not cfg.geyser_url:
        log.warning("no SOLANA_GEYSER_GRPC_URL configured: running the REST "
                    "polling fallback. Gap-safe, but higher latency than the "
                    "intended Geyser stream.")
    provider = GapSafeSolanaProvider(
        client=client, store=store, handler=handle,
        program_id=C.PUMPFUN_PROGRAM_ID, feed=None,
        cold_start_signatures=cfg.cold_start_signatures, on_alert=alert)

    stopping = asyncio.Event()

    def _stop(*_: object) -> None:
        log.info("pumpfun: shutdown requested")
        provider.stop()
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, _stop)

    async def heartbeat_loop() -> None:
        """A tick that RAN counts as ran, even if it failed.

        Staleness must only ever mean "stopped"; a broken-but-running service
        reports itself through an alert kind instead.
        """
        while not stopping.is_set():
            await reporter.heartbeat(ok=True, detail={
                "stats": vars(provider.stats),
                "publisher": {"launches": publisher.launches,
                              "graduations": publisher.graduations},
                "rpc": client.stats(),
            })
            try:
                await asyncio.wait_for(stopping.wait(),
                                       timeout=cfg.heartbeat_interval_s)
            except asyncio.TimeoutError:
                pass

    hb = asyncio.create_task(heartbeat_loop())
    try:
        await provider.run()
    finally:
        stopping.set()
        hb.cancel()
        await reporter.aclose()
        await client.aclose()
        log.info("pumpfun collector stopped: %s", vars(provider.stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
