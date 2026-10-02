"""Uniswap v3 as a Venue, parameterised by chain.

Base, Arbitrum and any other chain with a v3 deployment are CONFIG, not code:
the same class serves all of them. Both were validated identically -- tokens
confirmed by on-chain symbol()/decimals(), pools resolved through
factory.getPool(), and the quoter's marginal price cross-checked against each
pool's own slot0 sqrtPriceX96 math (drift 0.013bps on Arbitrum, 0.03bps on
Base). Adding a third chain means adding a DEPLOYMENTS entry and letting
connect() prove it.

state_id is the block number. Pool state cannot change within a block, so a
quote taken earlier in the same block is exact, not stale, and the engine's
LegCache reuses it -- measured as 29 polls per requote on Base, which is ~8x
less RPC for 5x more evaluations.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Hashable

from core.venue import AMM
from legs import Leg
from venues.evm import (
    GAS_ONE_SWAP,
    selector,
    EvmError,
    RpcClient,
    TokenMeta,
    TokenRegistry,
    client_for,
    measure_gas_usd,
    resolve_v3_pool,
    v3_leg,
)


@dataclass(frozen=True)
class Deployment:
    """Validated v3 addresses and tokens for one chain."""
    chain: str
    factory: str
    quoter: str
    # local key -> (address, expected on-chain symbol)
    tokens: dict[str, tuple[str, str]]


# Every address here survived the full validation chain on the live chain.
# Nothing is trusted on sight: connect() re-proves all of it at startup, so a
# wrong entry fails loudly instead of quoting a market that is not there.
DEPLOYMENTS: dict[str, Deployment] = {
    "base": Deployment(
        chain="base",
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        tokens={
            # Every entry below was DISCOVERED and then PROVED, never guessed:
            #   1. factory PoolCreated logs filtered on USDC -> 10,081 pools
            #   2. liquidity() on each -> 1,920 with any liquidity at all
            #   3. symbol()/decimals() on the counterparty token
            #   4. intersect with Coinbase's 404 tradable USDC spot pairs
            #   5. PRICE CROSS-CHECK: the pool's own slot0 mid against the
            #      Coinbase price, keeping only |ratio-1| <= 10%
            #
            # Step 5 is load-bearing, not a nicety. Symbol matching is an
            # IDENTITY GUESS, and the census found 124 distinct Base addresses
            # claiming the symbol of a Coinbase asset for only 75 assets. A
            # whole batch of them -- tokens calling themselves BTC, DOGE,
            # PEPE, SHIB, BONK, TRUMP -- sit in freshly seeded 10000-tier
            # pools with identical liquidity and prices around 1e-8 USDC.
            # They are not those assets. A wrong address does not announce
            # itself: it returns bytes that decode into a plausible price.
            #
            # The check also has a KNOWN BLIND SPOT: it cannot separate two
            # tokens that are both worth about a dollar. The census found a
            # second address reporting symbol USDT at ratio 1.0003, and the
            # entry below is the previously validated one, kept deliberately
            # -- a prior proof outranks a tiebreak on closeness to 1.
            #
            # Volumes are Coinbase 24h USD at census time, for ordering only;
            # connect() re-proves every address at startup regardless.
            "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC"),
            "WETH": ("0x4200000000000000000000000000000000000006", "WETH"),   # $234.3M/24h
            "LINK": ("0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", "LINK"),   # $18.0M/24h
            "AAVE": ("0x63706e401c06ac8513145b7687A14804d17f814b", "AAVE"),   # $15.9M/24h
            "UNI": ("0xc3De830EA07524a0761646a6a4e4be0e114a3C83", "UNI"),   # $14.5M/24h
            "ZRO": ("0x6985884C4392D348587B19cb9eAAf157F13271cd", "ZRO"),   # $13.7M/24h
            "USDT": ("0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "USDT"),   # $12.5M/24h
            "EURC": ("0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", "EURC"),   # $10.7M/24h
            "TAO": ("0xf3081494B87e8D5fb7960f066E931D1D0e6E3d67", "TAO"),   # $9.9M/24h
            "AERO": ("0x940181a94A35A4569E4529A3CDfB74e38FD98631", "AERO"),   # $7.2M/24h
            "VVV": ("0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf", "VVV"),   # $3.9M/24h
            "DRV": ("0x9d0E8f5b25384C7310CB8C6aE32C8fbeb645d083", "DRV"),   # $2.9M/24h
            "MORPHO": ("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842", "MORPHO"),   # $2.5M/24h
            "CRV": ("0x8Ee73c484A26e0A5df2Ee2a4960B789967dd0415", "CRV"),   # $1.7M/24h
            "CBETH": ("0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "cbETH"),   # $1.4M/24h
            "SPX": ("0x50dA645f148798F68EF2d7dB7C1CB22A6819bb2C", "SPX"),   # $1.2M/24h
            "XCN": ("0x9c632E6Aaa3eA73f91554f8A3cB2ED2F29605e0C", "XCN"),   # $1.2M/24h
            "PENDLE": ("0xA99F6e6785Da0F5d6fB42495Fe424BCE029Eeb3E", "PENDLE"),   # $0.9M/24h
            "VIRTUAL": ("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "VIRTUAL"),   # $0.7M/24h
            "BASECAT": ("0xB2000000000000000000004c27f6523082f41D01", "Basecat"),   # $0.6M/24h
            "KTA": ("0xc0634090F2Fe6c6d75e61Be2b949464aBB498973", "KTA"),   # $0.6M/24h
            "COMP": ("0x9e1028F5F1D5eDE59748FFceE5532509976840E0", "COMP"),   # $0.5M/24h
            "B3": ("0xB3B32F9f8827D4634fE7d973Fa1034Ec9fdDB3B3", "B3"),   # $0.4M/24h
            "ZEN": ("0xf43eB8De897Fbc7F2502483B2Bef7Bb9EA179229", "ZEN"),   # $0.3M/24h
            "EDGE": ("0xED6E000dEF95780fb89734c07EE2ce9F6dcAf110", "EDGE"),   # $0.3M/24h
            "TOSHI": ("0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4", "TOSHI"),   # $0.3M/24h
            "COOKIE": ("0xC0041EF357B183448B235a8Ea73Ce4E4eC8c265F", "COOKIE"),   # $0.2M/24h
            "SUP": ("0xa69f80524381275A7fFdb3AE01c54150644c8792", "SUP"),   # $0.2M/24h
            "PRO": ("0x18dD5B087bCA9920562aFf7A0199b96B9230438b", "PRO"),   # $0.2M/24h
            "SUSHI": ("0x7D49a065D17d6d4a55dc13649901fdBB98B2AFBA", "SUSHI"),   # $0.1M/24h
            "KAT": ("0xD5390300c5DB71F80d46f0fA9983Fc72D4d1e3da", "KAT"),   # $0.1M/24h
            "PROS": ("0x8B7DdE054BE9D180c1Be7FaE0874697374A49832", "PROS"),   # $0.1M/24h
            "LMTS": ("0x9EadbE35F3Ee3bF3e28180070C429298a1b02F93", "LMTS"),   # $0.1M/24h
            "RSR": ("0xaB36452DbAC151bE02b16Ca17d8919826072f64a", "RSR"),   # $0.1M/24h
            "KAITO": ("0x98d0baa52b2D063E780DE12F615f963Fe8537553", "KAITO"),   # $0.1M/24h
            "KEYCAT": ("0x9a26F5433671751C3276a065f57e5a02D2817973", "KEYCAT"),   # $0.1M/24h
            "OPG": ("0xFbC2051AE2265686a469421b2C5A2D5462FbF5eB", "OPG"),   # $0.1M/24h
            "1INCH": ("0xc5fecC3a29Fb57B5024eEc8a2239d4621e111CBE", "1INCH"),   # $0.0M/24h
            "DEGEN": ("0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "DEGEN"),   # $0.0M/24h
            "FAI": ("0xb33Ff54b9F7242EF1593d2C9Bcd8f9df46c77935", "FAI"),   # $0.0M/24h
            "MOG": ("0x2Da56AcB9Ea78330f947bD57C54119Debda7AF71", "Mog"),   # $0.0M/24h
            "RNBW": ("0xa53887F7e7c1bf5010b8627F1C1ba94fE7a5d6E0", "RNBW"),   # $0.0M/24h
            "CHECK": ("0x9126236476eFBA9Ad8aB77855c60eB5BF37586Eb", "CHECK"),   # $0.0M/24h
            "SOL": ("0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82", "SOL"),   # kept: validated earlier
            "cbBTC": ("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "cbBTC"),   # kept: validated earlier
        },
    ),
    "arbitrum": Deployment(
        chain="arbitrum",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        tokens={
            "WETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "WETH"),
            "USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "USDC"),
        },
    ),
}

# Gas price and native-token price move far slower than the poll loop.
GAS_REFRESH_S = 60.0

SEL_LIQUIDITY = selector("liquidity()")

# Canonical Uniswap v3 tiers. Other factories on the same chain use arbitrary
# fee values, which is a useful tell that a pool is not ours.
CANDIDATE_TIERS = (100, 500, 3000, 10000)

# How the native token is priced when the venue's own pair is not WETH/USDC.
# The 500 tier is the deep WETH/USDC pool on every chain here, and a small
# probe keeps the marginal price free of impact.
NATIVE_PRICE_TIER = 500
NATIVE_PRICE_SIZE_WETH = Decimal("0.01")


@dataclass
class UniV3Venue:
    """One chain, one fee tier, as a Venue."""
    chain: str
    # 0 means CHOOSE: resolve every canonical tier and take the one with the
    # most liquidity. The right tier is a property of the pair, not a global
    # default -- SOL/USDC on Base has pools at 100 and 500 with ZERO
    # liquidity and only 3000 is usable, so a fixed 500 made the quoter
    # revert on a pair that trades perfectly well.
    tier: int = 0
    # Which pair connect() proves exists. leg() was already generic -- this
    # was the only thing pinning the venue to WETH/USDC.
    base_symbol: str = "WETH"
    quote_symbol: str = "USDC"
    # Notional (in quote units) at which tiers are compared. Pick it near the
    # size you intend to trade: the best tier is size-dependent, since fee is
    # flat and slippage is not.
    select_size_quote: Decimal = Decimal("1000")
    name: str = ""
    kind: str = AMM

    client: RpcClient | None = None
    registry: TokenRegistry | None = None
    deployment: Deployment | None = None
    pool: str = ""
    liquidity: int = 0
    tier_choice: str = ""
    block: int = 0
    _gas_usd: Decimal = Decimal(0)
    _gas_at: float = 0.0
    _last_err: str = ""
    _native_usd: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"univ3:{self.chain}:{self.tier}"

    # --- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        d = DEPLOYMENTS.get(self.chain)
        if d is None:
            raise EvmError(
                f"no validated v3 deployment for {self.chain!r}; known: "
                f"{', '.join(sorted(DEPLOYMENTS))}. Validate before quoting."
            )
        self.deployment = d
        self.client = client_for(self.chain)
        self.client.verify_chain_id()
        if not self.client.has_code(d.quoter):
            raise EvmError(f"no contract at quoter {d.quoter} on {self.chain}")
        self.registry = TokenRegistry(self.client)
        self.registry.validate(d.tokens)
        for sym in (self.base_symbol, self.quote_symbol):
            if sym not in self.registry.tokens:
                raise EvmError(
                    f"{sym} is not a validated token on {self.chain}; add it "
                    "to DEPLOYMENTS after proving its address on chain")
        # Resolving the pool proves the pair exists on this chain, rather
        # than discovering it mid-run. A RESOLVED POOL IS NOT A LIQUID POOL:
        # getPool returns a real address for pools that were created and never
        # funded, and the quoter then reverts. So liquidity is checked too, and
        # a zero-liquidity pool is refused rather than quoted.
        base_t = self.registry[self.base_symbol]
        quote_t = self.registry[self.quote_symbol]
        # Choose on COST AT SIZE, not on raw liquidity. Picking the deepest
        # pool sounds right and is not: on Base the WETH/USDC 3000 tier holds
        # far more liquidity than the 500 tier, but 3000 charges 30bps of fee
        # and made every WETH route ~2bps worse than a fixed 500 had. Depth
        # only matters in so far as it reduces slippage, and a tier wins only
        # if fee PLUS slippage together come out ahead at the size we intend
        # to trade. Zero-liquidity pools fall out of this for free: they quote
        # nothing and so can never win.
        tiers = (self.tier,) if self.tier else CANDIDATE_TIERS
        best: tuple[int, Decimal, str, int] | None = None
        tried: list[str] = []
        for tier in tiers:
            try:
                pool = resolve_v3_pool(self.client, d.factory, base_t,
                                       quote_t, tier)
            except EvmError as e:
                tried.append(f"{tier}: {str(e)[:48]}")
                continue
            try:
                liq = int.from_bytes(
                    self.client.call(pool, SEL_LIQUIDITY)[:32], "big")
            except EvmError:
                tried.append(f"{tier}: liquidity() failed")
                continue
            if liq <= 0:
                tried.append(f"{tier}: empty")
                continue
            try:
                probe = v3_leg(self.client, d.quoter, quote_t, base_t,
                               tier, self.select_size_quote)
                out = probe.full(self.select_size_quote).amount_out
            except EvmError as e:
                tried.append(f"{tier}: quote failed")
                continue
            tried.append(f"{tier}: out={out:.6g}")
            if out > 0 and (best is None or out > best[1]):
                best = (tier, out, pool, liq)
        if best is None:
            raise EvmError(
                f"no usable v3 pool for {self.base_symbol}/{self.quote_symbol} "
                f"on {self.chain} -- tried [{'; '.join(tried)}]")
        self.tier, _, self.pool, self.liquidity = best
        self.tier_choice = "; ".join(tried)
        self.name = f"univ3:{self.chain}:{self.tier}"
        self.block = self.client.block_number()

    async def aclose(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    async def refresh(self) -> None:
        """One cheap eth_blockNumber decides whether quotes need redoing."""
        if self.client is None:
            return
        try:
            # to_thread: block_number() is synchronous httpx. Called inline it
            # blocks the event loop and starves any streaming venue alongside.
            self.block = await asyncio.to_thread(self.client.block_number)
            self._last_err = ""
        except EvmError as e:
            self._last_err = str(e)[:120]

    # --- Venue protocol ---------------------------------------------------

    def state_id(self) -> Hashable:
        return self.block

    @property
    def exact_while_state_unchanged(self) -> bool:
        # Pool state is block-atomic: within a block the quote IS the answer.
        return True

    def healthy(self) -> bool:
        return (self.client is not None and self.registry is not None
                and self.block > 0 and not self._last_err)

    def age_ms(self) -> float:
        # Meaningless for a block-atomic venue; the engine judges it on
        # state_id. Reported as zero rather than fabricating a number.
        return 0.0

    def fixed_cost_usd(self) -> Decimal:
        return self._gas_usd

    def assets(self) -> set[str]:
        return set(self.registry.tokens) if self.registry else set()

    def token(self, symbol: str) -> TokenMeta | None:
        if self.registry is None:
            return None
        return self.registry.tokens.get(symbol)

    async def _native_usd_price(self, leg, asset_in: str,
                                asset_out: str) -> Decimal:
        """USD per native token, for costing gas.

        Free when the leg in hand is already WETH/USDC -- its marginal rate is
        exactly that price, one way up or the other. Otherwise it costs one
        extra quote a minute, which is the correct price to pay rather than
        reusing a rate denominated in some other asset.
        """
        m = getattr(leg, "marginal_out_per_in", Decimal(0)) or Decimal(0)
        pair = {asset_in, asset_out}
        if pair == {"WETH", "USDC"} and m > 0:
            return m if asset_in == "WETH" else Decimal(1) / m
        weth, usdc = self.token("WETH"), self.token("USDC")
        if weth is None or usdc is None or self.deployment is None:
            return Decimal(0)
        try:
            probe = await asyncio.to_thread(
                v3_leg, self.client, self.deployment.quoter, weth, usdc,
                NATIVE_PRICE_TIER, NATIVE_PRICE_SIZE_WETH)
        except EvmError:
            return Decimal(0)
        px = getattr(probe, "marginal_out_per_in", Decimal(0)) or Decimal(0)
        return px if px > 0 else Decimal(0)

    async def leg(self, asset_in: str, asset_out: str,
                  size_in: Decimal) -> Leg | None:
        if self.client is None or self.registry is None or self.deployment is None:
            return None
        ti, to = self.token(asset_in), self.token(asset_out)
        if ti is None or to is None or size_in <= 0:
            return None
        try:
            leg = await asyncio.to_thread(
                v3_leg, self.client, self.deployment.quoter, ti, to,
                self.tier, size_in)
        except EvmError as e:
            self._last_err = str(e)[:120]
            return None

        # Gas is priced in the NATIVE token, so costing it needs a WETH/USDC
        # price -- and reading it off whatever leg we happen to have built is
        # only valid when that leg IS the WETH/USDC pair. For any other pair
        # the marginal rate is in the wrong units entirely, and the error does
        # not look like an error: screening MOG/USDC, whose marginal is ~9e6
        # MOG per USDC, inverted to 1.1e-7 and was passed off as the price of
        # ETH, which reported gas as -960bps of a $100 trade. The ranking that
        # number feeds would have been quietly wrong for every non-WETH asset.
        now = time.time()
        if now - self._gas_at > GAS_REFRESH_S:
            px = await self._native_usd_price(leg, asset_in, asset_out)
            if px > 0:
                try:
                    self._gas_usd = await asyncio.to_thread(
                        measure_gas_usd, self.client, GAS_ONE_SWAP, px)
                    self._native_usd = px
                    self._gas_at = now
                except EvmError:
                    pass
        return leg

    def status(self) -> str:
        bits = [f"block {self.block:,}", f"tier {self.tier}",
                f"liq {self.liquidity:,}", f"gas ${self._gas_usd:.4f}"]
        if self.tier_choice:
            bits.append(f"tiers[{self.tier_choice}]")
        if self._last_err:
            bits.append(f"ERR {self._last_err}")
        return "  ".join(bits)
