"""Rank every registered asset by MEASURED cross-venue edge, then pick the top N.

Why this exists: "the most profitable pairs" is not a thing you can recall or
reason your way to. It is a measurement, and the two obvious proxies for it
are both wrong in ways that cost real money:

  VOLUME is not edge. The deepest, most-traded pair is the one most heavily
  arbitraged already, so its dislocation is the smallest. BTC and ETH top the
  volume table and are nearly always the worst gross gap on it.

  A SNAPSHOT is not a distribution. One tick of +12bps tells you nothing about
  whether that gap is reachable, because the question is not "how wide does it
  get" but "how wide is it, for how long, repeatedly". So every asset is
  sampled over several passes and ranked on its MEDIAN, with the tail reported
  alongside. A single wide reading is a sample, never a signal.

Costs come from netedge.evaluate through the real Engine, not from arithmetic
repeated here. A screening script that reimplements the cost model ranks on a
different cost model than the one that will later disagree with it.

The ranking metric is NET edge at size, best of the two directions, because
that is the number that decides whether a route is worth anything. Gross is
reported beside it: gross is the market, net is what is left for you, and the
gap between them is almost entirely the Coinbase taker fee plus the v3 tier.

Usage:
  PYTHONPATH=. python screen.py --passes=3 --size=1000 --top=10
  PYTHONPATH=. python screen.py --only=AERO,VVV,LINK --passes=1
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from decimal import Decimal as D

from core.engine import Engine
from sweep import build_venues, parse
from venues.univ3 import DEPLOYMENTS

QUOTE = "USDC"

# Assets that cannot form a cross-venue route and would only add noise: the
# quote itself, and the two stablecoins whose "edge" is a peg basis rather
# than a dislocation.
SKIP = {"USDC", "USDT", "EURC"}


async def measure(base: str, size: D, opts: dict) -> dict | None:
    """One pass over one asset: connect, cost both directions, report the best."""
    # REST, not the streaming feed: see build_venues. A one-shot pass has no
    # time to wait for a level2 snapshot, and 42 websockets to screen 42
    # assets is a lot of machinery to end up with a staler book.
    venues = build_venues(base, QUOTE, {**opts, "tiers": "0", "ws": "0"})
    engine = Engine(venues=venues, base=base, quote=QUOTE)
    live = []
    refused = []
    for v in venues:
        try:
            await v.connect()
            live.append(v)
        except Exception as e:  # noqa: BLE001 -- a venue refusing itself is data
            refused.append(f"{v.name}: {type(e).__name__}: {str(e)[:70]}")
    engine.venues = live
    if len(live) < 2:
        await engine.aclose()
        return {"base": base, "ok": False, "why": "; ".join(refused) or "one venue"}

    await engine.refresh()
    out = []
    for route in engine.routes():
        if route.cross_venue_kind != "cross":
            continue
        r = await engine.evaluate_route(route, size)
        if r.ok and r.edge is not None:
            out.append((r.edge.net_edge_bps, r.edge.gross_edge_bps,
                        r.edge.fee_bps, r.edge.slippage_bps,
                        r.edge.fixed_cost_bps, route.key))
    tier = next((getattr(v, "tier", 0) for v in live
                 if hasattr(v, "tier")), 0)
    await engine.aclose()
    if not out:
        return {"base": base, "ok": False, "why": "no route could be quoted"}
    out.sort(reverse=True)
    net, gross, fee, slip, gas, key = out[0]
    return {"base": base, "ok": True, "net": float(net), "gross": float(gross),
            "fee": float(fee), "slip": float(slip), "gas": float(gas),
            "tier": tier, "route": key}


async def main() -> int:
    opts = parse(sys.argv[1:])
    size = D(opts.get("size", "1000"))
    passes = int(opts.get("passes", "3"))
    top_n = int(opts.get("top", "10"))

    if opts.get("only"):
        assets = [a.strip() for a in opts["only"].split(",")]
    else:
        assets = [k for k in DEPLOYMENTS["base"].tokens if k not in SKIP]

    print(f"screening {len(assets)} assets, {passes} pass(es) at ${size:,}\n")
    history: dict[str, list[dict]] = {a: [] for a in assets}
    dead: dict[str, str] = {}

    for p in range(passes):
        t0 = time.time()
        print(f"--- pass {p + 1}/{passes}")
        for a in assets:
            if a in dead:
                continue
            try:
                r = await measure(a, size, opts)
            except Exception as e:  # noqa: BLE001
                r = {"base": a, "ok": False, "why": f"{type(e).__name__}: {e}"}
            if r is None:
                continue
            if r["ok"]:
                history[a].append(r)
                print(f"  {a:<9} net {r['net']:+8.2f}  gross {r['gross']:+8.2f}"
                      f"  tier {r['tier']:<6} {r['route'][:52]}")
            else:
                # Record it once and stop paying for it. An asset that cannot
                # be quoted at all is a finding, not a retry candidate.
                dead[a] = r["why"]
                print(f"  {a:<9} -- {r['why'][:92]}")
        print(f"    pass took {time.time() - t0:.0f}s")

    ranked = []
    for a, rs in history.items():
        if not rs:
            continue
        nets = [r["net"] for r in rs]
        grosses = [r["gross"] for r in rs]
        ranked.append({
            "asset": a, "samples": len(rs),
            "net_median": statistics.median(nets), "net_best": max(nets),
            "gross_median": statistics.median(grosses), "gross_best": max(grosses),
            "fee": rs[-1]["fee"], "slip": rs[-1]["slip"], "gas": rs[-1]["gas"],
            "tier": rs[-1]["tier"], "route": rs[-1]["route"],
        })
    ranked.sort(key=lambda x: x["net_median"], reverse=True)

    print(f"\n{'=' * 104}\nRANKED by median NET edge at ${size:,} "
          f"({passes} passes, {len(ranked)} quotable of {len(assets)})\n{'=' * 104}")
    print(f"{'#':<4}{'asset':<9}{'net med':>9}{'net best':>10}"
          f"{'gross med':>11}{'gross best':>12}{'fee':>8}{'slip':>8}"
          f"{'gas':>7}{'tier':>7}{'n':>4}")
    for i, x in enumerate(ranked, 1):
        print(f"{i:<4}{x['asset']:<9}{x['net_median']:>9.2f}{x['net_best']:>10.2f}"
              f"{x['gross_median']:>11.2f}{x['gross_best']:>12.2f}"
              f"{x['fee']:>8.2f}{x['slip']:>8.2f}{x['gas']:>7.3f}"
              f"{x['tier']:>7}{x['samples']:>4}")

    print(f"\nTOP {top_n} by median net edge:")
    for i, x in enumerate(ranked[:top_n], 1):
        print(f"  {i:>2}. {x['asset']:<9} net {x['net_median']:+7.2f}bps  "
              f"gross {x['gross_median']:+7.2f}bps  tier {x['tier']}")

    if dead:
        print(f"\nnot quotable ({len(dead)}):")
        for a, why in sorted(dead.items()):
            print(f"  {a:<9} {why[:98]}")

    json.dump({"ranked": ranked, "dead": dead, "size_usd": float(size),
               "passes": passes, "ts": time.time()},
              open(opts.get("out", "screen_result.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
