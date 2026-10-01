"""Unit tests for the EVM adapter, against a mocked JSON-RPC transport.

These cover everything in venues/evm.py that does NOT require a live chain:
ABI codec, unit scaling, batch handling, and -- most importantly -- the
validation guards that are supposed to turn a wrong address into a crash
instead of a plausible wrong quote.

httpx.MockTransport is used rather than monkeypatching, so the real
batch_call() path runs: id reordering, error elements, size mismatches. The
network is the only thing faked.

What these tests CANNOT establish: that the quoter address, factory address,
or struct field order match the deployed contracts. Only a live chain settles
that. See test_selectors_match_published_values for the part that can be
pinned down offline.

Run: python tests/test_evm.py     (no pytest required)
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from venues.evm import (  # noqa: E402
    BASE,
    MIN_TINY_UNITS,
    SEL_DECIMALS,
    SEL_GET_POOL,
    SEL_GET_RESERVES,
    SEL_SYMBOL,
    SEL_TOKEN0,
    SEL_TOKEN1,
    EvmError,
    RpcClient,
    TokenMeta,
    TokenRegistry,
    V3Quoter,
    ZERO_ADDRESS,
    decode_v3_quote,
    encode_v3_quote,
    from_units,
    load_v2_pool,
    marginal_price_from_tiny,
    resolve_v3_pool,
    selector,
    to_units,
    v3_leg,
)

D = Decimal

WETH_ADDR = to_checksum_address("0x" + "11" * 20)
USDC_ADDR = to_checksum_address("0x" + "22" * 20)
POOL_ADDR = to_checksum_address("0x" + "33" * 20)
FACTORY = to_checksum_address("0x" + "44" * 20)
QUOTER = to_checksum_address("0x" + "55" * 20)
OTHER_ADDR = to_checksum_address("0x" + "66" * 20)

WETH = TokenMeta("WETH", WETH_ADDR, 18)
USDC = TokenMeta("USDC", USDC_ADDR, 6)


def word(n: int) -> str:
    return f"{n:064x}"


def addr_word(a: str) -> str:
    return "00" * 12 + a[2:].lower()


def str_word(s: str) -> str:
    """ABI-encoded dynamic string: offset, length, padded data."""
    raw = s.encode()
    pad = (32 - len(raw) % 32) % 32
    return word(32) + word(len(raw)) + raw.hex() + "00" * pad


class FakeChain:
    """Canned responses keyed by (to, selector), plus injectable failures."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], str] = {}
        self.chain_id = BASE.chain_id
        self.code: dict[str, str] = {}
        self.errors: set[tuple[str, str]] = set()
        self.batches: list[int] = []     # size of each batch received
        self.scramble = False            # return batch results out of order
        self.drop_one = False            # return a short batch

    def set(self, to: str, sel: bytes, result_hex: str) -> None:
        self.responses[(to.lower(), sel.hex())] = result_hex

    def set_full(self, to: str, calldata: bytes, result_hex: str) -> None:
        """Key on the whole calldata, for distinguishing quote sizes."""
        self.responses[(to.lower(), calldata.hex())] = result_hex

    def fail(self, to: str, sel: bytes) -> None:
        self.errors.add((to.lower(), sel.hex()))

    def _lookup(self, to: str, data: str) -> tuple[bool, str]:
        key_full = (to.lower(), data)
        key_sel = (to.lower(), data[:8])
        if key_full in self.errors or key_sel in self.errors:
            return False, "execution reverted"
        if key_full in self.responses:
            return True, self.responses[key_full]
        if key_sel in self.responses:
            return True, self.responses[key_sel]
        return False, f"no canned response for {to} {data[:10]}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)

        if isinstance(payload, dict):
            m = payload["method"]
            if m == "eth_chainId":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": payload["id"],
                    "result": hex(self.chain_id)})
            if m == "eth_blockNumber":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": payload["id"], "result": hex(1234567)})
            if m == "eth_getCode":
                addr = payload["params"][0].lower()
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": payload["id"],
                    "result": self.code.get(addr, "0x")})
            if m == "eth_call":
                c = payload["params"][0]
                ok, val = self._lookup(c["to"], c["data"][2:])
                body = ({"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + val}
                        if ok else
                        {"jsonrpc": "2.0", "id": payload["id"],
                         "error": {"code": -32000, "message": val}})
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": payload["id"],
                "error": {"code": -32601, "message": f"unknown {m}"}})

        self.batches.append(len(payload))
        out = []
        for item in payload:
            c = item["params"][0]
            ok, val = self._lookup(c["to"], c["data"][2:])
            out.append({"jsonrpc": "2.0", "id": item["id"], "result": "0x" + val}
                       if ok else
                       {"jsonrpc": "2.0", "id": item["id"],
                        "error": {"code": -32000, "message": val}})
        if self.scramble:
            out.reverse()
        if self.drop_one and out:
            out.pop()
        return httpx.Response(200, json=out)


