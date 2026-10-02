"""The measured candidate set: which assets to monitor, and the evidence why.

This file is a RESULT, not a configuration. Every asset here survived a
five-stage screen (see `screen.py` for the ranking pass and
`venues/DISCOVERY.md` for the census), and the numbers recorded beside each
one are what was actually measured, on the date stated. They are kept so that
the list can be argued with: a candidate whose edge has decayed should be
visible as a disagreement between this file and a fresh `screen.py` run,
rather than silently persisting because it was once a good idea.

HOW THE TEN WERE CHOSEN
  1. Coinbase census -> 404 tradable USDC spot pairs.
  2. Base v3 census -> factory PoolCreated logs filtered on USDC gave 10,081
     pools; liquidity() left 1,920 with anything in them at all.
  3. symbol()/decimals() on each counterparty token, intersected with (1).
  4. PRICE CROSS-CHECK against Coinbase, which rejected 37 of 75 symbol
     matches as different tokens wearing a familiar ticker.
  5. Cost every survivor through the real Engine at $1,000, four passes, and
     rank on MEDIAN net edge.

Stage 5 is why volume is absent from the ranking. Volume is not edge -- it is
closer to the opposite, since the most-traded pair is the most arbitraged and
therefore the tightest. BTC and ETH lead the volume table and ETH came second
here only because its costs are the lowest, not because its gap is the widest.

WHAT THE RANKING SAYS, PLAINLY: nine of these ten are net-NEGATIVE at $1,000.
They are the ten best of 42, not ten profitable trades. The tenth, XCN, is
net-positive and is the one to trust least -- see its note.

Costs assumed: Coinbase taker 10bps, which is the project's pinned figure and
NOT the entry Advanced Trade tier of ~60bps. At 60bps every line below moves
50bps worse and nothing here is positive. The v3 tier is per-asset and real.
"""
from __future__ import annotations

from dataclasses import dataclass

# When the screen below was run. Edge decays; a stale ranking is a liability.
MEASURED_ON = "2026-10-02"
MEASURED_SIZE_USD = 1000
MEASURED_PASSES = 4
COINBASE_TAKER_BPS_ASSUMED = 10


@dataclass(frozen=True)
class Candidate:
    """One asset worth monitoring, with the measurement that justifies it."""
    asset: str                  # DEPLOYMENTS key, and the Coinbase asset id
    net_median_bps: float       # median of 4 passes, best direction, $1,000
    gross_median_bps: float     # the market, before our costs
    tier: int                   # v3 fee tier chosen by cost at size
    ask_usd: float              # USD tradeable within 10bps, buy side
    bid_usd: float              # ... sell side
    note: str = ""
    quote: str = "USDC"
    chain: str = "base"

    @property
    def pair(self) -> str:
        return f"{self.asset}-{self.quote}"


