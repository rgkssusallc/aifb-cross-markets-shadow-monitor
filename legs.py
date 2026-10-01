"""Venue-agnostic leg model.

A Leg converts amount_in of asset_in into amount_out of asset_out. The three
quote methods exist so the calculator can decompose cost:

    frictionless -> marginal/best price, no fee, no impact   (gross edge)
    fee_only     -> marginal price with fee                  (isolates fees)
    full         -> real fill with fee AND price impact      (net edge)

Order-book venues implement impact by walking L2 depth. AMM venues implement
it from the pool's bonding curve. Both satisfy the same interface, so a path
can mix CEX and DEX legs freely.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Book:
    """L2 snapshot. bids descending by price, asks ascending by price."""
    product_id: str
    base: str
    quote: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ts_local: float
    ts_exchange: float | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None


@dataclass(frozen=True)
class LegQuote:
    amount_out: Decimal
    # True if the venue ran out of liquidity before filling amount_in.
    # A path containing an exhausted leg is at or past its capacity.
    exhausted: bool = False
    levels_used: int = 0


@runtime_checkable
class Leg(Protocol):
    asset_in: str
    asset_out: str
    venue: str
    ts_local: float

    def frictionless(self, amount_in: Decimal) -> Decimal: ...
    def fee_only(self, amount_in: Decimal) -> Decimal: ...
    def full(self, amount_in: Decimal) -> LegQuote: ...


# --- Order-book legs ------------------------------------------------------

@dataclass(frozen=True)
class BookLeg:
    """One side of one order-book product.

    Direction is inferred from asset_in: if asset_in is the quote currency we
    are buying base (walking asks); if it is the base we are selling (walking
    bids). Fee is charged on the quote side, which is how Coinbase bills it --
    modelled as reducing the quote amount by the fee rate in both directions.
    """
    book: Book
    asset_in: str
    fee_rate: Decimal
    venue: str = "cex"

    @property
    def asset_out(self) -> str:
        return self.book.base if self.asset_in == self.book.quote else self.book.quote

    @property
    def is_buy(self) -> bool:
        return self.asset_in == self.book.quote

    @property
    def ts_local(self) -> float:
        return self.book.ts_local

    def __post_init__(self) -> None:
        if self.asset_in not in (self.book.base, self.book.quote):
            raise ValueError(
                f"{self.asset_in} is neither base nor quote of {self.book.product_id}"
            )

    def _best(self) -> Decimal | None:
        return self.book.best_ask if self.is_buy else self.book.best_bid

    def frictionless(self, amount_in: Decimal) -> Decimal:
        px = self._best()
        if px is None or px <= 0:
            return Decimal(0)
        return amount_in / px if self.is_buy else amount_in * px

    def fee_only(self, amount_in: Decimal) -> Decimal:
        # Fee applies to the quote notional in both directions.
        if self.is_buy:
            return self.frictionless(amount_in * (Decimal(1) - self.fee_rate))
        return self.frictionless(amount_in) * (Decimal(1) - self.fee_rate)

    def full(self, amount_in: Decimal) -> LegQuote:
        if self.is_buy:
            spend = amount_in * (Decimal(1) - self.fee_rate)
            got = Decimal(0)
            used = 0
            for lvl in self.book.asks:
                if spend <= 0:
                    break
                cost_of_level = lvl.price * lvl.size
                used += 1
                if cost_of_level >= spend:
                    got += spend / lvl.price
                    spend = Decimal(0)
                    break
                got += lvl.size
                spend -= cost_of_level
            return LegQuote(got, exhausted=spend > 0, levels_used=used)

        remaining = amount_in
        proceeds = Decimal(0)
        used = 0
        for lvl in self.book.bids:
            if remaining <= 0:
                break
            used += 1
            take = lvl.size if lvl.size < remaining else remaining
            proceeds += take * lvl.price
            remaining -= take
        out = proceeds * (Decimal(1) - self.fee_rate)
        return LegQuote(out, exhausted=remaining > 0, levels_used=used)


# --- AMM legs -------------------------------------------------------------

@dataclass(frozen=True)
class ConstantProductLeg:
    """Uniswap v2 / Aerodrome volatile style: x * y = k, fee on input.

    Exact, not an approximation -- this is the pool's own arithmetic.
    """
    asset_in: str
    asset_out: str
    reserve_in: Decimal
    reserve_out: Decimal
    fee_rate: Decimal
    ts_local: float
    venue: str = "amm"

    def _marginal_price(self) -> Decimal:
        """asset_out per asset_in at infinitesimal size."""
        if self.reserve_in <= 0:
            return Decimal(0)
        return self.reserve_out / self.reserve_in

    def frictionless(self, amount_in: Decimal) -> Decimal:
        return amount_in * self._marginal_price()

    def fee_only(self, amount_in: Decimal) -> Decimal:
        return amount_in * (Decimal(1) - self.fee_rate) * self._marginal_price()

    def full(self, amount_in: Decimal) -> LegQuote:
        eff_in = amount_in * (Decimal(1) - self.fee_rate)
        if self.reserve_in <= 0 or self.reserve_out <= 0:
            return LegQuote(Decimal(0), exhausted=True)
        out = (eff_in * self.reserve_out) / (self.reserve_in + eff_in)
        # A constant-product pool never literally runs dry, but once you are
        # taking a large share of the reserve the quote is meaningless.
        return LegQuote(out, exhausted=eff_in > self.reserve_in / Decimal(2))


@dataclass(frozen=True)
class QuotedLeg:
    """A leg priced by an external quoter rather than local math.

    Use this for Uniswap v3/v4 (tick-crossing math is not worth
    reimplementing -- call QuoterV2.quoteExactInputSingle via eth_call) and
    for aggregator routes. `quote_fn` must return amount_out for amount_in,
    net of the pool fee. `marginal_out_per_in` is the spot price used for the
    frictionless baseline.
    """
    asset_in: str
    asset_out: str
    marginal_out_per_in: Decimal
    fee_rate: Decimal
    quote_fn: object  # Callable[[Decimal], Decimal]
    ts_local: float
    venue: str = "quoter"

    def frictionless(self, amount_in: Decimal) -> Decimal:
        return amount_in * self.marginal_out_per_in

    def fee_only(self, amount_in: Decimal) -> Decimal:
        return amount_in * (Decimal(1) - self.fee_rate) * self.marginal_out_per_in

    def full(self, amount_in: Decimal) -> LegQuote:
        out = self.quote_fn(amount_in)  # type: ignore[operator]
        return LegQuote(Decimal(out), exhausted=False)
