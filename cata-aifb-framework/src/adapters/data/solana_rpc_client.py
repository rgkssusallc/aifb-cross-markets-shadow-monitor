"""Generic Solana JSON-RPC client -- a parallel sibling to `rpc_http_client.py`.

NOT a subclass of the EVM client and not a port of it. There is no `eth_call`
or `eth_getLogs` here; Solana's RPC surface is a different set of methods with
different pagination. What IS reused -- deliberately, because it is the part
that was learned the hard way rather than designed -- is the SHAPE:

  * a fallback list of endpoints, tried in order
  * a per-instance throttle plus a concurrency semaphore, so one process
    cannot burst a provider into rate-limiting itself
  * a cooldown on an endpoint that just failed, so a sick provider is skipped
    rather than hammered
  * and the one distinction most worth preserving verbatim: a RETRYABLE or
    FAILOVER-WORTHY error is not the same thing as a BAD REQUEST. Retrying a
    malformed request against a second endpoint just launders a bug into a
    second provider's rate limit and hides the real cause. Those classes are
    separated in `_classify` and tested.

This file contains NO launch-decoding logic and knows nothing about pump.fun.
It is generic infra, the same tier as the EVM client, usable by anything.

TWO REAL-WORLD FACTS, both discovered by calling mainnet-beta rather than by
reading documentation, both of which silently break a naive client:

  TRANSACTION VERSIONS. `getTransaction` REFUSES a transaction whose version
  is above `maxSupportedTransactionVersion`, with error -32015, instead of
  degrading. Live pump.fun traffic contains version-1 transactions today, so
  the once-canonical `maxSupportedTransactionVersion: 0` drops real activity
  and looks exactly like a quiet program. The default here is
  MAX_SUPPORTED_TX_VERSION and it is sent on every call.

  SIGNATURE PAGINATION IS SIGNATURE-BASED, NOT SLOT-BASED.
  `getSignaturesForAddress` takes `before`/`until` as SIGNATURES. A resume
  position therefore cannot be only a slot number -- see position_store.py,
  which stores both and explains why.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

# Version-1 transactions are live on mainnet-beta today. Sending 0 (the value
# most older examples use) makes getTransaction fail with -32015 on them, and
# the failure looks like missing activity rather than a client misconfiguration.
MAX_SUPPORTED_TX_VERSION = 1

# getSignaturesForAddress caps at 1000 server-side; ask for that and let the
# caller page rather than guessing a smaller window.
MAX_SIGNATURE_PAGE = 1000

DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MIN_INTERVAL_S = 0.12
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_MAX_ATTEMPTS = 5

# JSON-RPC codes that mean "this request is wrong", so retrying it anywhere is
# pointless and failing over just spends another provider's budget on a bug.
BAD_REQUEST_CODES = frozenset({
    -32600,   # invalid request
    -32601,   # method not found
    -32602,   # invalid params
    -32015,   # unsupported transaction version -- a CONFIG bug, not a blip
})

# Codes/conditions worth trying again, here or on the next endpoint.
RETRYABLE_CODES = frozenset({
    -32005,   # node is behind / rate limited
    -32004,   # block not available for slot
    -32007,   # slot skipped or missing
    -32009,   # long-term storage slot skipped
    -32014,   # block status not yet available
})


def _host_of(url: str) -> str:
    """Hostname only. Used anywhere an endpoint is named in output.

    Endpoint URLs carry API keys in the path, so the full URL must never reach
    a log line, a stats dict or an exception message.
    """
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or "?"
    except Exception:  # noqa: BLE001
        return "?"


class SolanaRpcError(RuntimeError):
    """A JSON-RPC level error. `retryable` decides whether to try again."""

    def __init__(self, message: str, *, code: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SolanaRpcUnavailable(SolanaRpcError):
    """Every endpoint was tried and none answered. Caller should back off."""


@dataclass
class _Endpoint:
    url: str
    cooldown_until: float = 0.0
    failures: int = 0
    calls: int = 0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until


@dataclass
class SolanaRpcClient:
    """Multi-endpoint Solana JSON-RPC client.

    urls is tried in order; the first healthy endpoint serves the call. An
    endpoint that fails a retryable/transport error is put in cooldown so the
    next call skips it immediately instead of paying its timeout again.
    """
    urls: Sequence[str]
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    cooldown_s: float = DEFAULT_COOLDOWN_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_supported_tx_version: int = MAX_SUPPORTED_TX_VERSION

    _endpoints: list[_Endpoint] = field(default_factory=list, repr=False)
    _sem: asyncio.Semaphore | None = field(default=None, repr=False)
    _last_call_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock | None = field(default=None, repr=False)
    _client: Any = field(default=None, repr=False)
    _next_id: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        clean = [u.strip() for u in self.urls if u and u.strip()]
        if not clean:
            raise ValueError(
                "SolanaRpcClient needs at least one endpoint URL. An empty "
                "list here means the service would start up and fail on its "
                "first call, which reads as a dead chain rather than as "
                "missing configuration.")
        # Preserve order (it is a priority list) while dropping duplicates.
        seen: set[str] = set()
        self._endpoints = [_Endpoint(u) for u in clean
                           if not (u in seen or seen.add(u))]

    # --- lifecycle --------------------------------------------------------

    async def _ensure(self) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- plumbing ---------------------------------------------------------

    @staticmethod
    def _classify(code: int | None, message: str) -> bool:
        """True when trying again (here or elsewhere) could plausibly help.

        Kept as one function with one rule so the retry policy is readable in
        one place rather than scattered through call sites.
        """
        if code in BAD_REQUEST_CODES:
            return False
        if code in RETRYABLE_CODES:
            return True
        low = message.lower()
        if "rate limit" in low or "too many requests" in low:
            return True
        # Unknown codes are treated as retryable ONCE-per-endpoint rather than
        # as fatal: an unrecognised transient beats a hard stop, and the
        # attempt budget bounds the cost either way.
        return code is None or code not in BAD_REQUEST_CODES

    async def _throttle(self) -> None:
        """Space calls out per instance. Cheap insurance against self-inflicted 429s."""
        assert self._lock is not None
        async with self._lock:
            gap = time.monotonic() - self._last_call_at
            if gap < self.min_interval_s:
                await asyncio.sleep(self.min_interval_s - gap)
            self._last_call_at = time.monotonic()

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """One JSON-RPC call, with throttle, retry and endpoint failover."""
        await self._ensure()
        assert self._sem is not None
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method,
                   "params": params or []}

        last: Exception | None = None
        attempts = 0
        for ep in self._rotation():
            if attempts >= self.max_attempts:
                break
            attempts += 1
            try:
                async with self._sem:
                    await self._throttle()
                    ep.calls += 1
                    r = await self._client.post(ep.url, json=payload)
                if r.status_code == 429:
                    raise SolanaRpcError(f"{_host_of(ep.url)} rate limited (HTTP 429)",
                                         retryable=True)
                if r.status_code >= 500:
                    raise SolanaRpcError(
                        f"{_host_of(ep.url)} HTTP {r.status_code}", retryable=True)
                if r.status_code >= 400:
                    # A 4xx that is not 429 is our request's fault.
                    raise SolanaRpcError(
                        f"{_host_of(ep.url)} HTTP {r.status_code}: {r.text[:200]}",
                        retryable=False)
                body = r.json()
            except SolanaRpcError as e:
                last = e
                if not e.retryable:
                    raise
                self._penalise(ep)
                await self._backoff(attempts)
                continue
            except Exception as e:  # noqa: BLE001 -- transport/JSON failures
                last = SolanaRpcError(f"{_host_of(ep.url)}: {type(e).__name__}: {e}",
                                      retryable=True)
                self._penalise(ep)
                await self._backoff(attempts)
                continue

            if isinstance(body, dict) and "error" in body:
                err = body["error"] or {}
                code = err.get("code")
                msg = str(err.get("message", ""))[:300]
                retryable = self._classify(code, msg)
                e = SolanaRpcError(f"{method} failed on {_host_of(ep.url)}: {code} {msg}",
                                   code=code, retryable=retryable)
                if not retryable:
                    # Surface immediately. Failing over a bad request would
                    # spend a second provider's budget proving the same bug.
                    raise e
                last = e
                self._penalise(ep)
                await self._backoff(attempts)
                continue

            ep.failures = 0
            return body.get("result")

        raise SolanaRpcUnavailable(
            f"{method}: no endpoint answered after {attempts} attempt(s); "
            f"last error: {last}") from last

    def _rotation(self) -> Iterable[_Endpoint]:
        """Available endpoints first, then the rest as a last resort.

        Yielding the cooled-down ones too matters: if every endpoint is in
        cooldown, refusing to call anything turns a transient wobble into a
        self-inflicted outage.
        """
        ready = [e for e in self._endpoints if e.available]
        cold = [e for e in self._endpoints if not e.available]
        for _ in range(self.max_attempts):
            for e in ready + cold:
                yield e

    def _penalise(self, ep: _Endpoint) -> None:
        ep.failures += 1
        ep.cooldown_until = time.monotonic() + self.cooldown_s
        log.warning("solana rpc endpoint cooling down host=%s failures=%d",
                    _host_of(ep.url), ep.failures)

    async def _backoff(self, attempt: int) -> None:
        # Jittered exponential. The jitter stops several workers that failed on
        # the same provider outage from retrying in lockstep forever.
        delay = min(8.0, 0.4 * (2 ** (attempt - 1)))
        await asyncio.sleep(delay * (0.5 + random.random() / 2.0))

    # --- Solana methods ---------------------------------------------------

    async def get_slot(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getSlot", [{"commitment": commitment}]))

    async def get_block_time(self, slot: int) -> int | None:
        """Unix seconds for a slot, or None.

        None is a real answer, not an error: a slot can be skipped, and
        getBlockTime legitimately returns null for one. Callers must not
        substitute `now` for it -- that silently backdates a launch to
        whenever the indexer happened to run.
        """
        return await self.call("getBlockTime", [slot])

    async def get_account_info(self, pubkey: str, *, encoding: str = "base64",
                               commitment: str = "confirmed") -> dict | None:
        res = await self.call("getAccountInfo",
                              [pubkey, {"encoding": encoding,
                                        "commitment": commitment}])
        return (res or {}).get("value")

    async def get_signatures_for_address(
        self, address: str, *, before: str | None = None,
        until: str | None = None, limit: int = MAX_SIGNATURE_PAGE,
        commitment: str = "confirmed",
    ) -> list[dict]:
        """One page of signatures, newest first.

        `before`/`until` are SIGNATURES, not slots. That is the whole reason
        the position store persists a signature alongside the slot.
        """
        opts: dict[str, Any] = {
            "limit": max(1, min(int(limit), MAX_SIGNATURE_PAGE)),
            "commitment": commitment,
        }
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return list(await self.call("getSignaturesForAddress",
                                    [address, opts]) or [])

    async def get_transaction(self, signature: str, *,
                              encoding: str = "json",
                              commitment: str = "confirmed") -> dict | None:
        """One transaction, with the version ceiling always set.

        Omitting maxSupportedTransactionVersion makes this fail on every
        versioned transaction, and pump.fun's live traffic is full of them.
        """
        return await self.call("getTransaction", [
            signature,
            {"encoding": encoding, "commitment": commitment,
             "maxSupportedTransactionVersion": self.max_supported_tx_version},
        ])

    async def get_program_accounts(self, program_id: str, *,
                                   filters: list[dict] | None = None,
                                   encoding: str = "base64",
                                   data_slice: dict | None = None) -> list[dict]:
        opts: dict[str, Any] = {"encoding": encoding}
        if filters:
            opts["filters"] = filters
        if data_slice:
            opts["dataSlice"] = data_slice
        return list(await self.call("getProgramAccounts",
                                    [program_id, opts]) or [])

    async def get_health(self) -> str:
        """Liveness probe. Deliberately the cheapest call the API offers.

        Used as the "the socket is open but dead" check -- the failure mode
        that cost this project a 9.5 hour silent outage on the EVM side.
        """
        return str(await self.call("getHealth", []))

    def stats(self) -> dict[str, Any]:
        """Per-endpoint counters, reported by HOST and never by full URL.

        A provider URL carries its API key in the path
        (`.../v2/<key>`), and this dict is not just printed: it is attached to
        every operations heartbeat as `detail`, so a full URL here would be
        posted to another service and stored in its payloads. Reporting the
        hostname keeps the diagnostic value -- which provider is failing --
        with none of the exposure.
        """
        return {"endpoints": [
            {"host": _host_of(e.url), "calls": e.calls,
             "failures": e.failures, "cooling": not e.available}
            for e in self._endpoints]}
