"""One table row per candidate, for the `arbfeedall` event.

The columns asked for:

  Asset | Gap bps | Net bps | Good for | Buy at | Ask size | Sell at |
  Bid size | Venues | Quoted

Three of them need care, because the obvious reading is wrong.

GOOD FOR is capacity, not the size we quoted: the largest notional at which
net edge is still positive. When nothing is profitable it is 0, and that is
the honest answer rather than a blank. It is only searched when the probed
size is already positive, because solving it costs quotes and there is no
point paying for them to confirm a negative.

ASK SIZE and BID SIZE are order-book concepts, and one side of every route
here is a bonding curve with no size at a price. So both are reported on one
COMMON definition -- the USD notional tradeable within a stated slippage
budget -- and `depth_basis` says how each was obtained. An order book is
walked exactly. A curve is searched where its maths is local and therefore
free. An aggregator quote is pinned to the sizes already fetched, so it cannot
answer without more network calls, and the field is then null rather than
guessed. A fabricated depth number is worse than a missing one: it reads as a
measurement.

QUOTED is when the underlying data was captured, with its age, because a row
is only as good as the oldest leg in it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from legs import BookLeg

# Depth is "how much can I trade before slippage reaches this".
DEPTH_BUDGET_BPS = Decimal("10")

# Bounded search: enough to be informative, cheap enough to run every feed.
MAX_DEPTH_STEPS = 18


@dataclass
class FeedRow:
    asset: str
    quote: str
    gap_bps: float | None            # gross: the market, before our costs
    net_bps: float | None            # after fees, slippage and gas
    # Capacity: the largest size still net-positive. 0.0 means SEARCHED and
    # there is none, which is the common case. None means the search could not
    # run -- a different statement, and conflating the two reported "no
    # capacity" for the one route that had some.
    good_for_usd: float | None
    buy_at: float | None
    buy_venue: str
    ask_size_usd: float | None      # depth on the side we buy
    sell_at: float | None
    sell_venue: str
    bid_size_usd: float | None      # depth on the side we sell
    venues: str
    size_usd: float
    quoted_iso: str
    quoted_age_ms: float
    depth_basis: dict               # how each depth figure was obtained
    assumptions: list               # unproven equivalences this row leans on
    warnings: list                  # invariant violations; net still sound
    ok: bool
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def leg_price(leg) -> Decimal | None:
    """Price in QUOTE per BASE unit, whichever way the leg runs.

    An order-book leg and a curve leg expose price completely differently: a
    BookLeg has a touch (best ask when buying, best bid when selling) and no
    marginal_out_per_in at all, which is a QuotedLeg attribute. Reading the
    wrong one yields None rather than an error, so the column silently
    emptied instead of failing.

    A buy leg converts quote->base, so its rate is base per quote and has to
    be inverted to read like a price on a screen. A sell leg is already
    quote per base.
    """
    inner = getattr(leg, "inner", leg)
    if isinstance(inner, BookLeg):
        bk = inner.book
        return bk.best_ask if inner.is_buy else bk.best_bid
    m = getattr(inner, "marginal_out_per_in", None)
    if m is None or m <= 0:
        return None
    # asset_out being the quote side means the rate is already a price.
    out = getattr(inner, "asset_out", "")
    base = getattr(inner, "asset_in", "")
    # Heuristic kept explicit: a stable-looking output means quote per base.
    if out.upper() in ("USDC", "USDT", "USD", "DAI", "USDG"):
        return m
    if base.upper() in ("USDC", "USDT", "USD", "DAI", "USDG"):
        return Decimal(1) / m
    return m


def book_depth_usd(leg: BookLeg, budget_bps: Decimal = DEPTH_BUDGET_BPS
                   ) -> tuple[Decimal | None, str]:
    """Exact: walk the book until the VWAP has moved budget_bps from the touch.

    Returns USD notional, not base units, so the two sides of a route are
    comparable in the same unit as the size column.
    """
    book = leg.book
    levels = book.asks if leg.is_buy else book.bids
    if not levels:
        return None, "book: empty"
    touch = levels[0].price
    if touch <= 0:
        return None, "book: no touch"
    limit = (touch * (Decimal(1) + budget_bps / Decimal(10000)) if leg.is_buy
             else touch * (Decimal(1) - budget_bps / Decimal(10000)))
    notional = Decimal(0)
    for lvl in levels:
        if (leg.is_buy and lvl.price > limit) or \
           (not leg.is_buy and lvl.price < limit):
            break
        notional += lvl.price * lvl.size
    return notional, f"book walked to {budget_bps}bps"


def curve_depth_usd(leg, usd_per_in: Decimal, hint_usd: Decimal,
                    budget_bps: Decimal = DEPTH_BUDGET_BPS
                    ) -> tuple[Decimal | None, str]:
    """Search a curve for the size at which slippage reaches the budget.

    Only works where the leg can quote an arbitrary size. An aggregator leg
    pinned to pre-fetched sizes raises instead, and that is reported as
    unavailable rather than filled in with a guess.
    """
    if usd_per_in <= 0:
        return None, "curve: no price"

    def slip_bps(size_in: Decimal) -> Decimal | None:
        try:
            got = leg.full(size_in).amount_out
            base = leg.fee_only(size_in)
        except Exception:        # noqa: BLE001 -- pinned quoter, or no route
            return None
        if base <= 0:
            return None
        return (got / base - Decimal(1)) * Decimal(10000)

    probe = hint_usd / usd_per_in
    if slip_bps(probe) is None:
        return None, "curve: leg cannot quote arbitrary sizes"

    # Double until the budget is breached, then bisect.
    lo, hi = probe, probe
    for _ in range(MAX_DEPTH_STEPS // 2):
        s = slip_bps(hi)
        if s is None:
            break
        if -s > budget_bps:
            break
        lo, hi = hi, hi * 2
    else:
        return lo * usd_per_in, f"curve: >= {lo * usd_per_in:.0f} at {budget_bps}bps"
    for _ in range(MAX_DEPTH_STEPS // 2):
        mid = (lo + hi) / 2
        s = slip_bps(mid)
        if s is None:
            break
        if -s > budget_bps:
            hi = mid
        else:
            lo = mid
    return lo * usd_per_in, f"curve bisected to {budget_bps}bps"


def leg_depth(leg, usd_per_in: Decimal, hint_usd: Decimal
              ) -> tuple[Decimal | None, str]:
    """Depth on one common definition, however the venue prices."""
    if isinstance(leg, BookLeg):
        return book_depth_usd(leg)
    inner = getattr(leg, "inner", leg)        # engine wraps legs for renaming
    if isinstance(inner, BookLeg):
        return book_depth_usd(inner)
    return curve_depth_usd(leg, usd_per_in, hint_usd)
