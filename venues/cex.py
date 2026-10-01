"""Coinbase spot as a Venue, over either the level2 stream or REST.

Both transports already exist; this wraps them in the uniform interface so
the engine never knows which is in use. The transport is a constructor
argument, not a code path through the engine.

state_id is the level2 message count (or the book receipt time on REST), and
exact_while_state_unchanged is False: an order book is a new state on every
update and an old book is genuinely dangerous, so this venue is judged on
age_ms. That is the opposite of a chain venue, and it is the whole reason the
engine asks rather than assumes.

The quote-currency rewrite is handled here, where it belongs. The level2
channel silently serves ETH-USD when asked for ETH-USDC, so this venue
reports the symbols it ACTUALLY streams and exposes `substituted` for the
engine to record. Hiding that would bury a USDC/USD basis inside a signal
only a few basis points wide.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from config import COINBASE
from core.venue import CEX
from legs import Book, BookLeg, Leg
from venues.coinbase import CoinbaseMarketData
from venues.coinbase_ws import Level2Feed


@dataclass
class CoinbaseVenue:
    """One Coinbase spot product as a Venue.

    product is what you ask for; `actual_product` is what the venue really
    serves, which can differ on the streaming transport.
    """
    product: str = "ETH-USDC"
    use_ws: bool = True
    depth: int = 50
    taker_bps: Decimal = field(default_factory=lambda: COINBASE.taker_bps)
    name: str = ""
    kind: str = CEX

    rest: CoinbaseMarketData | None = None
    feed: Level2Feed | None = None
    actual_product: str = ""
    substituted: dict[str, str] = field(default_factory=dict)
    _book: Book | None = None
    _base: str = ""
    _quote: str = ""
    _polls: int = 0
    _last_err: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"coinbase:{self.product}:{'ws' if self.use_ws else 'rest'}"

    @property
    def fee_rate(self) -> Decimal:
        return self.taker_bps / Decimal(10000)

    # --- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        self.rest = CoinbaseMarketData(book_depth=self.depth)
        products = await self.rest.products()
        if self.product not in products:
            raise RuntimeError(
                f"{self.product} is not an online Coinbase spot product")
        self._base, self._quote = products[self.product]
        self.actual_product = self.product

        if self.use_ws:
            self.feed = Level2Feed(requested=(self.product,), depth=self.depth)
            await self.feed.start()
            if self.feed.confirmed:
                self.actual_product = self.feed.confirmed[0]
            # Record the rewrite rather than papering over it.
            if self.feed.aliased:
                self.substituted = dict(self.feed.aliased)
                b, _, q = self.actual_product.partition("-")
                self._base, self._quote = b, q

    async def aclose(self) -> None:
        if self.feed is not None:
            await self.feed.aclose()
            self.feed = None
        if self.rest is not None:
            await self.rest.aclose()
            self.rest = None

    async def refresh(self) -> None:
        if self.feed is not None:
            # Already current: the stream maintains the book, no round trip.
            self._book = self.feed.book(self.actual_product)
            if self._book is None:
                self._last_err = "feed unhealthy or book unusable"
            else:
                self._last_err = ""
                self._polls += 1
            return
        if self.rest is None:
            return
        b = await self.rest.book(self.actual_product, self._base, self._quote)
        if b is None:
            self._last_err = "rest book fetch failed"
        else:
            self._book = b
            self._last_err = ""
            self._polls += 1

    # --- Venue protocol ---------------------------------------------------

    def state_id(self):
        if self.feed is not None:
            return self.feed.messages
        return self._book.ts_local if self._book else None

    @property
    def exact_while_state_unchanged(self) -> bool:
        # An order book is never "exact until further notice": it ages.
        return False

    def healthy(self) -> bool:
        return self._book is not None and not self._last_err

    def age_ms(self) -> float:
        if self._book is None:
            return float("inf")
        return (time.time() - self._book.ts_local) * 1000.0

    def fixed_cost_usd(self) -> Decimal:
        return Decimal(0)   # no gas on a CEX; the fee is proportional

    def assets(self) -> set[str]:
        return {self._base, self._quote} if self._book else set()

    async def leg(self, asset_in: str, asset_out: str,
                  size_in: Decimal) -> Leg | None:
        if self._book is None or size_in <= 0:
            return None
        if {asset_in, asset_out} != {self._base, self._quote}:
            return None
        return BookLeg(book=self._book, asset_in=asset_in,
                       fee_rate=self.fee_rate, venue=self.name)

    def status(self) -> str:
        bits = [f"age {self.age_ms():.0f}ms"]
        if self.feed is not None:
            bits.append(self.feed.status())
        if self.substituted:
            bits.append("SUBSTITUTED " + ",".join(
                f"{k}->{v}" for k, v in self.substituted.items()))
        if self._last_err:
            bits.append(f"ERR {self._last_err}")
        return "  ".join(bits)
