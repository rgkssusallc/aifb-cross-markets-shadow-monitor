"""Unit tests for the venue framework, against fake venues.

The framework's value is that adding a venue needs no engine change, so these
tests implement venues that do not exist and check the engine handles them
correctly. If the engine ever needs to know a venue's identity, one of these
will break.

The invariants that matter:
  - a block-atomic venue's quote is reused while its state_id holds, and an
    order book's never is
  - an old order book is refused; an old chain quote is NOT, because it is
    exact until the block moves
  - leg 2 is sized from what leg 1 actually produced
  - unproven aliases are surfaced on exactly the routes that lean on them

Run: python tests/test_engine.py     (no pytest required)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engine import Engine, Route  # noqa: E402
from core.venue import AMM, CEX, Alias, Aliases, LegCache  # noqa: E402
from legs import Book, BookLeg, Level  # noqa: E402

D = Decimal
T0 = 1_000_000.0


def mkbook(pid: str, base: str, quote: str, bid: str, ask: str,
           size: str = "1000000", ts: float | None = None) -> Book:
    return Book(product_id=pid, base=base, quote=quote,
                bids=(Level(D(bid), D(size)),), asks=(Level(D(ask), D(size)),),
                ts_local=ts if ts is not None else time.time())


@dataclass
class FakeBookVenue:
    """Stands in for any order-book venue: ages, never cacheable."""
    name: str
    bid: str
    ask: str
    base: str = "WETH"
    quote: str = "USDC"
    fee: Decimal = D("0.001")
    kind: str = CEX
    book_age_ms: float = 0.0
    quote_calls: int = 0
    _healthy: bool = True

    async def connect(self) -> None: ...
    async def aclose(self) -> None: ...
    async def refresh(self) -> None: ...

    def state_id(self):
        return self.book_age_ms

    @property
    def exact_while_state_unchanged(self) -> bool:
        return False

    def healthy(self) -> bool:
        return self._healthy

    def age_ms(self) -> float:
        return self.book_age_ms

    def fixed_cost_usd(self) -> Decimal:
        return D(0)

    def assets(self) -> set[str]:
        return {self.base, self.quote}

    async def leg(self, asset_in: str, asset_out: str, size_in: Decimal):
        self.quote_calls += 1
        b = mkbook(f"{self.base}-{self.quote}", self.base, self.quote,
                   self.bid, self.ask,
                   ts=time.time() - self.book_age_ms / 1000.0)
        return BookLeg(book=b, asset_in=asset_in, fee_rate=self.fee,
                       venue=self.name)


@dataclass
class FakeChainVenue:
    """Stands in for any block-atomic venue: cacheable while the block holds."""
    name: str
    price: str                       # quote units per base unit
    block: int = 100
    gas: Decimal = D("0.01")
    base: str = "WETH"
    quote: str = "USDC"
    fee: Decimal = D("0.0005")
    kind: str = AMM
    quote_calls: int = 0

    async def connect(self) -> None: ...
    async def aclose(self) -> None: ...
    async def refresh(self) -> None: ...

    def state_id(self):
        return self.block

    @property
    def exact_while_state_unchanged(self) -> bool:
        return True

    def healthy(self) -> bool:
        return True

    def age_ms(self) -> float:
        return 0.0

    def fixed_cost_usd(self) -> Decimal:
        return self.gas

    def assets(self) -> set[str]:
        return {self.base, self.quote}

    async def leg(self, asset_in: str, asset_out: str, size_in: Decimal):
        self.quote_calls += 1
        px = D(self.price)
        # A deep two-sided book is the simplest honest stand-in for a pool
        # quoted at one price with no impact.
        b = mkbook(f"{self.base}-{self.quote}", self.base, self.quote,
                   str(px), str(px), ts=time.time())
        return BookLeg(book=b, asset_in=asset_in, fee_rate=self.fee,
                       venue=self.name)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --- 1. Caching follows the venue's own semantics ------------------------

def test_chain_quote_is_reused_while_the_block_holds():
    """The whole RPC saving. A pool quote cannot change within a block, so a
    second request for the same leg must not hit the chain again.
    """
    v = FakeChainVenue("chain", price="2700")
    cache = LegCache()

    async def go():
        a = await cache.get(v, "USDC", "WETH", D("1000"))
        b = await cache.get(v, "USDC", "WETH", D("1000"))
        return a, b

    a, b = run(go())
    assert a is b, "same block should return the identical cached leg"
    assert v.quote_calls == 1, v.quote_calls
    assert cache.hits == 1 and cache.misses == 1

    # New block invalidates it.
    v.block = 101
    run(cache.get(v, "USDC", "WETH", D("1000")))
    assert v.quote_calls == 2, v.quote_calls


def test_order_book_quote_is_never_reused():
    """An order book is a new state on every update, so caching it would
    serve a book the venue has already moved past.
    """
    v = FakeBookVenue("cex", bid="2700", ask="2701")
    cache = LegCache()

    async def go():
        await cache.get(v, "USDC", "WETH", D("1000"))
        await cache.get(v, "USDC", "WETH", D("1000"))

    run(go())
    assert v.quote_calls == 2, "book venue must be re-quoted every time"
    assert cache.hits == 0, cache.hits


# --- 2. Staleness is judged per venue type ------------------------------

def test_stale_order_book_is_refused():
    cex = FakeBookVenue("cex", bid="2700", ask="2701", book_age_ms=5000.0)
    chain = FakeChainVenue("chain", price="2700")
    eng = Engine(venues=[cex, chain], max_book_age_ms=400.0)
    r = run(eng.evaluate_route(Route("cex", "chain", "WETH", "USDC"), D("1000")))
    assert not r.ok
    assert "stale" in (r.rejected or ""), r.rejected


def test_chain_quote_is_not_refused_for_wall_clock_age():
    """The modelling point. A chain venue reports age 0 because its quote is
    exact until the block moves; judging it on wall clock would throw away
    perfectly good quotes.
    """
    chain_a = FakeChainVenue("a", price="2700")
    chain_b = FakeChainVenue("b", price="2720")
    eng = Engine(venues=[chain_a, chain_b], max_book_age_ms=1.0)
    r = run(eng.evaluate_route(Route("a", "b", "WETH", "USDC"), D("1000")))
    assert r.ok, r.rejected


def test_unhealthy_venue_is_refused():
    cex = FakeBookVenue("cex", bid="2700", ask="2701")
    cex._healthy = False
    chain = FakeChainVenue("chain", price="2700")
    eng = Engine(venues=[cex, chain])
    r = run(eng.evaluate_route(Route("cex", "chain", "WETH", "USDC"), D("1000")))
    assert not r.ok and "unhealthy" in (r.rejected or ""), r.rejected


# --- 3. Route enumeration grows with the venue list ---------------------

def test_routes_are_every_ordered_pair_plus_controls():
    """N venues give N*(N-1) cross routes and N same-venue controls. This is
    what makes a new venue multiply coverage instead of adding one case.
    """
    vs = [FakeChainVenue(f"v{i}", price="2700") for i in range(3)]
    eng = Engine(venues=vs)
    routes = eng.routes()
    cross = [r for r in routes if r.cross_venue_kind == "cross"]
    same = [r for r in routes if r.cross_venue_kind == "same"]
    assert len(cross) == 6, len(cross)     # 3*2
    assert len(same) == 3, len(same)
    # And the engine never needed to know what any of them are.
    assert {r.buy_on for r in cross} == {"v0", "v1", "v2"}


def test_venue_missing_the_pair_is_left_out():
    good = FakeChainVenue("good", price="2700")
    other = FakeChainVenue("other", price="2700", base="WBTC")
    eng = Engine(venues=[good, other], base="WETH", quote="USDC")
    names = {r.buy_on for r in eng.routes()} | {r.sell_on for r in eng.routes()}
    assert "other" not in names, names


# --- 4. Leg chaining uses realised output -------------------------------

def test_second_leg_is_sized_from_the_first_legs_actual_output():
    """If leg 1 under-delivers, leg 2 must trade what is actually held. The
    fake records the size it was asked for, so this is checked directly.
    """
    seen: list[Decimal] = []

    @dataclass
    class Recorder(FakeChainVenue):
        async def leg(self, asset_in, asset_out, size_in):
            seen.append(size_in)
            return await FakeChainVenue.leg(self, asset_in, asset_out, size_in)

    buy = FakeChainVenue("buy", price="2000")   # 1000 USDC -> ~0.5 WETH
    sell = Recorder("sell", price="2000")
    eng = Engine(venues=[buy, sell])
    r = run(eng.evaluate_route(Route("buy", "sell", "WETH", "USDC"), D("1000")))
    assert r.ok, r.rejected
    # Leg 2 receives WETH, not the original 1000 USDC.
    assert seen and seen[-1] < D("1"), seen
    assert approx(seen[-1], "0.49975", "0.001"), seen[-1]


def approx(got: Decimal, want: str, tol: str = "0.01") -> bool:
    return abs(got - D(want)) <= D(tol)


# --- 5. Aliases are declared, and their use is surfaced -----------------

def test_canonical_mapping_lets_differently_named_assets_chain():
    al = Aliases()
    assert al.canonical("ETH") == "WETH"
    assert al.canonical("USD") == "USDC"
    assert al.canonical("WETH") == "WETH"
    assert al.canonical("DAI") == "DAI"


def test_unproven_alias_is_reported_on_routes_that_use_it():
    """USDC==USD is a market relationship, not a contract. A result that
    depends on it must say so, or it reads as firm as one that does not.
    """
    usd_venue = FakeBookVenue("cex", bid="2700", ask="2701", quote="USD")
    chain = FakeChainVenue("chain", price="2700")
    eng = Engine(venues=[usd_venue, chain])
    r = run(eng.evaluate_route(Route("cex", "chain", "WETH", "USDC"), D("1000")))
    assert r.ok, r.rejected
    assert any("USDC=USD" in a for a in r.assumptions), r.assumptions

    # A route between two USDC-native venues carries no such caveat.
    chain2 = FakeChainVenue("chain2", price="2710")
    eng2 = Engine(venues=[chain, chain2])
    r2 = run(eng2.evaluate_route(Route("chain", "chain2", "WETH", "USDC"),
                                 D("1000")))
    assert r2.ok and not r2.assumptions, r2.assumptions


def test_proven_alias_is_not_reported_as_an_assumption():
    """WETH/ETH is 1:1 by the WETH contract, so it is not a caveat."""
    al = Aliases(declared=(Alias("WETH", "ETH", proven=True),))
    assert al.unproven_used({"ETH"}) == ()
    assert al.used({"ETH"})


# --- 6. Costs combine across venues -------------------------------------

def test_fixed_costs_from_both_venues_are_charged():
    """Two on-chain legs mean two lots of gas; the engine must add them."""
    a = FakeChainVenue("a", price="2700", gas=D("1.00"))
    b = FakeChainVenue("b", price="2700", gas=D("1.00"))
    eng = Engine(venues=[a, b])
    r = run(eng.evaluate_route(Route("a", "b", "WETH", "USDC"), D("1000")))
    assert r.ok, r.rejected
    # $2 of gas on $1000 = -20bps.
    assert approx(r.edge.fixed_cost_bps, "-20", "0.01"), r.edge.fixed_cost_bps


def test_same_venue_control_loses_the_spread():
    """A round trip inside one venue should cost its spread plus two fees.
    Bid 2700 / ask 2701 is ~3.7bps of spread, plus 2 x 10bps of fee.
    """
    v = FakeBookVenue("cex", bid="2700", ask="2701", fee=D("0.001"))
    eng = Engine(venues=[v])
    r = run(eng.evaluate_route(Route("cex", "cex", "WETH", "USDC"), D("1000")))
    assert r.ok, r.rejected
    assert approx(r.edge.gross_edge_bps, "-3.7", "0.2"), r.edge.gross_edge_bps
    assert approx(r.edge.fee_bps, "-20", "0.1"), r.edge.fee_bps


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
