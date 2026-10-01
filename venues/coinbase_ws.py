"""Coinbase Advanced Trade level2 WebSocket feed.

Public channel, NO credentials, cannot place an order -- the same deliberate
safety property as venues/coinbase.py. PRESERVE IT.

Why this exists. REST polling has two costs that compound: the book you get
is already one round trip old, and fetching it occupies the poll loop, so a
higher sampling rate costs proportionally more requests. Measured here: REST
gave 1.3 polls/s, meaning a dislocation shorter than ~750ms could pass
entirely unseen. This feed pushes updates at a measured ~16 events/s with a
median gap of 51ms, and reading the maintained book costs no network at all.
The book is simply always current, and the poll loop is free to evaluate as
often as it likes.

THREE TRAPS, ALL MEASURED ON THE LIVE FEED, ALL GUARDED HERE:

  The feed silently rewrites the quote currency. Subscribing to ETH-USDC
  returns ETH-USD -- confirmed, and subscribing to both collapses them into
  one stream. REST serves them as distinct books; this does not. Using the
  result as if it were the USDC book would insert an unflagged USDC/USD basis
  into a signal only a few basis points wide. So this adapter NEVER reports
  the product you asked for: it reports what the feed confirmed, records the
  substitution, and `aliased` lists every one so a caller can refuse or
  correct for it. Measured basis at the time of writing was 0.00bps with no
  USDC-USD product listed at all, i.e. Coinbase treats them as one book --
  true today, not guaranteed tomorrow, which is exactly why it is surfaced
  rather than assumed.

  The snapshot exceeds the default frame limit. ETH-USD arrived as 19,876
  levels in one message, over 1MiB, which kills the connection with 1009
  "message too big" unless max_size is raised. That failure looks like a
  network problem and is not.

  A dropped message corrupts the book silently. Every message carries
  sequence_num; a gap means updates were missed and the maintained book is
  now quietly wrong, which is far more dangerous than having no book. On a
  gap the feed marks itself stale and resubscribes for a fresh snapshot, and
  `healthy` is False until one arrives.

Side values are "bid" and "offer" -- not "ask". A new_quantity of 0 removes
the level rather than setting it to zero.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal

import websockets

from legs import Book, Level

WS_URL = "wss://advanced-trade-ws.coinbase.com"

# The level2 snapshot for a liquid product is comfortably over 1MiB, so the
# library default must be lifted or the connection dies on subscribe.
MAX_FRAME = None  # unlimited

RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0)


@dataclass
class ProductBook:
    """Maintained L2 state for one product.

    Prices are kept in dicts rather than sorted structures: updates are
    frequent and touch arbitrary levels, while reads only ever want the top
    of book, so heapq over the dict is cheaper than keeping 20k levels
    ordered on every tick.
    """
    product_id: str
    base: str
    quote: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    offers: dict[Decimal, Decimal] = field(default_factory=dict)
    ts_local: float = 0.0          # when the last message was RECEIVED
    ts_event: float = 0.0          # exchange event_time of the last update
    updates_applied: int = 0
    has_snapshot: bool = False

    def apply(self, updates: list[dict], ts_local: float) -> None:
        for u in updates:
            try:
                px = Decimal(u["price_level"])
                qty = Decimal(u["new_quantity"])
            except (KeyError, ArithmeticError, TypeError):
                continue
            side = self.bids if u.get("side") == "bid" else self.offers
            if qty <= 0:
                side.pop(px, None)      # zero quantity REMOVES the level
            else:
                side[px] = qty
        self.ts_local = ts_local
        self.updates_applied += len(updates)

    def reset(self) -> None:
        self.bids.clear()
        self.offers.clear()
        self.has_snapshot = False

    def snapshot(self, depth: int = 50) -> Book | None:
        """Top-of-book as a legs.Book, or None if not usable.

        ts_local is when the last message arrived, which is what skew checks
        against other venues must use -- exchange clocks are not comparable.
        """
        if not self.has_snapshot or not self.bids or not self.offers:
            return None
        top_bids = heapq.nlargest(depth, self.bids.items())
        top_asks = heapq.nsmallest(depth, self.offers.items())
        if not top_bids or not top_asks:
            return None
        # A crossed book means we have applied updates incorrectly, or the
        # snapshot is mid-repair. Refuse it rather than invent free money:
        # a crossed book reads as an enormous arbitrage.
        if top_bids[0][0] >= top_asks[0][0]:
            return None
        return Book(
            product_id=self.product_id, base=self.base, quote=self.quote,
            bids=tuple(Level(p, s) for p, s in top_bids),
            asks=tuple(Level(p, s) for p, s in top_asks),
            ts_local=self.ts_local,
            ts_exchange=self.ts_event or None,
        )


@dataclass
class Level2Feed:
    """Background level2 consumer. Call book() whenever you want the state.

    requested is what you asked for; `confirmed` is what the feed actually
    streams and `aliased` maps any rewrite. Always key off confirmed.
    """
    requested: tuple[str, ...]
    depth: int = 50
    books: dict[str, ProductBook] = field(default_factory=dict)
    confirmed: tuple[str, ...] = ()
    aliased: dict[str, str] = field(default_factory=dict)

    seq: int | None = None
    gaps: int = 0
    reconnects: int = 0
    messages: int = 0
    stale: bool = True
    last_msg_at: float = 0.0
    _task: asyncio.Task | None = None
    _stop: bool = False
    _ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def healthy(self) -> bool:
        """False whenever the maintained book cannot be trusted.

        A silently wrong book is worse than no book, so anything that could
        have dropped updates flips this until a fresh snapshot lands.
        """
        return (not self.stale
                and bool(self.books)
                and any(b.has_snapshot for b in self.books.values()))

    async def start(self, timeout: float = 25.0) -> None:
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"level2 feed produced no snapshot within {timeout:.0f}s"
            ) from e

    async def aclose(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def book(self, product_id: str) -> Book | None:
        """Current top-of-book. None when unusable -- never a stale guess."""
        if not self.healthy:
            return None
        pb = self.books.get(product_id)
        return pb.snapshot(self.depth) if pb else None

    def _note_subscription(self, events: list[dict]) -> None:
        got: list[str] = []
        for ev in events or []:
            subs = ev.get("subscriptions") or {}
            for lst in subs.values():
                got.extend(lst or [])
        if not got:
            return
        self.confirmed = tuple(dict.fromkeys(got))
        # Record any rewrite, e.g. ETH-USDC -> ETH-USD. Matching on the base
        # currency because that is what the feed preserves.
        for want in self.requested:
            if want in self.confirmed:
                continue
            base = want.split("-")[0]
            for actual in self.confirmed:
                if actual.split("-")[0] == base:
                    self.aliased[want] = actual

    async def _run(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                async with websockets.connect(
                    WS_URL, open_timeout=20, max_size=MAX_FRAME,
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": list(self.requested),
                        "channel": "level2",
                    }))
                    attempt = 0
                    self.seq = None
                    for pb in self.books.values():
                        pb.reset()
                    await self._consume(ws)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 -- reconnect, never die
                self.stale = True
                self.reconnects += 1
                delay = RECONNECT_BACKOFF_S[min(attempt,
                                                len(RECONNECT_BACKOFF_S) - 1)]
                attempt += 1
                await asyncio.sleep(delay)

    async def _consume(self, ws) -> None:
        while not self._stop:
            raw = await ws.recv()
            ts_local = time.time()
            self.messages += 1
            self.last_msg_at = ts_local
            try:
                msg = json.loads(raw)
            except ValueError:
                continue

            # Sequence FIRST, for every message, whatever its channel.
            # sequence_num is per-connection and spans all channels: the
            # `subscriptions` acknowledgement consumes a number in the middle
            # of the l2_data stream. Checking it only on l2_data makes that
            # acknowledgement look like a dropped update, which reconnects,
            # which re-sends it -- a loop that reports constant gaps on a
            # perfectly healthy feed.
            sq = msg.get("sequence_num")
            if isinstance(sq, int):
                if self.seq is not None and sq != self.seq + 1:
                    self.gaps += 1
                    self.stale = True
                    for pb in self.books.values():
                        pb.reset()
                    raise ConnectionError(
                        f"level2 sequence gap: expected {self.seq + 1}, got {sq}"
                    )
                self.seq = sq

            channel = msg.get("channel")
            if channel == "subscriptions":
                self._note_subscription(msg.get("events", []))
                continue
            if channel in ("error",) or msg.get("type") == "error":
                self.stale = True
                continue
            if channel != "l2_data":
                continue

            for ev in msg.get("events", []):
                pid = ev.get("product_id")
                if not pid:
                    continue
                pb = self.books.get(pid)
                if pb is None:
                    base, _, quote = pid.partition("-")
                    pb = ProductBook(product_id=pid, base=base, quote=quote)
                    self.books[pid] = pb
                etype = ev.get("type")
                if etype == "snapshot":
                    pb.reset()
                    pb.apply(ev.get("updates", []), ts_local)
                    pb.has_snapshot = True
                    self.stale = False
                    self._ready.set()
                elif etype == "update":
                    if pb.has_snapshot:
                        pb.apply(ev.get("updates", []), ts_local)

    def status(self) -> str:
        parts = [f"msgs {self.messages}", f"gaps {self.gaps}",
                 f"reconnects {self.reconnects}",
                 "healthy" if self.healthy else "STALE"]
        if self.aliased:
            parts.append("aliased " + ",".join(
                f"{k}->{v}" for k, v in self.aliased.items()))
        return "  ".join(parts)


async def main() -> None:
    """Smoke test: PYTHONPATH=. python venues/coinbase_ws.py [product]"""
    import sys
    product = sys.argv[1] if len(sys.argv) > 1 else "ETH-USD"
    feed = Level2Feed(requested=(product,))
    await feed.start()
    if feed.aliased:
        print(f"WARNING feed rewrote the product: {feed.aliased}")
        print("  The quote currency you asked for is NOT what is streaming.")
    print(f"confirmed: {feed.confirmed}")
    pid = feed.confirmed[0] if feed.confirmed else product

    t0 = time.time()
    seen = 0
    while time.time() - t0 < 10:
        b = feed.book(pid)
        if b is not None:
            seen += 1
            age = (time.time() - b.ts_local) * 1000
            if seen % 20 == 1:
                print(f"  {pid} bid {b.best_bid} ask {b.best_ask} "
                      f"spread {(b.best_ask / b.best_bid - 1) * 10000:.2f}bps "
                      f"age {age:.0f}ms  levels {len(b.bids)}/{len(b.asks)}")
        await asyncio.sleep(0.1)

    pb = feed.books.get(pid)
    print(f"\n{feed.status()}")
    if pb:
        print(f"book depth held: {len(pb.bids)} bids / {len(pb.offers)} offers, "
              f"{pb.updates_applied:,} level updates applied")
    print(f"reads returning a usable book: {seen}/100")
    await feed.aclose()


if __name__ == "__main__":
    asyncio.run(main())