def mk_client(fake: FakeChain) -> RpcClient:
    transport = httpx.MockTransport(fake.handler)
    return RpcClient(url="https://rpc.test/v1", chain=BASE,
                     _client=httpx.Client(transport=transport))


def good_tokens(fake: FakeChain) -> None:
    fake.set(WETH_ADDR, SEL_SYMBOL, str_word("WETH"))
    fake.set(WETH_ADDR, SEL_DECIMALS, word(18))
    fake.set(USDC_ADDR, SEL_SYMBOL, str_word("USDC"))
    fake.set(USDC_ADDR, SEL_DECIMALS, word(6))


def approx(got: Decimal, want: str, tol: str = "0.0001") -> bool:
    return abs(got - D(want)) <= D(tol)


# --- 1. Codec ------------------------------------------------------------

def test_selectors_match_published_values():
    """A typo in a signature string yields a selector that reverts or, worse,
    collides with another method. These six are publicly documented, so they
    pin the keccak path down without a chain.
    """
    assert selector("decimals()").hex() == "313ce567"
    assert selector("symbol()").hex() == "95d89b41"
    assert selector("getReserves()").hex() == "0902f1ac"
    assert selector("token0()").hex() == "0dfe1681"
    assert selector("token1()").hex() == "d21220a7"
    assert selector("getPool(address,address,uint24)").hex() == "1698ee82"


def test_unit_scaling_uses_the_tokens_own_decimals():
    """USDC is 6dp and sits on one side of nearly every path here. Assuming
    18 everywhere is wrong by 10^12, which is not a subtle bug but is a silent
    one if nothing checks.
    """
    assert to_units(D("1"), 18) == 10**18
    assert to_units(D("1"), 6) == 10**6
    assert to_units(D("2500.75"), 6) == 2_500_750_000
    assert from_units(10**18, 18) == D(1)
    assert from_units(2_500_750_000, 6) == D("2500.75")
    # Round trip through the chain's truncation.
    assert from_units(to_units(D("0.123456789"), 6), 6) == D("0.123456")


def test_v3_quote_calldata_round_trips():
    data = encode_v3_quote(WETH, USDC, 10**18, 500)
    assert data[:4].hex() == "c6a5026a"
    # struct inline: tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96
    body = data[4:]
    assert len(body) == 160, len(body)
    assert body[12:32].hex() == WETH_ADDR[2:].lower()
    assert body[44:64].hex() == USDC_ADDR[2:].lower()
    assert int.from_bytes(body[64:96], "big") == 10**18
    assert int.from_bytes(body[96:128], "big") == 500
    assert int.from_bytes(body[128:160], "big") == 0  # no price limit
    assert decode_v3_quote(bytes.fromhex(word(12345) + word(1) + word(2) + word(3))) == 12345


def test_short_quote_response_raises():
    try:
        decode_v3_quote(b"\x00" * 8)
    except EvmError as e:
        assert "expected >= 32" in str(e), e
    else:
        raise AssertionError("a truncated quote was accepted")


# --- 2. The marginal-price trick -----------------------------------------

def test_marginal_price_divides_the_pool_fee_back_out():
    """1 WETH probe at 3000 USDC/WETH through a 5bps pool returns 2998.5 USDC.
    Dividing by (1 - 0.0005) must recover exactly 3000 -- the frictionless
    baseline netedge.py needs to separate fee cost from slippage.
    """
    tiny_in = 10**18
    tiny_out = 2_998_500_000           # 2998.5 USDC at 6dp
    px = marginal_price_from_tiny(tiny_in, tiny_out, WETH, USDC, D("0.0005"))
    assert approx(px, "3000", "0.01"), px


