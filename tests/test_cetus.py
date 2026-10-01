"""Unit tests for the direct Cetus adapter, offline against synthetic pools.

The headline test is the one the handover's author warned about by name:

  "a DEX adapter that reported a fee AND used a post-fee reference *would*
   double count. That is a per-adapter contract, so check it when you add
   Cetus direct."

Their own FlowX path is safe because it reports fee_usd = 0 and the pool fee
is already inside amountOut. This adapter takes the opposite approach -- a
genuinely pre-fee marginal from the pool's sqrt_price, with the fee applied
separately -- so it is exposed to precisely that error, and nothing but a test
distinguishes "applied once" from "applied twice". Double-charging a 5bps fee
understates every Cetus route by 5bps, which is the direction that silently
hides opportunities.

The discriminating check: as size goes to zero, impact vanishes, so
full(size)/size must converge on ONE fee below the frictionless price. Two
fees means the reference was already net.

Run: python tests/test_cetus.py     (no pytest required)
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from venues.cetus import (  # noqa: E402
    Q64,
    CetusError,
    PoolState,
    generic_params,
    normalize_coin_type,
    sqrt_price_to_price,
    tick_to_sqrt_x64,
)

D = Decimal

# USDC is token0 (6dp), SUI is token1 (9dp) -- the real Pool<USDC, SUI> order.
DEC0, DEC1 = 6, 9
FEE = D("0.0005")        # the pool's real 500/1e6


def pool(price_1_per_0: str = "1", liquidity: str = "1e18",
         fee: Decimal = FEE, spacing: int = 10, tick: int | None = None,
         paused: bool = False) -> PoolState:
    """A synthetic pool at a chosen human price, for exact arithmetic.

    price_1_per_0 is SUI per USDC. Inverting the decimals scaling recovers the
    sqrt_price the pool would hold.

    The tick is DERIVED from that sqrt price rather than passed in. Setting
    them independently makes an impossible pool: a first draft of this file
    used price 1 with tick 0, where the price actually implies tick ~69078, so
    the tick bounds sat nowhere near the current price and every swap reported
    as crossing. That looked like four adapter bugs and was one fixture bug.
    """
    p = D(price_1_per_0)
    raw = p / (D(10) ** (DEC0 - DEC1))          # undo the decimals scaling
    sqrt_x64 = raw.sqrt() * Q64
    if tick is None:
        # tick = log(raw_price) / log(1.0001)
        tick = int(raw.ln() / D("1.0001").ln())
    return PoolState(
        pool_id="0xtest", version=1, sqrt_x64=sqrt_x64,
        liquidity=D(liquidity), fee_rate=fee, tick_spacing=spacing,
        tick=tick, paused=paused,
        coin0="0x1::usdc::USDC", coin1="0x2::sui::SUI",
        dec0=DEC0, dec1=DEC1,
    )


def approx(got: Decimal, want: Decimal, tol: str = "1e-9") -> bool:
    return abs(got - want) <= D(tol)


# --- 1. THE double-count test -------------------------------------------

def test_pool_fee_is_charged_exactly_once():
    """full(size)/size -> frictionless * (1 - fee) as size -> 0.

    If the sqrt_price reference were already net of the fee and the fee were
    applied again, the limit would be frictionless * (1 - fee)^2 instead. At
    5bps those differ by 0.025bps per unit -- small, systematic, and in the
    direction that hides edge, so only an explicit check catches it.
    """
    s = pool(price_1_per_0="1", liquidity="1e24")   # deep: impact ~ 0
    marginal = s.price_1_per_0()
    assert approx(marginal, D(1), "1e-12"), marginal

    one_fee = marginal * (D(1) - FEE)
    two_fees = marginal * (D(1) - FEE) ** 2

    tiny = D("0.000001")
    out, crossed = s.quote_exact_in(tiny, zero_for_one=True)
    assert not crossed
    rate = out / tiny

    d1 = abs(rate - one_fee)
    d2 = abs(rate - two_fees)
    assert d1 < d2, (
        f"rate {rate} is closer to TWO fees ({two_fees}) than one ({one_fee}) "
        "-- the fee is being double counted"
    )
    assert approx(rate, one_fee, "1e-9"), (rate, one_fee)


def test_frictionless_reference_is_pre_fee():
    """The reference itself must carry no fee at all.

    price_1_per_0 comes straight from sqrt_price, which is the pool's price
    before anybody is charged anything. If this ever became net, the test
    above would still pass while every cost decomposition silently shifted.
    """
    s = pool(price_1_per_0="2.5")
    # Independently recomputed from the raw sqrt, not via the adapter.
    independent = sqrt_price_to_price(s.sqrt_x64, DEC0, DEC1)
    assert approx(s.price_1_per_0(), independent, "1e-15")
    # And it is NOT reduced by the fee.
    assert s.price_1_per_0() > D("2.5") * (D(1) - FEE)
    assert approx(s.price_1_per_0(), D("2.5"), "1e-9")


def test_a_bigger_fee_reduces_output_proportionally():
    """Output must scale with (1 - fee) exactly once, for any fee."""
    tiny = D("0.000001")
    base = None
    for fee in (D("0"), D("0.0005"), D("0.003"), D("0.01")):
        s = pool(price_1_per_0="1", liquidity="1e24", fee=fee)
        out, _ = s.quote_exact_in(tiny, zero_for_one=True)
        rate = out / tiny
        assert approx(rate, D(1) - fee, "1e-9"), (fee, rate)
        if fee == 0:
            base = rate
    assert base is not None and approx(base, D(1), "1e-9")


# --- 2. Impact direction and monotonicity -------------------------------

def test_impact_is_monotone_and_always_adverse():
    """More size can never buy a better rate."""
    s = pool(price_1_per_0="1", liquidity="1e15")
    rates = []
    for size in (D("1"), D("10"), D("100"), D("1000")):
        out, crossed = s.quote_exact_in(size, zero_for_one=True)
        if crossed:
            break
        rates.append(out / size)
    assert len(rates) >= 2, "pool too shallow to compare sizes"
    for a, b in zip(rates, rates[1:]):
        assert b <= a, f"rate improved with size: {a} -> {b}"
    # Even the smallest size is below the frictionless price.
    assert rates[0] < s.price_1_per_0()


def test_both_directions_lose_to_the_round_trip():
    """Selling then rebuying must end below where you started.

    A pool has no spread, so the loss is exactly the two fees plus impact. If
    a round trip ever came out ahead, the direction handling is inverted.
    """
    s = pool(price_1_per_0="1", liquidity="1e24")
    start = D("1000")
    mid, c1 = s.quote_exact_in(start, zero_for_one=True)      # USDC -> SUI
    assert not c1
    back, c2 = s.quote_exact_in(mid, zero_for_one=False)      # SUI -> USDC
    assert not c2
    assert back < start, (back, start)
    # Two 5bps fees ~= 10bps of loss on a deep pool.
    loss_bps = (D(1) - back / start) * D(10000)
    assert D("9.5") < loss_bps < D("10.5"), loss_bps


# --- 3. The tick guard --------------------------------------------------

def test_a_swap_leaving_the_tick_is_flagged():
    """Liquidity past the boundary is unknown without the tick_manager, so
    the quote must be flagged rather than extrapolated. Extrapolating
    overstates output exactly where size starts to matter.
    """
    s = pool(price_1_per_0="1", liquidity="1e9", spacing=10)
    small, c_small = s.quote_exact_in(D("0.000001"), zero_for_one=True)
    assert not c_small and small > 0
    _, c_big = s.quote_exact_in(D("1e12"), zero_for_one=True)
    assert c_big, "an enormous swap stayed inside one tick"


def test_tick_bounds_bracket_the_current_price():
    s = pool(price_1_per_0="1", spacing=10)
    lo, hi = s.tick_bounds_x64()
    assert lo < hi
    # Boundaries are tick_spacing apart in tick terms.
    assert approx(hi / lo, (D("1.0001") ** D(5)), "1e-6")


def test_tick_to_sqrt_matches_the_price_identity():
    """sqrt(1.0001^tick) is the whole tick convention; off-by-two in the
    exponent is the classic error and it compounds with tick size.
    """
    for tick in (0, 100, -100, 67540):
        got = tick_to_sqrt_x64(tick) / Q64
        want = (D("1.0001") ** D(tick)).sqrt()
        assert abs(got - want) / want < D("1e-20"), (tick, got, want)


# --- 4. Unusable pools --------------------------------------------------

def test_zero_liquidity_quotes_nothing():
    s = pool(liquidity="0")
    out, _ = s.quote_exact_in(D("1"), zero_for_one=True)
    assert out == 0


def test_paused_pool_is_visible_in_state():
    assert pool(paused=True).paused
    assert not pool().paused


# --- 5. The two Sui string traps ----------------------------------------

def test_short_and_padded_addresses_normalise_together():
    """The same coin type has two valid spellings. The pool writes
    0x2::sui::SUI; coin metadata returns the padded form. Comparing them
    unnormalised fails, which blocks checking the pool's token order -- and
    getting that order backwards inverts the price into an apparent huge
    arbitrage.
    """
    short = "0x2::sui::SUI"
    padded = "0x" + "0" * 63 + "2::sui::SUI"
    assert normalize_coin_type(short) == normalize_coin_type(padded)
    assert normalize_coin_type(short).startswith("0x" + "0" * 63 + "2")
    # Case is normalised too, but the module path is preserved verbatim.
    assert normalize_coin_type("0xAB::M::T").endswith("::M::T")


def test_generic_params_split_at_the_top_level_only():
    """A naive split(',') breaks on nested generics, silently."""
    t = "0xp::pool::Pool<0xa::usdc::USDC, 0x2::sui::SUI>"
    assert generic_params(t) == ("0xa::usdc::USDC", "0x2::sui::SUI")
    nested = "0xp::m::T<0xa::b::C<0xd::e::F, 0xg::h::I>, 0x2::sui::SUI>"
    got = generic_params(nested)
    assert len(got) == 2, got
    assert got[1] == "0x2::sui::SUI"
    assert generic_params("0xa::b::C") == ()


def test_q64_not_q96():
    """Cetus is Q64.64; Uniswap is Q96. Using the wrong shift is wrong by
    2^32 ~= 4.3e9, which is obvious -- but only if something checks.
    """
    s = pool(price_1_per_0="1")
    as_q96 = (s.sqrt_x64 / (D(2) ** 96)) ** 2 * (D(10) ** (DEC0 - DEC1))
    assert approx(s.price_1_per_0(), D(1), "1e-9")
    assert as_q96 < D("1e-15"), as_q96


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
