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
