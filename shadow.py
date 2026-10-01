"""Continuous shadow logger: watch for dislocation without trading.

Answers two different questions, and they need different machinery:

  "Is there an opportunity NOW?"   -> the live line this prints each poll.
  "Was there EVER an opportunity?" -> the edge_samples distribution, read
                                      back with `python storage.py shadow.db`.

The second is the one that decides whether to risk capital, and it is the
reason this logs EVERY evaluation rather than only the profitable ones. A log
containing nothing is ambiguous -- no edge, or a broken logger? A log of the
full distribution is not: it says how close the market came, how often, and
for how long.

SAMPLING RATE IS THE WHOLE GAME. An opportunity that lives 200ms is invisible
to a logger that looks every 2 seconds; you would conclude the market is
efficient when you simply never looked while it wasn't. So the loop measures
and reports its own achieved rate, and the gross edge -- which is
size-independent, being a ratio of marginal prices -- is computed from ONE
tiny quote per side so a poll stays cheap enough to repeat often. Net edge at
size costs a full quote and is sampled at fewer sizes for that reason.

What this CANNOT tell you, by construction: whether you could have filled.
The edge recorded here is what was visible at time t. Feed the log through
replay.py to find out what survives your latency -- that is a separate and
much harsher number.

Usage:
  PYTHONPATH=. python shadow.py                      # ETH-USDC, 5bps tier
  PYTHONPATH=. python shadow.py --product=ETH-USDC --tier=500 --seconds=300
  PYTHONPATH=. python shadow.py --sizes=1000,10000 --db=shadow.db
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal as D

import notify
from config import COINBASE, RunConfig
from legs import Book, BookLeg
from netedge import evaluate
from storage import OpenOpportunity, Store
from venues.coinbase import CoinbaseMarketData
from venues.evm import (
    CANDIDATES,
    GAS_ONE_SWAP,
    TokenMeta,
    TokenRegistry,
    client_for,
    measure_gas_usd,
    v3_deployment,
    v3_leg,
)

# Gas price and the chain's native USD price move slowly compared with the
# poll loop, so re-reading them every poll wastes round trips that are better
# spent on sampling rate.
GAS_REFRESH_S = 60.0


@dataclass
class Watcher:
    """One CEX<->DEX pair under observation."""
    product: str          # Coinbase product, e.g. ETH-USDC
    tier: int             # Uniswap v3 fee tier in uint24 units
    sizes: tuple[D, ...]
    token_in: TokenMeta   # the volatile token (WETH)
    token_out: TokenMeta  # the stable token (USDC)

    def key(self, direction: str) -> str:
        return f"{self.product}:base-v3-{self.tier}:{direction}"


@dataclass
class Loop:
    cfg: RunConfig
    store: Store
    watcher: Watcher
    cb: CoinbaseMarketData
    client: object
    quoter: str
    cex_fee: D

    sink: notify.Fanout | None = None
    heartbeat_s: float = 2.0

    polls: int = 0
    errors: int = 0
    uncommitted: int = 0
    gas_usd: D = D("0")
    _gas_at: float = 0.0
    _last_beat: float = 0.0
    open_opps: dict[str, OpenOpportunity] = field(default_factory=dict)
    best_seen: dict[str, D] = field(default_factory=dict)
    latest: dict[str, dict] = field(default_factory=dict)
    stop: bool = False

    def push(self, ev: dict) -> None:
        """Notify, never block. A missed notification costs less than a
        missed sample, so sinks are fire-and-forget."""
        if self.sink is not None:
            self.sink.emit(ev)

    async def poll(self) -> str | None:
        w = self.watcher
        c = self.client

        raw = await self.cb.book(w.product, w.token_in.symbol.lstrip("W"),
                                 w.token_out.symbol)
        if raw is None:
            self.errors += 1
            return None
        # Relabel ETH -> WETH so paths close. 1:1 by the WETH contract; the
        # wrap gas is not counted, nor is any inter-venue transfer.
        book = Book(w.product, w.token_in.symbol, w.token_out.symbol,
                    raw.bids, raw.asks, raw.ts_local, raw.ts_exchange)

        # One tiny quote per side gives the marginal price, which is all the
        # gross edge needs. Reference size is the smallest watched size.
        ref = min(w.sizes)
        dex_buy = v3_leg(c, self.quoter, w.token_out, w.token_in, w.tier, ref)
        # dex_buy is USDC->WETH, so its marginal is WETH PER USDC. Converting
        # the USDC reference size into WETH is a multiply. Dividing here blew
        # the reference up by ~price^2, which made the "tiny" probe large
        # enough to move the pool and corrupted the marginal price -- gross
        # edge then read worse than net, which is arithmetically impossible
        # and is the signature of a bad baseline.
        weth_ref = (ref * dex_buy.marginal_out_per_in
                    if dex_buy.marginal_out_per_in > 0 else ref)
        dex_sell = v3_leg(c, self.quoter, w.token_in, w.token_out, w.tier,
                          weth_ref)

        now = time.time()
        if now - self._gas_at > GAS_REFRESH_S:
            try:
                self.gas_usd = measure_gas_usd(
                    c, GAS_ONE_SWAP, dex_sell.marginal_out_per_in)
                self._gas_at = now
            except Exception:  # noqa: BLE001 -- stale gas beats no sample
                pass

        skew = abs(dex_buy.ts_local - book.ts_local) * 1000.0
        cex_sell = BookLeg(book=book, asset_in=w.token_in.symbol,
                           fee_rate=self.cex_fee, venue="coinbase")
        cex_buy = BookLeg(book=book, asset_in=w.token_out.symbol,
                          fee_rate=self.cex_fee, venue="coinbase")

        headline: str | None = None
        for direction, path in (("dex->cex", [dex_buy, cex_sell]),
                                ("cex->dex", [cex_buy, dex_sell])):
            key = w.key(direction)
            for size in w.sizes:
                r = evaluate(path, size, fixed_cost_usd=self.gas_usd,
                             start_asset_usd_price=D(1),
                             max_skew_ms=self.cfg.max_skew_ms)
                if not r.ok:
                    self.store.record_rejection(key, r.reason or "unknown")
                    continue

                # Every cost term is non-positive, so gross >= net always. A
                # violation means the frictionless baseline is wrong, not that
                # the market is odd -- log the rejection rather than poison
                # the distribution with a number that cannot be true.
                if r.gross_edge_bps < r.net_edge_bps - D("0.01"):
                    self.errors += 1
                    self.store.record_rejection(key, "baseline gross<net")
                    continue

                self.store.record_sample(
                    key, size, r.gross_edge_bps, r.net_edge_bps,
                    fee_bps=r.fee_bps, slip_bps=r.slippage_bps,
                    skew_ms=skew, exhausted=r.exhausted, ts=now)
                self.uncommitted += 1

                prev = self.best_seen.get(key)
                if prev is None or r.gross_edge_bps > prev:
                    self.best_seen[key] = r.gross_edge_bps

                self.latest[f"{key}@{size}"] = {
                    "path": key, "size_usd": size,
                    "gross_bps": r.gross_edge_bps, "net_bps": r.net_edge_bps,
                    "fee_bps": r.fee_bps, "slip_bps": r.slippage_bps,
                    "gas_bps": r.fixed_cost_bps, "skew_ms": round(skew, 1),
                }

                breakdown = {"gross": r.gross_edge_bps, "fee": r.fee_bps,
                             "slippage": r.slippage_bps, "fixed": r.fixed_cost_bps}
                opp_key = f"{key}@{size}"
                if r.net_edge_bps > self.cfg.min_edge_bps:
                    opp = self.open_opps.get(opp_key)
                    if opp is None:
                        self.open_opps[opp_key] = OpenOpportunity(
                            cycle_key=opp_key, path=r.path,
                            venues=("coinbase", f"base-v3-{w.tier}"),
                            t_open=now, size_usd=size,
                            edge_open_bps=r.net_edge_bps,
                            peak_edge_bps=r.net_edge_bps,
                            peak_breakdown=breakdown, max_skew_ms=skew,
                            exhausted=r.exhausted,
                            last_edge_bps=r.net_edge_bps)
                        headline = (f"OPEN {opp_key} net {r.net_edge_bps:+.2f}bps")
                        # Pushed the moment it opens: this is the rare event
                        # you actually want to be told about.
                        self.push(notify.event(
                            "open", id=opp_key, path=key, size_usd=size,
                            net_bps=r.net_edge_bps, gross_bps=r.gross_edge_bps,
                            fee_bps=r.fee_bps, slip_bps=r.slippage_bps,
                            gas_bps=r.fixed_cost_bps, skew_ms=round(skew, 1),
                            venues=["coinbase", f"base-v3-{w.tier}"]))
                    else:
                        opp.observe(r.net_edge_bps, breakdown, skew,
                                    r.exhausted, None)
                elif opp_key in self.open_opps:
                    # Closed: lifetime is the number that decides your fate.
                    opp = self.open_opps.pop(opp_key)
                    self.store.record_opportunity(opp, now)
                    life_ms = (now - opp.t_open) * 1000.0
                    headline = (f"CLOSED {opp_key} after {life_ms:.0f}ms "
                                f"peak {opp.peak_edge_bps:+.2f}bps")
                    # `id` is the correlation key a frontend pairs open with
                    # close on. It must be identical in both events and in the
                    # shutdown flush below, or every opportunity looks orphaned.
                    self.push(notify.event(
                        "close", id=opp_key, path=key, size_usd=size,
                        lifetime_ms=round(life_ms, 1),
                        peak_net_bps=opp.peak_edge_bps,
                        edge_open_bps=opp.edge_open_bps,
                        samples=opp.samples,
                        max_skew_ms=round(opp.max_skew_ms, 1)))

        if self.uncommitted >= 200:
            self.store.conn.commit()
            self.uncommitted = 0
        self.polls += 1

        # Heartbeat carries current state on an interval. Raw samples are not
        # pushed: several per second is a firehose the UI cannot use.
        if now - self._last_beat >= self.heartbeat_s:
            self._last_beat = now
            self.push(notify.event(
                "heartbeat",
                product=w.product, tier=w.tier,
                cex_bid=book.best_bid, cex_ask=book.best_ask,
                dex_mid=dex_sell.marginal_out_per_in,
                gas_usd=self.gas_usd, skew_ms=round(skew, 1),
                polls=self.polls, errors=self.errors,
                open_count=len(self.open_opps),
                best_gross_bps=max(self.best_seen.values())
                if self.best_seen else D(0),
                paths=list(self.latest.values())))
        return headline

    async def run(self, seconds: float) -> None:
        t0 = time.time()
        last_report = t0
        print(f"watching {self.watcher.product} <-> base v3 "
              f"{self.watcher.tier} | cex fee {self.cex_fee * 10000:.0f}bps | "
              f"sizes {[f'${s:,.0f}' for s in self.watcher.sizes]}")
        print(f"min_edge {self.cfg.min_edge_bps}bps  "
              f"max_skew {self.cfg.max_skew_ms:.0f}ms  "
              f"db {self.cfg.db_path}\n")

        while not self.stop and time.time() - t0 < seconds:
            try:
                headline = await self.poll()
            except Exception as e:  # noqa: BLE001 -- one bad poll is not fatal
                self.errors += 1
                headline = None
                if self.errors <= 3:
                    print(f"  poll error: {type(e).__name__}: {str(e)[:90]}")
            if headline:
                print(f"  {time.strftime('%H:%M:%S')}  {headline}")

            now = time.time()
            if now - last_report >= 15.0:
                rate = self.polls / (now - t0)
                best = max(self.best_seen.values()) if self.best_seen else D(0)
                print(f"  {time.strftime('%H:%M:%S')}  {self.polls} polls "
                      f"({rate:.2f}/s)  best gross so far {best:+.2f}bps  "
                      f"open {len(self.open_opps)}  errors {self.errors}")
                last_report = now
            await asyncio.sleep(self.cfg.poll_interval_s)

        # Flush anything still open, so a run that ends mid-dislocation does
        # not silently drop it.
        end = time.time()
        for opp in self.open_opps.values():
            self.store.record_opportunity(opp, end)
            # Also PUSH the close. Without this a frontend that saw the open
            # would show an opportunity stuck open forever, because shutdown
            # is exactly when nothing else will ever contradict it.
            self.push(notify.event(
                "close", id=opp.cycle_key,
                path=opp.cycle_key.rsplit("@", 1)[0], size_usd=opp.size_usd,
                lifetime_ms=round((end - opp.t_open) * 1000.0, 1),
                peak_net_bps=opp.peak_edge_bps,
                edge_open_bps=opp.edge_open_bps, samples=opp.samples,
                max_skew_ms=round(opp.max_skew_ms, 1),
                reason="logger shutdown"))
        self.open_opps.clear()
        self.store.conn.commit()

        elapsed = end - t0
        print(f"\n{self.polls} polls in {elapsed:.0f}s "
              f"({self.polls / elapsed if elapsed else 0:.2f}/s), "
              f"{self.errors} errors")
        if self.polls:
            print("Sampling rate bounds what you can see: a dislocation "
                  f"shorter than ~{1000 * elapsed / self.polls:.0f}ms can "
                  "pass entirely between polls.")


def parse_args(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in argv:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            out[k] = v
    return out


async def amain() -> int:
    opts = parse_args(sys.argv[1:])
    product = opts.get("product", "ETH-USDC")
    tier = int(opts.get("tier", "500"))
    seconds = float(opts.get("seconds", "120"))
    sizes = tuple(D(x) for x in opts.get("sizes", "1000,10000").split(","))
    cex_fee_bps = D(opts.get("cexfee", str(COINBASE.taker_bps)))
    cfg = RunConfig(
        db_path=opts.get("db", "shadow.db"),
        poll_interval_s=float(opts.get("interval", "0.25")),
        min_edge_bps=D(opts.get("minedge", "0.5")),
    )

    client = client_for("base")
    client.verify_chain_id()
    reg = TokenRegistry(client)
    reg.validate(CANDIDATES["base"])
    quoter = v3_deployment("base").quoter

    cb = CoinbaseMarketData(book_depth=50)
    products = await cb.products()
    if product not in products:
        print(f"{product} is not an online Coinbase spot product")
        await cb.aclose()
        client.close()
        return 2

    sink = notify.build(
        status=opts.get("status"),
        jsonl=opts.get("jsonl"),
        webhook=opts.get("webhook"),
        secret=opts.get("secret") or os.environ.get("SHADOW_WEBHOOK_SECRET"),
    )
    for s in sink.sinks:
        if isinstance(s, notify.Webhook):
            s.start()

    store = Store(cfg.db_path)
    loop = Loop(
        cfg=cfg, store=store,
        watcher=Watcher(product=product, tier=tier, sizes=sizes,
                        token_in=reg["WETH"], token_out=reg["USDC"]),
        cb=cb, client=client, quoter=quoter,
        cex_fee=cex_fee_bps / 10000,
        sink=sink if sink.sinks else None,
        heartbeat_s=float(opts.get("heartbeat", "2")),
    )

    def handle(*_: object) -> None:
        loop.stop = True
        print("\n  stopping; flushing open opportunities...")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, handle)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, handle)

    try:
        await loop.run(seconds)
    finally:
        await cb.aclose()
        client.close()
        if loop.sink is not None:
            stats = loop.sink.stats()
            await loop.sink.aclose()
            if stats:
                print(f"notify: {stats}")
        print()
        print(store.distribution())
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
