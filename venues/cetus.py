"""Cetus read directly, with no aggregator in the path.

Why direct beats going through FlowX for MEASUREMENT (execution is a separate
question, and the handover's router path remains the right answer there):

  No new network dependency. The pool object is read over the Sui JSON-RPC
  endpoint this project already has, so nothing needs allowlisting. The
  aggregator needs api.flowx.finance, which is not reachable here.

  No aggregator glitches. A single FlowX sample was observed 2.8% off the
  market, and a buy price stuck stale for minutes below the same venue's sell
  price. Pool state cannot glitch like that: it is what it is, at a version.

  No unexecutable routes. An aggregator may route through a source that
  cannot be built. Reading one pool quotes exactly the pool you named.

  THE FEE COMES FROM THE POOL. This is the one that actually bit: the FlowX
  venue in this repo hardcoded 25bps, and the pool reports fee_rate 500 over
  a denominator of 1e6, i.e. 5bps. Five times too high, which would have made
  every Cetus route look 20bps worse than it is. Assumed costs are the whole
  problem this project exists to avoid.

What direct access costs, stated plainly: the aggregator would have handed us
a full quote including tick-crossing impact, and here that has to be computed.
Within the current tick the constant-liquidity formula is exact, so that is
what is implemented -- and a swap large enough to leave the current tick is
REFUSED rather than extrapolated, because liquidity beyond the boundary is
unknown without walking the tick_manager. Guessing there would overstate
output at exactly the sizes where it matters.

Cetus uses Q64.64 sqrt prices, not Uniswap's Q96. Using the wrong shift gives
a price off by 2^32, which is obvious; using the wrong decimals scaling gives
one off by 10^3 for this pair, which is not.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Hashable

import httpx

from core.venue import AMM
from legs import LegQuote, QuotedLeg

# Q64.64 sqrt prices and 10^9-scale integers need more than the default 28
# digits, or the price itself loses significance before any arithmetic.
getcontext().prec = 60

Q64 = Decimal(2) ** 64
FEE_DENOM = Decimal(1_000_000)
TICK_BASE = Decimal("1.0001")

SUI_COIN = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"
USDC_COIN = ("0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
             "::usdc::USDC")

# Cetus SUI/USDC. Discovered from the handover's recorded FlowX route, then
# proved here: connect() reads the object and checks its declared type names
# both coins in the expected order.
SUI_USDC_POOL = "0x51e883ba7c0b566a26cbc8a94cd33eb0abd418a77cc1e60ad22fd9b1f29cd2ab"

# Their live measurement for a ~$5 swap was 0.0006-0.0016 SUI. Upper end used;
# gas_measured stays False because it is neither our measurement nor our size.
THEIR_GAS_SUI_HIGH = Decimal("0.0016")


class CetusError(RuntimeError):
    """Anything that must not be papered over with a default."""


def sqrt_price_to_price(sqrt_x64: Decimal, dec0: int, dec1: int) -> Decimal:
    """token1 per token0, in human units.

    Cetus stores sqrt(token1_raw/token0_raw) in Q64.64. Squaring gives the raw
    ratio; the decimals difference converts it to human units.
    """
    raw = (sqrt_x64 / Q64) ** 2
    return raw * (Decimal(10) ** (dec0 - dec1))


def tick_to_sqrt_x64(tick: int) -> Decimal:
    """Boundary sqrt price for a tick index, in Q64.64."""
    return (TICK_BASE ** (Decimal(tick) / Decimal(2))) * Q64


def normalize_coin_type(coin_type: str) -> str:
    """Canonical form of a Sui coin type.

    Sui accepts an address short OR zero-padded, so the SAME coin appears as
    both `0x2::sui::SUI` and
    `0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI`.
    The pool's declared type uses the short form while the handover's quotes
    use the padded one, so a plain string comparison between two correct
    values fails. Normalising is not cosmetic: without it the pool's token
    order cannot be checked, and getting that order backwards inverts the
    price into what looks like an enormous arbitrage.
    """
    addr, sep, rest = coin_type.partition("::")
    if not sep:
        return coin_type.lower()
    body = addr[2:] if addr.lower().startswith("0x") else addr
    return "0x" + body.rjust(64, "0").lower() + "::" + rest


def generic_params(type_str: str) -> tuple[str, ...]:
    """The <A, B> parameters of a Move type, split at the top level only.

    Nested generics mean a naive split(",") is wrong, and silently so.
    """
    if "<" not in type_str or ">" not in type_str:
        return ()
    inner = type_str[type_str.index("<") + 1:type_str.rindex(">")]
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return tuple(out)


def _i32(field_value) -> int:
    """Cetus wraps signed ticks as an I32 struct with a `bits` field."""
    if isinstance(field_value, dict):
        bits = int(field_value.get("fields", {}).get("bits", 0))
        return bits - (1 << 32) if bits >= (1 << 31) else bits
    return int(field_value)


@dataclass(frozen=True)
class PoolState:
    """One immutable reading of a Cetus pool."""
    pool_id: str
    version: int
    sqrt_x64: Decimal
    liquidity: Decimal
    fee_rate: Decimal          # as a rate, e.g. 0.0005
    tick_spacing: int
    tick: int
    paused: bool
    coin0: str
    coin1: str
    dec0: int
    dec1: int

    def price_1_per_0(self) -> Decimal:
        return sqrt_price_to_price(self.sqrt_x64, self.dec0, self.dec1)

    def tick_bounds_x64(self) -> tuple[Decimal, Decimal]:
        """Sqrt-price bounds of the tick the pool currently sits in.

        Conservative: liquidity may well extend past these, but establishing
        that needs the tick_manager. Refusing at the boundary can only
        understate capacity, never overstate output.
        """
        lo_tick = (self.tick // self.tick_spacing) * self.tick_spacing
        return tick_to_sqrt_x64(lo_tick), tick_to_sqrt_x64(lo_tick + self.tick_spacing)

    def quote_exact_in(self, amount_in: Decimal, zero_for_one: bool
                       ) -> tuple[Decimal, bool]:
        """(amount_out, crossed_tick) for an exact-input swap.

        Exact within the current tick: with liquidity constant,
            token0 in:  1/sqrtP' = 1/sqrtP + dx/L ,  dy = L * (sqrtP - sqrtP')
            token1 in:  sqrtP'   = sqrtP   + dy/L ,  dx = L * (1/sqrtP - 1/sqrtP')
        crossed_tick True means the result left the current tick and the
        caller must not trust the number.
        """
        if self.liquidity <= 0 or amount_in <= 0:
            return Decimal(0), False
        sp = self.sqrt_x64 / Q64
        eff = amount_in * (Decimal(1) - self.fee_rate)
        lo, hi = self.tick_bounds_x64()

        if zero_for_one:
            # Selling token0 -> price falls.
            dx_raw = eff * (Decimal(10) ** self.dec0)
            inv_new = (Decimal(1) / sp) + (dx_raw / self.liquidity)
            sp_new = Decimal(1) / inv_new
            dy_raw = self.liquidity * (sp - sp_new)
            out = dy_raw / (Decimal(10) ** self.dec1)
        else:
            # Selling token1 -> price rises.
            dy_raw = eff * (Decimal(10) ** self.dec1)
            sp_new = sp + (dy_raw / self.liquidity)
            dx_raw = self.liquidity * ((Decimal(1) / sp) - (Decimal(1) / sp_new))
            out = dx_raw / (Decimal(10) ** self.dec0)

        new_x64 = sp_new * Q64
        crossed = new_x64 < lo or new_x64 > hi
        return (out if out > 0 else Decimal(0)), crossed


@dataclass
class CetusVenue:
    """Cetus SUI/USDC, read straight from the pool object."""
    name: str = "cetus:sui-usdc"
    kind: str = AMM
    pool_id: str = SUI_USDC_POOL
    timeout_s: float = 20.0
    rpc_url: str = ""
    transport: httpx.AsyncBaseTransport | None = None

    state: PoolState | None = None
    coins: dict[str, tuple[str, int]] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None
    _last_err: str = ""
    reads: int = 0
    tick_refusals: int = 0

    # --- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        self.rpc_url = self.rpc_url or os.environ.get("NON_EVM_SUI_RPC_URL", "")
        if not self.rpc_url:
            raise CetusError("set NON_EVM_SUI_RPC_URL")
        self._client = httpx.AsyncClient(timeout=self.timeout_s,
                                         transport=self.transport)
        # Prove the coin types, the Sui analogue of ERC20 symbol()/decimals().
        for want, ct in (("SUI", SUI_COIN), ("USDC", USDC_COIN)):
            meta = await self._rpc("suix_getCoinMetadata", [ct])
            if not meta:
                raise CetusError(f"no coin metadata for {ct}")
            if str(meta.get("symbol", "")).upper() != want:
                raise CetusError(
                    f"{ct} reports symbol {meta.get('symbol')!r}, expected "
                    f"{want!r} -- wrong coin type")
            dec = meta.get("decimals")
            if not isinstance(dec, int) or not 0 <= dec <= 36:
                raise CetusError(f"{ct} implausible decimals {dec!r}")
            self.coins[want] = (ct, dec)
        await self.refresh()
        if self.state is None:
            raise CetusError(f"could not read pool {self.pool_id}: {self._last_err}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _rpc(self, method: str, params: list):
        assert self._client is not None
        try:
            r = await self._client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except httpx.HTTPError as e:
            raise CetusError(
                f"{method} unreachable ({type(e).__name__}: {str(e)[:70]})") from e
        if r.status_code != 200:
            raise CetusError(f"{method} HTTP {r.status_code}")
        j = r.json()
        if "error" in j:
            raise CetusError(f"{method}: {str(j['error'])[:120]}")
        return j.get("result")

    async def refresh(self) -> None:
        """One object read gives price, liquidity, fee and tick together."""
        try:
            res = await self._rpc("sui_getObject",
                                  [self.pool_id,
                                   {"showContent": True, "showType": True}])
            data = (res or {}).get("data")
            if not data:
                raise CetusError("pool object not returned")
            typ = data.get("type", "")
            f = data["content"]["fields"]

            ct0, d0 = self.coins.get("USDC", (USDC_COIN, 6))
            ct1, d1 = self.coins.get("SUI", (SUI_COIN, 9))
            # Confirm <X, Y> order from the declared type rather than assuming
            # it. Backwards means the price is inverted, which presents as an
            # enormous arbitrage rather than an error. Both sides are
            # normalised because the pool writes 0x2::sui::SUI while the coin
            # metadata gives the padded form -- two spellings of one type.
            params = tuple(normalize_coin_type(p) for p in generic_params(typ))
            want = (normalize_coin_type(ct0), normalize_coin_type(ct1))
            if params != want:
                raise CetusError(
                    f"pool type params {params} are not (USDC, SUI) {want}")

            self.state = PoolState(
                pool_id=self.pool_id, version=int(data.get("version", 0)),
                sqrt_x64=Decimal(f["current_sqrt_price"]),
                liquidity=Decimal(f["liquidity"]),
                fee_rate=Decimal(int(f["fee_rate"])) / FEE_DENOM,
                tick_spacing=int(f["tick_spacing"]),
                tick=_i32(f.get("current_tick_index")),
                paused=bool(f.get("is_pause", False)),
                coin0=ct0, coin1=ct1, dec0=d0, dec1=d1,
            )
            self.reads += 1
            self._last_err = ""
        except Exception as e:  # noqa: BLE001 -- one bad read is not fatal
            self._last_err = f"{type(e).__name__}: {str(e)[:110]}"

    # --- Venue protocol ---------------------------------------------------

    def state_id(self) -> Hashable:
        # The object version changes exactly when the pool changes, which is a
        # tighter state token than a checkpoint number: no version bump means
        # the quote is not merely fresh, it is identical.
        return self.state.version if self.state else None

    @property
    def exact_while_state_unchanged(self) -> bool:
        return True

    def healthy(self) -> bool:
        s = self.state
        return (s is not None and not self._last_err and not s.paused
                and s.liquidity > 0)

    def age_ms(self) -> float:
        return 0.0

    def fixed_cost_usd(self) -> Decimal:
        if self.state is None:
            return Decimal(0)
        sui_usd = Decimal(1) / self.state.price_1_per_0()
        return THEIR_GAS_SUI_HIGH * sui_usd

    def assets(self) -> set[str]:
        return set(self.coins)

    async def leg(self, asset_in: str, asset_out: str,
                  size_in: Decimal) -> QuotedLeg | None:
        s = self.state
        if s is None or not self.healthy():
            return None
        if asset_in not in self.coins or asset_out not in self.coins:
            return None
        if asset_in == asset_out or size_in <= 0:
            return None

        zero_for_one = (asset_in == "USDC")      # token0 is USDC
        out, crossed = s.quote_exact_in(size_in, zero_for_one)
        if crossed:
            # Liquidity past the tick boundary is unknown without walking the
            # tick_manager, and extrapolating would overstate output exactly
            # where size starts to matter.
            self.tick_refusals += 1
            self._last_err = (
                f"swap of {size_in} {asset_in} leaves the current tick; "
                "refusing rather than extrapolating unknown liquidity")
            return None
        if out <= 0:
            return None

        # Frictionless baseline is the pool's own marginal price, fee excluded.
        p10 = s.price_1_per_0()                  # SUI per USDC
        marginal = p10 if zero_for_one else (Decimal(1) / p10)

        pinned = {size_in: out}

        def quote_fn(amount: Decimal) -> Decimal:
            hit = pinned.get(amount)
            if hit is not None:
                return hit
            o, c = s.quote_exact_in(amount, zero_for_one)
            if c:
                raise CetusError(
                    f"{amount} {asset_in} leaves the current tick")
            return o

        return QuotedLeg(
            asset_in=asset_in, asset_out=asset_out,
            marginal_out_per_in=marginal,
            fee_rate=s.fee_rate,
            quote_fn=quote_fn,
            ts_local=time.time(),
            venue=self.name,
        )

    def status(self) -> str:
        s = self.state
        if s is None:
            return f"no pool state  ERR {self._last_err}"
        bits = [f"v{s.version}", f"1 SUI = {Decimal(1) / s.price_1_per_0():.6f} USDC",
                f"fee {s.fee_rate * Decimal(10000):.1f}bps",
                f"liq {s.liquidity:,.0f}", f"tick {s.tick}", f"reads {self.reads}"]
        if s.paused:
            bits.append("PAUSED")
        if self.tick_refusals:
            bits.append(f"tick-refusals {self.tick_refusals}")
        if self._last_err:
            bits.append(f"ERR {self._last_err}")
        return "  ".join(bits)


async def main() -> None:
    """Smoke test: PYTHONPATH=. python venues/cetus.py"""
    import asyncio
    v = CetusVenue()
    await v.connect()
    print(v.status())
    s = v.state
    assert s is not None
    lo, hi = s.tick_bounds_x64()
    print(f"\ntick {s.tick} (spacing {s.tick_spacing})  sqrt bounds "
          f"{lo:.4E} .. {hi:.4E}   current {s.sqrt_x64:.4E}")
    print("\nSUI -> USDC, exact within the tick:")
    for size in (Decimal("1"), Decimal("10"), Decimal("100"),
                 Decimal("1000"), Decimal("10000")):
        leg = await v.leg("SUI", "USDC", size)
        if leg is None:
            print(f"  {size:>8} SUI  REFUSED ({v._last_err[:60]})")
            continue
        out = leg.full(size).amount_out
        marg = leg.frictionless(size)
        print(f"  {size:>8} SUI -> {out:>12,.6f} USDC   "
              f"marginal {marg:>12,.6f}   impact+fee "
              f"{(out / marg - 1) * 10000:+7.2f}bps")
    await v.aclose()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
