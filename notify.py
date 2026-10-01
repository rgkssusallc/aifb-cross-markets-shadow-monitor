"""Push shadow-logger events to a frontend.

Three transports, because where the logger runs decides what can work:

  StatusFile   atomically rewrites a small JSON snapshot of current state.
               Simplest possible integration -- your frontend polls it, or any
               static file server serves it. Works everywhere.
  JsonlSink    appends one JSON object per event to a file. The durable
               event log; survives restarts and is replayable.
  Webhook      POSTs each event to a URL you own. The ONLY option when the
               logger runs in a cloud container, because that container's
               network is outbound-only: nothing can connect in to it, so a
               WebSocket or SSE server hosted beside the logger is
               unreachable from your browser.

THE LOGGER MUST NEVER WAIT ON YOUR FRONTEND. A notification is not worth a
missed sample: sampling rate is what decides whether brief dislocations are
visible at all, so a slow or dead endpoint must not slow the poll loop. Every
transport here is fire-and-forget. Webhook enqueues and returns immediately,
a background task drains the queue, and when the queue is full events are
DROPPED rather than blocking -- the drop count is reported so silent data
loss is visible.

Events are deliberately low-volume. Opportunity open/close is pushed
immediately because it is rare and it is what you want to be told about.
Raw samples are not: at several per second they would be a firehose that
tells your UI nothing it can use. Instead a heartbeat carries current state
on an interval you choose.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

# Beyond this many undelivered events we drop rather than grow without bound.
# A frontend that cannot keep up is a frontend problem, not a reason to stall
# the measurement.
MAX_QUEUE = 500

# Webhook calls are capped so a flapping opportunity cannot turn into a
# denial-of-service against your own endpoint.
MIN_POST_INTERVAL_S = 0.05


def _plain(x: Any) -> Any:
    """JSON-safe: Decimal -> float, tuples -> lists, recursively."""
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def event(kind: str, **fields: Any) -> dict[str, Any]:
    """Build one event. kind is open | close | heartbeat | error."""
    out = {"kind": kind, "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out.update(_plain(fields))
    return out


class Sink(Protocol):
    def emit(self, ev: dict[str, Any]) -> None: ...
    async def aclose(self) -> None: ...


@dataclass
class StatusFile:
    """Current state as one small JSON file, replaced atomically.

    Written via a temp file and os.replace so a frontend polling it never
    reads a half-written document -- the rename is atomic, so a reader sees
    either the old snapshot or the new one, never a truncated one.
    """
    path: str
    state: dict[str, Any] = field(default_factory=dict)

    def emit(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "heartbeat":
            self.state = ev
        elif kind in ("open", "close"):
            recent = list(self.state.get("recent", []))
            recent.insert(0, ev)
            self.state["recent"] = recent[:20]
        self._write()

    def _write(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass  # never let a disk problem stop the measurement

    async def aclose(self) -> None:
        self._write()


@dataclass
class JsonlSink:
    """Append-only event log, one JSON object per line."""
    path: str
    _fh: Any = field(default=None, repr=False)

    def emit(self, ev: dict[str, Any]) -> None:
        try:
            if self._fh is None:
                self._fh = open(self.path, "a", buffering=1)
            self._fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        except OSError:
            pass

    async def aclose(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


@dataclass
class Webhook:
    """POST each event to your backend, without ever blocking the caller.

    url should be an endpoint you control. secret, if given, is sent as
    X-Shadow-Token so your handler can reject anything else -- this endpoint
    will be reachable from the internet, and an unauthenticated one accepts
    fabricated opportunities from anybody who finds it.
    """
    url: str
    secret: str | None = None
    timeout_s: float = 5.0
    queue: asyncio.Queue | None = field(default=None, repr=False)
    dropped: int = 0
    sent: int = 0
    failed: int = 0
    _task: Any = field(default=None, repr=False)
    _client: Any = field(default=None, repr=False)
    _last_post: float = 0.0

    def start(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=MAX_QUEUE)
        if self._task is None:
            self._task = asyncio.create_task(self._drain())

    def emit(self, ev: dict[str, Any]) -> None:
        if self.queue is None:
            self.start()
        assert self.queue is not None
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            # Drop, do not block. A missed notification costs less than a
            # missed sample, and the count makes the loss visible.
            self.dropped += 1

    async def _drain(self) -> None:
        import httpx
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Shadow-Token"] = self.secret
        assert self.queue is not None
        try:
            while True:
                ev = await self.queue.get()
                if ev is None:  # shutdown sentinel
                    return
                gap = time.monotonic() - self._last_post
                if gap < MIN_POST_INTERVAL_S:
                    await asyncio.sleep(MIN_POST_INTERVAL_S - gap)
                try:
                    r = await self._client.post(self.url, json=ev, headers=headers)
                    self._last_post = time.monotonic()
                    if r.status_code >= 400:
                        self.failed += 1
                    else:
                        self.sent += 1
                except Exception:  # noqa: BLE001 -- your endpoint is not our problem
                    self.failed += 1
                    self._last_post = time.monotonic()
        except asyncio.CancelledError:
            pass
        finally:
            if self._client is not None:
                await self._client.aclose()

    async def aclose(self) -> None:
        if self.queue is not None:
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def preflight(self) -> tuple[bool, str]:
        """POST one hello event and say plainly whether it arrived.

        Without this the logger starts happily and accumulates `failed`
        counts that nobody reads until the run is over -- the worst way to
        discover that an endpoint was never reachable. The common causes are
        worth distinguishing, because the fix differs:

          egress proxy 403  -> the host is not in the environment's allowed
                               domains, or the policy change has not reached
                               this container yet (it applies at startup)
          connect refused   -> the tunnel or server is not running
          401 / 403 body    -> reachable, but the token was rejected
        """
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Shadow-Token"] = self.secret
        ev = event("hello", note="preflight from the shadow logger")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(self.url, json=ev, headers=headers)
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:140]
            # The egress proxy's CONNECT code distinguishes the two causes,
            # and they need opposite fixes. Verified by probing a host known
            # to be denied against one known to be allowed:
            #   403 -> policy denial: the host is not in the allowed domains
            #   502 -> policy ALLOWS it; nothing is listening upstream
            # Reporting 502 as a policy problem sends you to change a setting
            # that is already correct, which is worse than saying nothing.
            if "403" in msg:
                return False, (
                    f"{type(e).__name__}: {msg}\n"
                    "    -> policy denial. Add this host to the environment's "
                    "allowed domains. Note a leading '*.' matches subdomains "
                    "only, not the bare domain."
                )
            if "502" in msg:
                return False, (
                    f"{type(e).__name__}: {msg}\n"
                    "    -> the host IS allowed, but nothing answered. Start "
                    "the tunnel or server and check the URL."
                )
            return False, f"{type(e).__name__}: {msg}"
        if r.status_code in (401, 403):
            return False, (f"HTTP {r.status_code} -- reachable, but the "
                           f"endpoint rejected X-Shadow-Token")
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
        return True, f"HTTP {r.status_code}"

    def stats(self) -> str:
        return (f"webhook sent {self.sent} failed {self.failed} "
                f"dropped {self.dropped}")


@dataclass
class Fanout:
    """Emit to several sinks; one failing sink never affects the others."""
    sinks: list[Sink] = field(default_factory=list)

    def emit(self, ev: dict[str, Any]) -> None:
        for s in self.sinks:
            try:
                s.emit(ev)
            except Exception:  # noqa: BLE001
                pass

    async def aclose(self) -> None:
        for s in self.sinks:
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> str:
        bits = [s.stats() for s in self.sinks if hasattr(s, "stats")]
        return "  ".join(bits)


def build(status: str | None = None, jsonl: str | None = None,
          webhook: str | None = None, secret: str | None = None) -> Fanout:
    """Assemble the sinks named on the command line."""
    sinks: list[Sink] = []
    if status:
        sinks.append(StatusFile(status))
    if jsonl:
        sinks.append(JsonlSink(jsonl))
    if webhook:
        wh = Webhook(webhook, secret=secret)
        sinks.append(wh)
    return Fanout(sinks)
