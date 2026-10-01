"""Coinbase Advanced Trade market data.

Uses only the public /market/* endpoints, so the shadow logger needs NO API
credentials and cannot place an order even by accident. That is a deliberate
safety property: keep it that way until you have decided to trade.

Fees are taken from config (pinned at 10 bps/leg). Verify your real tier with
an authenticated GET /api/v3/brokerage/transaction_summary before trusting it;
the entry Advanced Trade tier is roughly 60 bps taker, which closes almost
every path on its own.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Iterable

import httpx

from legs import Book, Level

BASE_URL = "https://api.coinbase.com/api/v3/brokerage"
PRODUCTS = f"{BASE_URL}/market/products"
PRODUCT_BOOK = f"{BASE_URL}/market/product_book"


class CoinbaseMarketData:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_concurrency: int = 8,
        book_depth: int = 50,
        timeout: float = 10.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"Accept": "application/json"}
        )
        self._owns_client = client is None
        self._sem = asyncio.Semaphore(max_concurrency)
        self.book_depth = book_depth

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def products(self) -> dict[str, tuple[str, str]]:
        """product_id -> (base, quote) for online, tradable spot products."""
        r = await self._client.get(PRODUCTS, params={"product_type": "SPOT"})
        r.raise_for_status()
        out: dict[str, tuple[str, str]] = {}
        for p in r.json().get("products", []):
            if p.get("trading_disabled") or p.get("is_disabled"):
                continue
            if p.get("status", "online") != "online":
                continue
            base = p.get("base_currency_id") or p.get("base_currency")
            quote = p.get("quote_currency_id") or p.get("quote_currency")
            pid = p.get("product_id")
            if pid and base and quote:
                out[pid] = (base, quote)
        return out

    async def book(self, product_id: str, base: str, quote: str) -> Book | None:
        """One L2 snapshot. ts_local is set from the local clock on receipt.

        Both timestamps matter: ts_local drives skew checks between venues,
        because exchange clocks are not comparable across venues.
        """
        async with self._sem:
            try:
                r = await self._client.get(
                    PRODUCT_BOOK,
                    params={"product_id": product_id, "limit": self.book_depth},
                )
                ts_local = time.time()
                r.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException):
                return None

        pb = r.json().get("pricebook") or {}
        bids = tuple(
            Level(Decimal(str(x["price"])), Decimal(str(x["size"])))
            for x in pb.get("bids", []) if Decimal(str(x["size"])) > 0
        )
        asks = tuple(
            Level(Decimal(str(x["price"])), Decimal(str(x["size"])))
            for x in pb.get("asks", []) if Decimal(str(x["size"])) > 0
        )
        if not bids or not asks:
            return None
        return Book(
            product_id=product_id, base=base, quote=quote,
            bids=bids, asks=asks, ts_local=ts_local,
        )

    async def books(
        self, products: dict[str, tuple[str, str]]
    ) -> dict[str, Book]:
        """Fetch many books as concurrently as the semaphore allows.

        Concurrency here is not an optimisation, it is a correctness
        requirement: the wider the fetch window, the larger the skew between
        legs, and skew is what manufactures phantom arbitrage.
        """
        tasks = [
            self.book(pid, base, quote)
            for pid, (base, quote) in products.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, Book] = {}
        for res in results:
            if isinstance(res, Book):
                out[res.product_id] = res
        return out

    async def probe_rtt_ms(self, product_id: str = "BTC-USD", n: int = 10) -> list[float]:
        """Public REST round-trip, as a lower bound on your real latency.

        An order-ack round-trip is strictly worse than this. Use it to sanity
        check whether logged opportunity lifetimes are even in reach.
        """
        samples: list[float] = []
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = await self._client.get(
                    PRODUCT_BOOK, params={"product_id": product_id, "limit": 1}
                )
                r.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException):
                continue
            samples.append((time.perf_counter() - t0) * 1000.0)
            await asyncio.sleep(0.1)
        return samples


async def _demo() -> None:
    md = CoinbaseMarketData()
    try:
        prods = await md.products()
        print(f"online spot products: {len(prods)}")
        usd_quoted = {k: v for k, v in prods.items() if v[1] == "USD"}
        print(f"  USD-quoted: {len(usd_quoted)}")
        t0 = time.time()
        books = await md.books({k: prods[k] for k in list(prods)[:5]})
        print(f"fetched {len(books)} books in {(time.time()-t0)*1000:.0f}ms")
        for b in books.values():
            spread = (b.best_ask - b.best_bid) / b.best_bid * 10000
            print(f"  {b.product_id:12s} bid {b.best_bid}  ask {b.best_ask}"
                  f"  spread {spread:.1f}bps  levels {len(b.bids)}/{len(b.asks)}")
        rtt = await md.probe_rtt_ms(n=5)
        if rtt:
            print(f"public REST rtt: avg {sum(rtt)/len(rtt):.0f}ms  max {max(rtt):.0f}ms")
    finally:
        await md.aclose()


if __name__ == "__main__":
    asyncio.run(_demo())