# Ordered by measured median net edge, best first.
#
# A depth figure equal to the $1,000 probe is a FLOOR, not a measurement: the
# search starts there and reports the probe size when the slippage budget is
# already breached at it. Read "1000" as "at most 1000".
TOP: tuple[Candidate, ...] = (
    Candidate("XCN", 136.90, 318.05, 3000, 1000, 1460, note=(
        "THE ONLY NET-POSITIVE LINE, AND THE LEAST BELIEVABLE. A ~3.2% gross "
        "gap that held across all four passes on a $1.2M/day asset is not a "
        "dislocation anybody would leave lying there, so the likely readings "
        "are (a) the Base pool's mid is stale and unexecutable at any size, "
        "or (b) the token is not Coinbase's XCN -- it passed the price check "
        "at ratio 0.9524, the widest of anything accepted. Against (b): the "
        "token is 810 days old with 5 live pools, so it is not a fresh "
        "impostor, which leaves the thin-and-stale reading as the likely one. "
        "Capacity was solved at ~$1,580, so +137bps is about $20 a round "
        "trip. Verify the token identity independently before believing it, "
        "and note that a gap this wide persisting for days is itself the "
        "evidence that nobody can take it.")),
    Candidate("WETH", -10.69, 11.86, 500, 2828, 195750, note=(
        "The cheapest costs in the set (5bps tier, ~0bps slippage at size) "
        "and the only one with real depth on both sides. Second place is "
        "earned on cost, not on gap. Its best route is often Base<->Arbitrum "
        "rather than Coinbase<->Base, which needs inventory on both chains.")),
    Candidate("VIRTUAL", -28.08, 18.63, 3000, 5297, 1187),
    Candidate("AERO", -30.32, 4.15, 500, 1000, 3223, note=(
        "Cheap tier, but the thinnest gross gap here -- Base's native DEX "
        "token is priced on Base first, so there is little for Coinbase to "
        "disagree with.")),
    Candidate("AAVE", -35.94, 53.85, 3000, 1000, 28081, note=(
        "Wide gap, but -50bps of slippage eats it: the gap is wide BECAUSE "
        "the Base pool is thin.")),
    Candidate("LINK", -37.99, 21.47, 3000, 1000, 43902),
    Candidate("VVV", -42.34, 4.42, 3000, 3881, 2355),
    Candidate("SOL", -52.60, 2.69, 3000, 273029, 1000),
    Candidate("MORPHO", -56.04, 65.36, 10000, 1334, 4331, note=(
        "Second-widest gap in the set, but only the 100bps tier can quote it, "
        "which costs more than the gap is worth.")),
    Candidate("SPX", -80.21, 82.27, 10000, 1000, 2465, note=(
        "The widest gap of any asset with usable depth, and still negative: "
        "100bps tier plus -52bps slippage. A clean illustration that gross "
        "edge is not edge.")),
)

# Screened, quotable, and NOT selected -- kept so the cut is reviewable.
# Each was costed at $1,000 and came out worse than every line above.
ALSO_SCREENED: dict[str, float] = {
    "UNI": -85.80, "BASECAT": -110.88, "EDGE": -158.28, "TOSHI": -265.45,
}

# Registered and price-confirmed, but the pool cannot absorb $1,000: every one
# returned worse than -400bps net, which is a depth verdict rather than a
# market one. Re-screen them at $100 before dismissing them entirely.
TOO_THIN_AT_1K: tuple[str, ...] = (
    "ZRO", "SUP", "CHECK", "PROS", "KAT", "CBETH", "ZEN", "PENDLE", "OPG",
    "KAITO", "CRV", "KEYCAT", "KTA", "DEGEN", "RSR", "PRO", "RNBW", "TAO",
    "COMP", "COOKIE", "DRV", "MOG", "FAI", "LMTS", "1INCH", "B3", "SUSHI",
)


def candidate_pairs(n: int | None = None) -> list[str]:
    """The top n candidate pairs, for --candidates=N."""
    picks = TOP if n is None else TOP[:n]
    return [c.pair for c in picks]


def by_asset(asset: str) -> Candidate | None:
    return next((c for c in TOP if c.asset == asset.upper()), None)


def report() -> str:
    out = [f"measured {MEASURED_ON} at ${MEASURED_SIZE_USD:,} over "
           f"{MEASURED_PASSES} passes, Coinbase taker assumed "
           f"{COINBASE_TAKER_BPS_ASSUMED}bps",
           f"{'#':<4}{'asset':<9}{'net med':>9}{'gross med':>11}"
           f"{'tier':>7}{'ask $':>11}{'bid $':>11}"]
    for i, c in enumerate(TOP, 1):
        out.append(f"{i:<4}{c.asset:<9}{c.net_median_bps:>9.2f}"
                   f"{c.gross_median_bps:>11.2f}{c.tier:>7}"
                   f"{c.ask_usd:>11,.0f}{c.bid_usd:>11,.0f}")
    pos = [c.asset for c in TOP if c.net_median_bps > 0]
    out.append(f"\nnet-positive at ${MEASURED_SIZE_USD:,}: "
               + (", ".join(pos) if pos else "none"))
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
