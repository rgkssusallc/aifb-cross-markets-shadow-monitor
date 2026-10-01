"""Sui via the FlowX aggregator, restricted to Cetus pools. READ-ONLY.

Built from a handover pack of a system that has been executing live Sui swaps
since 2026-09-29. This file deliberately uses ONE endpoint from it -- the
FlowX quote GET -- and none of the execution path. No GraphQL, no object
resolver, no BCS encoder, no signer, no submitter. It cannot place a trade
even by accident, which is the same property venues/coinbase.py and
venues/evm.py hold, and it is why those parts are not imported here.

A correction to an earlier assumption in this project: Sui DOES have an
eth_call equivalent -- GraphQL `simulateTransaction`, or JSON-RPC
`dryRunTransactionBlock` / `devInspectTransactionBlock`, all returning full
effects, gas and balance changes before sending. The reason a Sui swap needs
its own leg type is the object model, not the absence of simulation: coins are
objects you merge and split, gas comes from a named coin, shared objects need
their initialSharedVersion, and results are read from effects. None of that
affects quoting, which is why this venue is small.

THE PAIR HERE IS SUI/USDC, NOT ETH/USDC. That is what Cetus has depth in, and
it is what the handover's recorded mainnet quotes cover. Asking this venue for
WETH returns None rather than a thin bridged-wrapper price dressed up as the
real thing, so Engine.routes() simply leaves it out of ETH/USDC routes.

FOUR GUARDS, EVERY ONE PAID FOR BY SOMEBODY ELSE'S LIVE TRADING:

  Only sources we could actually execute. FlowX once returned a route over
  Momentum, which their encoder cannot build. For an execution system that is
  an unbuildable plan; for a MEASUREMENT system it is worse -- it is a
  phantom opportunity, an edge that was never available at all. So
  includeSources is pinned to CETUS and an empty source set is refused,
  because FlowX treats empty as unrestricted.

  Crossed quotes are invalid. They observed a buy price stuck stale for
  minutes, below the same venue's sell price. A crossed pair of quotes is not
  a 100% arbitrage, it is broken data, and it is exactly the shape that would
  top any opportunity log.

  Outlier quotes are rejected. A single 10-second sample came back 2.8% off
  the market. Logged naively that is a 280bps opportunity, dwarfing every
  real signal in the distribution.

  Gas is measured, not guessed. Their first estimate of 0.02 SUI was 10-30x
  too high and HID real opportunities -- the mirror image of the usual error.
  The figure below is their measurement, cited as theirs, and flagged as not
  ours.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Hashable

import httpx

from core.venue import AMM
from legs import LegQuote, QuotedLeg

FLOWX_QUOTE_URL = "https://api.flowx.finance/flowx-ag-routing/api/v1/quote"

SUI_COIN = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"
USDC_COIN = ("0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
             "::usdc::USDC")

# Only sources whose transactions could actually be built. Empty means
# UNRESTRICTED to FlowX, so it is refused rather than sent.
DEFAULT_SOURCES = ("CETUS",)

# Their measurement: a ~$5 swap paid 0.0006-0.0016 SUI. Upper end taken, and
# gas_measured stays False because it is not OUR measurement and not at our
# size. Their own lesson was that an inflated gas figure hides real edges.
THEIR_GAS_SUI_HIGH = Decimal("0.0016")

TINY_DIVISOR = 10_000
MIN_TINY_RAW = 1_000

# A quote this far from the session's running reference is treated as a glitch.
# 2.8% was observed live; 1.5% is comfortably outside normal impact at the
# sizes measured here and comfortably inside that glitch.
MAX_DEVIATION = Decimal("0.015")


class SuiError(RuntimeError):
    """Anything that must not be smoothed over with a default."""


@dataclass
class PinnedQuote:
    """Answers exactly the sizes that were actually quoted.

    The FlowX quote is an async HTTP call but legs.QuotedLeg calls its quoter
    synchronously, so sizes are fetched in leg() and pinned here. An
    unexpected size raises instead of interpolating: a made-up number in the
    middle of a cost decomposition is worse than a crash.
    """
    out_by_in: dict[Decimal, Decimal] = field(default_factory=dict)

    def __call__(self, amount_in: Decimal) -> Decimal:
        hit = self.out_by_in.get(amount_in)
        if hit is not None:
            return hit
        # Tolerate float/Decimal round-tripping, nothing more.
        for k, v in self.out_by_in.items():
            if k > 0 and abs(k - amount_in) / k < Decimal("0.000001"):
                return v
        raise SuiError(
            f"no FlowX quote pinned for size {amount_in}; "
            f"have {sorted(self.out_by_in)}"
        )


def to_raw(amount: Decimal, decimals: int) -> int:
    return int(amount * (Decimal(10) ** decimals))


def from_raw(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


@dataclass
class SuiFlowXVenue:
    """SUI/USDC on Cetus, priced through FlowX. Quote endpoint only."""
    name: str = "sui:flowx:cetus"
    kind: str = AMM
    sources: tuple[str, ...] = DEFAULT_SOURCES
    timeout_s: float = 12.0
    # FALLBACK ONLY. The real fee is summed from the route's per-hop fees in
    # _record_route; this value is used solely if a response carries none.
    # It was originally set to 25bps as a guess, which measurement showed to
    # be 5x the direct pool's 5bps and 3.5x a measured 3-hop route's 7bps.
    pool_fee_rate: Decimal = Decimal("0.0007")

    # Route shape from the last quote, recorded because it is a RISK
    # disclosure, not a detail: a 3-hop route through two thin intermediate
    # assets is not the same instrument as a direct swap, even when the
    # output is a tenth of a basis point better.
    last_hops: int = 0
    last_paths: int = 0
    last_intermediates: tuple[str, ...] = ()
    last_fee_rate: Decimal = Decimal(0)

    rpc_url: str = ""
    checkpoint: int = 0
    coins: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Injectable so tests can replay the handover's recorded mainnet
    # responses without a network or a key.
    transport: httpx.AsyncBaseTransport | None = None
    rpc_transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = None
    _rpc: httpx.AsyncClient | None = None
    _ref_price: Decimal | None = None
    _last_err: str = ""
    quotes_made: int = 0
    glitches_rejected: int = 0
    crossed_rejected: int = 0

    # --- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        if not self.sources:
            raise SuiError(
                "refusing to quote with no sources: FlowX treats an empty "
                "includeSources as unrestricted, which would return routes "
                "through venues this project cannot verify"
            )
        self.rpc_url = (self.rpc_url
                        or os.environ.get("NON_EVM_SUI_RPC_URL", ""))
        if not self.rpc_url:
            raise SuiError("set NON_EVM_SUI_RPC_URL for the Sui JSON-RPC endpoint")
        self._client = httpx.AsyncClient(timeout=self.timeout_s,
                                         transport=self.transport)
        self._rpc = httpx.AsyncClient(timeout=self.timeout_s,
                                      transport=self.rpc_transport)

        # Prove the coin types instead of trusting the strings, the same way
        # ERC20 symbol()/decimals() is checked on EVM.
        for want, coin_type in (("SUI", SUI_COIN), ("USDC", USDC_COIN)):
            meta = await self._rpc_call("suix_getCoinMetadata", [coin_type])
            if not meta:
                raise SuiError(f"no coin metadata for {coin_type}")
            sym, dec = meta.get("symbol"), meta.get("decimals")
            if str(sym).upper() != want:
                raise SuiError(
                    f"{coin_type} reports symbol {sym!r}, expected {want!r} "
                    "-- wrong coin type"
                )
            if not isinstance(dec, int) or not 0 <= dec <= 36:
                raise SuiError(f"{coin_type} implausible decimals {dec!r}")
            self.coins[want] = (coin_type, dec)
        await self.refresh()

    async def aclose(self) -> None:
        for c in (self._client, self._rpc):
            if c is not None:
                await c.aclose()
        self._client = self._rpc = None

    async def _rpc_call(self, method: str, params: list):
        assert self._rpc is not None
        try:
            r = await self._rpc.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except httpx.HTTPError as e:
            raise SuiError(
                f"sui rpc {method} unreachable "
                f"({type(e).__name__}: {str(e)[:70]})"
            ) from e
        if r.status_code != 200:
            raise SuiError(f"sui rpc {method} HTTP {r.status_code}")
        j = r.json()
        if "error" in j:
            raise SuiError(f"sui rpc {method}: {j['error']}")
        return j.get("result")

    async def refresh(self) -> None:
        """Checkpoint sequence is the state token, Sui's analogue of a block."""
        try:
            cp = await self._rpc_call("sui_getLatestCheckpointSequenceNumber", [])
            self.checkpoint = int(cp)
            self._last_err = ""
        except Exception as e:  # noqa: BLE001
            self._last_err = f"{type(e).__name__}: {str(e)[:100]}"

    # --- Venue protocol ---------------------------------------------------

    def state_id(self) -> Hashable:
        return self.checkpoint

    @property
    def exact_while_state_unchanged(self) -> bool:
        # Pool state is checkpoint-atomic, like a block. Sui checkpoints are
        # fast (sub-second), so the cache helps less than on Base -- but the
        # semantics are the same and the engine needs no special case.
        return True

    def healthy(self) -> bool:
        return bool(self.coins) and self.checkpoint > 0 and not self._last_err

    def age_ms(self) -> float:
        return 0.0

    def fixed_cost_usd(self) -> Decimal:
        # SUI-denominated gas converted at the running reference price. Their
        # figure, their size -- see THEIR_GAS_SUI_HIGH.
        if self._ref_price is None:
            return Decimal(0)
        return THEIR_GAS_SUI_HIGH * self._ref_price

    def assets(self) -> set[str]:
        # Deliberately NOT WETH. Cetus's depth here is SUI/USDC, and offering
        # a bridged ETH wrapper as if it were the real pair is how a thin
        # market gets mistaken for a tradeable one.
        return set(self.coins)

    # --- quoting ----------------------------------------------------------

    async def _flowx(self, coin_in: str, coin_out: str, raw_in: int) -> dict:
        assert self._client is not None
        params = {
            "tokenIn": coin_in, "tokenOut": coin_out, "amountIn": str(raw_in),
            "includeSources": ",".join(sorted(self.sources)),
        }
        try:
            r = await self._client.get(FLOWX_QUOTE_URL, params=params)
        except httpx.HTTPError as e:
            # A blocked host, DNS failure or timeout must surface as a venue
            # that cannot quote, never as an exception that takes the whole
            # sweep down with it. One unreachable venue is not a failed run.
            raise SuiError(
                f"flowx unreachable ({type(e).__name__}: {str(e)[:80]})"
            ) from e
        if r.status_code != 200:
            raise SuiError(f"flowx HTTP {r.status_code}: {r.text[:120]}")
        j = r.json()
        if j.get("code") != 0:
            raise SuiError(f"flowx code {j.get('code')}: {str(j.get('message'))[:90]}")
        data = j.get("data") or {}
        if not data.get("amountOut"):
            raise SuiError("flowx returned no amountOut")
        self.quotes_made += 1
        return data

    def _record_route(self, data: dict) -> Decimal:
        """Read the route's real fee total and shape out of the response.

        The pool fee CANNOT be a constant for an aggregator. A measured route
        was SUI -> CERT -> BUCK -> USDC with per-hop fees of 100, 500 and 100
        over a denominator of 1e6: 7bps across three hops, not the 25bps this
        venue originally hardcoded and not the 5bps of the direct pool. Which
        hops get chosen changes with size, so the fee changes with size, and
        the only honest source for it is the route itself.
        """
        paths = data.get("paths") or []
        total_fee = Decimal(0)
        hops = 0
        inter: list[str] = []
        for path in paths:
            for hop in path:
                hops += 1
                extra = hop.get("extra") or {}
                fee = extra.get("fee")
                denom = extra.get("feeDenominator")
                if fee is not None and denom:
                    total_fee += Decimal(int(fee)) / Decimal(int(denom))
                out_t = hop.get("tokenOut", "")
                if out_t and out_t != USDC_COIN:
                    short = out_t.partition("::")[2] or out_t
                    if short not in inter:
                        inter.append(short)
        self.last_paths = len(paths)
        self.last_hops = hops
        self.last_intermediates = tuple(inter)
        # Fees across hops compound rather than add, but at single-digit bps
        # the difference is below the noise; summing is the conservative read.
        self.last_fee_rate = total_fee
        return total_fee

    def _check_sane(self, price: Decimal) -> None:
        """Reject the 2.8%-off glitch rather than logging it as an edge."""
        if self._ref_price is None:
            self._ref_price = price
            return
        dev = abs(price - self._ref_price) / self._ref_price
        if dev > MAX_DEVIATION:
            self.glitches_rejected += 1
            raise SuiError(
                f"quote {price:.6f} deviates {dev * 100:.2f}% from reference "
                f"{self._ref_price:.6f}; treating as a glitch, not an edge"
            )
        # Track slowly so a genuine trend is followed but a spike is not.
        self._ref_price = (self._ref_price * Decimal(9) + price) / Decimal(10)

    async def leg(self, asset_in: str, asset_out: str,
                  size_in: Decimal) -> QuotedLeg | None:
        if asset_in not in self.coins or asset_out not in self.coins:
            return None
        if size_in <= 0 or self._client is None:
            return None
        (ci, di), (co, do) = self.coins[asset_in], self.coins[asset_out]
        raw_in = to_raw(size_in, di)
        if raw_in <= 0:
            return None
        tiny_raw = max(raw_in // TINY_DIVISOR, MIN_TINY_RAW)

        try:
            real = await self._flowx(ci, co, raw_in)
            tiny = await self._flowx(ci, co, tiny_raw)
        except SuiError as e:
            self._last_err = str(e)[:140]
            return None

        fee_rate = self._record_route(real)
        real_out = from_raw(int(real["amountOut"]), do)
        tiny_out = from_raw(int(tiny["amountOut"]), do)
        tiny_in = from_raw(tiny_raw, di)
        if tiny_out <= 0 or tiny_in <= 0 or real_out <= 0:
            self._last_err = "flowx returned a zero quote"
            return None

        # Divide the pool fee back out of the negligible-size quote to recover
        # the frictionless baseline netedge.py needs, exactly as the v3 venue
        # does with QuoterV2.
        # Baseline uses the TINY route's own fee, since a small order may be
        # routed differently from a large one and therefore pay a different
        # fee total. Falling back to the configured default only if the
        # response carried no per-hop fees at all.
        tiny_fee = self._record_route(tiny) or self.pool_fee_rate
        marginal = (tiny_out / tiny_in) / (Decimal(1) - tiny_fee)
        try:
            self._check_sane(marginal if asset_in == "SUI" else Decimal(1) / marginal)
        except SuiError as e:
            self._last_err = str(e)[:140]
            return None

        return QuotedLeg(
            asset_in=asset_in, asset_out=asset_out,
            marginal_out_per_in=marginal,
            fee_rate=fee_rate or self.pool_fee_rate,
            quote_fn=PinnedQuote({size_in: real_out}),
            ts_local=time.time(),
            venue=self.name,
        )

    def status(self) -> str:
        bits = [f"checkpoint {self.checkpoint:,}", f"quotes {self.quotes_made}",
                f"sources {'+'.join(self.sources)}"]
        if self.last_hops:
            bits.append(f"route {self.last_paths}path/{self.last_hops}hop "
                        f"fee {self.last_fee_rate * Decimal(10000):.1f}bps")
        if self.last_intermediates:
            bits.append("via " + ",".join(self.last_intermediates))
        if self.glitches_rejected:
            bits.append(f"glitches {self.glitches_rejected}")
        if self.crossed_rejected:
            bits.append(f"crossed {self.crossed_rejected}")
        if self._last_err:
            bits.append(f"ERR {self._last_err}")
        return "  ".join(bits)


def crossed(buy_price: Decimal, sell_price: Decimal) -> bool:
    """True when quotes are inconsistent: you could buy below the sell price.

    Observed live as a buy price stuck stale for minutes, below the same
    venue's sell price. That is not a 100% arbitrage, it is broken data, and
    it is precisely the shape that tops an opportunity log.
    """
    return buy_price < sell_price
