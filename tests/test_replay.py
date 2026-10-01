"""Unit tests for the fill simulator.

The whole point of replay.py is to refuse to flatter the strategy, so these
tests are built around the specific ways a replay lies:

  - filling against the book that triggered the signal (tests 2, 5)
  - extrapolating a stale book forward when data runs out (test 4)
  - pricing sequential legs as if they executed simultaneously (test 5)
  - letting unscorable attempts quietly raise the hit rate (test 10)

Every expected number is hand-computed in the comments. Fees are zero in most
cases so the arithmetic stays checkable by eye -- fee handling is already
covered by test_netedge.py.

Run: python tests/test_replay.py     (no pytest required)
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from legs import Book, Level  # noqa: E402
from replay import (  # noqa: E402
    ATOMIC,
    SEQUENTIAL,
    BookTape,
    FillResult,
    LegSpec,
    aggregate,
    simulate,
)

D = Decimal
T0 = 1_000_000.0
ZERO = D(0)
BIG = "1000000"

A = "A:BTC-USD"   # venue we buy on
B = "B:BTC-USD"   # venue we sell on


def tape(pid: str, points: list[tuple[float, str | None, str | None, str]]) -> BookTape:
    """Build a tape from (offset_s, bid, ask, size) points. None = empty side."""
    snaps = tuple(
        Book(
            product_id=pid, base="BTC", quote="USD",
            bids=(Level(D(bid), D(size)),) if bid else (),
            asks=(Level(D(ask), D(size)),) if ask else (),
            ts_local=T0 + dt,
        )
        for dt, bid, ask, size in points
    )
    return BookTape(pid, snaps)


def buy_leg(latency_ms: float, fee: Decimal = ZERO) -> LegSpec:
    """USD -> BTC on venue A."""
    return LegSpec(product_id=A, asset_in="USD", fee_rate=fee,
                   venue="A", latency_ms=latency_ms)


def sell_leg(latency_ms: float, fee: Decimal = ZERO) -> LegSpec:
    """BTC -> USD on venue B."""
    return LegSpec(product_id=B, asset_in="BTC", fee_rate=fee,
                   venue="B", latency_ms=latency_ms)


def approx(got: Decimal, want: str, tol: str = "0.05") -> bool:
    return abs(got - D(want)) <= D(tol)


# --- 1. A still market fills exactly as advertised ------------------------

def test_no_movement_realizes_the_decision():
    """Buy at 100 on A, sell at 101 on B: 1000 USD -> 10 BTC -> 1010 USD.
    (1010/1000 - 1) * 10000 = +100 bps, and nothing moves, so realized == decision.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG), (0.5, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert r.filled, r.reason
    assert approx(r.decision_edge_bps, "100"), r.decision_edge_bps
    assert approx(r.realized_edge_bps, "100"), r.realized_edge_bps
    assert approx(r.adverse_selection_bps, "0"), r.adverse_selection_bps
    assert approx(r.end_amount, "1010", "0.01"), r.end_amount


# --- 2. The edge decays inside the latency window -------------------------

def test_adverse_movement_is_charged_not_ignored():
    """Same 100 bps signal, but B's bid falls 101 -> 100.5 before we arrive.

    Legs are 200ms each, so leg 2 executes at T0+0.4 and prices against the
    T0+0.3 snapshot: 10 BTC * 100.5 = 1005 USD -> +50 bps realized.
    A replay that filled against the triggering book would report +100 bps.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG),
                    (0.3, "100.5", "102", BIG),
                    (0.5, "100.5", "102", BIG)]),
    }
    r = simulate([buy_leg(200), sell_leg(200)], tapes, T0, D("1000"))

    assert r.filled, r.reason
    assert approx(r.decision_edge_bps, "100"), r.decision_edge_bps
    assert approx(r.realized_edge_bps, "50"), r.realized_edge_bps
    assert approx(r.adverse_selection_bps, "-50"), r.adverse_selection_bps
    # The second leg's own slip accounts for all of it.
    assert approx(r.fills[1].slip_bps, "-49.5", "0.5"), r.fills[1].slip_bps


# --- 3. Point-in-time lookup never looks forward --------------------------

def test_as_of_never_peeks_into_the_future():
    t = tape(A, [(0.0, "99", "100", BIG), (1.0, "90", "91", BIG)])

    assert t.as_of(T0 - 1) is None                 # before the tape starts
    assert t.as_of(T0).ts_local == T0
    assert t.as_of(T0 + 0.99).ts_local == T0       # the 1.0 snapshot is invisible
    assert t.as_of(T0 + 1.0).ts_local == T0 + 1.0
    assert t.as_of(T0 + 99).ts_local == T0 + 1.0

    assert t.covers(T0 + 1.0)
    assert not t.covers(T0 + 1.01)                 # beyond what was observed


# --- 4. Missing data is reported, never extrapolated ----------------------

def test_tape_ending_in_latency_window_is_unverifiable():
    """The tape stops at the trigger instant, so what we would have filled
    against is genuinely unknown. Reusing the T0 book would invent a +100 bps
    profit out of nothing -- the exact failure this module exists to prevent.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert not r.filled
    assert r.unverifiable
    assert "ends before its fill time" in (r.reason or ""), r.reason
    # The advertised edge is preserved for diagnosis, but nothing was realized.
    assert approx(r.decision_edge_bps, "100"), r.decision_edge_bps
    assert r.realized_edge_bps == ZERO
    assert r.end_amount == ZERO


# --- 5. Sequential delay compounds down the path --------------------------

def test_sequential_legs_compound_their_latency():
    """100ms per leg: leg 1 at T0+0.1, leg 2 at T0+0.2 -- not both at T0+0.1.

    B's bid steps 101 -> 100.5 at T0+0.15. Leg 2 therefore sees 100.5.
    Had the simulator priced leg 2 at leg 1's time (T0+0.1) it would still see
    101 and report the full +100 bps.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.4, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG),
                    (0.15, "100.5", "102", BIG),
                    (0.4, "100.5", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert r.filled, r.reason
    assert abs(r.fills[0].t_fill - (T0 + 0.1)) < 1e-9, r.fills[0].t_fill
    assert abs(r.fills[1].t_fill - (T0 + 0.2)) < 1e-9, r.fills[1].t_fill
    # Leg 2 priced off the T0+0.15 snapshot, which is the newest it could see.
    assert abs(r.fills[1].book_ts - (T0 + 0.15)) < 1e-9, r.fills[1].book_ts
    assert approx(r.realized_edge_bps, "50"), r.realized_edge_bps


# --- 6. Atomic execution has one price instant and no leg risk ------------

def test_atomic_prices_every_leg_at_one_instant():
    """On-chain atomic execution: both legs land in the same block, so they
    price off the same moment. B's later move is irrelevant because the whole
    path settled before it.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.4, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG),
                    (0.15, "100.5", "102", BIG),
                    (0.4, "100.5", "102", BIG)]),
    }
    r = simulate(
        [buy_leg(100), sell_leg(100)], tapes, T0, D("1000"),
        execution=ATOMIC, atomic_latency_ms=100.0,
    )

    assert r.filled, r.reason
    assert r.fills[0].t_fill == r.fills[1].t_fill
    # Both at T0+0.1, which still sees bid 101 -> the full edge survives.
    assert approx(r.realized_edge_bps, "100"), r.realized_edge_bps
    assert r.stranded_asset is None


