"""Cost one asset pair across every venue that can quote it.

The venue list is the only thing that changes when a venue is added; the
engine enumerates routes from whatever it is handed. Venues that cannot prove
themselves at connect() are dropped loudly and never quoted on a guess.

Which venues can serve which pair is a property of the venues, not of this
script: a venue that does not hold the pair simply does not report it in
assets(), and Engine.routes() leaves it out. That is why Robinhood is absent
from any USDC pair (its stablecoin is USDG, not USDC) and why the Sui venue
is absent from ETH pairs (Cetus's depth there is SUI/USDC).

Usage:
  PYTHONPATH=. python sweep.py                      # ETH/USDC
  PYTHONPATH=. python sweep.py --pair=SUI-USDC
  PYTHONPATH=. python sweep.py --pair=SUI-USDC --sizes=500,2000 --tiers=500
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal as D

from core.engine import Engine
from venues.cex import CoinbaseVenue
from venues.univ3 import DEPLOYMENTS, UniV3Venue

# Which on-chain venues are worth even trying for a given base asset. Keeping
# this explicit avoids connecting to chains that provably do not hold the
# pair -- see venues/DISCOVERY.md for what was checked and how.
CHAIN_VENUES_BY_BASE = {
    "WETH": "univ3",
    "SUI": "sui",
}


def parse(argv: list[str]) -> dict[str, str]:
    return {a[2:].split("=", 1)[0]: (a[2:].split("=", 1)[1] if "=" in a else "1")
            for a in argv if a.startswith("--")}


def build_venues(base: str, quote: str, opts: dict[str, str]) -> list:
    """Assemble the venues that could hold this pair."""
    # Coinbase names the volatile asset without the wrapper prefix.
    cb_base = base[1:] if base.startswith("W") and base != "WLD" else base
    venues: list = [CoinbaseVenue(product=f"{cb_base}-{quote}", use_ws=True)]

    kind = CHAIN_VENUES_BY_BASE.get(base)
    if kind == "univ3":
        tiers = tuple(int(x) for x in opts.get("tiers", "500").split(","))
        chains = tuple(opts.get("chains", ",".join(sorted(DEPLOYMENTS))).split(","))
        for chain in chains:
            for tier in tiers:
                venues.append(UniV3Venue(chain=chain, tier=tier))
    elif kind == "sui":
        from venues.sui_flowx import SuiFlowXVenue
        venues.append(SuiFlowXVenue())
    return venues


async def main() -> int:
    opts = parse(sys.argv[1:])
    pair = opts.get("pair", "WETH-USDC")
    base, _, quote = pair.partition("-")
    sizes = tuple(D(x) for x in opts.get("sizes", "1000,10000").split(","))

    venues = build_venues(base, quote, opts)
    engine = Engine(venues=venues, base=base, quote=quote)

    print(f"pair {base}/{quote}   connecting and validating {len(venues)} venues...")
    ok: list = []
    for v in engine.venues:
        try:
            await v.connect()
            ok.append(v)
            print(f"  [ok ] {v.name}")
        except Exception as e:  # noqa: BLE001
            print(f"  [REFUSED] {v.name}: {type(e).__name__}: {str(e)[:130]}")
    engine.venues = ok

    for v in ok:
        sub = getattr(v, "substituted", None)
        if sub:
            print(f"\nWARNING {v.name} serves {sub} -- the quote currency "
                  "differs from what was requested.")

    if len(ok) < 2:
        print(f"\nonly {len(ok)} usable venue; need two to compare a route")
        await engine.aclose()
        return 2

    await engine.refresh()
    await asyncio.sleep(1.0)      # let a streamed book accumulate updates
    await engine.refresh()
    results = await engine.sweep(sizes)

    print(f"\n{len(engine.routes())} routes x {len(sizes)} sizes "
          f"= {len(results)} evaluations\n")
    cross = [r for r in results if r.route.cross_venue_kind == "cross"]
    same = [r for r in results if r.route.cross_venue_kind == "same"]
    worst = D(-99999)

    print("CROSS-VENUE (needs inventory on both sides; cross-chain also a bridge)")
    for r in sorted(cross, key=lambda r: -(r.edge.net_edge_bps if r.ok else worst)):
        print(r.line())
    print("\nSAME-VENUE controls (should lose exactly spread + fees)")
    for r in sorted(same, key=lambda r: -(r.edge.net_edge_bps if r.ok else worst)):
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
