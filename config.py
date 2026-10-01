"""Cost configuration.

Every number here is a real cost you pay. Nothing is a placeholder that
defaults to zero -- a zero cost must be stated explicitly, because silent
zeros are how backtests lie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

BPS = Decimal(10000)


def bps(x: str | int | float | Decimal) -> Decimal:
    """Basis points as a Decimal rate. bps(10) -> 0.001"""
    return Decimal(str(x)) / BPS


@dataclass(frozen=True)
class VenueFees:
    """Per-venue trading fees, expressed in basis points."""
    name: str
    taker_bps: Decimal
    maker_bps: Decimal

    @property
    def taker_rate(self) -> Decimal:
        return self.taker_bps / BPS

    @property
    def maker_rate(self) -> Decimal:
        return self.maker_bps / BPS


@dataclass(frozen=True)
class ChainCosts:
    """Fixed per-execution on-chain costs, in USD.

    These do NOT scale with notional, which is why on-chain paths have a
    hard minimum profitable size. gas_usd should be a measured estimate for
    the actual swap path, not a guess.
    """
    name: str
    gas_usd: Decimal
    # Extra payment to get included ahead of competitors. Zero means you are
    # broadcasting publicly and accepting that you may be frontrun.
    inclusion_usd: Decimal = Decimal("0")

    @property
    def total_usd(self) -> Decimal:
        return self.gas_usd + self.inclusion_usd


# --- Defaults -------------------------------------------------------------
# Coinbase fee pinned at 0.1% per leg per instruction. NOTE: verify against
# GET /api/v3/brokerage/transaction_summary -- the entry Advanced Trade tier
# is considerably worse than this (~0.60% taker).
COINBASE = VenueFees(name="coinbase", taker_bps=Decimal("10"), maker_bps=Decimal("10"))

# Uniswap-style pool fee tiers, for reference when constructing AMM legs.
UNI_FEE_TIERS_BPS = {
    "0.01%": Decimal("1"),
    "0.05%": Decimal("5"),
    "0.30%": Decimal("30"),
    "1.00%": Decimal("100"),
}

BASE_CHAIN = ChainCosts(name="base", gas_usd=Decimal("0.02"))
ETHEREUM = ChainCosts(name="ethereum", gas_usd=Decimal("8.00"))


@dataclass(frozen=True)
class RunConfig:
    """Shadow-logger run parameters."""
    # Reject any evaluation whose legs were snapshotted further apart than
    # this. Comparing a stale quote against a live one manufactures fake arbs.
    max_skew_ms: float = 500.0
    # Only open an opportunity record above this net edge.
    min_edge_bps: Decimal = Decimal("0.5")
    # Notional sizes to evaluate at, in quote currency (USD).
    probe_sizes_usd: tuple[Decimal, ...] = (
        Decimal("100"), Decimal("1000"), Decimal("10000"), Decimal("50000"),
    )
    poll_interval_s: float = 1.0
    db_path: str = "shadow.db"


def breakeven_bps(legs: int, fee_bps: Decimal) -> Decimal:
    """Round-trip fee cost of an n-leg path. The number you must beat."""
    return Decimal(legs) * fee_bps


# --- inventory mode -------------------------------------------------------
# The single largest term in a cross-venue cost model is not a fee. It is
# whether funds move per trade or are already sitting on both venues, and
# until this was made explicit the model here was out by ~29bps -- enough to
# turn a dead strategy into a live one, in the wrong direction.
#
# Numbers below are from an operator running this pair live (Coinbase <-> Sui
# since 2026-09-29), not from estimates.

@dataclass(frozen=True)
class InventoryMode:
    """How capital reaches the two venues, and what that costs.

    min_gap_duration_s is the part that is easy to miss and decides most of
    the outcome: an opportunity that dies before you can act on it was never
    yours. Moving funds per trade means only gaps lasting tens of seconds are
    reachable; pre-positioned inventory means the limit is your round-trip
    latency instead, which is three orders of magnitude smaller.
    """
    name: str
    # Per-cycle costs that do NOT scale with notional in the same way fees do;
    # expressed in bps of notional for comparability.
    withdrawal_deposit_transfer_bps: Decimal
    # A reserve against adverse movement between quote and fill. This is a
    # RESERVE, not a measurement: correct when deciding to trade, wrong when
    # measuring whether edge exists at all. Both are kept so the two
    # questions do not get confused.
    slippage_allowance_bps_per_leg: Decimal
    # How long a dislocation must survive to be capturable in this mode.
    min_gap_duration_s: Decimal
    note: str = ""

    def total_overhead_bps(self, legs: int = 2,
                           include_allowance: bool = True) -> Decimal:
        out = self.withdrawal_deposit_transfer_bps
        if include_allowance:
            out += self.slippage_allowance_bps_per_leg * Decimal(legs)
        return out


# Funds moved Coinbase -> wallet -> Coinbase for each trade. Their measured
# transfers took 12-53s, so a gap must last roughly half a minute to be
# captured at all. Their stated break-even on this basis was ~45bps on the
# engine estimate and ~60bps on the stricter live check.
TRANSFER_CYCLE = InventoryMode(
    name="transfer-cycle",
    withdrawal_deposit_transfer_bps=Decimal("9.2"),
    slippage_allowance_bps_per_leg=Decimal("10"),
    min_gap_duration_s=Decimal("30"),
    note="measured live: transfers took 12-53s; break-even ~45-60bps",
)

# Inventory already held on both venues. No withdrawal, deposit or transfer
# per trade, and the binding constraint becomes round-trip latency -- measured
# here at ~60ms to Coinbase. Their words: "with funds pre-positioned on both
# venues you could catch much shorter gaps, and far more of the tail becomes
# reachable."
PRE_POSITIONED = InventoryMode(
    name="pre-positioned",
    withdrawal_deposit_transfer_bps=Decimal("0"),
    slippage_allowance_bps_per_leg=Decimal("0"),
    min_gap_duration_s=Decimal("0.06"),
    note="requires capital idle on both venues; latency-bound, not transfer-bound",
)

INVENTORY_MODES = {m.name: m for m in (TRANSFER_CYCLE, PRE_POSITIONED)}