def test_marginal_price_refuses_a_probe_too_small_to_price():
    """Below the noise floor the pool's integer math dominates, so a confident
    answer here would be fabricated precision.
    """
    try:
        marginal_price_from_tiny(MIN_TINY_UNITS - 1, 5, WETH, USDC, D("0.0005"))
    except EvmError as e:
        assert "too small" in str(e), e
    else:
        raise AssertionError("an unpriceable probe was accepted")


def test_marginal_price_refuses_an_empty_pool():
    try:
        marginal_price_from_tiny(10**18, 0, WETH, USDC, D("0.0005"))
    except EvmError as e:
        assert "returned 0" in str(e), e
    else:
        raise AssertionError("a zero quote was treated as a price")


# --- 3. Batch handling ---------------------------------------------------

def test_batch_results_are_matched_by_id_not_arrival_order():
    """JSON-RPC permits a batch response in any order. Zipping by position
    would hand each leg another leg's answer -- quotes that are individually
    valid and collectively nonsense.
    """
    fake = FakeChain()
    fake.set(WETH_ADDR, SEL_DECIMALS, word(18))
    fake.set(USDC_ADDR, SEL_DECIMALS, word(6))
    fake.scramble = True
    client = mk_client(fake)

    out = client.batch_call([(WETH_ADDR, SEL_DECIMALS), (USDC_ADDR, SEL_DECIMALS)])
    assert int.from_bytes(out[0], "big") == 18, "first result is not WETH's"
    assert int.from_bytes(out[1], "big") == 6, "second result is not USDC's"


def test_reverted_call_raises_instead_of_decoding_as_zero():
    """An empty return decodes as amount 0, which reads as 'no liquidity'
    rather than 'the call is broken'. That distinction is worth a crash.
    """
    fake = FakeChain()
    fake.fail(WETH_ADDR, SEL_DECIMALS)
    client = mk_client(fake)
    try:
        client.batch_call([(WETH_ADDR, SEL_DECIMALS)])
    except EvmError as e:
        assert "reverted" in str(e), e
    else:
        raise AssertionError("a revert was swallowed")


def test_short_batch_response_raises():
    fake = FakeChain()
    fake.set(WETH_ADDR, SEL_DECIMALS, word(18))
    fake.set(USDC_ADDR, SEL_DECIMALS, word(6))
    fake.drop_one = True
    client = mk_client(fake)
    try:
        client.batch_call([(WETH_ADDR, SEL_DECIMALS), (USDC_ADDR, SEL_DECIMALS)])
    except EvmError as e:
        assert "batch size mismatch" in str(e), e
    else:
        raise AssertionError("a short batch was accepted")


def test_wrong_chain_id_is_fatal():
    """An RPC URL pointing at the wrong network returns perfectly well-formed
    quotes for a market we are not trading.
    """
    fake = FakeChain()
    fake.chain_id = 1  # mainnet, not Base
    client = mk_client(fake)
    try:
        client.verify_chain_id()
    except EvmError as e:
        assert "wrong network" in str(e) and "8453" in str(e), e
    else:
        raise AssertionError("a mismatched chain_id was accepted")


def test_matching_chain_id_passes():
    mk_client(FakeChain()).verify_chain_id()


# --- 4. Token validation (the wrong-address guard) -----------------------

def test_symbol_mismatch_rejects_a_wrong_address():
    """The headline guard. An address that exists and answers decimals() but
    is not the token we meant must fail loudly at startup, not quietly price
    a path that does not exist.
    """
    fake = FakeChain()
    fake.set(WETH_ADDR, SEL_SYMBOL, str_word("DAI"))   # not WETH
    fake.set(WETH_ADDR, SEL_DECIMALS, word(18))
    reg = TokenRegistry(mk_client(fake))
    try:
        reg.validate({"WETH": (WETH_ADDR, "WETH")})
    except EvmError as e:
        assert "wrong address" in str(e) and "DAI" in str(e), e
    else:
        raise AssertionError("a wrong token address survived validation")


