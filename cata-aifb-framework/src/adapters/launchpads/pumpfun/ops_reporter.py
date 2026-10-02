"""Heartbeat and alert reporting over the operations HTTP contract.

ITS OWN COPY, not an import of src/tier0/ops_reporter.py. That module is
reachable without breaking the isolation rule -- it is arguably generic infra
like the RPC client -- but it currently lives under tier0, and importing
across into a PONS/EVM-shaped package is the seam the rule is written to stop.
A ~60-line HTTP client is a cheaper price than that coupling. Stated here
because the spec explicitly asks for the choice to be stated.

ALERTING GOES NOWHERE ELSE. Telegram and Discord are trading-signal channels
by explicit repo rule; operational alerts go to operations and only there.

TWO CONTRACT DETAILS THAT ARE EASY TO MISS:
  * The component name must be PRE-REGISTERED in operations' own lifespan()
    (`_state["expected"]["pumpfun_collector"] = <interval>`) or the heartbeat
    write is silently refused with reason "unknown_component" -- a heartbeat
    that looks delivered and is not is worse than none. See
    docs/OPERATIONS_PATCH.md for the exact edit.
  * A failing-but-running tick still counts as RAN. Staleness only ever means
    "it stopped entirely"; a tick that ran and failed must report its own
    failure through an alert kind, because advancing last_run_at is what keeps
    "stopped" and "broken" distinguishable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

# operations' staleness convention: interval x 3 before a missed heartbeat
# becomes a stored job_not_run alert.
STALE_MULTIPLE = 3


@dataclass
class OperationsReporter:
    base_url: str
    component: str
    interval_seconds: int
    timeout_s: float = 5.0
    sent: int = 0
    failed: int = 0
    _client: Any = field(default=None, repr=False)

    async def _post(self, path: str, body: dict) -> bool:
        if not self.base_url:
            log.debug("operations reporter disabled (no base url)")
            return False
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        try:
            r = await self._client.post(
                f"{self.base_url.rstrip('/')}{path}", json=body)
            if r.status_code >= 400:
                self.failed += 1
                log.warning("operations %s -> HTTP %s: %s", path,
                            r.status_code, r.text[:200])
                return False
            # A 200 does not mean accepted: an unregistered component is
            # refused in the BODY. Surface it loudly, once per occurrence.
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = {}
            reason = (payload or {}).get("reason")
            if reason == "unknown_component":
                self.failed += 1
                log.error(
                    "operations refused the heartbeat: component %r is not "
                    "registered in operations' lifespan(). Add "
                    '_state["expected"][%r] = %d -- until then this service '
                    "has NO staleness monitoring at all.",
                    self.component, self.component, self.interval_seconds)
                return False
            self.sent += 1
            return True
        except Exception as e:  # noqa: BLE001 -- never let reporting crash us
            self.failed += 1
            log.warning("operations %s failed: %s", path, e)
            return False

    async def heartbeat(self, ok: bool = True,
                        detail: Optional[dict] = None) -> bool:
        return await self._post("/api/v1/operations/heartbeat", {
            "component": self.component,
            "interval_seconds": self.interval_seconds,
            "ok": bool(ok),
            "detail": detail or {},
        })

    async def alert(self, kind: str, payload: Optional[dict] = None) -> bool:
        """Raise an alert. An unregistered kind is stored but never auto-resolves.

        That is a visible "not finished properly" signal rather than a hard
        failure, which is why new kinds must be added to operations'
        kinds.REGISTRY with an explicit AutoResolve choice.
        """
        return await self._post("/api/v1/operations/alerts", {
            "kind": kind,
            "component": self.component,
            "payload": payload or {},
        })

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
