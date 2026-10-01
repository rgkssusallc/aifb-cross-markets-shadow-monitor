"""Unit tests for the Sui/FlowX venue, replaying recorded mainnet responses.

The numbers below are REAL FlowX mainnet quotes, taken from the handover
pack's fixtures, so the parsing and scaling are checked against what the API
actually returned rather than against a guess at its shape:

  sell 10 SUI     ->  10000000000 raw in  ->    11653674 raw out
  sell 2500 SUI   -> 2500000000000 raw in -> 2916848728 raw out
  buy with 1000 USDC -> 1000000000 raw in -> 856006186953 raw out

SUI is 9 decimals and USDC is 6. Neither is 18, so any assumed-18 scaling is
wrong by orders of magnitude and these tests would catch it.

The guards are tested because each one was paid for by somebody else's live
trading: a route through a source that cannot be executed, a crossed quote
stuck stale for minutes, and a single sample 2.8% off the market. All three
would top an opportunity log.

Run: python tests/test_sui_flowx.py     (no pytest required)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from venues.sui_flowx import (  # noqa: E402
    MAX_DEVIATION,
    SUI_COIN,
    USDC_COIN,
    PinnedQuote,
    SuiError,
    SuiFlowXVenue,
    crossed,
    from_raw,
    to_raw,
)

D = Decimal

# Recorded mainnet quotes: raw_in -> raw_out, SUI->USDC direction.
SELL_SUI = {
    10_000_000_000: 11_653_674,          # 10 SUI   -> 11.653674 USDC
    2_500_000_000_000: 2_916_848_728,    # 2500 SUI -> 2916.848728 USDC
}
# The tiny probe this venue issues for 10 SUI is 10e9/10000 = 1e6 raw.
# Priced at the 2500-SUI marginal rate for a clean, self-consistent fixture.
TINY_SUI_RAW = 1_000_000
TINY_SUI_OUT = 1_166                     # 0.001 SUI -> 0.001166 USDC


def flowx_body(raw_out: int, raw_in: int) -> dict:
    return {"code": 0, "message": "successfully", "data": {
        "tokenIn": SUI_COIN, "tokenOut": USDC_COIN,
        "amountIn": str(raw_in), "amountOut": str(raw_out),
        "amountInUsd": "0", "amountOutUsd": "0",
        "priceImpact": "-0.001", "feeToken": "", "feeAmount": "0",
        "paths": [[{}]], "protocolConfig": {"cetus": {}}}, "requestId": "t"}


class Fake:
    """Serves the recorded quotes, and whatever the test wants to inject."""

    def __init__(self, quotes: dict[int, int] | None = None,
                 symbols: dict[str, str] | None = None,
                 checkpoint: int = 329_196_433) -> None:
        self.quotes = dict(SELL_SUI) if quotes is None else dict(quotes)
        self.quotes.setdefault(TINY_SUI_RAW, TINY_SUI_OUT)
        self.symbols = symbols or {SUI_COIN: "SUI", USDC_COIN: "USDC"}
        self.checkpoint = checkpoint
        self.asked: list[dict] = []

    def flowx(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.asked.append(q)
        raw_in = int(q["amountIn"])
        out = self.quotes.get(raw_in)
        if out is None:
            return httpx.Response(200, json={"code": 1,
                                             "message": "no route", "data": {}})
        return httpx.Response(200, json=flowx_body(out, raw_in))

    def rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        m, p = body["method"], body.get("params", [])
        if m == "suix_getCoinMetadata":
            ct = p[0]
            sym = self.symbols.get(ct)
            if sym is None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": None})
            dec = 9 if sym == "SUI" else 6
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "symbol": sym, "decimals": dec, "name": sym}})
        if m == "sui_getLatestCheckpointSequenceNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": str(self.checkpoint)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"message": f"no {m}"}})


def venue(fake: Fake, **kw) -> SuiFlowXVenue:
    return SuiFlowXVenue(
        rpc_url="https://sui.test/v2/k",
        transport=httpx.MockTransport(fake.flowx),
        rpc_transport=httpx.MockTransport(fake.rpc),
        **kw)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def approx(got: Decimal, want: str, tol: str = "0.0001") -> bool:
    return abs(got - D(want)) <= D(tol)


# --- 1. Scaling, against real recorded amounts --------------------------

def test_sui_is_nine_decimals_and_usdc_is_six():
    """Neither is 18. Assuming 18 is wrong by 10^9 for SUI and 10^12 for USDC,
    and would turn every quote into nonsense that still parses.
    """
    assert to_raw(D("10"), 9) == 10_000_000_000
    assert to_raw(D("2500"), 9) == 2_500_000_000_000
    assert from_raw(11_653_674, 6) == D("11.653674")
    assert from_raw(2_916_848_728, 6) == D("2916.848728")
    assert from_raw(856_006_186_953, 9) == D("856.006186953")


def test_recorded_quote_is_parsed_to_the_right_amount():
    """10 SUI -> 11.653674 USDC, straight from a recorded mainnet response."""
    f = Fake()
    v = venue(f)

    async def go():
        await v.connect()
        leg = await v.leg("SUI", "USDC", D("10"))
        await v.aclose()
        return leg

    leg = run(go())
    assert leg is not None
    assert approx(leg.full(D("10")).amount_out, "11.653674", "0.000001")
    assert leg.asset_in == "SUI" and leg.asset_out == "USDC"


def test_marginal_price_divides_the_pool_fee_back_out():
    """The tiny probe returns 0.001166 USDC for 0.001 SUI = 1.166 net of fee.
    At a 25bps pool fee the frictionless baseline must read 1.166/0.9975.
    """
    f = Fake()
    v = venue(f, pool_fee_rate=D("0.0025"))

    async def go():
        await v.connect()
        leg = await v.leg("SUI", "USDC", D("10"))
        await v.aclose()
        return leg

    leg = run(go())
    assert leg is not None
    want = (D("1.166") / (D(1) - D("0.0025")))
    assert abs(leg.marginal_out_per_in - want) < D("0.001"), leg.marginal_out_per_in
    # And the three quote methods must disagree in the right direction.
    assert leg.frictionless(D(1)) > leg.fee_only(D(1))


def test_a_tiny_probe_is_actually_requested():
    """Two calls per leg: the fill and the negligible-size baseline."""
    f = Fake()
    v = venue(f)

    async def go():
        await v.connect()
        await v.leg("SUI", "USDC", D("10"))
        await v.aclose()

    run(go())
    sizes = sorted(int(a["amountIn"]) for a in f.asked)
    assert sizes == [TINY_SUI_RAW, 10_000_000_000], sizes


# --- 2. The guards ------------------------------------------------------

def test_empty_sources_is_refused():
    """FlowX treats an empty includeSources as UNRESTRICTED, so sending
    nothing would quote routes through venues this project cannot verify.
    """
    f = Fake()
    v = venue(f, sources=())
    try:
        run(v.connect())
    except SuiError as e:
        assert "unrestricted" in str(e), e
    else:
        raise AssertionError("empty source set accepted")


def test_sources_are_pinned_to_cetus_in_the_request():
    f = Fake()
    v = venue(f)

    async def go():
        await v.connect()
        await v.leg("SUI", "USDC", D("10"))
        await v.aclose()

    run(go())
    assert f.asked, "no quote requested"
    assert all(a["includeSources"] == "CETUS" for a in f.asked), f.asked


def test_glitch_quote_is_rejected_not_logged_as_edge():
    """A single sample 2.8% off the market was observed live. Logged naively
    that is a 280bps opportunity that dwarfs every real signal.
    """
    f = Fake()
    v = venue(f)

    async def go():
        await v.connect()
        # First leg establishes the reference.
        first = await v.leg("SUI", "USDC", D("10"))
        assert first is not None
        # Now make the tiny probe come back far off the market.
        bad = int(TINY_SUI_OUT * (1 + float(MAX_DEVIATION) + 0.02))
        f.quotes[TINY_SUI_RAW] = bad
        second = await v.leg("SUI", "USDC", D("10"))
        await v.aclose()
        return second

    assert run(go()) is None, "a glitched quote produced a leg"
    assert v.glitches_rejected == 1, v.glitches_rejected
    assert "glitch" in v.status()


def test_normal_drift_is_not_treated_as_a_glitch():
    """The guard must not reject ordinary movement, or it filters the market
    out along with the noise.
    """
    f = Fake()
    v = venue(f)

    async def go():
        await v.connect()
        await v.leg("SUI", "USDC", D("10"))
        f.quotes[TINY_SUI_RAW] = int(TINY_SUI_OUT * 1.004)   # 0.4%
        second = await v.leg("SUI", "USDC", D("10"))
        await v.aclose()
        return second

    assert run(go()) is not None, "0.4% drift was wrongly rejected"
    assert v.glitches_rejected == 0


def test_crossed_quotes_are_recognised():
    """Buying below the sell price is broken data, not free money."""
    assert crossed(D("1.10"), D("1.20"))
    assert not crossed(D("1.20"), D("1.10"))
    assert not crossed(D("1.15"), D("1.15"))


def test_wrong_coin_type_is_refused_at_connect():
    """The Sui analogue of the ERC20 symbol() check. A plausible but wrong
    coin type must fail loudly, not price an unrelated asset.
    """
    f = Fake(symbols={SUI_COIN: "NOTSUI", USDC_COIN: "USDC"})
    v = venue(f)
    try:
        run(v.connect())
    except SuiError as e:
        assert "wrong coin type" in str(e), e
    else:
        raise AssertionError("a mismatched coin type was accepted")


def test_missing_coin_metadata_is_refused():
    f = Fake(symbols={USDC_COIN: "USDC"})
    v = venue(f)
    try:
        run(v.connect())
    except SuiError as e:
        assert "no coin metadata" in str(e), e
    else:
        raise AssertionError("a coin type with no metadata was accepted")


def test_pinned_quote_refuses_an_unquoted_size():
    """Interpolating a number into the middle of a cost decomposition is
    worse than crashing, because it looks like a measurement.
    """
    pq = PinnedQuote({D("10"): D("11.653674")})
    assert pq(D("10")) == D("11.653674")
    try:
        pq(D("25"))
    except SuiError as e:
        assert "no FlowX quote pinned" in str(e), e
    else:
        raise AssertionError("an unquoted size returned a number")


# --- 3. Venue protocol conformance --------------------------------------

def test_checkpoint_is_the_state_id_and_quotes_are_block_exact():
    f = Fake(checkpoint=329_196_433)
    v = venue(f)
    run(v.connect())
    assert v.state_id() == 329_196_433
    assert v.exact_while_state_unchanged is True
    assert v.healthy()
    assert v.age_ms() == 0.0
    run(v.aclose())


def test_venue_does_not_claim_to_quote_weth():
    """Cetus's depth here is SUI/USDC. Offering a bridged ETH wrapper as if it
    were the real pair is how a thin market passes for a tradeable one, so
    the venue simply does not list it and Engine.routes() leaves it out.
    """
    f = Fake()
    v = venue(f)
    run(v.connect())
    assert v.assets() == {"SUI", "USDC"}, v.assets()
    assert run(v.leg("WETH", "USDC", D("1000"))) is None
    run(v.aclose())


def test_no_route_returns_none_rather_than_zero():
    f = Fake(quotes={})
    v = venue(f)

    async def go():
        await v.connect()
        leg = await v.leg("SUI", "USDC", D("10"))
        await v.aclose()
        return leg

    assert run(go()) is None


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