def test_validation_populates_real_decimals():
    fake = FakeChain()
    good_tokens(fake)
    reg = TokenRegistry(mk_client(fake))
    found = reg.validate({"WETH": (WETH_ADDR, "WETH"), "USDC": (USDC_ADDR, "USDC")})

    assert found["WETH"].decimals == 18
    assert found["USDC"].decimals == 6
    assert reg["USDC"].address == USDC_ADDR
    # One batch for the whole registry, not two calls per token.
    assert fake.batches == [4], fake.batches


def test_implausible_decimals_rejected():
    fake = FakeChain()
    fake.set(WETH_ADDR, SEL_SYMBOL, str_word("WETH"))
    fake.set(WETH_ADDR, SEL_DECIMALS, word(200))
    reg = TokenRegistry(mk_client(fake))
    try:
        reg.validate({"WETH": (WETH_ADDR, "WETH")})
    except EvmError as e:
        assert "implausible decimals" in str(e), e
    else:
        raise AssertionError("decimals=200 accepted")


def test_bytes32_symbol_is_accepted():
    """Some long-lived tokens return a fixed bytes32 symbol. Rejecting those
    would discard correct addresses.
    """
    fake = FakeChain()
    raw = b"WETH".ljust(32, b"\x00").hex()
    fake.set(WETH_ADDR, SEL_SYMBOL, raw)
    fake.set(WETH_ADDR, SEL_DECIMALS, word(18))
    reg = TokenRegistry(mk_client(fake))
    assert reg.validate({"WETH": (WETH_ADDR, "WETH")})["WETH"].symbol == "WETH"


def test_unvalidated_token_cannot_be_fetched():
    reg = TokenRegistry(mk_client(FakeChain()))
    try:
        reg["WETH"]
    except EvmError as e:
        assert "has not been validated" in str(e), e
    else:
        raise AssertionError("an unvalidated token was handed out")


# --- 5. v2 reserve ordering ----------------------------------------------

def _v2_fake(token0: str, token1: str, r0: int, r1: int) -> FakeChain:
    fake = FakeChain()
    fake.code[POOL_ADDR.lower()] = "0x6080"
    fake.set(POOL_ADDR, SEL_GET_RESERVES, word(r0) + word(r1) + word(1_700_000_000))
    fake.set(POOL_ADDR, SEL_TOKEN0, addr_word(token0))
    fake.set(POOL_ADDR, SEL_TOKEN1, addr_word(token1))
    return fake


def test_v2_reserves_follow_the_pools_own_token_order():
    """Pools sort their tokens by address, not by our argument order. Reading
    reserves in the wrong order inverts the price, which presents as an
    enormous arbitrage rather than as an error.
    """
    r_weth = 100 * 10**18
    r_usdc = 300_000 * 10**6

    # Case A: WETH is token0.
    c = mk_client(_v2_fake(WETH_ADDR, USDC_ADDR, r_weth, r_usdc))
    a, b = load_v2_pool(c, POOL_ADDR, WETH, USDC)
    assert approx(a, "100", "0.001") and approx(b, "300000", "1"), (a, b)

    # Case B: the same pool with the order flipped must give the same answer.
    c = mk_client(_v2_fake(USDC_ADDR, WETH_ADDR, r_usdc, r_weth))
    a, b = load_v2_pool(c, POOL_ADDR, WETH, USDC)
    assert approx(a, "100", "0.001") and approx(b, "300000", "1"), (a, b)


def test_v2_pool_holding_the_wrong_pair_raises():
    c = mk_client(_v2_fake(WETH_ADDR, OTHER_ADDR, 10**18, 10**18))
    try:
        load_v2_pool(c, POOL_ADDR, WETH, USDC)
    except EvmError as e:
        assert "not WETH/USDC" in str(e), e
    else:
        raise AssertionError("a pool for the wrong pair was accepted")


def test_v2_empty_reserve_raises():
    c = mk_client(_v2_fake(WETH_ADDR, USDC_ADDR, 0, 10**6))
    try:
        load_v2_pool(c, POOL_ADDR, WETH, USDC)
    except EvmError as e:
        assert "empty reserve" in str(e), e
    else:
        raise AssertionError("an empty pool was quoted")


