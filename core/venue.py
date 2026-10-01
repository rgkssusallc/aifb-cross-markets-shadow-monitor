"""The Venue interface: the one thing a new venue has to implement.

Adding a venue must not mean editing the engine. Everything the engine needs
is behind this protocol, so a new exchange or chain is one new file.

The three ideas that make one interface fit both an order book and a bonding
curve:

STATE ID, not timestamps. A venue reports an opaque `state_id` -- a block
number on a chain, a message sequence on a streaming book. Combined with
`exact_while_state_unchanged`, this replaces per-venue special-casing of
caching AND of staleness:

  On a chain the pool state cannot change within a block, so a quote taken
  earlier in the same block is not stale, it is EXACT. Re-quoting is pure
  waste -- measured on Base, blocks arrive every ~2s while a CEX book pushes
  ~17/s, so re-quoting per poll spent four eth_calls to get an identical
  answer. The engine caches on state_id and the saving is automatic for every
  chain venue ever added.

  On an order book every update is a new state and an old book is genuinely
  dangerous, so those venues set exact_while_state_unchanged False and are
  judged on `age_ms` instead.

Judging both by one wall-clock skew threshold is the modelling error this
replaces: it is the right test for two order books and the wrong one for a
mixed path.

ALIASES ARE DECLARED, WITH COSTS. ETH and WETH are not the same token; USDC
and USD are not the same asset. Pretending otherwise is how an unflagged
basis ends up inside a signal a few basis points wide -- which nearly
happened here, because the Coinbase level2 feed silently serves ETH-USD when
asked for ETH-USDC. So an equivalence must be declared, carries its own cost
and basis, and every path records which ones it leaned on.

VENUES QUOTE LEGS, THEY DO NOT KNOW ABOUT PATHS. A venue answers "what do I
give you for this much of X" and nothing else. Path construction, costing and
logging belong to the engine, so venues stay small and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Hashable, Protocol, runtime_checkable

from legs import Leg

CEX = "cex"
AMM = "amm"
AGGREGATOR = "aggregator"


@dataclass(frozen=True)
class Alias:
    """A declared equivalence between two asset symbols.

    proven=True means the equivalence is enforced by a contract (WETH's
    deposit/withdraw is exactly 1:1). proven=False means it is a market
    relationship that can and does move -- USDC/USD being the obvious one --
    and basis_bps is then an OBSERVATION with a timestamp, not a constant.
    """
    a: str
    b: str
    proven: bool
    cost_usd: Decimal = Decimal(0)
    basis_bps: Decimal = Decimal(0)
    note: str = ""

    def describe(self) -> str:
        kind = "contract 1:1" if self.proven else f"market, basis {self.basis_bps:+.2f}bps"
        tail = f", cost ${self.cost_usd}" if self.cost_usd else ""
        return f"{self.a}={self.b} ({kind}{tail})"


# WETH's deposit()/withdraw() is exactly 1:1 by construction, so the rate is
# not an assumption -- only the gas is, and it is carried explicitly.
WETH_ETH = Alias("WETH", "ETH", proven=True, cost_usd=Decimal(0),
                 note="WETH deposit/withdraw is 1:1; wrap gas not included")

# NOT proven. Measured at 0.00bps on Coinbase with no USDC-USD product listed,
# i.e. Coinbase treats them as one book -- true when measured, not guaranteed.
# Any path relying on this records it, so a result that depends on the peg is
# never mistaken for one that does not.
USDC_USD = Alias("USDC", "USD", proven=False, basis_bps=Decimal(0),
                 note="measured 0.00bps on Coinbase; re-measure before trusting")


@dataclass
class Aliases:
    """Canonicalises asset symbols and remembers what that assumed."""
    declared: tuple[Alias, ...] = (WETH_ETH, USDC_USD)

    def canonical(self, symbol: str) -> str:
        """Map a venue's symbol to the engine's canonical name."""
        for al in self.declared:
            if symbol == al.b:
                return al.a
        return symbol

    def used(self, venue_symbols: set[str]) -> tuple[str, ...]:
        """Which declared aliases a set of venue symbols relied on."""
        out = []
        for al in self.declared:
            if al.b in venue_symbols:
                out.append(al.describe())
        return tuple(out)

    def unproven_used(self, venue_symbols: set[str]) -> tuple[Alias, ...]:
        """Aliases in play that are NOT contractually guaranteed.

        These are the ones that can quietly invalidate a result, so the
        engine surfaces them rather than averaging them in.
        """
        return tuple(al for al in self.declared
                     if al.b in venue_symbols and not al.proven)


@runtime_checkable
class Venue(Protocol):
    """What a venue must provide. Nothing here mentions arbitrage."""

    name: str          # unique, appears in logs and cycle keys
    kind: str          # CEX | AMM | AGGREGATOR

    async def connect(self) -> None:
        """Set up connections and validate configuration. Raise to refuse."""

    async def aclose(self) -> None: ...

    async def refresh(self) -> None:
        """Bring internal state up to date. Cheap for pushed feeds."""

    def state_id(self) -> Hashable:
        """Opaque token for the current state. See module docstring."""

    @property
    def exact_while_state_unchanged(self) -> bool:
        """True when a quote stays exact until state_id changes.

        True for chains (block-atomic state), False for order books.
        """

    def healthy(self) -> bool:
        """False when quotes must not be trusted at all."""

    def age_ms(self) -> float:
        """Age of the underlying data. Meaningful mainly when not exact."""

    def fixed_cost_usd(self) -> Decimal:
        """Per-execution cost that does not scale with notional (gas)."""

    def assets(self) -> set[str]:
        """Venue-native asset symbols this venue can currently quote."""

    async def leg(self, asset_in: str, asset_out: str,
                  size_in: Decimal) -> Leg | None:
        """A Leg converting size_in of asset_in, or None if unavailable.

        size_in is in units of asset_in. Returning None must mean "cannot
        quote this", never a zero-priced leg.

        Async because quoting may do I/O, and a venue that blocks the event
        loop starves every streaming venue beside it: synchronous RPC calls
        here stopped a level2 background task from ever being scheduled, so
        its book aged into rejection during a sweep that was itself the cause.
        A venue doing blocking I/O must hand it to a thread.
        """


@dataclass
class LegCache:
    """Reuses legs while the venue's state_id is unchanged.

    This is where the block-gating saving actually happens, once, for every
    venue rather than per venue. A venue that is not exact-while-unchanged
    never gets a cache hit, which is correct rather than a limitation.
    """
    hits: int = 0
    misses: int = 0
    _entries: dict[tuple, tuple[Hashable, Leg]] = field(default_factory=dict)

    async def get(self, venue: Venue, asset_in: str, asset_out: str,
                  size_in: Decimal) -> Leg | None:
        key = (venue.name, asset_in, asset_out, size_in)
        sid = venue.state_id()
        if venue.exact_while_state_unchanged:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == sid:
                self.hits += 1
                return cached[1]
        leg = await venue.leg(asset_in, asset_out, size_in)
        self.misses += 1
        if leg is not None and venue.exact_while_state_unchanged:
            self._entries[key] = (sid, leg)
        return leg

    def stats(self) -> str:
        total = self.hits + self.misses
        pct = 100.0 * self.hits / total if total else 0.0
        return f"legcache {self.hits}/{total} reused ({pct:.0f}%)"
