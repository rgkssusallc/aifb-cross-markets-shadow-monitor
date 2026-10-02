"""Sustained monitoring across any venues: distribution AND lifetime.

The framework-native replacement for shadow.py, which was welded to Coinbase
plus Base. This takes whatever venues the Engine was given.

WHY LIFETIME IS THE POINT. An operator running this pair live shared three
days of 10-second samples of the SUI/USDC gap: median about -0.01%, p90 about
0.15%, p99 about 0.44%, against a break-even near 0.45% on a transfer cycle
and nearer 0.17% on pre-positioned inventory. So the p99 tail is where the
question lives.

But a point-in-time distribution sampled every 10 seconds cannot answer the
question that decides everything:

  Does an excursion into that tail SURVIVE long enough to act on, or is it a
  single-sample glitch?

Their own data cannot say -- at 10-second spacing a 2-second gap is invisible,
and a gap seen once may be the 2.8% quote glitch they measured rather than a
market. This logs every evaluation at whatever rate the venues allow, tracks
each excursion from open to close, and reports the LIFETIME distribution
beside the edge distribution. A tail that exists but dies in 200ms is not an
opportunity at 60ms round-trip latency, let alone at a 12-53s transfer.

Every sample is recorded regardless of sign, because a log containing only
winners cannot distinguish "no edge existed" from "the logger was broken",
nor being 2bps short from 200bps short.

Usage:
  PYTHONPATH=. python monitor.py --pair=SUI-USDC --seconds=300 --size=1000
  PYTHONPATH=. python monitor.py --pair=WETH-USDC --seconds=600 --db=run.db
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

import notify
from feedrow import FeedRow, leg_depth, leg_price
import time
from dataclasses import dataclass, field
from decimal import Decimal as D

from candidates import candidate_pairs
from config import PRE_POSITIONED, TRANSFER_CYCLE
from core.engine import Engine
from netedge import solve_capacity
from storage import OpenOpportunity, Store
from sweep import build_venues, parse


@dataclass
class Monitor:
    # One engine per pair. A list rather than one engine so that several
    # candidates are monitored in ONE loop and therefore appear in ONE
    # arbfeedall event: ten monitors would each push their own single-row
    # feed, and the table the frontend renders is meant to be all candidates
    # side by side. Route keys already carry the pair, so nothing collides.
    engines: list[Engine]
    store: Store
    size: D
    # An excursion is tracked once GROSS clears this, not net: the gross
    # series is the market, and whether it clears YOUR costs depends on
    # inventory mode, which is a separate question answered at the end.
    open_above_bps: D = D("0")
    interval_s: float = 1.0
    open_opps: dict[str, OpenOpportunity] = field(default_factory=dict)
    cycles: int = 0
    samples: int = 0
    uncommitted: int = 0
    best_gross: dict[str, D] = field(default_factory=dict)
    latest: dict[str, dict] = field(default_factory=dict)
    sink: notify.Fanout | None = None
    heartbeat_s: float = 5.0
    summary_s: float = 60.0
    feed_s: float = 15.0
    _last_beat: float = 0.0
    _last_summary: float = 0.0
    _last_feed: float = 0.0
    rows: dict = field(default_factory=dict)
    row_errors: int = 0
    pair_errors: int = 0
    capacity_errors: int = 0
    stop: bool = False

    def push(self, ev: dict) -> None:
        """Fire-and-forget. A missed notification costs less than a missed
        sample, so the sink never blocks the loop."""
        if self.sink is not None:
            self.sink.emit(ev)

    async def tick(self) -> None:
        # ALL venues, not just the ageing ones. refresh_volatile() skips
        # block-atomic venues because their quotes do not rot between
        # refreshes -- correct inside one sweep, wrong in a loop: their
        # state_id then never advances, LegCache serves the same leg forever,
        # and the run produces a confident flat distribution of one stale
        # quote. The cheap per-venue state check (eth_blockNumber, checkpoint,
        # object version) is exactly what makes refreshing all of them
        # affordable, and the cache still prevents re-quoting an unchanged one.
        for eng in self.engines:
            await eng.refresh()
        now = time.time()

        # Pairs are costed CONCURRENTLY, which with ten candidates is the
        # difference between seeing a short gap and not seeing it at all.
        # Sequentially, ten pairs took ~8.4s per cycle -- so any dislocation
        # shorter than 8.4s could pass entirely between two samples, and the
        # measurement would report a quiet market because it blinked. The
        # venues behind each engine are independent (own RPC client, own
        # feed), so there is nothing to serialise for.
        async def one(eng: Engine) -> None:
            for route in eng.routes():
                if route.cross_venue_kind != "cross":
                    continue       # controls are verified in sweep, not logged
                await self._sample(eng, route, now)

        # return_exceptions so one bad pair cannot cancel the other nine --
        # but the results are then read back and reported, because a gather
        # whose exceptions are never inspected is a silent failure channel.
        for eng, res in zip(self.engines,
                            await asyncio.gather(
                                *(one(e) for e in self.engines),
                                return_exceptions=True)):
            if isinstance(res, BaseException):
                self.pair_errors += 1
                if self.pair_errors <= 5:
                    print(f"  {eng.base}/{eng.quote} failed this cycle "
                          f"({type(res).__name__}: {str(res)[:70]})")

        if self.uncommitted >= 200:
            self.store.conn.commit()
            self.uncommitted = 0
        self.cycles += 1
        self.emit_periodic(now)

    async def _sample(self, eng: Engine, route, now: float) -> None:
        """Cost one route and fold the result into every output."""
        r = await eng.evaluate_route(route, self.size)
        key = route.key
        if not r.ok:
            self.store.record_rejection(key, r.rejected or "unknown")
            # Still emit a row. A candidate that disappears from the table
            # reads as a UI bug, and "we could not quote this" is itself
            # information -- which is why FeedRow carries ok/reason.
            self.rows[key] = self._row_rejected(route, r)
            return
        e = r.edge
        assert e is not None
        self.store.record_sample(
            key, self.size, e.gross_edge_bps, e.net_edge_bps,
            fee_bps=e.fee_bps, slip_bps=e.slippage_bps,
            skew_ms=0.0, exhausted=e.exhausted, ts=now)
        self.samples += 1
        self.uncommitted += 1

        prev = self.best_gross.get(key)
        if prev is None or e.gross_edge_bps > prev:
            self.best_gross[key] = e.gross_edge_bps
        try:
            self.rows[key] = self._row(eng, route, r, e)
        except Exception as ex:   # noqa: BLE001 -- a row must never stop a sample
            # Counted and reported. Swallowing this silently is what made
            # a one-word NameError look like an unimplemented feature.
            self.rows.pop(key, None)
            self.row_errors += 1
            if self.row_errors <= 2:
                print(f"  row build failed ({type(ex).__name__}: "
                      f"{str(ex)[:80]}); arbfeedall will be incomplete")

        self.latest[key] = {
            "route": key, "size_usd": self.size,
            "gross_bps": e.gross_edge_bps, "net_bps": e.net_edge_bps,
            "fee_bps": e.fee_bps, "slip_bps": e.slippage_bps,
            "gas_bps": e.fixed_cost_bps,
            "assumptions": list(r.assumptions),
        }

        breakdown = {"gross": e.gross_edge_bps, "fee": e.fee_bps,
                     "slippage": e.slippage_bps, "fixed": e.fixed_cost_bps}
        if e.gross_edge_bps > self.open_above_bps:
            opp = self.open_opps.get(key)
            if opp is None:
                self.open_opps[key] = OpenOpportunity(
                    cycle_key=key, path=e.path,
                    venues=(route.buy_on, route.sell_on),
                    t_open=now, size_usd=self.size,
                    edge_open_bps=e.gross_edge_bps,
                    peak_edge_bps=e.gross_edge_bps,
                    peak_breakdown=breakdown,
                    last_edge_bps=e.gross_edge_bps)
                self.push(notify.event(
                    "open", id=key, route=key, size_usd=self.size,
                    gross_bps=e.gross_edge_bps, net_bps=e.net_edge_bps))
            else:
                opp.observe(e.gross_edge_bps, breakdown, 0.0,
                            e.exhausted, None)
        elif key in self.open_opps:
            opp = self.open_opps.pop(key)
            self.store.record_opportunity(opp, now)
            self.push(notify.event(
                "close", id=key, route=key, size_usd=self.size,
                lifetime_ms=round((now - opp.t_open) * 1000.0, 1),
                peak_gross_bps=opp.peak_edge_bps, samples=opp.samples))

    def emit_periodic(self, now: float) -> None:
        """Heartbeat, feed and summary, on their own intervals."""
        if now - self._last_beat >= self.heartbeat_s:
            self._last_beat = now
            self.push(notify.event(
                "heartbeat", cycles=self.cycles, samples=self.samples,
                open_count=len(self.open_opps),
                best_gross_bps=max(self.best_gross.values())
                if self.best_gross else D(0),
                routes=list(self.latest.values())))
        # The distribution is what a dashboard should actually show: an
        # instantaneous gross number means little, the percentile tail is the
        # thing that decides whether the strategy exists.
        if now - self._last_feed >= self.feed_s and self.rows:
            self._last_feed = now
            self.push(notify.event("arbfeedall",
                                   size_usd=self.size,
                                   rows=list(self.rows.values())))
        if now - self._last_summary >= self.summary_s:
            self._last_summary = now
            self.store.conn.commit()
            self.push(notify.event("summary", **self.percentiles()))

    def percentiles(self) -> dict:
        out: dict[str, dict] = {}
        c = self.store.conn.cursor()
        for (key,) in c.execute(
                "SELECT DISTINCT cycle_key FROM edge_samples"):
            row = {}
            for label, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
                v = self.store._pctile("gross_bps", key, q)
                if v is not None:
                    row[label] = round(v, 2)
            mx, n = c.execute(
                "SELECT MAX(gross_bps), COUNT(*) FROM edge_samples "
                "WHERE cycle_key=?", (key,)).fetchone()
            row["max"] = round(mx, 2) if mx is not None else None
            row["samples"] = n
            out[key] = row
        return {"gross_bps_by_route": out}

    def _row_rejected(self, route, r) -> dict:
        """A candidate that could not be quoted this tick, still as a row.

        Every number is null, because none was measured -- not zero, which
        would read as "measured and found to be nothing". `reason` carries
        the engine's rejection verbatim so the table can say WHY instead of
        just dropping the asset.
        """
        return FeedRow(
            asset=route.base, quote=route.quote,
            gap_bps=None, net_bps=None, good_for_usd=None,
            buy_at=None, buy_venue=route.buy_on, ask_size_usd=None,
            sell_at=None, sell_venue=route.sell_on, bid_size_usd=None,
            venues=f"{route.buy_on}->{route.sell_on}",
            size_usd=float(self.size),
            quoted_iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
            quoted_age_ms=0.0,
            depth_basis={}, assumptions=[], warnings=[],
            ok=False, reason=(r.rejected or "unknown")[:160],
        ).as_dict()

    def _row(self, eng: Engine, route, r, e) -> dict:
        """One table row per candidate. See feedrow.py for the column notes."""
        buy_v = eng.by_name(route.buy_on)
        sell_v = eng.by_name(route.sell_on)
        l1 = r.legs[0] if getattr(r, "legs", None) else None
        l2 = r.legs[1] if getattr(r, "legs", None) and len(r.legs) > 1 else None

        # leg_price handles both leg types; a BookLeg has a touch and no
        # marginal_out_per_in, which is what emptied this column before.
        buy_at = leg_price(l1) if l1 is not None else None
        sell_at = leg_price(l2) if l2 is not None else None

        basis: dict = {}
        ask_sz = bid_sz = None
        if l1 is not None:
            ask_sz, basis["ask"] = leg_depth(l1, D(1), self.size)
        if l2 is not None and sell_at is not None and sell_at > 0:
            bid_sz, basis["bid"] = leg_depth(l2, sell_at, self.size)

        # Capacity only when there is something to size. Searching to confirm
        # a negative costs quotes and tells you nothing you do not know -- so
        # a non-positive net is a genuine, searched zero.
        good_for: D | None = D(0)
        if e.net_edge_bps > 0 and r.legs:
            try:
                good_for = solve_capacity(
                    r.legs, lo=self.size, hi=self.size * D(50),
                    fixed_cost_usd=(buy_v.fixed_cost_usd() if buy_v else D(0))
                    + (sell_v.fixed_cost_usd() if sell_v else D(0)),
                    start_asset_usd_price=D(1))
            except Exception as ex:  # noqa: BLE001
                # UNKNOWN, not zero. The solver re-quotes at trial sizes and a
                # leg that cannot answer one raises; reporting that as 0 says
                # "no capacity" about the only route that had any.
                good_for = None
                self.capacity_errors += 1
                if self.capacity_errors <= 2:
                    print(f"  capacity search failed for {route.key[:40]} "
                          f"({type(ex).__name__}: {str(ex)[:60]}); "
                          "good_for_usd will be null")

        ages = [v.age_ms() for v in (buy_v, sell_v) if v is not None]
        age = max(ages) if ages else 0.0
        warn = []
        if e.slippage_bps > D("0.01"):
            warn.append("positive slippage: fee/slip split unreliable, net is sound")

        f = lambda x: float(x) if x is not None else None
        return FeedRow(
            asset=route.base, quote=route.quote,
            gap_bps=f(e.gross_edge_bps), net_bps=f(e.net_edge_bps),
            good_for_usd=f(good_for),
            buy_at=f(buy_at), buy_venue=route.buy_on, ask_size_usd=f(ask_sz),
            sell_at=f(sell_at), sell_venue=route.sell_on, bid_size_usd=f(bid_sz),
            venues=f"{route.buy_on}->{route.sell_on}",
            size_usd=float(self.size),
            quoted_iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
            quoted_age_ms=round(age, 1),
            depth_basis=basis, assumptions=list(r.assumptions),
            warnings=warn, ok=True,
        ).as_dict()

    async def run(self, seconds: float) -> None:
        t0 = time.time()
        last = t0
        while not self.stop and time.time() - t0 < seconds:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 -- one bad cycle is not fatal
                print(f"  cycle error: {type(e).__name__}: {str(e)[:90]}")
            now = time.time()
            if self.interval_s:
                await asyncio.sleep(self.interval_s)
            if now - last >= 20.0:
                best = max(self.best_gross.values()) if self.best_gross else D(0)
                print(f"  {time.strftime('%H:%M:%S')}  {self.cycles} cycles  "
                      f"{self.samples} samples  "
                      f"({self.samples / (now - t0):.1f}/s)  "
                      f"best gross {best:+.2f}bps  open {len(self.open_opps)}")
                last = now
        end = time.time()
        # Flush, so a run ending mid-excursion does not drop it.
        for opp in self.open_opps.values():
            self.store.record_opportunity(opp, end)
        self.open_opps.clear()
        self.store.conn.commit()
        el = end - t0
        print(f"\n{self.cycles} cycles, {self.samples} samples in {el:.0f}s "
              f"({self.samples / el if el else 0:.1f}/s)")
        if self.cycles:
            print(f"a gap shorter than ~{1000 * el / self.cycles:.0f}ms can "
                  "pass entirely between cycles")


def lifetime_report(store: Store) -> str:
    c = store.conn.cursor()
    n, = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()
    if not n:
        return ("no gross excursions recorded -- the gap never went positive "
                "in this window, which is itself the answer for this window")
    out = [f"\nexcursions above 0 gross: {n}"]
    for key, cnt, avg_life, max_life, peak in c.execute(
        """SELECT cycle_key, COUNT(*), AVG(lifetime_ms), MAX(lifetime_ms),
                  MAX(peak_edge_bps)
           FROM opportunities GROUP BY cycle_key ORDER BY COUNT(*) DESC"""
    ):
        out.append(f"  {key}")
        out.append(f"    {cnt} excursions  mean life {avg_life:.0f}ms  "
                   f"max life {max_life:.0f}ms  peak gross {peak:+.2f}bps")
    # The number that decides it: how many survived a round trip.
    for label, ms in (("60ms (measured CEX round trip)", 60),
                      ("1s", 1000), ("30s (transfer cycle)", 30000)):
        survived, = c.execute(
            "SELECT COUNT(*) FROM opportunities WHERE lifetime_ms >= ?",
            (ms,)).fetchone()
        out.append(f"  lasted >= {label}: {survived}/{n}")
    return "\n".join(out)


async def main() -> int:
    opts = parse(sys.argv[1:])
    size = D(opts.get("size", "1000"))
    seconds = float(opts.get("seconds", "180"))

    # --pairs takes a list; --pair stays as it was; --candidates runs the
    # measured top N from candidates.py, which is the normal way in.
    if opts.get("candidates"):
        n = opts["candidates"]
        pairs = candidate_pairs(None if n in ("1", "all") else int(n))
    elif opts.get("pairs"):
        pairs = [p.strip() for p in opts["pairs"].split(",") if p.strip()]
    else:
        pairs = [opts.get("pair", "SUI-USDC")]

    # Tier 0 means "choose by cost at size", which is how every candidate in
    # candidates.py was measured. Inheriting sweep's fixed 500 default here
    # silently drops half of them: AAVE, SOL, MORPHO and SPX have no 500 pool
    # at all, so they refused themselves at a tier they were never screened
    # at and vanished from the run as "no usable v3 pool".
    opts.setdefault("tiers", "0")

    engines: list[Engine] = []
    for pair in pairs:
        base, _, quote = pair.partition("-")
        engine = Engine(venues=build_venues(base, quote, opts),
                        base=base, quote=quote)
        ok = []
        for v in engine.venues:
            try:
                await v.connect()
                ok.append(v)
                print(f"  [ok ] {v.name}")
            except Exception as e:  # noqa: BLE001
                print(f"  [REFUSED] {v.name}: {type(e).__name__}: {str(e)[:110]}")
        engine.venues = ok
        # One pair failing must not take the run down with it. With ten
        # candidates up, a single delisted product or drained pool is normal.
        if len(ok) < 2:
            print(f"  [SKIP] {pair}: need two venues to compare a route")
            await engine.aclose()
            continue
        engines.append(engine)

    if not engines:
        print("no pair had two usable venues")
        return 2

    async def close_all() -> None:
        for e in engines:
            await e.aclose()

    # Prefer the environment. A secret passed as --secret is visible in shell
    # history and in `ps` output to every user on the box; an env var is not.
    sink = notify.build(status=opts.get("status"), jsonl=opts.get("jsonl"),
                        webhook=opts.get("webhook"),
                        secret=(os.environ.get("SHADOW_WEBHOOK_SECRET")
                                or opts.get("secret")))
    for sk in sink.sinks:
        if isinstance(sk, notify.Webhook):
            # Prove the endpoint BEFORE spending a run on it. A webhook that
            # was never reachable looks exactly like a quiet market.
            ok, detail = await sk.preflight()
            print(f"  webhook preflight: {'OK' if ok else 'FAILED'}  {detail}")
            if not ok and opts.get("require-webhook", "0") not in ("0", "false"):
                print("  --require-webhook set; refusing to start")
                await close_all()
                return 3
            if not ok:
                print("  continuing anyway: samples still land in the db and "
                      "jsonl, but the frontend will receive nothing")
            sk.start()
    store = Store(opts.get("db", "monitor.db"))
    mon = Monitor(engines=engines, store=store, size=size,
                  sink=sink if sink.sinks else None,
                  interval_s=float(opts.get("interval", "1.0")),
                  heartbeat_s=float(opts.get("heartbeat", "5")),
                  summary_s=float(opts.get("summary", "60")),
                  feed_s=float(opts.get("feed", "15")))

    def handle(*_: object) -> None:
        mon.stop = True
        print("\n  stopping; flushing open excursions...")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, handle)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, handle)

    live = ", ".join(f"{e.base}/{e.quote}" for e in engines)
    print(f"\nmonitoring {len(engines)} pair(s) at ${size:,.0f} for "
          f"{seconds:.0f}s\n  {live}\n")
    try:
        await mon.run(seconds)
    finally:
        await close_all()
        if mon.sink is not None:
            st = mon.sink.stats()
            await mon.sink.aclose()
            if st:
                print(f"notify: {st}")
        print()
        print(store.distribution())
        print(lifetime_report(store))
        print("\nbreak-even to beat, by inventory mode:")
        for m in (PRE_POSITIONED, TRANSFER_CYCLE):
            print(f"  {m.name:15s} overhead {m.total_overhead_bps():>5.1f}bps "
                  f"on top of the route's own fees, and the gap must last "
                  f">= {m.min_gap_duration_s}s")
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
