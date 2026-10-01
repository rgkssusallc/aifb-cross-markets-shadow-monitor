"""Fill simulation: what you would ACTUALLY have got, not what you saw.

A shadow log says "at t there was 14bps of edge". That is a statement about
the past, and it is not the number that matters. The number that matters is
what you would have filled at, given that your order does not reach the venue
until t + latency, by which time the book has moved -- usually against you,
because the same information that created the edge is what moved it.

That gap is adverse selection, and it is the difference between a backtest
and a fantasy. The rule this module exists to enforce:

    TRIGGER on the book at t. FILL against the book at t + measured_latency.

Two further sources of honesty, both of which flatter you if ignored:

  Sequential legs compound delay. In a non-atomic path (CEX leg, then DEX leg)
  leg 2 cannot be sent until leg 1 comes back, so leg k fills against the book
  at t + sum(latency of legs 1..k). And it fills with whatever leg k-1 actually
  produced, not with the amount the plan assumed. A bad first fill shrinks
  every leg after it.

  Missing data is not a free pass. If the tape has no snapshot at or after a
  leg's fill time, we do NOT know what the book looked like and the attempt is
  reported unverifiable rather than filled. Quietly reusing the triggering
  book here is the single mistake that makes homemade crypto backtests
  worthless, and it is precisely the case that looks like a profit.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from legs import Book, BookLeg
from netedge import BPS, evaluate

SEQUENTIAL = "sequential"
ATOMIC = "atomic"


@dataclass(frozen=True)
class BookTape:
    """Time-ordered L2 snapshots for one product, queried point-in-time.

    as_of() never looks forward past the requested instant, which is the only
    way to stop a replay from using information the strategy could not have had.
    """
    product_id: str
    snapshots: tuple[Book, ...]

    def __post_init__(self) -> None:
        stamps = [s.ts_local for s in self.snapshots]
        if stamps != sorted(stamps):
            raise ValueError(
                f"{self.product_id}: snapshots must be ascending by ts_local"
            )
        object.__setattr__(self, "_stamps", stamps)

    def as_of(self, t: float) -> Book | None:
        """Most recent snapshot at or before t. None if the tape starts later."""
        stamps: list[float] = object.__getattribute__(self, "_stamps")
        i = bisect.bisect_right(stamps, t)
        return self.snapshots[i - 1] if i else None

    @property
    def last_ts(self) -> float | None:
        return self.snapshots[-1].ts_local if self.snapshots else None

    def covers(self, t: float) -> bool:
        """True if the tape observed the book at or after t.

        Without this the replay silently extrapolates the last known book
        forward, which manufactures fills that never could have happened.
        """
        last = self.last_ts
        return last is not None and last >= t


@dataclass(frozen=True)
class LegSpec:
    """How to rebuild a leg from a tape at an arbitrary instant.

    A concrete Leg is pinned to one snapshot, so it cannot be re-priced at a
    later time. The spec keeps the *recipe* -- product, direction, fee, and the
    venue's own measured latency -- so the same path can be built at the
    trigger instant and again at each fill instant.
    """
    product_id: str
    asset_in: str
    fee_rate: Decimal
    venue: str
    latency_ms: float

    def build(self, book: Book) -> BookLeg:
        return BookLeg(
            book=book,
            asset_in=self.asset_in,
            fee_rate=self.fee_rate,
            venue=self.venue,
        )


@dataclass(frozen=True)
class LegFill:
    """What one leg did when it actually executed."""
    product_id: str
    venue: str
    asset_in: str
    asset_out: str
    t_fill: float
    book_ts: float          # snapshot the fill priced against
    book_age_ms: float      # how stale that snapshot was at fill time
    amount_in: Decimal
    amount_out: Decimal
    expected_out: Decimal   # what the triggering book promised for this amount_in
    exhausted: bool

    @property
    def slip_bps(self) -> Decimal:
        """Shortfall against the triggering book, on this leg alone."""
        if self.expected_out <= 0:
            return Decimal(0)
        return (self.amount_out / self.expected_out - Decimal(1)) * BPS


@dataclass(frozen=True)
class FillResult:
    filled: bool
    reason: str | None

    decision_edge_bps: Decimal   # what the trigger book advertised
    realized_edge_bps: Decimal   # what the latency-shifted books delivered
    start_amount: Decimal
    end_amount: Decimal
    fills: tuple[LegFill, ...]
    unverifiable: bool           # tape ran out; outcome genuinely unknown
    stranded_asset: str | None   # non-atomic path that stopped mid-way

    @property
    def adverse_selection_bps(self) -> Decimal:
        """How much of the advertised edge the latency window ate.

        Negative means the market moved against you between seeing the
        opportunity and reaching it. Over a log this is the single most
        informative statistic: if its mean swallows the mean decision edge,
        the strategy has no edge at your latency, whatever the log says.
        """
        return self.realized_edge_bps - self.decision_edge_bps

    def summary(self) -> str:
        if self.unverifiable:
            return f"UNVERIFIABLE  {self.reason}"
        if not self.filled:
            strand = f"  stranded in {self.stranded_asset}" if self.stranded_asset else ""
            return f"NO FILL  {self.reason}{strand}"
        return (
            f"decision {self.decision_edge_bps:+.2f}bps  "
            f"realized {self.realized_edge_bps:+.2f}bps  "
            f"adverse {self.adverse_selection_bps:+.2f}bps"
        )


def _fixed_cost_bps(
    fixed_cost_usd: Decimal, start_amount: Decimal, start_asset_usd_price: Decimal
) -> Decimal:
    notional = start_amount * start_asset_usd_price
    if notional <= 0:
        return Decimal(0)
    return -(fixed_cost_usd / notional) * BPS


def _fill_times(
    specs: Sequence[LegSpec],
    t_trigger: float,
    execution: str,
    atomic_latency_ms: float | None,
) -> list[float]:
    """When each leg actually executes.

    Atomic: one block, one instant, every leg at the same price snapshot --
    this is the on-chain flashloan case and it carries no leg risk.
    Sequential: delay compounds down the path, because you cannot send leg 2
    until leg 1 has come back.
    """
    if execution == ATOMIC:
        if atomic_latency_ms is None:
            raise ValueError("atomic execution requires atomic_latency_ms")
        t = t_trigger + atomic_latency_ms / 1000.0
        return [t] * len(specs)
    if execution != SEQUENTIAL:
        raise ValueError(f"unknown execution model: {execution}")
    out: list[float] = []
    cursor = t_trigger
    for spec in specs:
        cursor += spec.latency_ms / 1000.0
        out.append(cursor)
    return out


def simulate(
    specs: Sequence[LegSpec],
    tapes: Mapping[str, BookTape],
    t_trigger: float,
    start_amount: Decimal,
    *,
    execution: str = SEQUENTIAL,
    atomic_latency_ms: float | None = None,
    fixed_cost_usd: Decimal = Decimal(0),
    start_asset_usd_price: Decimal = Decimal(1),
    max_skew_ms: float | None = None,
) -> FillResult:
    """Trigger on the book at t_trigger, fill against the books at t + latency.

    Returns the advertised edge and the realized edge side by side. The spread
    between them is the cost of being slow, which no amount of spread-watching
    will reveal.
    """
    blank = dict(
        decision_edge_bps=Decimal(0), realized_edge_bps=Decimal(0),
        start_amount=start_amount, end_amount=Decimal(0), fills=(),
        unverifiable=False, stranded_asset=None,
    )

    # --- the decision: every leg priced off the book visible at t_trigger ---
    trigger_books: list[Book] = []
    for spec in specs:
        tape = tapes.get(spec.product_id)
        if tape is None:
            return FillResult(False, f"no tape for {spec.product_id}", **blank)
        book = tape.as_of(t_trigger)
        if book is None:
            return FillResult(
                False, f"{spec.product_id} has no snapshot at or before trigger", **blank
            )
        trigger_books.append(book)

    trigger_legs = [spec.build(b) for spec, b in zip(specs, trigger_books)]
    decision = evaluate(
        trigger_legs,
        start_amount,
        fixed_cost_usd=fixed_cost_usd,
        start_asset_usd_price=start_asset_usd_price,
        max_skew_ms=max_skew_ms,
    )
    if not decision.ok:
        return FillResult(False, f"decision rejected: {decision.reason}", **blank)

    # --- the fill: each leg against the book at its own execution time ------
    times = _fill_times(specs, t_trigger, execution, atomic_latency_ms)

    for spec, t_fill in zip(specs, times):
        if not tapes[spec.product_id].covers(t_fill):
            partial = dict(blank)
            partial["decision_edge_bps"] = decision.net_edge_bps
            partial["unverifiable"] = True
            return FillResult(
                False,
                f"tape for {spec.product_id} ends before its fill time "
                f"(+{(t_fill - t_trigger) * 1000:.0f}ms); outcome unknown",
                **partial,
            )

    amount = start_amount
    fills: list[LegFill] = []
    for spec, trig_book, t_fill in zip(specs, trigger_books, times):
        fill_book = tapes[spec.product_id].as_of(t_fill)
        assert fill_book is not None  # covers() checked above

        # What the triggering book promised for the amount we actually arrive
        # with -- so per-leg slip isolates price movement from size shrinkage
        # inherited from earlier legs.
        expected = spec.build(trig_book).full(amount).amount_out
        quote = spec.build(fill_book).full(amount)

        fills.append(LegFill(
            product_id=spec.product_id,
            venue=spec.venue,
            asset_in=spec.asset_in,
            asset_out=spec.build(fill_book).asset_out,
            t_fill=t_fill,
            book_ts=fill_book.ts_local,
            book_age_ms=(t_fill - fill_book.ts_local) * 1000.0,
            amount_in=amount,
            amount_out=quote.amount_out,
            expected_out=expected,
            exhausted=quote.exhausted,
        ))

        if quote.amount_out <= 0:
            return FillResult(
                filled=False,
                reason=f"leg {spec.product_id} returned nothing at fill time",
                decision_edge_bps=decision.net_edge_bps,
                realized_edge_bps=Decimal(0),
                start_amount=start_amount,
                end_amount=Decimal(0),
                fills=tuple(fills),
                unverifiable=False,
                stranded_asset=spec.asset_in if execution == SEQUENTIAL else None,
            )
        amount = quote.amount_out

    realized = (amount / start_amount - Decimal(1)) * BPS + _fixed_cost_bps(
        fixed_cost_usd, start_amount, start_asset_usd_price
    )

    return FillResult(
        filled=True,
        reason=None,
        decision_edge_bps=decision.net_edge_bps,
        realized_edge_bps=realized,
        start_amount=start_amount,
        end_amount=amount,
        fills=tuple(fills),
        unverifiable=False,
        stranded_asset=None,
    )


# --- aggregate reporting --------------------------------------------------

@dataclass(frozen=True)
class ReplayStats:
    """The go/no-go read on a whole log, not a single event."""
    attempted: int
    filled: int
    unverifiable: int
    rejected: int
    mean_decision_bps: Decimal
    mean_realized_bps: Decimal
    mean_adverse_bps: Decimal
    worst_adverse_bps: Decimal
    profitable_after_latency: int

    @property
    def survival_rate(self) -> float:
        """Share of advertised opportunities still profitable once filled."""
        return self.profitable_after_latency / self.filled if self.filled else 0.0

    def summary(self) -> str:
        lines = [
            f"attempted {self.attempted}  filled {self.filled}  "
            f"unverifiable {self.unverifiable}  rejected {self.rejected}",
        ]
        if self.filled:
            lines += [
                f"  mean decision edge  {self.mean_decision_bps:+.2f}bps",
                f"  mean realized edge  {self.mean_realized_bps:+.2f}bps",
                f"  mean adverse sel.   {self.mean_adverse_bps:+.2f}bps",
                f"  worst adverse sel.  {self.worst_adverse_bps:+.2f}bps",
                f"  still profitable    {self.profitable_after_latency}/{self.filled}"
                f"  ({self.survival_rate * 100:.1f}%)",
            ]
            if self.mean_realized_bps <= 0:
                lines.append(
                    "  VERDICT: no edge at this latency. The spread was real and "
                    "was not yours."
                )
        if self.unverifiable:
            lines.append(
                f"  {self.unverifiable} attempts could not be scored -- the tape "
                "ended inside the latency window. Sample books faster than your "
                "round-trip or these are silently excluded."
            )
        return "\n".join(lines)


def aggregate(results: Sequence[FillResult]) -> ReplayStats:
    filled = [r for r in results if r.filled]
    unver = sum(1 for r in results if r.unverifiable)
    rejected = sum(1 for r in results if not r.filled and not r.unverifiable)

    def mean(xs: list[Decimal]) -> Decimal:
        return sum(xs) / Decimal(len(xs)) if xs else Decimal(0)

    adverse = [r.adverse_selection_bps for r in filled]
    return ReplayStats(
        attempted=len(results),
        filled=len(filled),
        unverifiable=unver,
        rejected=rejected,
        mean_decision_bps=mean([r.decision_edge_bps for r in filled]),
        mean_realized_bps=mean([r.realized_edge_bps for r in filled]),
        mean_adverse_bps=mean(adverse),
        worst_adverse_bps=min(adverse) if adverse else Decimal(0),
        profitable_after_latency=sum(1 for r in filled if r.realized_edge_bps > 0),
    )
