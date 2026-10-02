"""SolanaRpcClient: retry/failover policy and the version ceiling.

Hand-rolled fakes, no unittest.mock, no pytest-asyncio -- async tests run via
asyncio.run(_run()) inside the test body, matching the repo's convention.

Run: python src/adapters/data/solana_rpc_client_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.adapters.data.solana_rpc_client import (  # noqa: E402
    MAX_SIGNATURE_PAGE, SolanaRpcClient, SolanaRpcError, SolanaRpcUnavailable,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


class FakeHttp:
    """Records every request and replays a scripted list of responses."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    async def post(self, url, json=None):          # noqa: A002
        self.requests.append((url, json))
        item = self.script.pop(0) if self.script else FakeResponse(
            {"jsonrpc": "2.0", "result": "default"})
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True


def _client(script, **kw):
    c = SolanaRpcClient(urls=kw.pop("urls", ["http://a", "http://b"]),
                        min_interval_s=0.0, cooldown_s=0.0, **kw)
    c._client = FakeHttp(script)
    c._sem = asyncio.Semaphore(4)
    c._lock = asyncio.Lock()
    return c


def test_an_empty_endpoint_list_is_refused_at_construction():
    """Failing later looks like a dead chain rather than missing config."""
    for bad in ([], [""], ["   "]):
        try:
            SolanaRpcClient(urls=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not construct")


def test_duplicate_endpoints_are_collapsed_but_order_is_kept():
    c = SolanaRpcClient(urls=["http://b", "http://a", "http://b"])
    assert [e.url for e in c._endpoints] == ["http://b", "http://a"]


def test_a_bad_request_is_raised_immediately_and_never_failed_over():
    """The distinction worth preserving verbatim.

    Retrying a malformed request on a second provider launders our bug into
    their rate limit and hides the cause.
    """
    async def _run():
        c = _client([FakeResponse({"error": {"code": -32602,
                                             "message": "invalid params"}})])
        try:
            await c.call("getTransaction", ["x"])
        except SolanaRpcError as e:
            assert e.retryable is False
            assert len(c._client.requests) == 1, (
                "a bad request must not be retried anywhere")
            return
        raise AssertionError("invalid params must raise")
    asyncio.run(_run())


def test_an_unsupported_transaction_version_is_treated_as_a_config_bug():
    """-32015 means our maxSupportedTransactionVersion is too low.

    Retrying it forever against every endpoint would look like an outage; it
    is a one-line configuration fault and must surface as one.
    """
    async def _run():
        c = _client([FakeResponse({"error": {"code": -32015,
                                             "message": "version not supported"}})])
        try:
            await c.call("getTransaction", ["x"])
        except SolanaRpcError as e:
            assert e.code == -32015 and e.retryable is False
            assert len(c._client.requests) == 1
            return
        raise AssertionError("-32015 must raise immediately")
    asyncio.run(_run())


def test_a_rate_limited_endpoint_fails_over_to_the_next_one():
    async def _run():
        c = _client([
            FakeResponse({}, status_code=429, text="slow down"),
            FakeResponse({"jsonrpc": "2.0", "result": 42}),
        ])
        assert await c.call("getSlot") == 42
        urls = [u for u, _ in c._client.requests]
        assert urls == ["http://a", "http://b"], urls
    asyncio.run(_run())


def test_a_transport_exception_fails_over_rather_than_propagating():
    async def _run():
        c = _client([ConnectionError("boom"),
                     FakeResponse({"jsonrpc": "2.0", "result": 7})])
        assert await c.call("getSlot") == 7
    asyncio.run(_run())


def test_exhausting_every_endpoint_raises_unavailable_with_the_last_cause():
    async def _run():
        c = _client([ConnectionError("down")] * 10, max_attempts=3)
        try:
            await c.call("getSlot")
        except SolanaRpcUnavailable as e:
            assert "no endpoint answered" in str(e)
            assert len(c._client.requests) == 3, "attempt budget must bound it"
            return
        raise AssertionError("exhaustion must raise SolanaRpcUnavailable")
    asyncio.run(_run())


def test_get_transaction_always_sends_the_version_ceiling():
    """Omitting it makes every versioned transaction fail, and pump.fun's
    live traffic is full of them -- the failure looks like a quiet program."""
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": {"slot": 1}})])
        await c.get_transaction("sig")
        _, body = c._client.requests[0]
        opts = body["params"][1]
        assert opts["maxSupportedTransactionVersion"] >= 1, opts
    asyncio.run(_run())


def test_signature_pagination_passes_signature_cursors_not_slots():
    """before/until are SIGNATURES. This is why the position store keeps one."""
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": []})])
        await c.get_signatures_for_address("PROG", before="SIGA",
                                           until="SIGB", limit=10)
        _, body = c._client.requests[0]
        assert body["params"][0] == "PROG"
        assert body["params"][1]["before"] == "SIGA"
        assert body["params"][1]["until"] == "SIGB"
    asyncio.run(_run())


def test_signature_page_size_is_clamped_to_the_server_maximum():
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": []})])
        await c.get_signatures_for_address("PROG", limit=99999)
        _, body = c._client.requests[0]
        assert body["params"][1]["limit"] == MAX_SIGNATURE_PAGE
    asyncio.run(_run())


def test_block_time_of_none_is_returned_as_none_not_substituted():
    """A skipped slot legitimately has no time.

    Substituting `now` would backdate a launch to whenever the indexer ran,
    which is invisible and poisons every age calculation downstream.
    """
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": None})])
        assert await c.get_block_time(123) is None
    asyncio.run(_run())


def test_omitted_optional_cursors_are_not_sent_at_all():
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": []})])
        await c.get_signatures_for_address("PROG")
        _, body = c._client.requests[0]
        assert "before" not in body["params"][1]
        assert "until" not in body["params"][1]
    asyncio.run(_run())


def test_each_call_uses_a_fresh_request_id():
    async def _run():
        c = _client([FakeResponse({"jsonrpc": "2.0", "result": 1}),
                     FakeResponse({"jsonrpc": "2.0", "result": 2})])
        await c.call("getSlot")
        await c.call("getSlot")
        ids = [body["id"] for _, body in c._client.requests]
        assert len(set(ids)) == 2, ids
    asyncio.run(_run())


SECRET_URL = "https://solana-mainnet.example.com/v2/SUPER_SECRET_API_KEY"


def test_stats_reports_hosts_and_never_leaks_the_api_key():
    """A provider URL carries its key in the path.

    stats() is not merely printed -- it is attached to every operations
    heartbeat as `detail`, so a full URL here is posted to another service
    and stored in its payloads.
    """
    c = SolanaRpcClient(urls=[SECRET_URL])
    blob = str(c.stats())
    assert "SUPER_SECRET_API_KEY" not in blob, blob
    assert "solana-mainnet.example.com" in blob


def test_error_messages_name_the_host_not_the_full_url():
    """Exception text ends up in logs and in alert payloads."""
    async def _run():
        c = _client([FakeResponse({}, status_code=400, text="bad")],
                    urls=[SECRET_URL])
        try:
            await c.call("getSlot")
        except SolanaRpcError as e:
            assert "SUPER_SECRET_API_KEY" not in str(e), str(e)
            assert "solana-mainnet.example.com" in str(e)
            return
        raise AssertionError("a 400 must raise")
    asyncio.run(_run())


def test_exhaustion_error_does_not_leak_the_url_either():
    async def _run():
        c = _client([ConnectionError("down")] * 5, urls=[SECRET_URL],
                    max_attempts=2)
        try:
            await c.call("getSlot")
        except SolanaRpcUnavailable as e:
            assert "SUPER_SECRET_API_KEY" not in str(e), str(e)
            return
        raise AssertionError("exhaustion must raise")
    asyncio.run(_run())


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
