"""The engine: pair up whatever venues it is given and cost every route.

Knows nothing about Coinbase, Uniswap, or any chain. It takes a list of
Venue objects and an asset pair, enumerates every ORDERED pair of venues,
builds the two-leg round trip through each, and costs it. Adding a venue
changes the input list and nothing here.

With N venues quoting the same pair there are N*(N-1) ordered routes, so the
work grows quadratically while the venue code grows linearly -- which is the
point. Three venues give 6 routes; the engine found cross-chain routes between
Base and Arbitrum for free the moment both were registered, without being
told that cross-chain arbitrage exists.

Each route is judged by the guard that fits its venues, not by one global
threshold:

  Order-book venues are checked on age_ms, because an old book is dangerous.
  Block-atomic venues are checked on state_id, because a quote from the
  current block is exact however old the wall clock says it is.

A route is also tagged with any UNPROVEN alias it leaned on (USDC treated as
USD, say). That does not disqualify it -- it records that the result depends
on a basis nobody has guaranteed, so it is never confused with one that
stands on its own.

NOT MODELLED, and the engine says so rather than implying otherwise: moving
inventory between venues. A route from Coinbase to Base assumes you already
hold both sides. Cross-chain routes additionally assume a bridge, which has
its own cost, delay and risk. These are measurements of dislocation, not
executable plans.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import permutations

from core.venue import Aliases, Venue, LegCache
from netedge import EdgeResult, evaluate

BPS = Decimal(10000)


@dataclass(frozen=True)
class Route:
    """A two-leg round trip: buy the volatile asset on one venue, sell on another."""
    buy_on: str      # venue name where quote -> base
    sell_on: str     # venue name where base -> quote
    base: str
    quote: str

    @property
    def key(self) -> str:
        return f"{self.base}-{self.quote}:{self.buy_on}=>{self.sell_on}"

    @property
    def cross_venue_kind(self) -> str:
        return "same" if self.buy_on == self.sell_on else "cross"


@dataclass(frozen=True)
class RouteResult:
    route: Route
    size_usd: Decimal
    edge: EdgeResult | None
    rejected: str | None
    # Unproven equivalences this result depends on, e.g. USDC==USD.
    assumptions: tuple[str, ...] = ()
    # True when the two legs settle on different chains / venues and therefore
    # need inventory on both sides, or a bridge.
    needs_inventory: bool = True

    @property
    def ok(self) -> bool:
        return self.edge is not None and self.edge.ok and self.rejected is None

    def line(self) -> str:
        if not self.ok:
            return f"  {self.route.key:52s} ${self.size_usd:>8,.0f}  REJECTED {self.rejected}"
        e = self.edge
        assert e is not None
        flag = "  <== POSITIVE" if e.net_edge_bps > 0 else ""
        note = "  [assumes " + "; ".join(self.assumptions) + "]" if self.assumptions else ""
        return (f"  {self.route.key:52s} ${self.size_usd:>8,.0f}  "
                f"net {e.net_edge_bps:+9.2f}bps  gross {e.gross_edge_bps:+8.2f} "
                f"fees {e.fee_bps:+7.2f} slip {e.slippage_bps:+8.2f} "
                f"gas {e.fixed_cost_bps:+6.2f}{flag}{note}")


@dataclass
class Engine:
    """Costs every route across a set of venues for one asset pair."""
    venues: list[Venue]
    base: str = "WETH"
    quote: str = "USDC"
    aliases: Aliases = field(default_factory=Aliases)
    cache: LegCache = field(default_factory=LegCache)
    # An order-book quote older than this is refused. Chain venues are
    # exempt by construction -- see the module docstring.
    max_book_age_ms: float = 400.0

    evaluated: int = 0
    rejected: int = 0

    async def connect(self) -> None:
        for v in self.venues:
            await v.connect()

    async def aclose(self) -> None:
        for v in self.venues:
            try:
                await v.aclose()
            except Exception:  # noqa: BLE001 -- one venue must not block the rest
                pass

    async def refresh(self) -> None:
        for v in self.venues:
            try:
                await v.refresh()
            except Exception:  # noqa: BLE001
                pass

    def by_name(self, name: str) -> Venue | None:
        for v in self.venues:
            if v.name == name:
                return v
        return None

    def _usable(self, v: Venue) -> str | None:
        """Why this venue cannot be quoted right now, or None if it can."""
        if not v.healthy():
            return f"{v.name} unhealthy"
        if not v.exact_while_state_unchanged and v.age_ms() > self.max_book_age_ms:
            return f"{v.name} book stale ({v.age_ms():.0f}ms)"
        return None

    def _venue_assets(self, v: Venue) -> dict[str, str]:
        """canonical symbol -> the venue's own symbol for it."""
        return {self.aliases.canonical(s): s for s in v.assets()}

    def routes(self) -> list[Route]:
        """Every ordered venue pair that can quote this asset pair.

        Includes same-venue routes, which are a useful control: a round trip
        inside one venue should be negative by roughly its spread plus fees,
        and if it is not, the measurement is wrong rather than the market
        being generous.
        """
        able = []
        for v in self.venues:
            m = self._venue_assets(v)
            if self.base in m and self.quote in m:
                able.append(v)
        out = [Route(a.name, b.name, self.base, self.quote)
               for a, b in permutations(able, 2)]
        out += [Route(v.name, v.name, self.base, self.quote) for v in able]
        return out

    async def evaluate_route(self, route: Route,
                             size_usd: Decimal) -> RouteResult:
        buy_v, sell_v = self.by_name(route.buy_on), self.by_name(route.sell_on)
        if buy_v is None or sell_v is None:
            return RouteResult(route, size_usd, None, "venue missing")

        for v in (buy_v, sell_v):
            why = self._usable(v)
            if why:
                self.rejected += 1
                return RouteResult(route, size_usd, None, why)

        buy_map, sell_map = self._venue_assets(buy_v), self._venue_assets(sell_v)
        # Each venue is addressed in ITS OWN symbols; the canonical names are
        # only the engine's bookkeeping.
        b_in, b_out = sell_map.get(self.quote), buy_map.get(self.base)
        leg1 = await self.cache.get(buy_v, buy_map[self.quote],
                                    buy_map[self.base], size_usd)
        if leg1 is None:
            self.rejected += 1
            return RouteResult(route, size_usd, None, f"{buy_v.name} no quote")

        # Size the second leg from what the first actually produced.
        got = leg1.full(size_usd).amount_out
        if got <= 0:
            self.rejected += 1
            return RouteResult(route, size_usd, None, f"{buy_v.name} zero out")
        leg2 = await self.cache.get(sell_v, sell_map[self.base],
                                    sell_map[self.quote], got)
        if leg2 is None:
            self.rejected += 1
            return RouteResult(route, size_usd, None, f"{sell_v.name} no quote")

        # Relabel to canonical names so the path closes across venues that
        # spell the same asset differently.
        legs = [_Canon(leg1, self.quote, self.base),
                _Canon(leg2, self.base, self.quote)]

        fixed = buy_v.fixed_cost_usd() + sell_v.fixed_cost_usd()
        r = evaluate(legs, size_usd, fixed_cost_usd=fixed,
                     start_asset_usd_price=Decimal(1), max_skew_ms=None)
        self.evaluated += 1

        venue_syms = set(buy_v.assets()) | set(sell_v.assets())
        unproven = self.aliases.unproven_used(venue_syms)
        return RouteResult(
            route, size_usd, r, None if r.ok else r.reason,
            assumptions=tuple(a.describe() for a in unproven),
            needs_inventory=True,
        )

    async def refresh_volatile(self) -> None:
        """Re-read only the venues whose data ages.

        A streamed order book is maintained in the background, so refreshing
        it costs no network -- but it MUST be re-read immediately before use,
        because a sweep spends seconds in RPC calls and a book snapshot taken
        at the start of that is long dead by the end. Block-atomic venues are
        skipped: their quotes do not rot between refreshes, only between
        blocks.
        """
        for v in self.venues:
            if not v.exact_while_state_unchanged:
                try:
                    await v.refresh()
                except Exception:  # noqa: BLE001
                    pass

    async def sweep(self, sizes: tuple[Decimal, ...]) -> list[RouteResult]:
        out = []
        for route in self.routes():
            for size in sizes:
                # Freshen ageing venues per route, not once per sweep.
                await self.refresh_volatile()
                out.append(await self.evaluate_route(route, size))
        return out

    def control_check(self, results: list[RouteResult]) -> list[str]:
        """Self-test: a same-venue round trip cannot have positive gross edge.

        Buying and selling the same asset at the same venue must lose at
        least its spread before any cost. A positive gross there is not a
        market observation, it is proof the frictionless baseline for that
        venue is wrong -- and a wrong baseline misattributes cost between the
        fee and slippage columns on every cross-venue route too.

        This caught a real error: an aggregator's "marginal price" taken from
        a tiny-size quote is not a pool price at all. A small order can be
        routed through entirely different hops than a large one, so the two
        directions' tiny quotes are not reciprocal and their product implies
        free money. An aggregator cannot report its own frictionless price;
        only a direct pool read can.
        """
        problems: list[str] = []
        for r in results:
            if r.route.cross_venue_kind != "same" or not r.ok:
                continue
            assert r.edge is not None
            if r.edge.gross_edge_bps > Decimal("0.01"):
                problems.append(
                    f"{r.route.buy_on}: same-venue round trip shows gross "
                    f"{r.edge.gross_edge_bps:+.2f}bps at ${r.size_usd:,.0f}. "
                    "Impossible -- that venue's frictionless baseline is "
                    "wrong, so its fee/slippage split cannot be trusted."
                )
        return problems

    def status(self) -> str:
        lines = [f"engine: {self.evaluated} evaluated, {self.rejected} rejected, "
                 f"{self.cache.stats()}"]
        for v in self.venues:
            mark = "ok " if v.healthy() else "BAD"
            extra = v.status() if hasattr(v, "status") else ""
            lines.append(f"  [{mark}] {v.name:28s} {v.kind:4s} {extra}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Canon:
    """Wraps a Leg so its asset names are the engine's canonical ones.

    Venues spell assets differently -- WETH here, ETH there -- and a path only
    closes when the names match. Renaming at the boundary keeps the venue
    honest about its own symbols while letting paths chain.
    """
    inner: object
    asset_in: str
    asset_out: str

    @property
    def venue(self) -> str:
        return getattr(self.inner, "venue", "?")

    @property
    def ts_local(self) -> float:
        return getattr(self.inner, "ts_local", 0.0)

    def frictionless(self, amount_in: Decimal) -> Decimal:
        return self.inner.frictionless(amount_in)  # type: ignore[attr-defined]

    def fee_only(self, amount_in: Decimal) -> Decimal:
        return self.inner.fee_only(amount_in)      # type: ignore[attr-defined]

    def full(self, amount_in: Decimal):
        return self.inner.full(amount_in)          # type: ignore[attr-defined]
