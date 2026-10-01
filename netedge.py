"""Net-edge calculation over a path of heterogeneous legs.

Pure: no network, no state, no orders. Everything is a function of
(legs, costs, size), so it can be unit-tested against hand-built venues.

The output is a decomposition, not a single number. After a week of logs the
breakdown tells you which single term is killing you -- and therefore whether
the fix is a fee tier, a smaller size, a faster path, or nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from legs import Leg

BPS = Decimal(10000)


@dataclass(frozen=True)
class EdgeResult:
    ok: bool
    reason: str | None

    net_edge_bps: Decimal
    gross_edge_bps: Decimal      # before any cost
    fee_bps: Decimal             # cost of venue/pool fees (negative)
    slippage_bps: Decimal        # cost of price impact / depth (negative)
    fixed_cost_bps: Decimal      # gas + inclusion, amortised over notional

    start_amount: Decimal
    end_amount: Decimal
    exhausted: bool              # some leg ran out of liquidity
    max_skew_ms: float
    path: tuple[str, ...]

    def summary(self) -> str:
        tail = "  [EXHAUSTED]" if self.exhausted else ""
        return (
            f"{' -> '.join(self.path)}  @{self.start_amount:,.0f}  "
            f"net {self.net_edge_bps:+.2f}bps  "
            f"(gross {self.gross_edge_bps:+.2f} fees {self.fee_bps:+.2f} "
            f"slip {self.slippage_bps:+.2f} fixed {self.fixed_cost_bps:+.2f})"
            f"{tail}"
        )


def _path_labels(path: Sequence[Leg]) -> tuple[str, ...]:
    if not path:
        return ()
    out = [path[0].asset_in]
    for leg in path:
        out.append(leg.asset_out)
    return tuple(out)


def _check_closed(path: Sequence[Leg]) -> str | None:
    """A path must chain asset-to-asset and return to its starting asset."""
    if not path:
        return "empty path"
    for a, b in zip(path, path[1:]):
        if a.asset_out != b.asset_in:
            return f"path break: {a.asset_out} != {b.asset_in}"
    if path[-1].asset_out != path[0].asset_in:
        return f"path not closed: ends {path[-1].asset_out}, starts {path[0].asset_in}"
    return None


def skew_ms(path: Sequence[Leg]) -> float:
    """Spread between the oldest and newest snapshot in the path.

    This is the single biggest source of phantom arbitrage: comparing a stale
    quote on one venue against a live quote on another invents edge out of
    nothing. Measured and enforced, never assumed away.
    """
    stamps = [leg.ts_local for leg in path]
    return (max(stamps) - min(stamps)) * 1000.0


def evaluate(
    path: Sequence[Leg],
    start_amount: Decimal,
    *,
    fixed_cost_usd: Decimal = Decimal(0),
    start_asset_usd_price: Decimal = Decimal(1),
    max_skew_ms: float | None = None,
) -> EdgeResult:
    """Evaluate a closed path at one size.

    fixed_cost_usd covers gas + inclusion payments: costs that do NOT scale
    with notional, which is what gives on-chain paths a hard minimum size.
    start_asset_usd_price converts the start asset into USD so the fixed cost
    is amortised correctly (1 when starting from USD or a USD stablecoin).
    """
    labels = _path_labels(path)
    observed_skew = skew_ms(path) if path else 0.0
    blank = dict(
        net_edge_bps=Decimal(0), gross_edge_bps=Decimal(0), fee_bps=Decimal(0),
        slippage_bps=Decimal(0), fixed_cost_bps=Decimal(0),
        start_amount=start_amount, end_amount=Decimal(0), exhausted=False,
        max_skew_ms=observed_skew, path=labels,
    )

    broken = _check_closed(path)
    if broken:
        return EdgeResult(ok=False, reason=broken, **blank)

    if max_skew_ms is not None and observed_skew > max_skew_ms:
        return EdgeResult(
            ok=False,
            reason=f"skew {observed_skew:.0f}ms > {max_skew_ms:.0f}ms",
            **blank,
        )

    if start_amount <= 0:
        return EdgeResult(ok=False, reason="non-positive size", **blank)

    # Three parallel walks of the same path isolate each cost term.
    a_frictionless = start_amount
    a_fee_only = start_amount
    a_full = start_amount
    exhausted = False

    for leg in path:
        a_frictionless = leg.frictionless(a_frictionless)
        a_fee_only = leg.fee_only(a_fee_only)
        q = leg.full(a_full)
        a_full = q.amount_out
        exhausted = exhausted or q.exhausted
        if a_full <= 0:
            dead = dict(blank)
            dead["exhausted"] = True
            return EdgeResult(
                ok=False,
                reason=f"leg produced nothing ({leg.asset_in} to {leg.asset_out})",
                **dead,
            )

    def to_bps(end: Decimal) -> Decimal:
        return (end / start_amount - Decimal(1)) * BPS

    gross = to_bps(a_frictionless)
    after_fees = to_bps(a_fee_only)
    after_impact = to_bps(a_full)

    notional_usd = start_amount * start_asset_usd_price
    if notional_usd > 0:
        fixed_bps = -(fixed_cost_usd / notional_usd) * BPS
    else:
        fixed_bps = Decimal(0)

    return EdgeResult(
        ok=True,
        reason=None,
        net_edge_bps=after_impact + fixed_bps,
        gross_edge_bps=gross,
        fee_bps=after_fees - gross,
        slippage_bps=after_impact - after_fees,
        fixed_cost_bps=fixed_bps,
        start_amount=start_amount,
        end_amount=a_full,
        exhausted=exhausted,
        max_skew_ms=observed_skew,
        path=labels,
    )


def solve_capacity(
    path: Sequence[Leg],
    *,
    lo: Decimal = Decimal("1"),
    hi: Decimal = Decimal("1000000"),
    fixed_cost_usd: Decimal = Decimal(0),
    start_asset_usd_price: Decimal = Decimal(1),
    tolerance: Decimal = Decimal("0.01"),
    max_iters: int = 60,
) -> Decimal:
    """Largest start size at which net edge is still positive.

    This number matters more than the bps figure: expected profit per event is
    net_edge_bps x fillable size, and capacity is usually the binding
    constraint. Returns 0 if the path is unprofitable even at `lo`.

    Assumes net edge is monotonically decreasing in size -- true for order-book
    depth and for AMM curves. With a nonzero fixed cost the curve is unimodal,
    so pass a `lo` above min_profitable_size for the answer to be the upper
    crossing rather than the lower one.
    """
    def net(size: Decimal) -> Decimal:
        r = evaluate(
            path, size,
            fixed_cost_usd=fixed_cost_usd,
            start_asset_usd_price=start_asset_usd_price,
        )
        return r.net_edge_bps if r.ok else Decimal(-1)

    if net(lo) <= 0:
        return Decimal(0)
    if net(hi) > 0:
        return hi  # capacity exceeds the search range

    for _ in range(max_iters):
        if hi - lo <= tolerance:
            break
        mid = (lo + hi) / 2
        if net(mid) > 0:
            lo = mid
        else:
            hi = mid
    return lo


def min_profitable_size(
    path: Sequence[Leg],
    *,
    fixed_cost_usd: Decimal,
    start_asset_usd_price: Decimal = Decimal(1),
    lo: Decimal = Decimal("1"),
    hi: Decimal = Decimal("1000000"),
    tolerance: Decimal = Decimal("0.01"),
    max_iters: int = 60,
) -> Decimal | None:
    """Smallest start size that clears a fixed cost (gas). None if never.

    On-chain paths are unprofitable below this size no matter how good the
    spread looks, because gas does not scale down with your notional.
    """
    if fixed_cost_usd <= 0:
        return lo

    def net(size: Decimal) -> Decimal:
        r = evaluate(
            path, size,
            fixed_cost_usd=fixed_cost_usd,
            start_asset_usd_price=start_asset_usd_price,
        )
        return r.net_edge_bps if r.ok else Decimal(-1)

    if net(hi) <= 0:
        return None
    if net(lo) > 0:
        return lo

    for _ in range(max_iters):
        if hi - lo <= tolerance:
            break
        mid = (lo + hi) / 2
        if net(mid) > 0:
            hi = mid
        else:
            lo = mid
    return hi
