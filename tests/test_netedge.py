"""Unit tests for the net-edge calculator, against hand-built venues.

A sign error in netedge.py produces a bot that confidently loses money, so the
expected values here are computed by hand in the comments rather than captured
from a previous run.

Run: python tests/test_netedge.py     (no pytest required)
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import COINBASE, bps  # noqa: E402
from legs import Book, BookLeg, ConstantProductLeg, Level  # noqa: E402
from netedge import evaluate, min_profitable_size, solve_capacity  # noqa: E402

D = Decimal
FEE = COINBASE.taker_rate  # 10 bps = 0.001
T0 = 1_000_000.0


def deep(price: Decimal, size: Decimal = D("1000000")) -> tuple[Level, ...]:
    """A single level so large that price impact is nil."""
    return (Level(price, size),)


def book(pid: str, base: str, quote: str, bid: str, ask: str,
         size: str = "1000000", ts: float = T0) -> Book:
    return Book(
        product_id=pid, base=base, quote=quote,
        bids=deep(D(bid), D(size)), asks=deep(D(ask), D(size)),
        ts_local=ts,
    )


def approx(got: Decimal, want: str, tol: str = "0.05") -> bool:
    return abs(got - D(want)) <= D(tol)


# --- 1. No mispricing, no fees -> no edge ---------------------------------

def test_fair_triangle_zero_fee_is_flat():
    btc_usd = book("BTC-USD", "BTC", "USD", "100000", "100000")
    btc_usdt = book("BTC-USDT", "BTC", "USDT", "100000", "100000")
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1")
    path = [
        BookLeg(btc_usd, "USD", D(0)),
        BookLeg(btc_usdt, "BTC", D(0)),
        BookLeg(usdt_usd, "USDT", D(0)),
    ]
    r = evaluate(path, D("10000"))
    assert r.ok, r.reason
    assert approx(r.net_edge_bps, "0"), r.summary()
    assert r.path == ("USD", "BTC", "USDT", "USD"), r.path


# --- 2. Known mispricing, no fees -> known gross edge ---------------------

def test_known_mispricing_gross_edge():
    # Buy BTC at 100_000 USD, sell at 101_000 USDT, USDT->USD at 1.0
    # 10_000 USD -> 0.1 BTC -> 10_100 USDT -> 10_100 USD = +100 bps
    btc_usd = book("BTC-USD", "BTC", "USD", "99999", "100000")
    btc_usdt = book("BTC-USDT", "BTC", "USDT", "101000", "101001")
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1.0001")
    path = [
        BookLeg(btc_usd, "USD", D(0)),
        BookLeg(btc_usdt, "BTC", D(0)),
        BookLeg(usdt_usd, "USDT", D(0)),
    ]
    r = evaluate(path, D("10000"))
    assert r.ok, r.reason
    assert approx(r.net_edge_bps, "100"), r.summary()
    assert approx(r.fee_bps, "0"), r.summary()


# --- 3. Same mispricing at 10 bps/leg -> fees eat ~30 bps ----------------

def test_fees_cost_three_legs():
    # Each leg multiplies by (1 - 0.001), so 1.01 * 0.999**3 = 1.0069730...
    # -> +69.73 bps net, and fee_bps should be about -30.07
    btc_usd = book("BTC-USD", "BTC", "USD", "99999", "100000")
    btc_usdt = book("BTC-USDT", "BTC", "USDT", "101000", "101001")
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1.0001")
    path = [
        BookLeg(btc_usd, "USD", FEE),
        BookLeg(btc_usdt, "BTC", FEE),
        BookLeg(usdt_usd, "USDT", FEE),
    ]
    r = evaluate(path, D("10000"))
    assert r.ok, r.reason
    assert approx(r.net_edge_bps, "69.73", "0.1"), r.summary()
    assert approx(r.gross_edge_bps, "100"), r.summary()
    assert approx(r.fee_bps, "-30.27", "0.5"), r.summary()
    # Depth is effectively infinite here, so impact must be nil.
    assert approx(r.slippage_bps, "0"), r.summary()


# --- 4. A 100 bps gross loop dies at a 0.6% entry fee tier ---------------

def test_entry_tier_closes_the_loop():
    entry_taker = bps(60)  # Coinbase Advanced Trade entry tier, ~0.60%
    btc_usd = book("BTC-USD", "BTC", "USD", "99999", "100000")
    btc_usdt = book("BTC-USDT", "BTC", "USDT", "101000", "101001")
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1.0001")
    path = [
        BookLeg(btc_usd, "USD", entry_taker),
        BookLeg(btc_usdt, "BTC", entry_taker),
        BookLeg(usdt_usd, "USDT", entry_taker),
    ]
    r = evaluate(path, D("10000"))
    assert r.ok, r.reason
    # 1.01 * 0.994**3 = 0.991929 -> -80.71 bps. Arithmetically closed.
    assert r.net_edge_bps < 0, r.summary()
    assert approx(r.net_edge_bps, "-80.71", "0.2"), r.summary()


# --- 5. Thin depth shows up as slippage, and caps capacity ---------------

def test_thin_book_slippage_and_capacity():
    # Only 0.05 BTC at the good ask; the rest is 2% worse.
    thin_asks = (Level(D("100000"), D("0.05")), Level(D("102000"), D("100")))
    btc_usd = Book(
        "BTC-USD", "BTC", "USD",
        bids=deep(D("99999")), asks=thin_asks, ts_local=T0,
    )
    btc_usdt = book("BTC-USDT", "BTC", "USDT", "101000", "101001")
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1.0001")

    def mk():
        return [
            BookLeg(btc_usd, "USD", FEE),
            BookLeg(btc_usdt, "BTC", FEE),
            BookLeg(usdt_usd, "USDT", FEE),
        ]

    small = evaluate(mk(), D("1000"))      # 1000 USD -> 0.00999 BTC, fits
    large = evaluate(mk(), D("50000"))     # walks into the 102_000 level
    assert small.ok and large.ok
    assert approx(small.slippage_bps, "0"), small.summary()
    assert large.slippage_bps < -100, large.summary()
    assert large.net_edge_bps < small.net_edge_bps

    cap = solve_capacity(mk(), lo=D("100"), hi=D("100000"))
    # Good ask holds 0.05 BTC = 5000 USD of notional; capacity must land near
    # there, and must not claim the full search range.
    assert D("4000") < cap < D("9000"), cap


# --- 6. Skew rejection ----------------------------------------------------

def test_stale_leg_is_rejected_not_counted():
    fresh = book("BTC-USD", "BTC", "USD", "99999", "100000", ts=T0)
    stale = book("BTC-USDT", "BTC", "USDT", "101000", "101001", ts=T0 - 3.0)
    usdt_usd = book("USDT-USD", "USDT", "USD", "1", "1.0001", ts=T0)
    path = [
        BookLeg(fresh, "USD", FEE),
        BookLeg(stale, "BTC", FEE),
        BookLeg(usdt_usd, "USDT", FEE),
    ]
    r = evaluate(path, D("10000"), max_skew_ms=500)
    assert not r.ok
    assert "skew" in (r.reason or ""), r.reason
    assert r.max_skew_ms >= 3000
    # Without the guard the same books look like a fat opportunity.
    loose = evaluate(path, D("10000"))
    assert loose.ok and loose.net_edge_bps > 50, loose.summary()


# --- 7. Broken paths are refused -----------------------------------------

def test_unclosed_path_refused():
    btc_usd = book("BTC-USD", "BTC", "USD", "99999", "100000")
    r = evaluate([BookLeg(btc_usd, "USD", FEE)], D("1000"))
    assert not r.ok and "not closed" in (r.reason or ""), r.reason


def test_direction_inference():
    btc_usd = book("BTC-USD", "BTC", "USD", "99999", "100000")
    buy = BookLeg(btc_usd, "USD", D(0))
    sell = BookLeg(btc_usd, "BTC", D(0))
    assert buy.is_buy and buy.asset_out == "BTC"
    assert (not sell.is_buy) and sell.asset_out == "USD"
    try:
        BookLeg(btc_usd, "SOL", D(0))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an asset not in the pair")


# --- 8. AMM leg: constant product math -----------------------------------

def test_constant_product_matches_hand_math():
    # 1000 WETH / 100_000_000 USDC, 5 bps fee, swap 10 WETH in:
    #   eff_in = 10 * 0.9995 = 9.995
    #   out = 9.995 * 1e8 / (1000 + 9.995) = 989_608.86
    leg = ConstantProductLeg(
        asset_in="WETH", asset_out="USDC",
        reserve_in=D("1000"), reserve_out=D("100000000"),
        fee_rate=bps(5), ts_local=T0,
    )
    out = leg.full(D("10")).amount_out
    assert abs(out - D("989608.86")) < D("1"), out
    # Marginal price is 100_000 USDC/WETH, so frictionless on 10 is 1_000_000.
    assert leg.frictionless(D("10")) == D("1000000")
    # Impact must be a real cost, not zero.
    assert leg.full(D("10")).amount_out < leg.fee_only(D("10"))


def test_cex_to_dex_roundtrip_mixes_leg_types():
    # Buy ETH on the CEX, sell it into an AMM pool priced 1% higher.
    eth_usd = book("ETH-USD", "ETH", "USD", "3999", "4000", size="10000")
    cex_buy = BookLeg(eth_usd, "USD", FEE, venue="coinbase")
    pool_sell = ConstantProductLeg(
        asset_in="ETH", asset_out="USD",
        reserve_in=D("100000"), reserve_out=D("404000000"),  # 4040 USD/ETH
        fee_rate=bps(5), ts_local=T0,
    )
    path = [cex_buy, pool_sell]
    r = evaluate(path, D("10000"), fixed_cost_usd=D("0.02"))
    assert r.ok, r.reason
    assert r.gross_edge_bps > 90, r.summary()
    assert r.net_edge_bps > 0, r.summary()
    assert r.fixed_cost_bps < 0


# --- 9. Gas creates a hard minimum size ----------------------------------

def test_gas_sets_a_floor_on_size():
    eth_usd = book("ETH-USD", "ETH", "USD", "3999", "4000", size="10000")
    pool = ConstantProductLeg(
        asset_in="ETH", asset_out="USD",
        reserve_in=D("100000"), reserve_out=D("404000000"),
        fee_rate=bps(5), ts_local=T0,
    )

    def mk():
        return [BookLeg(eth_usd, "USD", FEE), pool]

    # Mainnet-scale gas: 8 USD is 800 bps of a 100 USD trade.
    tiny = evaluate(mk(), D("100"), fixed_cost_usd=D("8"))
    big = evaluate(mk(), D("50000"), fixed_cost_usd=D("8"))
    assert tiny.net_edge_bps < 0, tiny.summary()
    assert big.net_edge_bps > 0, big.summary()

    floor = min_profitable_size(mk(), fixed_cost_usd=D("8"), lo=D("1"), hi=D("100000"))
    assert floor is not None and D("100") < floor < D("20000"), floor
    # Base-scale gas moves the floor down by orders of magnitude.
    cheap = min_profitable_size(mk(), fixed_cost_usd=D("0.02"), lo=D("1"), hi=D("100000"))
    assert cheap is not None and cheap < floor, (cheap, floor)


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
