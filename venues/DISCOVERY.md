# Venue discovery log

What was found on each chain, and how. Nothing here came from model memory:
every address was discovered from on-chain data and then proved by calling
`symbol()`/`decimals()` on it, resolving its pool through a factory, or
cross-checking a quote against the pool's own `slot0` arithmetic.

Recorded because the costly part of adding a venue is not the code, it is
finding out what is actually deployed and which assets actually exist.

## Method

Guessing a token address is unsafe in a way that does not announce itself: a
wrong address returns bytes that decode cleanly into a plausible, wrong
price. So addresses are discovered, never assumed:

1. `eth_getLogs` for `Swap(address,address,int256,int256,uint160,uint128,int24)`
   over recent blocks. Every emitting contract IS a live v3-style pool.
2. `pool.token0()` / `token1()` / `fee()` / `factory()` on those pools, which
   yields the real factory and the real token addresses together.
3. `PoolCreated` logs on that factory to enumerate every token that has a
   pool, which answers "does asset X exist here" definitively.
4. `symbol()` + `decimals()` on each, so a wrong address fails loudly.

Providers cap log response size, so step 3 needs chunking and some windows
fail; the token census is therefore a lower bound, which is fine for proving
presence and misleading for proving absence. Absence was confirmed instead by
the most-paired-token census being dominated by other assets.

## base (chain_id 8453)

Uniswap v3 at the canonical addresses. Validated: tokens by symbol/decimals,
pools via `factory.getPool()`, and the quoter cross-checked against each
pool's own `slot0` sqrtPriceX96 price to within 0.03bps across the 100/500/3000
tiers.

| | |
|---|---|
| factory | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` |
| QuoterV2 | `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |
| WETH | `0x4200000000000000000000000000000000000006` (18dp) |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (6dp) |

Block time ~2.0s (measured median 1994ms). Gas for two swaps ~$0.004.

## arbitrum (chain_id 42161)

Uniswap v3 at canonical addresses. Same validation; quoter-vs-slot0 drift
0.013bps across three tiers.

| | |
|---|---|
| factory | `0x1F98431c8aD98523631AE4a59f267346ea31F984` |
| QuoterV2 | `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` |
| WETH | `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1` (18dp) |
| USDC | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` (6dp) |

Gas for one swap ~$0.010.

## robinhood (chain_id 4663)

A Uniswap fork, NOT at canonical addresses, and the headline finding is about
assets rather than code.

**USDC does not exist on this chain.** The census over 59,879 `PoolCreated`
events covering 58,377 distinct tokens found no USDC, no USDT and no DAI. The
chain's stablecoin is **USDG** (Global Dollar, 6dp) with 3,019 pools. So the
ETH-USDC pair cannot be built here at all; the pair that exists is WETH/USDG.

USDG is not USDC. Treating them as interchangeable is exactly the unflagged
basis this project guards against, and it is worse here than the USDC/USD case
on Coinbase: there, both legs were on one venue and the basis measured
0.00bps, whereas USDG/USDC has no market on this chain to measure against at
all. The framework handles this correctly without special-casing -- the venue
does not report USDC in `assets()`, so `Engine.routes()` leaves it out of
ETH-USDC routes.

The canonical Uniswap factory address DOES hold code here (2,109 bytes), which
is a trap: it is not a v3 factory. `feeAmountTickSpacing()` and `owner()` both
return nothing, and a real v3 factory is ~23KB. Trusting the address because
it had code would have produced quotes from an unrelated contract.

| | |
|---|---|
| v3-style factory | `0x1f7d7550B1b028f7571E69A784071F0205FD2EfA` |
| v2-style factory | `0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f` |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` (18dp, 53,364 pools) |
| USDG | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` (6dp, 3,019 pools) |
| **QuoterV2** | **not found** |

WETH/USDG v3 pools, with mids from each pool's own `slot0`:

| fee | pool | slot0 mid (USDG/WETH) |
|---|---|---|
| 100 | `0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca` | 2,695.97 |
| 500 | `0x69BfaF19C9f377BB306a89aEd9F6B07e2c1a8d9a` | 2,696.93 |
| 3000 | `0xa9188730Fe85Be88ad499D7d52B099e800fB0334` | 2,699.35 |
| 10000 | `0x5f009E071F07e92B6C624e83F52F17bBDa34680D` | 2,691.91 |

**No working quoter was found.** Both canonical quoter addresses hold code
that returns 0 bytes for `quoteExactInputSingle`, i.e. they are something
else. Without a quoter the v3 pools here yield a marginal price (from slot0)
but NOT an executable quote including tick-crossing impact. A venue that can
only price the margin must say so: reporting slippage as zero understates cost
and manufactures edge at size. Do not add these v3 pools as a quoting venue
until either the fork's own quoter is located or impact is costed another way.

The v2-style pair needs no quoter, because constant-product math is exact and
`ConstantProductLeg` already implements it:

| | |
|---|---|
| WETH/USDG pair | `0x8803c117ccae7B5146297876c2A25DF135141C4d` |
| reserves | 77.71 WETH / 210,051 USDG |
| implied mid | 2,702.92 USDG/WETH |

That pool is ~$210k a side, and the depth decides whether it is worth
anything: selling 1 WETH (~$2.7k, about 1.3% of the reserve) returns 2,660.67
USDG against a marginal 2,702.92 -- **156bps of slippage on a single ETH**.
At 4 WETH it is 516bps. Any edge this pool appears to offer is consumed by
impact long before a meaningful size, so its practical capacity is a few
hundred dollars. Worth logging for completeness, not worth trading.

## sui

Cetus, per instruction. Not yet implemented: Sui's Move object model has no
`eth_call`, so it needs its own leg type rather than a reuse of the EVM
adapter. Endpoint verified live: `sui_getChainIdentifier` returns `35834a8a`
and checkpoints advance.

## Held

**solana** and **arc** deferred by instruction. Solana's endpoint is verified
(`getSlot`, `getHealth` both fine); arc answers `eth_chainId` 5042 and its
blocks advance, but no DEX deployment has been looked for on it.
