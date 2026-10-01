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
            "WETH": ("0x4200000000000000000000000000000000000006", "WETH"),
            "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC"),
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


@dataclass
class UniV3Venue:
    """One chain, one fee tier, as a Venue."""
    chain: str
    tier: int = 500
    name: str = ""
    kind: str = AMM

    client: RpcClient | None = None
    registry: TokenRegistry | None = None
    deployment: Deployment | None = None
    pool: str = ""
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
        # Resolving the pool proves the pair exists at this tier on this
        # chain, rather than discovering it mid-run.
        self.pool = resolve_v3_pool(
            self.client, d.factory,
            self.registry["WETH"], self.registry["USDC"], self.tier)
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

        # Refresh gas off the leg we just built, so no extra price feed is
        # needed: the WETH/USDC marginal price IS the native token price.
        now = time.time()
        if now - self._gas_at > GAS_REFRESH_S:
            px = (leg.marginal_out_per_in if asset_in == "WETH"
                  else (Decimal(1) / leg.marginal_out_per_in
                        if leg.marginal_out_per_in > 0 else Decimal(0)))
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
        bits = [f"block {self.block:,}", f"gas ${self._gas_usd:.4f}"]
        if self._last_err:
            bits.append(f"ERR {self._last_err}")
        return "  ".join(bits)