def test_v2_pool_without_code_raises():
    fake = _v2_fake(WETH_ADDR, USDC_ADDR, 10**18, 10**6)
    fake.code.clear()            # address exists but holds no contract
    try:
        load_v2_pool(mk_client(fake), POOL_ADDR, WETH, USDC)
    except EvmError as e:
        assert "no contract at pool" in str(e), e
    else:
        raise AssertionError("a codeless address was treated as a pool")


# --- 6. Pool resolution --------------------------------------------------

def test_missing_v3_pool_raises_rather_than_returning_zero():
    """factory.getPool() answers with the zero address for a pair/fee that has
    no pool. Quoting against address(0) is how a nonexistent market gets
    backtested.
    """
    fake = FakeChain()
    fake.code[FACTORY.lower()] = "0x6080"
    fake.set(FACTORY, SEL_GET_POOL, addr_word(ZERO_ADDRESS))
    try:
        resolve_v3_pool(mk_client(fake), FACTORY, WETH, USDC, 500)
    except EvmError as e:
        assert "no v3 pool" in str(e), e
    else:
        raise AssertionError("the zero address was returned as a pool")


def test_resolved_v3_pool_is_returned_checksummed():
    fake = FakeChain()
    fake.code[FACTORY.lower()] = "0x6080"
    fake.set(FACTORY, SEL_GET_POOL, addr_word(POOL_ADDR))
    got = resolve_v3_pool(mk_client(fake), FACTORY, WETH, USDC, 500)
    assert got == POOL_ADDR, got


# --- 7. v3 leg assembly --------------------------------------------------

def _quoter_fake() -> FakeChain:
    """3000 USDC/WETH marginal, 2990 realised on a 1 WETH fill (5bps pool)."""
    fake = FakeChain()
    fake.code[QUOTER.lower()] = "0x6080"
    tiny_units = 10**18 // 10_000                      # 1e14
    tiny_out = 299_850                                 # 0.29985 USDC at 6dp
    fake.set_full(QUOTER, encode_v3_quote(WETH, USDC, tiny_units, 500),
                  word(tiny_out) + word(0) + word(0) + word(0))
    fake.set_full(QUOTER, encode_v3_quote(WETH, USDC, 10**18, 500),
                  word(2_990_000_000) + word(0) + word(0) + word(0))
    return fake


def test_v3_leg_separates_fee_from_slippage():
    """The three quote methods must disagree in the right directions:
    frictionless 3000 > fee_only 2998.5 > full 2990. netedge.py subtracts
    these to attribute cost, so collapsing any two hides a real expense.
    """
    leg = v3_leg(mk_client(_quoter_fake()), QUOTER, WETH, USDC, 500, D("1"))

    assert leg.asset_in == "WETH" and leg.asset_out == "USDC"
    assert approx(leg.fee_rate, "0.0005"), leg.fee_rate
    assert approx(leg.frictionless(D(1)), "3000", "0.01"), leg.frictionless(D(1))
    assert approx(leg.fee_only(D(1)), "2998.5", "0.01"), leg.fee_only(D(1))
    assert approx(leg.full(D(1)).amount_out, "2990", "0.01"), leg.full(D(1))
    assert leg.venue == "base:v3:500"


def test_v3_leg_fetches_baseline_and_fill_in_one_batch():
    """The baseline and the executable quote must describe the same block, or
    the 'frictionless' price belongs to a different market than the fill.
    """
    fake = _quoter_fake()
    v3_leg(mk_client(fake), QUOTER, WETH, USDC, 500, D("1"))
    assert fake.batches == [2], fake.batches


def test_v3_quoter_memoises_repeated_sizes():
    """solve_capacity() binary-searches size; without a cache that is dozens
    of round trips per path and the legs drift across blocks mid-search.
    """
    q = V3Quoter(mk_client(_quoter_fake()), QUOTER, WETH, USDC, 500)
    first = q(D("1"))
    second = q(D("1"))
    assert first == second
    assert q.calls_made == 1, q.calls_made


def test_v3_leg_rejects_a_codeless_quoter():
    fake = _quoter_fake()
    fake.code.clear()
    try:
        v3_leg(mk_client(fake), QUOTER, WETH, USDC, 500, D("1"))
    except EvmError as e:
        assert "no contract at quoter" in str(e), e
    else:
        raise AssertionError("a codeless quoter was accepted")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"pass {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
