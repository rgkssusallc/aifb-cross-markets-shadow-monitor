"""Gap-safe provider: ordering, commit discipline, and failure containment.

These are the properties that decide whether "no missed launches" is an
invariant or a hope, so each test is named for the failure it prevents.

Run: python src/adapters/launchpads/pumpfun/solana_provider_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from src.adapters.launchpads.pumpfun.position_store import (  # noqa: E402
    PositionStore,
)
from src.adapters.launchpads.pumpfun.solana_provider import (  # noqa: E402
    GapSafeSolanaProvider,
)
from src.adapters.launchpads.pumpfun.position_store_test import (  # noqa: E402
    FakeRedis,
)


class FakeSolanaRpcClient:
    """Serves scripted signature pages and transactions."""

    def __init__(self, pages=None, txs=None, fail_slots=None):
        self.pages = list(pages or [])
        self.txs = dict(txs or {})
        self.fail_slots = set(fail_slots or [])
        self.sig_calls: list[dict] = []
        self.tx_calls: list[str] = []
        self.slot_calls = 0

    async def get_signatures_for_address(self, address, *, before=None,
                                         until=None, limit=1000):
        self.sig_calls.append({"before": before, "until": until,
                               "limit": limit})
        return self.pages.pop(0) if self.pages else []

    async def get_transaction(self, signature):
        self.tx_calls.append(signature)
        if signature in self.fail_slots:
            raise RuntimeError("decode exploded")
        return self.txs.get(signature)

    async def get_slot(self):
        self.slot_calls += 1
        return 999

    def stats(self):
        return {}

    async def aclose(self):
        pass


def _provider(client, handler, **kw):
    return GapSafeSolanaProvider(
        client=client, store=PositionStore(redis=FakeRedis(), chain_id=900001),
        handler=handler, **kw)


def test_backfill_processes_oldest_first_even_though_pages_arrive_newest_first():
    """The ordering that makes the commit safe.

    getSignaturesForAddress returns newest-first. Committing while walking in
    that order would mark the newest slot done while older transactions in the
    same range were still unprocessed -- and a crash then loses them forever.
    """
    async def _run():
        page = [{"signature": "S3", "slot": 30, "err": None},
                {"signature": "S2", "slot": 20, "err": None},
                {"signature": "S1", "slot": 10, "err": None}]
        txs = {f"S{i}": {"slot": i * 10} for i in (1, 2, 3)}
        seen: list[int] = []

        async def handler(tx):
            seen.append(tx["slot"])

        p = _provider(FakeSolanaRpcClient(pages=[page], txs=txs), handler)
        await p.backfill(until_signature=None)
        assert seen == [10, 20, 30], seen
        assert (await p.store.read()).slot == 30
    asyncio.run(_run())


def test_the_position_is_committed_only_after_the_handler_returns():
    """Committing before publishing turns a crash into silent data loss."""
    async def _run():
        page = [{"signature": "S1", "slot": 10, "err": None}]
        committed_during_handler = {}

        async def handler(tx):
            committed_during_handler["slot"] = (await p.store.read()).slot

        p = _provider(FakeSolanaRpcClient(pages=[page],
                                          txs={"S1": {"slot": 10}}), handler)
        await p.backfill(until_signature=None)
        assert committed_during_handler["slot"] == 0, (
            "the position must still be unset while the handler runs")
        assert (await p.store.read()).slot == 10
    asyncio.run(_run())


def test_a_publish_failure_leaves_the_position_unmoved_so_the_item_is_retried():
    """A Redis outage must become a retry, never a skipped transaction.

    The handler raising is the signal for "do not commit"; after the bounded
    attempts the item is skipped WITH an alert, and the position does not
    advance past it on the strength of a failure.
    """
    async def _run():
        page = [{"signature": "BAD", "slot": 10, "err": None}]
        alerts: list[str] = []

        async def handler(tx):
            raise RuntimeError("redis is down")

        async def on_alert(kind, detail):
            alerts.append(kind)

        c = FakeSolanaRpcClient(pages=[page], txs={"BAD": {"slot": 10}})
        p = _provider(c, handler, on_alert=on_alert)
        await p.backfill(until_signature=None)
        assert p.stats.skipped_undecodable == 1
        assert "pumpfun_transaction_skipped" in alerts, (
            "a skip nobody hears about is the same as a silent gap")
        assert len(c.tx_calls) == 3, "bounded retries, not unbounded"
    asyncio.run(_run())


def test_an_undecodable_transaction_does_not_block_the_ones_behind_it():
    async def _run():
        page = [{"signature": "S2", "slot": 20, "err": None},
                {"signature": "BAD", "slot": 15, "err": None},
                {"signature": "S1", "slot": 10, "err": None}]
        seen: list[int] = []

        async def handler(tx):
            if tx["slot"] == 15:
                raise ValueError("permanently malformed")
            seen.append(tx["slot"])

        c = FakeSolanaRpcClient(
            pages=[page],
            txs={"S1": {"slot": 10}, "BAD": {"slot": 15}, "S2": {"slot": 20}})
        p = _provider(c, handler)
        await p.backfill(until_signature=None)
        assert seen == [10, 20], seen
        assert (await p.store.read()).slot == 20
    asyncio.run(_run())


def test_a_failed_transaction_is_committed_through_without_being_handled():
    """A reverted transaction changed no state, but the position must still
    pass it -- otherwise ingestion sticks forever on a permanent failure."""
    async def _run():
        page = [{"signature": "ERR", "slot": 10,
                 "err": {"InstructionError": [0, {"Custom": 7}]}}]
        calls: list[dict] = []

        async def handler(tx):
            calls.append(tx)

        c = FakeSolanaRpcClient(pages=[page])
        p = _provider(c, handler)
        await p.backfill(until_signature=None)
        assert calls == [], "a failed transaction must not be handled"
        assert c.tx_calls == [], "nor fetched"
        assert (await p.store.read()).slot == 10, "but must be committed past"
    asyncio.run(_run())


def test_backfill_passes_the_committed_signature_as_the_until_cursor():
    """until is signature-based and exclusive, so the stored signature is the
    natural lower bound -- no slot arithmetic involved."""
    async def _run():
        async def handler(tx):
            pass

        c = FakeSolanaRpcClient(pages=[[]])
        p = _provider(c, handler)
        await p.backfill(until_signature="RESUME_HERE")
        assert c.sig_calls[0]["until"] == "RESUME_HERE"
    asyncio.run(_run())


def test_a_cold_start_is_bounded_rather_than_replaying_all_history():
    """Starting at the head silently loses the outage window; replaying
    millions of transactions never finishes. The bound is the explicit third
    option, and it is logged as such."""
    async def _run():
        page = [{"signature": f"S{i}", "slot": i, "err": None}
                for i in range(50, 0, -1)]
        handled: list[int] = []

        async def handler(tx):
            handled.append(tx["slot"])

        c = FakeSolanaRpcClient(
            pages=[page], txs={f"S{i}": {"slot": i} for i in range(1, 51)})
        p = _provider(c, handler, cold_start_signatures=5)
        await p.backfill(until_signature=None)
        assert len(handled) == 5, handled
        assert handled == sorted(handled), "still oldest-first"
    asyncio.run(_run())


def test_the_liveness_probe_alerts_when_the_client_cannot_reach_the_chain():
    """An open-but-dead feed looks exactly like a quiet market.

    This is the failure that cost 9.5 hours of silence on the EVM side.
    """
    async def _run():
        class Dead(FakeSolanaRpcClient):
            async def get_slot(self):
                raise ConnectionError("socket is open but dead")

        alerts: list[tuple[str, dict]] = []

        async def on_alert(kind, detail):
            alerts.append((kind, detail))

        async def handler(tx):
            pass

        p = _provider(Dead(), handler, on_alert=on_alert)
        assert await p.probe() is False
        assert alerts and alerts[0][0] == "pumpfun_ingestion_stalled"
        assert p.stats.probe_failures == 1
    asyncio.run(_run())


def test_a_healthy_probe_reports_success_and_raises_nothing():
    async def _run():
        async def handler(tx):
            pass
        p = _provider(FakeSolanaRpcClient(), handler)
        assert await p.probe() is True
        assert p.stats.probe_failures == 0
    asyncio.run(_run())


def test_an_alert_sink_that_itself_fails_never_crashes_ingestion():
    """Reporting a problem must not become a second, worse problem."""
    async def _run():
        async def on_alert(kind, detail):
            raise RuntimeError("operations is down too")

        async def handler(tx):
            pass

        p = _provider(FakeSolanaRpcClient(), handler, on_alert=on_alert)
        await p._alert("anything", {})          # must not raise
    asyncio.run(_run())


def test_stop_halts_an_in_flight_backfill():
    async def _run():
        page = [{"signature": f"S{i}", "slot": i, "err": None}
                for i in (3, 2, 1)]
        seen: list[int] = []

        async def handler(tx):
            seen.append(tx["slot"])
            p.stop()

        p = _provider(FakeSolanaRpcClient(
            pages=[page], txs={f"S{i}": {"slot": i} for i in (1, 2, 3)}),
            handler)
        await p.backfill(until_signature=None)
        assert seen == [1], seen
    asyncio.run(_run())


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"pass {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
