"""Cost ETH/USDC across every registered venue.

The venue list below is the ONLY thing that changes when a venue is added.
The engine enumerates routes from whatever it is handed.

Usage:
  PYTHONPATH=. python sweep.py
  PYTHONPATH=. python sweep.py --sizes=1000,10000 --tiers=100,500 --norest
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal as D

from core.engine import Engine
from venues.cex import CoinbaseVenue
from venues.univ3 import DEPLOYMENTS, UniV3Venue


def parse(argv: list[str]) -> dict[str, str]:
    return {a[2:].split("=", 1)[0]: (a[2:].split("=", 1)[1] if "=" in a else "1")
            for a in argv if a.startswith("--")}


async def main() -> int:
    opts = parse(sys.argv[1:])
    sizes = tuple(D(x) for x in opts.get("sizes", "1000,10000").split(","))
    tiers = tuple(int(x) for x in opts.get("tiers", "500").split(","))
    chains = tuple(opts.get("chains", ",".join(sorted(DEPLOYMENTS))).split(","))

    venues: list = [CoinbaseVenue(product="ETH-USDC", use_ws=True)]
    for chain in chains:
        for tier in tiers:
            venues.append(UniV3Venue(chain=chain, tier=tier))

    engine = Engine(venues=venues, base="WETH", quote="USDC")

    print("connecting and validating every venue...")
    ok: list = []
    for v in engine.venues:
        try:
            await v.connect()
            ok.append(v)
            print(f"  [ok ] {v.name}")
        except Exception as e:  # noqa: BLE001
            # A venue that cannot prove itself is dropped, loudly. It is never
            # quoted on a guess.
            print(f"  [REFUSED] {v.name}: {type(e).__name__}: {str(e)[:110]}")
    engine.venues = ok
    if len(ok) < 2:
        print("\nfewer than two usable venues; nothing to compare")
        await engine.aclose()
        return 2

    for v in ok:
        sub = getattr(v, "substituted", None)
        if sub:
            print(f"\nWARNING {v.name} serves {sub} -- the quote currency "
                  "differs from what was requested.")

    await engine.refresh()
    await asyncio.sleep(1.0)
    await engine.refresh()
    results = await engine.sweep(sizes)

    print(f"\n{len(engine.routes())} routes x {len(sizes)} sizes "
          f"= {len(results)} evaluations\n")
    cross = [r for r in results if r.route.cross_venue_kind == "cross"]
    same = [r for r in results if r.route.cross_venue_kind == "same"]

    print("CROSS-VENUE routes (need inventory on both sides; cross-chain also a bridge)")
    for r in sorted(cross, key=lambda r: -(r.edge.net_edge_bps if r.ok else D(-99999))):
        print(r.line())
    print("\nSAME-VENUE round trips (control: should be negative by spread + fees)")
    for r in sorted(same, key=lambda r: -(r.edge.net_edge_bps if r.ok else D(-99999))):
        print(r.line())

    best = max((r for r in results if r.ok),
               key=lambda r: r.edge.net_edge_bps, default=None)
    print()
    if best is not None:
        e = best.edge
        print(f"best: {best.route.key} ${best.size_usd:,.0f} "
              f"net {e.net_edge_bps:+.2f}bps -> "
              f"{'OPEN' if e.net_edge_bps > 0 else 'CLOSED'}")
    print(f"\n{engine.status()}")
    await engine.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
