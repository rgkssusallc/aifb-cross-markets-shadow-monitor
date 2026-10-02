"""Position store: the monotonic invariant, which is the whole point.

The fake Redis implements the ONE Lua script this store uses, by executing
its semantics rather than its text. That is the honest way to fake an eval:
asserting on the script string would pass while the semantics were wrong.

Run: python src/adapters/launchpads/pumpfun/position_store_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from src.adapters.launchpads.pumpfun.position_store import (  # noqa: E402
    PositionStore,
)


class FakeRedis:
    """Hashes plus a compare-and-set eval matching _SET_MAX_LUA's semantics."""

    def __init__(self, initial=None):
        self.store: dict[str, dict[str, str]] = dict(initial or {})
        self.evals = 0

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def eval(self, script, numkeys, key, slot, signature):
        self.evals += 1
        assert numkeys == 1
        cur = int(self.store.get(key, {}).get("slot", -1))
        new = int(slot)
        if new > cur:
            self.store[key] = {"slot": str(new), "signature": signature}
            return [1, str(new)]
        return [0, str(cur)]


def test_an_unset_position_reads_as_slot_zero_not_as_now():
    """A cold start must be visible as unset.

    Reading "now" here is how a restart silently loses the whole outage
    window -- main.py logs the cold start loudly instead.
    """
    async def _run():
        s = PositionStore(redis=FakeRedis(), chain_id=900001)
        pos = await s.read()
        assert pos.slot == 0 and pos.signature is None
        assert pos.is_set is False
    asyncio.run(_run())


def test_the_key_is_namespaced_to_pumpfun_and_to_the_chain_id():
    """Sharing a namespace with the EVM bridge would couple two services the
    isolation rule says must be separable."""
    s = PositionStore(redis=FakeRedis(), chain_id=900001)
    assert s.key == "pumpfun-bridge:last_processed_slot:900001"
    assert "pumpfun" in s.key


def test_a_committed_position_round_trips_with_its_signature():
    """Both halves are needed: the slot orders, the signature resumes."""
    async def _run():
        r = FakeRedis()
        s = PositionStore(redis=r, chain_id=900001)
        assert await s.commit(100, "SIGA") is True
        pos = await s.read()
        assert pos.slot == 100 and pos.signature == "SIGA"
    asyncio.run(_run())


def test_the_position_refuses_to_move_backward():
    """The invariant. The live feed and the backfill write concurrently by
    design, and the backfill is working through OLDER history -- a plain SET
    would let it rewind the committed watermark and cause a replay or a gap.
    """
    async def _run():
        r = FakeRedis()
        s = PositionStore(redis=r, chain_id=900001)
        await s.commit(500, "NEW")
        assert await s.commit(100, "OLD") is False
        pos = await s.read()
        assert pos.slot == 500 and pos.signature == "NEW", (
            "an older commit must not overwrite the signature either")
        assert s.rejected_writes == 1
    asyncio.run(_run())


def test_committing_the_same_slot_twice_is_refused_as_not_forward():
    async def _run():
        s = PositionStore(redis=FakeRedis(), chain_id=900001)
        await s.commit(10, "A")
        assert await s.commit(10, "B") is False
    asyncio.run(_run())


def test_a_nonpositive_slot_is_rejected_without_touching_redis():
    """Guards against committing a slot that was never resolved."""
    async def _run():
        r = FakeRedis()
        s = PositionStore(redis=r, chain_id=900001)
        assert await s.commit(0, "X") is False
        assert await s.commit(-5, "X") is False
        assert r.evals == 0, "a bad slot must not reach the server"
    asyncio.run(_run())


def test_byte_encoded_redis_values_are_decoded():
    """A client configured without decode_responses returns bytes."""
    async def _run():
        r = FakeRedis({"pumpfun-bridge:last_processed_slot:900001":
                       {b"slot": b"77", b"signature": b"SIGB"}})
        s = PositionStore(redis=r, chain_id=900001)
        pos = await s.read()
        assert pos.slot == 77 and pos.signature == "SIGB"
    asyncio.run(_run())


def test_a_corrupt_stored_slot_reads_as_unset_rather_than_crashing():
    """A bad value must not wedge startup; a cold start is recoverable."""
    async def _run():
        r = FakeRedis({"pumpfun-bridge:last_processed_slot:900001":
                       {"slot": "not-a-number"}})
        s = PositionStore(redis=r, chain_id=900001)
        assert (await s.read()).slot == 0
    asyncio.run(_run())


def test_two_chain_ids_keep_independent_positions():
    async def _run():
        r = FakeRedis()
        main = PositionStore(redis=r, chain_id=900001)
        dev = PositionStore(redis=r, chain_id=900002)
        await main.commit(10, "M")
        await dev.commit(99, "D")
        assert (await main.read()).slot == 10
        assert (await dev.read()).slot == 99
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