def test_atomic_requires_an_explicit_block_latency():
    tapes = {A: tape(A, [(0.0, "99", "100", BIG)]),
             B: tape(B, [(0.0, "101", "102", BIG)])}
    try:
        simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"),
                 execution=ATOMIC)
    except ValueError as e:
        assert "atomic_latency_ms" in str(e), e
    else:
        raise AssertionError("atomic execution accepted a missing block latency")


# --- 7. A bad first fill shrinks every leg after it -----------------------

def test_shortfall_propagates_to_later_legs():
    """A offers only 5 BTC at 100, so 1000 USD buys 5 BTC, not 10. Leg 2 must
    receive 5 BTC -- the amount actually held -- not the 10 the plan assumed.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", "5"), (0.5, "99", "100", "5")]),
        B: tape(B, [(0.0, "101", "102", BIG), (0.5, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert r.filled, r.reason
    assert r.fills[0].exhausted, "thin book should report exhaustion"
    assert approx(r.fills[0].amount_out, "5", "0.001"), r.fills[0].amount_out
    # The chaining invariant: leg 2 trades exactly what leg 1 produced.
    assert r.fills[1].amount_in == r.fills[0].amount_out
    assert approx(r.end_amount, "505", "0.01"), r.end_amount


# --- 8. A broken sequential path leaves you holding the wrong asset -------

def test_sequential_failure_reports_the_stranded_asset():
    """B's bids vanish before leg 2 arrives. We bought BTC and cannot sell it:
    that is naked inventory, not a no-op, and the result must say so.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG), (0.3, None, "102", BIG),
                    (0.5, None, "102", BIG)]),
    }
    r = simulate([buy_leg(200), sell_leg(200)], tapes, T0, D("1000"))

    assert not r.filled
    assert not r.unverifiable, "the book WAS observed -- this is a real failure"
    assert r.stranded_asset == "BTC", r.stranded_asset
    assert len(r.fills) == 2 and r.fills[0].amount_out > 0


# --- 9. The decision-side guards still apply -----------------------------

