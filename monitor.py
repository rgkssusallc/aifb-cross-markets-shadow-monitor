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
import signal
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal as D

from config import PRE_POSITIONED, TRANSFER_CYCLE
from core.engine import Engine
from storage import OpenOpportunity, Store
from sweep import build_venues, parse


@dataclass
class Monitor:
    engine: Engine
    store: Store
    size: D
    # An excursion is tracked once GROSS clears this, not net: the gross
    # series is the market, and whether it clears YOUR costs depends on
    # inventory mode, which is a separate question answered at the end.
    open_above_bps: D = D("0")
    open_opps: dict[str, OpenOpportunity] = field(default_factory=dict)
    cycles: int = 0
    samples: int = 0
    uncommitted: int = 0
    best_gross: dict[str, D] = field(default_factory=dict)
    stop: bool = False

    async def tick(self) -> None:
        # ALL venues, not just the ageing ones. refresh_volatile() skips
        # block-atomic venues because their quotes do not rot between
        # refreshes -- correct inside one sweep, wrong in a loop: their
        # state_id then never advances, LegCache serves the same leg forever,
        # and the run produces a confident flat distribution of one stale
        # quote. The cheap per-venue state check (eth_blockNumber, checkpoint,
        # object version) is exactly what makes refreshing all of them
        # affordable, and the cache still prevents re-quoting an unchanged one.
        await self.engine.refresh()
        now = time.time()
        for route in self.engine.routes():
            if route.cross_venue_kind != "cross":
                continue           # controls are verified in sweep, not logged
            r = await self.engine.evaluate_route(route, self.size)
            key = route.key
            if not r.ok:
                self.store.record_rejection(key, r.rejected or "unknown")
                continue
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
                else:
                    opp.observe(e.gross_edge_bps, breakdown, 0.0,
                                e.exhausted, None)
            elif key in self.open_opps:
                self.store.record_opportunity(self.open_opps.pop(key), now)

        if self.uncommitted >= 200:
            self.store.conn.commit()
            self.uncommitted = 0
        self.cycles += 1

    async def run(self, seconds: float) -> None:
        t0 = time.time()
        last = t0
        while not self.stop and time.time() - t0 < seconds:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 -- one bad cycle is not fatal
                print(f"  cycle error: {type(e).__name__}: {str(e)[:90]}")
            now = time.time()
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
    pair = opts.get("pair", "SUI-USDC")
    base, _, quote = pair.partition("-")
    size = D(opts.get("size", "1000"))
    seconds = float(opts.get("seconds", "180"))

    venues = build_venues(base, quote, opts)
    engine = Engine(venues=venues, base=base, quote=quote)
    ok = []
    for v in engine.venues:
        try:
            await v.connect()
            ok.append(v)
            print(f"  [ok ] {v.name}")
        except Exception as e:  # noqa: BLE001
            print(f"  [REFUSED] {v.name}: {type(e).__name__}: {str(e)[:110]}")
    engine.venues = ok
    if len(ok) < 2:
        print("need two venues to compare a route")
        await engine.aclose()
        return 2

    store = Store(opts.get("db", "monitor.db"))
    mon = Monitor(engine=engine, store=store, size=size)

    def handle(*_: object) -> None:
        mon.stop = True
        print("\n  stopping; flushing open excursions...")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, handle)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, handle)

    print(f"\nmonitoring {base}/{quote} at ${size:,.0f} for {seconds:.0f}s\n")
    try:
        await mon.run(seconds)
    finally:
        await engine.aclose()
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
