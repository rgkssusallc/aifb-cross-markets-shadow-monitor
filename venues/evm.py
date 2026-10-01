"""EVM adapter: one client for Base, Arbitrum, Optimism and friends.

Read-only. Raw batched JSON-RPC over httpx with eth_abi for codec work, and
no signing key anywhere in the module -- like venues/coinbase.py, this cannot
place a trade even by accident. PRESERVE THAT PROPERTY.

Two design choices worth the explanation:

WE DO NOT REIMPLEMENT TICK MATH. Uniswap v3 price impact lives in crossing
initialized ticks and sqrtPriceX96 Q96 arithmetic, and a subtle error there
produces quotes that look plausible and are wrong -- the worst possible
failure for this project. Instead we ask the chain: QuoterV2 is called at the
real notional for the executable amount, and again at a tiny notional to
recover the marginal price. Dividing the pool fee back out of the tiny quote
gives the frictionless baseline that netedge.py needs to separate fee cost
from slippage. The pool's own code is the only quoter that is definitionally
correct.

EVERY ADDRESS IS PROVEN BEFORE USE. A wrong token address does not error --
it silently returns garbage that decodes cleanly, which is how a backtest
ends up trading a path that does not exist. So: tokens are confirmed by
on-chain symbol() and decimals(), contracts are confirmed to have code, pools
are resolved through factory.getPool() rather than trusted from a list, and
the RPC's own eth_chainId is checked against the chain we think we asked for.
Any mismatch raises. None of the addresses in CANDIDATES below are treated as
facts; they are starting guesses that must survive validation.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Sequence

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from legs import ConstantProductLeg, LegQuote, QuotedLeg

# Dividing the real size down by this gives the "tiny" probe used for the
# marginal price. Too large and price impact contaminates the baseline; too
# small and integer truncation in the pool's own math becomes the dominant
# term. 1e-4 of the real size keeps impact well under a basis point for any
# pool deep enough to be worth trading.
TINY_DIVISOR = 10_000

# Below this many base units a quote is mostly rounding noise, so we refuse to
# derive a marginal price from it rather than returning a confident wrong one.
MIN_TINY_UNITS = 1_000


def selector(signature: str) -> bytes:
    """First 4 bytes of keccak(signature), e.g. 'decimals()' -> 0x313ce567."""
    return keccak(text=signature)[:4]


SEL_DECIMALS = selector("decimals()")
SEL_SYMBOL = selector("symbol()")
SEL_GET_RESERVES = selector("getReserves()")
SEL_TOKEN0 = selector("token0()")
SEL_TOKEN1 = selector("token1()")
SEL_GET_POOL = selector("getPool(address,address,uint24)")
SEL_QUOTE_V3 = selector(
    "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)

ZERO_ADDRESS = "0x" + "00" * 20


class EvmError(RuntimeError):
    """Any failure that must not be papered over with a default value."""


# --- chain registry -------------------------------------------------------

@dataclass(frozen=True)
class Chain:
    """A chain we can quote on.

    chain_id is checked against the RPC's own eth_chainId at startup, which is
    what catches an RPC URL pointing at a different network than intended --
    an error that otherwise yields quotes for the wrong market entirely.
    """
    name: str
    chain_id: int
    # Fixed USD cost of landing one swap transaction here. This is the term
    # that sets the minimum profitable size, so a wrong value here silently
    # moves the threshold below which every on-chain path loses money.
    gas_usd: Decimal
    # False means gas_usd is an UNMEASURED placeholder. Nothing stops you
    # quoting with it, but no on-chain go/no-go decision should rest on it
    # until it has been measured on the actual swap path.
    gas_measured: bool = False


# chain_id values below were read from each RPC's own eth_chainId, not from
# memory or a block explorer. ARC and ROBINHOOD were unknown to this project
# until measured; both are real and distinct.
BASE = Chain("base", 8453, Decimal("0.02"))
ARBITRUM = Chain("arbitrum", 42161, Decimal("0.05"))
OPTIMISM = Chain("optimism", 10, Decimal("0.03"))
ARC = Chain("arc", 5042, Decimal("0.02"))
ROBINHOOD = Chain("robinhood", 4663, Decimal("0.02"))

CHAINS = {c.name: c for c in (BASE, ARBITRUM, OPTIMISM, ARC, ROBINHOOD)}

# Where each chain's RPC URL lives in the environment. Keys are never read
# from the chain name directly, so a typo gives a clear "not configured"
# error instead of falling back to some other chain's endpoint.
RPC_ENV_VARS: dict[str, tuple[str, ...]] = {
    "base": ("EVM_BASE_RPC_URL",),
    "arbitrum": ("EVM_ARB_RPC_URL",),
    "optimism": ("EVM_OPT_RPC_URL",),
    "arc": ("EVM_ARC_RPC_URL",),
    "robinhood": ("EVM_ROBIN_RPC_URL",),
}

# Non-EVM endpoints live here for reference; they need their own adapters and
# cannot be driven through RpcClient (no eth_call).
NON_EVM_ENV_VARS: dict[str, tuple[str, ...]] = {
    "solana": ("NON_EVM_SOL_RPC_URL",),
    "sui": ("NON_EVM_SUI_RPC_URL",),
}


def rpc_url_for(chain_name: str) -> str:
    """The configured RPC URL for a chain, or a loud error naming the var.

    A generic EVM_RPC_URL is accepted as a last resort, but only when no
    chain-specific variable is set -- otherwise a single stale generic value
    would silently serve every chain, which is exactly the wrong-network
    failure verify_chain_id() exists to catch.
    """
    names = RPC_ENV_VARS.get(chain_name, ())
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    generic = os.environ.get("EVM_RPC_URL")
    if generic:
        return generic
    wanted = " or ".join(names) if names else f"(no variable mapped for {chain_name!r})"
    raise EvmError(
        f"no RPC URL configured for {chain_name}: set {wanted} "
        "(or EVM_RPC_URL) in the environment"
    )


def client_for(chain_name: str, **kw: Any) -> RpcClient:
    """Build a client for a named chain from the environment."""
    chain = CHAINS.get(chain_name)
    if chain is None:
        raise EvmError(
            f"unknown chain {chain_name!r}; known: {', '.join(sorted(CHAINS))}"
        )
    return RpcClient(url=rpc_url_for(chain_name), chain=chain, **kw)

# Starting guesses ONLY. validate() proves or rejects each one against the
# chain; nothing here is relied upon until it has. Symbols are what we expect
# the address to report -- a mismatch means the address is wrong, not that the
# expectation is.
CANDIDATES: dict[str, dict[str, tuple[str, str]]] = {
    "base": {
        # Both confirmed live by on-chain symbol() and decimals().
        "WETH": ("0x4200000000000000000000000000000000000006", "WETH"),
        "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC"),
    },
}


@dataclass(frozen=True)
class V3Deployment:
    """Uniswap v3 contract addresses for one chain."""
    factory: str
    quoter: str


# Base's addresses were validated by a chain that fails loudly if any link is
# wrong: both have code, factory.getPool() returns non-zero pools at the 100,
# 500 and 3000 fee tiers, each pool's token0()/token1() match WETH/USDC, and
# the quoter's marginal price agreed with the pool's OWN slot0 sqrtPriceX96
# price to within 0.03bps. That last check is the important one -- it is an
# independent code path, so agreement to a fraction of a basis point confirms
# the QuoterV2 struct field order, the decimals handling and the fee division
# all at once. A wrong field order would be off by orders of magnitude.
V3_DEPLOYMENTS: dict[str, V3Deployment] = {
    "base": V3Deployment(
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    ),
}


def v3_deployment(chain_name: str) -> V3Deployment:
    """Validated v3 addresses for a chain, or a loud error.

    Unlisted chains raise rather than falling back to another chain's
    addresses, which on a different network would be either a codeless
    address or, worse, an unrelated live contract.
    """
    d = V3_DEPLOYMENTS.get(chain_name)
    if d is None:
        raise EvmError(
            f"no validated Uniswap v3 deployment recorded for {chain_name!r}; "
            f"known: {', '.join(sorted(V3_DEPLOYMENTS)) or '(none)'}. "
            "Resolve and validate the addresses before quoting."
        )
    return d


# --- JSON-RPC -------------------------------------------------------------

@dataclass
class RpcClient:
    """Batched read-only JSON-RPC.

    Batching matters for correctness, not just speed: quoting a path leg by
    leg over separate round trips spreads the reads across hundreds of
    milliseconds and several blocks, which is exactly the timestamp skew that
    netedge.py rejects. One batch is one point in time.
    """
    url: str
    chain: Chain
    timeout_s: float = 10.0
    _client: httpx.Client | None = field(default=None, repr=False)
    last_rtt_ms: float = 0.0

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _post(self, payload: Any) -> Any:
        assert self._client is not None
        t0 = time.perf_counter()
        try:
            resp = self._client.post(self.url, json=payload)
        except httpx.HTTPError as e:
            raise EvmError(f"{self.chain.name} RPC transport failure: {e}") from e
        self.last_rtt_ms = (time.perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            raise EvmError(
                f"{self.chain.name} RPC HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def rpc(self, method: str, params: list[Any]) -> Any:
        out = self._post({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params})
        if "error" in out:
            raise EvmError(f"{method} failed: {out['error']}")
        return out["result"]

    def batch_call(
        self, calls: Sequence[tuple[str, bytes]], block: str = "latest"
    ) -> list[bytes]:
        """eth_call a list of (to, calldata), returning raw return data.

        A revert is raised, never returned as empty bytes. An empty result
        decodes as a zero amount, which reads as "no liquidity" instead of
        "the call is broken" -- a distinction worth a loud failure.
        """
        if not calls:
            return []
        payload = [
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_call",
                "params": [{"to": to_checksum_address(to), "data": "0x" + data.hex()},
                           block],
            }
            for i, (to, data) in enumerate(calls)
        ]
        raw = self._post(payload)
        if not isinstance(raw, list):
            raise EvmError(f"expected a batch response, got {type(raw).__name__}")
        if len(raw) != len(calls):
            raise EvmError(
                f"batch size mismatch: sent {len(calls)}, got {len(raw)}"
            )

        # A JSON-RPC batch response may arrive in any order.
        by_id = {item.get("id"): item for item in raw}
        out: list[bytes] = []
        for i, (to, data) in enumerate(calls):
            item = by_id.get(i)
            if item is None:
                raise EvmError(f"batch response missing id {i}")
            if "error" in item:
                raise EvmError(
                    f"eth_call to {to} (0x{data[:4].hex()}) reverted: {item['error']}"
                )
            result = item.get("result")
            if not isinstance(result, str) or not result.startswith("0x"):
                raise EvmError(f"eth_call to {to} returned {result!r}")
            out.append(bytes.fromhex(result[2:]))
        return out

    def call(self, to: str, data: bytes, block: str = "latest") -> bytes:
        return self.batch_call([(to, data)], block=block)[0]

    def block_number(self) -> int:
        return int(self.rpc("eth_blockNumber", []), 16)

    def verify_chain_id(self) -> None:
        """Confirm the RPC serves the chain we think it does."""
        got = int(self.rpc("eth_chainId", []), 16)
        if got != self.chain.chain_id:
            raise EvmError(
                f"RPC at {self.url.split('/')[2]} reports chain_id {got}, "
                f"but it was configured as {self.chain.name} "
                f"({self.chain.chain_id}). Quotes would be for the wrong network."
            )

    def has_code(self, address: str) -> bool:
        code = self.rpc("eth_getCode", [to_checksum_address(address), "latest"])
        return isinstance(code, str) and len(code) > 2


# --- decoding helpers -----------------------------------------------------

def _decode_uint(data: bytes) -> int:
    if len(data) < 32:
        raise EvmError(f"expected a uint256, got {len(data)} bytes")
    return int.from_bytes(data[:32], "big")


def _decode_address(data: bytes) -> str:
    if len(data) < 32:
        raise EvmError(f"expected an address, got {len(data)} bytes")
    return to_checksum_address("0x" + data[12:32].hex())


def _decode_string(data: bytes) -> str:
    """ERC20 symbol(), tolerating the old bytes32 form.

    Several long-lived tokens return a fixed bytes32 rather than a dynamic
    string. Guessing wrong here would make a correct address look invalid and
    get it rejected, so both encodings are handled.
    """
    try:
        return abi_decode(["string"], data)[0]
    except Exception:  # noqa: BLE001 -- fall back to the bytes32 convention
        return data[:32].rstrip(b"\x00").decode("utf-8", errors="replace")


def to_units(amount: Decimal, decimals: int) -> int:
    """Human amount -> integer base units, truncating like the chain does."""
    return int(amount * (Decimal(10) ** decimals))


def from_units(units: int, decimals: int) -> Decimal:
    """Integer base units -> human amount.

    Always via the token's validated decimals. Assuming 18 is the single most
    common way to be wrong by a factor of 10^12, and USDC -- 6 decimals -- is
    on one side of nearly every path here.
    """
    return Decimal(units) / (Decimal(10) ** decimals)


# --- tokens ---------------------------------------------------------------

@dataclass(frozen=True)
class TokenMeta:
    symbol: str
    address: str
    decimals: int


@dataclass
class TokenRegistry:
    """Tokens that have been proven to exist and to be what we expected."""
    client: RpcClient
    tokens: dict[str, TokenMeta] = field(default_factory=dict)

    def validate(self, expected: dict[str, tuple[str, str]]) -> dict[str, TokenMeta]:
        """Confirm each candidate on-chain. Raises on the first mismatch.

        expected maps a local key to (address, expected_symbol). Both the
        symbol and the presence of code are checked, so a plausible-looking
        but wrong address cannot survive into a quote.
        """
        keys = list(expected)
        calls: list[tuple[str, bytes]] = []
        for k in keys:
            addr = to_checksum_address(expected[k][0])
            calls.append((addr, SEL_SYMBOL))
            calls.append((addr, SEL_DECIMALS))

        results = self.client.batch_call(calls)
        problems: list[str] = []
        found: dict[str, TokenMeta] = {}

        for i, k in enumerate(keys):
            addr, want_symbol = expected[k]
            addr = to_checksum_address(addr)
            try:
                symbol = _decode_string(results[2 * i])
                decimals = _decode_uint(results[2 * i + 1])
            except EvmError as e:
                problems.append(f"{k} at {addr}: undecodable metadata ({e})")
                continue
            if symbol.strip().upper() != want_symbol.strip().upper():
                problems.append(
                    f"{k} at {addr}: chain says symbol {symbol!r}, "
                    f"expected {want_symbol!r} -- wrong address"
                )
                continue
            if not 0 <= decimals <= 36:
                problems.append(f"{k} at {addr}: implausible decimals {decimals}")
                continue
            found[k] = TokenMeta(symbol=symbol, address=addr, decimals=decimals)

        if problems:
            raise EvmError(
                f"token validation failed on {self.client.chain.name}:\n  "
                + "\n  ".join(problems)
            )
        self.tokens.update(found)
        return found

    def __getitem__(self, key: str) -> TokenMeta:
        if key not in self.tokens:
            raise EvmError(
                f"{key} has not been validated on {self.client.chain.name}; "
                "call TokenRegistry.validate() before quoting"
            )
        return self.tokens[key]


# --- Uniswap v2 / Aerodrome volatile pools --------------------------------

def load_v2_pool(
    client: RpcClient, pool: str, a: TokenMeta, b: TokenMeta
) -> tuple[Decimal, Decimal]:
    """Reserves of (a, b) in human units, ordered to the arguments.

    token0()/token1() is read rather than assumed: reserve ordering follows
    the pool's own sort, and getting it backwards inverts the price into a
    quote that looks like a huge arbitrage.
    """
    if not client.has_code(pool):
        raise EvmError(f"no contract at pool address {pool} on {client.chain.name}")

    res, t0_raw, t1_raw = client.batch_call([
        (pool, SEL_GET_RESERVES),
        (pool, SEL_TOKEN0),
        (pool, SEL_TOKEN1),
    ])

    # getReserves() returns (uint112 reserve0, uint112 reserve1, uint32 ts),
    # each padded to a 32-byte word.
    if len(res) < 96:
        raise EvmError(f"getReserves() at {pool} returned {len(res)} bytes")
    reserve0 = int.from_bytes(res[0:32], "big")
    reserve1 = int.from_bytes(res[32:64], "big")
    token0 = _decode_address(t0_raw)
    token1 = _decode_address(t1_raw)

    pair = {token0, token1}
    if pair != {a.address, b.address}:
        raise EvmError(
            f"pool {pool} holds {token0}/{token1}, not {a.symbol}/{b.symbol} "
            f"({a.address}/{b.address})"
        )
    if reserve0 == 0 or reserve1 == 0:
        raise EvmError(f"pool {pool} has an empty reserve; nothing to quote")

    if token0 == a.address:
        return from_units(reserve0, a.decimals), from_units(reserve1, b.decimals)
    return from_units(reserve1, a.decimals), from_units(reserve0, b.decimals)


def v2_leg(
    client: RpcClient,
    pool: str,
    token_in: TokenMeta,
    token_out: TokenMeta,
    fee_rate: Decimal,
) -> ConstantProductLeg:
    """A constant-product leg priced from live reserves."""
    r_in, r_out = load_v2_pool(client, pool, token_in, token_out)
    return ConstantProductLeg(
        asset_in=token_in.symbol,
        asset_out=token_out.symbol,
        reserve_in=r_in,
        reserve_out=r_out,
        fee_rate=fee_rate,
        ts_local=time.time(),
        venue=f"{client.chain.name}:v2",
    )


# --- Uniswap v3 -----------------------------------------------------------

def resolve_v3_pool(
    client: RpcClient, factory: str, a: TokenMeta, b: TokenMeta, fee_bps_raw: int
) -> str:
    """factory.getPool(a, b, fee) -> pool address, or raise.

    Resolved, never hardcoded. fee_bps_raw is Uniswap's own uint24 units
    (500 = 0.05%, 3000 = 0.30%), not basis points.
    """
    if not client.has_code(factory):
        raise EvmError(
            f"no contract at factory {factory} on {client.chain.name}"
        )
    data = SEL_GET_POOL + abi_encode(
        ["address", "address", "uint24"], [a.address, b.address, fee_bps_raw]
    )
    pool = _decode_address(client.call(factory, data))
    if pool == to_checksum_address(ZERO_ADDRESS):
        raise EvmError(
            f"no v3 pool for {a.symbol}/{b.symbol} at fee {fee_bps_raw} "
            f"on {client.chain.name}"
        )
    return pool


def encode_v3_quote(
    token_in: TokenMeta, token_out: TokenMeta, amount_units: int, fee_bps_raw: int
) -> bytes:
    """Calldata for QuoterV2.quoteExactInputSingle.

    The parameter is a struct, which abi-encodes as a tuple inline -- hence
    the selector over '(address,address,uint256,uint24,uint160)'. Field order
    is (tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96); a limit of 0
    means "no limit", i.e. quote the whole fill however far it moves price.
    """
    return SEL_QUOTE_V3 + abi_encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(token_in.address, token_out.address, amount_units, fee_bps_raw, 0)],
    )


def decode_v3_quote(data: bytes) -> int:
    """amountOut from (uint256, uint160, uint32, uint256)."""
    if len(data) < 32:
        raise EvmError(f"QuoterV2 returned {len(data)} bytes; expected >= 32")
    return int.from_bytes(data[0:32], "big")


def marginal_price_from_tiny(
    tiny_in_units: int,
    tiny_out_units: int,
    token_in: TokenMeta,
    token_out: TokenMeta,
    fee_rate: Decimal,
) -> Decimal:
    """Pre-fee marginal price (out per in) from a negligible-size quote.

    The quoter's answer is net of the pool fee, but netedge.py needs a
    frictionless baseline so it can attribute cost to fees separately from
    slippage. Dividing the fee back out recovers it without touching tick math.
    """
    if tiny_in_units < MIN_TINY_UNITS:
        raise EvmError(
            f"tiny probe of {tiny_in_units} base units of {token_in.symbol} is "
            "too small to price; integer truncation would dominate"
        )
    if tiny_out_units <= 0:
        raise EvmError(
            f"tiny probe for {token_in.symbol}->{token_out.symbol} returned 0; "
            "pool is empty or the fee tier is wrong"
        )
    if fee_rate >= 1:
        raise EvmError(f"nonsensical fee rate {fee_rate}")

    a_in = from_units(tiny_in_units, token_in.decimals)
    a_out = from_units(tiny_out_units, token_out.decimals)
    return (a_out / a_in) / (Decimal(1) - fee_rate)


@dataclass
class V3Quoter:
    """On-demand QuoterV2 access with memoisation.

    netedge.evaluate() calls full() once per leg, but solve_capacity()
    binary-searches size and would otherwise issue dozens of eth_calls per
    path. Repeats are served from cache; genuinely new sizes cost one round
    trip. Treat capacity solving over v3 legs as RPC-expensive and prefer
    prefetching the probe sizes in a single batch.
    """
    client: RpcClient
    quoter: str
    token_in: TokenMeta
    token_out: TokenMeta
    fee_bps_raw: int
    _cache: dict[int, int] = field(default_factory=dict, repr=False)
    calls_made: int = 0

    @property
    def fee_rate(self) -> Decimal:
        return Decimal(self.fee_bps_raw) / Decimal(1_000_000)

    def quote_units(self, amount_units: int) -> int:
        if amount_units in self._cache:
            return self._cache[amount_units]
        data = encode_v3_quote(
            self.token_in, self.token_out, amount_units, self.fee_bps_raw
        )
        out = decode_v3_quote(self.client.call(self.quoter, data))
        self.calls_made += 1
        self._cache[amount_units] = out
        return out

    def prefetch(self, amounts: Iterable[Decimal]) -> None:
        """Quote several sizes in ONE batch, so they share a block."""
        wanted = [
            to_units(a, self.token_in.decimals) for a in amounts
        ]
        missing = [u for u in dict.fromkeys(wanted) if u not in self._cache]
        if not missing:
            return
        calls = [
            (self.quoter,
             encode_v3_quote(self.token_in, self.token_out, u, self.fee_bps_raw))
            for u in missing
        ]
        for u, raw in zip(missing, self.client.batch_call(calls)):
            self._cache[u] = decode_v3_quote(raw)
        self.calls_made += 1

    def __call__(self, amount_in: Decimal) -> Decimal:
        units = to_units(amount_in, self.token_in.decimals)
        if units <= 0:
            return Decimal(0)
        return from_units(self.quote_units(units), self.token_out.decimals)


def v3_leg(
    client: RpcClient,
    quoter: str,
    token_in: TokenMeta,
    token_out: TokenMeta,
    fee_bps_raw: int,
    reference_size: Decimal,
) -> QuotedLeg:
    """A v3 leg: executable quotes from the pool, baseline from a tiny probe.

    reference_size sets the scale of the tiny probe, so pass the notional you
    actually intend to trade.
    """
    if not client.has_code(quoter):
        raise EvmError(f"no contract at quoter {quoter} on {client.chain.name}")

    q = V3Quoter(client, quoter, token_in, token_out, fee_bps_raw)
    real_units = to_units(reference_size, token_in.decimals)
    tiny_units = max(real_units // TINY_DIVISOR, MIN_TINY_UNITS)

    # Both quotes in one batch: the baseline and the fill must describe the
    # same block, or the "frictionless" price is from a different market.
    calls = [
        (quoter, encode_v3_quote(token_in, token_out, tiny_units, fee_bps_raw)),
        (quoter, encode_v3_quote(token_in, token_out, real_units, fee_bps_raw)),
    ]
    tiny_raw, real_raw = client.batch_call(calls)
    tiny_out = decode_v3_quote(tiny_raw)
    q._cache[real_units] = decode_v3_quote(real_raw)
    q._cache[tiny_units] = tiny_out

    marginal = marginal_price_from_tiny(
        tiny_units, tiny_out, token_in, token_out, q.fee_rate
    )
    return QuotedLeg(
        asset_in=token_in.symbol,
        asset_out=token_out.symbol,
        marginal_out_per_in=marginal,
        fee_rate=q.fee_rate,
        quote_fn=q,
        ts_local=time.time(),
        venue=f"{client.chain.name}:v3:{fee_bps_raw}",
    )


# --- gas ------------------------------------------------------------------

# Rough gas for a single Uniswap v3 exactInputSingle, and for two swaps in one
# transaction (the atomic arb case). Real usage depends on how many ticks the
# swap crosses, so these are deliberately on the generous side.
GAS_ONE_SWAP = 180_000
GAS_TWO_SWAPS = 260_000


def measure_gas_usd(
    client: RpcClient, gas_units: int, native_usd: Decimal
) -> Decimal:
    """Live gas cost in USD for a transaction of gas_units.

    Replaces Chain.gas_usd, which is an unmeasured placeholder. native_usd is
    the chain's gas token in USD -- on an ETH L2 that is the ETH price, which
    the WETH/USDC quote already gives you, so this needs no price feed.

    Measured on Base at 0.006 gwei this comes to well under a cent, which
    makes gas essentially irrelevant there: the binding cost is the pool fee,
    not the gas. Do not carry that conclusion to a chain you have not
    measured, and re-measure when the chain is congested.
    """
    if gas_units <= 0:
        raise EvmError(f"gas_units must be positive, got {gas_units}")
    if native_usd <= 0:
        raise EvmError(f"native_usd must be positive, got {native_usd}")
    gas_price_wei = int(client.rpc("eth_gasPrice", []), 16)
    return Decimal(gas_price_wei) * Decimal(gas_units) / Decimal(10) ** 18 * native_usd


# --- startup self-check ---------------------------------------------------

def preflight(client: RpcClient, expected_tokens: dict[str, tuple[str, str]]
              ) -> TokenRegistry:
    """Prove the RPC and every token before any quoting happens.

    Run this once at startup. It is cheap, and it converts the entire class of
    "wrong address / wrong network" errors from silent bad data into an
    immediate crash.
    """
    client.verify_chain_id()
    reg = TokenRegistry(client)
    reg.validate(expected_tokens)
    return reg


def main() -> None:
    """Smoke test. Reads the chain's RPC URL from the environment.

    Usage: PYTHONPATH=. python venues/evm.py [chain]
    The URL (and therefore the API key) is never printed -- only the host.
    """
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "base"
    try:
        client = client_for(name)
    except EvmError as e:
        print(f"ERROR {e}")
        raise SystemExit(2) from None

    try:
        host = client.url.split("/")[2]
        client.verify_chain_id()
        print(f"{name}: chain_id {client.chain.chain_id} ok via {host}  "
              f"block {client.block_number():,}  rtt {client.last_rtt_ms:.0f}ms")
        if not client.chain.gas_measured:
            print(f"  NOTE gas_usd {client.chain.gas_usd} is an unmeasured "
                  "placeholder; do not size on-chain paths from it yet")

        candidates = CANDIDATES.get(name)
        if not candidates:
            print(f"  no token candidates recorded for {name}; nothing to validate")
            return
        for key, meta in TokenRegistry(client).validate(candidates).items():
            print(f"  validated {key:6s} {meta.symbol:8s} "
                  f"{meta.decimals:2d}dp  {meta.address}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