def test_skew_guard_rejects_before_simulating():
    """Legs snapshotted 2s apart: the signal is an artifact of stale data, so
    it must never reach the fill stage at all.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (5.0, "99", "100", BIG)]),
        B: tape(B, [(-2.0, "101", "102", BIG), (5.0, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"),
                 max_skew_ms=500.0)

    assert not r.filled
    assert not r.unverifiable
    assert "skew" in (r.reason or ""), r.reason


def test_unclosed_path_is_refused():
    """A path that does not return to its starting asset has no edge to speak
    of; netedge's closure check must survive the extra indirection.
    """
    tapes = {A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)])}
    r = simulate([buy_leg(100)], tapes, T0, D("1000"))

    assert not r.filled
    assert "decision rejected" in (r.reason or ""), r.reason


def test_missing_tape_is_an_error_not_a_zero():
    tapes = {A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)])}
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert not r.filled
    assert "no tape" in (r.reason or ""), r.reason


# --- 10. Aggregation must not launder the unscorable ----------------------

def test_aggregate_excludes_unverifiable_from_the_hit_rate():
    """Two real fills, one winner; plus an unverifiable attempt carrying a fat
    advertised edge. Survival must read 1/2, not 2/3 -- an unscored attempt is
    not a win.
    """
    def res(filled, decision, realized, unver=False):
        return FillResult(
            filled=filled, reason=None,
            decision_edge_bps=D(decision), realized_edge_bps=D(realized),
            start_amount=D("1000"), end_amount=D("1000"), fills=(),
            unverifiable=unver, stranded_asset=None,
        )

    stats = aggregate([
        res(True, "100", "50"),      # survived
        res(True, "100", "-20"),     # eaten by latency
        res(False, "900", "0", True),  # unscorable
        res(False, "100", "0"),      # rejected outright
    ])

    assert stats.attempted == 4
    assert stats.filled == 2
    assert stats.unverifiable == 1
    assert stats.rejected == 1
    assert stats.profitable_after_latency == 1
    assert abs(stats.survival_rate - 0.5) < 1e-9, stats.survival_rate
    # The 900 bps phantom must not contaminate the means.
    assert approx(stats.mean_decision_bps, "100"), stats.mean_decision_bps
    assert approx(stats.mean_realized_bps, "15"), stats.mean_realized_bps
    assert approx(stats.mean_adverse_bps, "-85"), stats.mean_adverse_bps
    assert approx(stats.worst_adverse_bps, "-120"), stats.worst_adverse_bps
    assert "could not be scored" in stats.summary()


def test_aggregate_calls_a_dead_strategy_dead():
    """When mean realized edge is negative the summary must say so outright,
    rather than leaving a positive mean decision edge as the headline.
    """
    def res(decision, realized):
        return FillResult(
            filled=True, reason=None,
            decision_edge_bps=D(decision), realized_edge_bps=D(realized),
            start_amount=D("1000"), end_amount=D("1000"), fills=(),
            unverifiable=False, stranded_asset=None,
        )

    stats = aggregate([res("40", "-5"), res("60", "-15")])
    assert stats.mean_decision_bps > 0
    assert stats.mean_realized_bps < 0
    assert "no edge at this latency" in stats.summary()


# --- 11. Tape hygiene -----------------------------------------------------

def test_out_of_order_tape_is_rejected_at_construction():
    """An unsorted tape would silently break bisect and return wrong books."""
    try:
        BookTape(A, (
            Book(A, "BTC", "USD", (), (), ts_local=T0 + 1),
            Book(A, "BTC", "USD", (), (), ts_local=T0),
        ))
    except ValueError as e:
        assert "ascending" in str(e), e
    else:
        raise AssertionError("out-of-order snapshots accepted")


def test_gas_is_charged_against_the_realized_edge():
    """A +100 bps path, 1000 USD notional, 5 USD of gas.

    Fixed cost is -(5/1000) * 10000 = -50 bps, so BOTH the decision and the
    realized edge must read +50. Charging gas only on the decision side would
    make every on-chain path look better after execution than before it, which
    is how a gas-losing strategy gets shipped.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG), (0.5, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"),
                 fixed_cost_usd=D("5"), start_asset_usd_price=D("1"))

    assert r.filled, r.reason
    assert approx(r.decision_edge_bps, "50"), r.decision_edge_bps
    assert approx(r.realized_edge_bps, "50"), r.realized_edge_bps
    # Gas hits both sides equally, so it is not adverse selection.
    assert approx(r.adverse_selection_bps, "0"), r.adverse_selection_bps

    # Ten times the gas on the same notional flips the same path negative.
    heavy = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"),
                     fixed_cost_usd=D("50"), start_asset_usd_price=D("1"))
    assert heavy.filled, heavy.reason
    assert approx(heavy.realized_edge_bps, "-400"), heavy.realized_edge_bps


def test_book_age_is_measured_not_assumed():
    """Reporting how stale the fill book was keeps an under-sampled tape from
    masquerading as a clean fill.
    """
    tapes = {
        A: tape(A, [(0.0, "99", "100", BIG), (0.5, "99", "100", BIG)]),
        B: tape(B, [(0.0, "101", "102", BIG), (0.5, "101", "102", BIG)]),
    }
    r = simulate([buy_leg(100), sell_leg(100)], tapes, T0, D("1000"))

    assert r.filled, r.reason
    # Leg 2 fills at T0+0.2 against the T0 book: 200ms stale.
    assert abs(r.fills[1].book_age_ms - 200.0) < 1e-6, r.fills[1].book_age_ms


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
