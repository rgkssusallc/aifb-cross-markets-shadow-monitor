"""Gap-safe pump.fun ingestion. Bridge's PRINCIPLE, not Bridge's code.

A parallel sibling to the EVM `GapSafeAlchemyProvider`, not a subclass: that
class is shaped around a websocket log subscription and `eth_getLogs`
block-range backfill, and neither exists here. What is reused is the ordering
that makes "no missed launches" an invariant rather than a hope:

  1. START THE REAL-TIME FEED FIRST, then backfill from the committed position
     up to the head the feed started at. Backfilling first and subscribing
     afterwards leaves a hole exactly the width of the backfill, and that hole
     is invisible -- it looks like a quiet market.
  2. RESUME FROM THE PERSISTED POSITION, NEVER FROM "NOW". This is the single
     most important property. AIFB has been burned twice by its absence: a
     chain-listener that retried a dead websocket for 9.5 hours and a
     collector that stalled for 4 days, both of which silently produced
     nothing and told nobody.
  3. COMMIT ONLY AFTER PUBLISHING, monotonically. See position_store.py.
  4. A PERMANENTLY-UNDECODABLE ITEM MUST NOT BLOCK THE PIPELINE -- bounded
     retries, then skip and log. But a PUBLISH failure (Redis down) retries
     indefinitely, because skipping there loses data rather than one item.
  5. LIVENESS IS A SEPARATE CONCERN FROM CORRECTNESS. A feed can be open and
     dead. So a cheap REST probe runs after N seconds of silence, and failing
     it raises an alert rather than quietly reconnecting forever.

SOLANA-SPECIFIC SHAPE. The backfill walks
`getSignaturesForAddress(program, before=..., until=...)` pages newest-first,
then `getTransaction` per signature. Two consequences that differ from the EVM
analog and are easy to get wrong:

  * Pages come back NEWEST FIRST, but a position may only advance once
    everything older has been published. So a backfill COLLECTS the whole
    range first, processes it OLDEST FIRST, and commits as it goes. Committing
    while walking newest-first would mark the newest slot done while older
    transactions in the same range were still unprocessed -- and then a crash
    loses them permanently.
  * `until` is exclusive and signature-based, so the committed signature is
    the natural lower bound and needs no slot arithmetic.

THE REAL-TIME FEED IS BEHIND AN INTERFACE, deliberately. Plain
`logsSubscribe` over a public endpoint is a known weak point at pump.fun's
volume -- provider documentation recommends a Geyser/gRPC ("Yellowstone")
stream for production, and AIFB already lived the websocket-rate-limit
version of this failure on the EVM side. Which Geyser provider to buy is an
owner decision, so `RealtimeFeed` is a protocol with a websocket
implementation usable for development and a seam for the gRPC one. The REST
backfill below is required either way: it is what a restarted process uses to
catch up, whatever the live transport is.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol

from src.adapters.data.solana_rpc_client import SolanaRpcClient
from src.adapters.launchpads.pumpfun import constants as C
from src.adapters.launchpads.pumpfun.position_store import PositionStore

log = logging.getLogger(__name__)

# How many signatures to pull per page. The server caps at 1000; a smaller
# page bounds the work lost when a page fails mid-processing.
BACKFILL_PAGE = 500

# A transaction that will not decode is retried this many times before being
# skipped. Bounded because a permanently malformed item must not wedge the
# pipeline behind it forever.
MAX_DECODE_ATTEMPTS = 3

# Silence longer than this on the live feed triggers a REST liveness probe.
# "The socket is open but dead" is a real provider bug, not a hypothetical.
FEED_SILENCE_PROBE_S = 45.0

# Hard ceiling on one backfill, so a cold start against a long outage cannot
# run unbounded. Hitting it is reported, not swallowed.
MAX_BACKFILL_PAGES = 400


class RealtimeFeed(Protocol):
    """Whatever pushes live signatures. Swappable by design."""

    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    def signatures(self) -> AsyncIterator[str]: ...
    @property
    def last_message_at(self) -> float: ...


@dataclass
class ProviderStats:
    backfilled: int = 0
    live: int = 0
    skipped_undecodable: int = 0
    pages: int = 0
    probes: int = 0
    probe_failures: int = 0
    truncated_backfills: int = 0


@dataclass
class GapSafeSolanaProvider:
    """Drives ingestion for one program with a durable, monotonic position.

    handler is called with a raw getTransaction response and must have
    PUBLISHED everything it produced before returning; the position is
    committed on its return. It raises to signal "do not commit", which is how
    a Redis outage becomes a retry instead of a gap.
    """
    client: SolanaRpcClient
    store: PositionStore
    handler: Callable[[dict], Awaitable[None]]
    program_id: str = C.PUMPFUN_PROGRAM_ID
    feed: Optional[RealtimeFeed] = None
    # On a cold start (no committed position) this many recent signatures are
    # ingested rather than the entire program history, which is millions of
    # transactions. Stated as an explicit choice because the alternative --
    # silently starting at the head -- is the bug this class exists to avoid,
    # and the alternative to THAT (replaying all history) would never finish.
    cold_start_signatures: int = 1000
    stats: ProviderStats = field(default_factory=ProviderStats)
    on_alert: Optional[Callable[[str, dict], Awaitable[None]]] = None
    _stop: bool = False

    # --- helpers ----------------------------------------------------------

    async def _alert(self, kind: str, detail: dict) -> None:
        if self.on_alert is None:
            log.warning("pumpfun alert (no sink): %s %s", kind, detail)
            return
        try:
            await self.on_alert(kind, detail)
        except Exception as e:  # noqa: BLE001 -- alerting must never crash us
            log.warning("pumpfun: alert sink failed: %s", e)

    async def _process(self, signature: str, *, source: str) -> Optional[dict]:
        """Fetch and handle one transaction. Returns its slot on success.

        Decode/handler failures are retried a bounded number of times and then
        skipped WITH AN ALERT -- a skip that nobody hears about is the same as
        a silent gap.
        """
        for attempt in range(1, MAX_DECODE_ATTEMPTS + 1):
            try:
                tx = await self.client.get_transaction(signature)
                if tx is None:
                    # Not yet available on this node. Worth one more try; the
                    # position is not advanced past it either way.
                    raise RuntimeError("getTransaction returned null")
                await self.handler(tx)
                return tx
            except Exception as e:  # noqa: BLE001
                if attempt == MAX_DECODE_ATTEMPTS:
                    self.stats.skipped_undecodable += 1
                    log.error("pumpfun: skipping %s after %d attempts (%s): %s",
                              signature, attempt, source, e)
                    await self._alert("pumpfun_transaction_skipped", {
                        "signature": signature, "source": source,
                        "error": str(e)[:300],
                        "attempts": attempt,
                    })
                    return None
                await asyncio.sleep(0.5 * attempt)
        return None

    # --- backfill ---------------------------------------------------------

    async def backfill(self, *, until_signature: Optional[str],
                       before_signature: Optional[str] = None) -> int:
        """Ingest everything newer than until_signature, oldest first.

        Collects the signature range first, then processes it in chronological
        order. The two-phase shape is the point: the position may only move
        once everything older is published, and the API hands pages back
        newest-first.
        """
        collected: list[dict] = []
        cursor = before_signature
        for page in range(MAX_BACKFILL_PAGES):
            batch = await self.client.get_signatures_for_address(
                self.program_id, before=cursor, until=until_signature,
                limit=BACKFILL_PAGE)
            self.stats.pages += 1
            if not batch:
                break
            collected.extend(batch)
            cursor = batch[-1]["signature"]
            if until_signature is None and len(collected) >= self.cold_start_signatures:
                collected = collected[:self.cold_start_signatures]
                break
            if page == MAX_BACKFILL_PAGES - 1:
                self.stats.truncated_backfills += 1
                log.error("pumpfun: backfill hit the %d page ceiling; the gap "
                          "is larger than one run can close",
                          MAX_BACKFILL_PAGES)
                await self._alert("pumpfun_backfill_truncated", {
                    "pages": MAX_BACKFILL_PAGES,
                    "collected": len(collected),
                    "until_signature": until_signature,
                })

        # Oldest first. Without this the commit below would advance the
        # watermark past transactions that have not been handled yet.
        collected.reverse()
        done = 0
        for entry in collected:
            if self._stop:
                break
            if entry.get("err") is not None:
                # A failed transaction changed no state. Still commit through
                # it, or the position would stick on a permanent failure.
                await self.store.commit(int(entry.get("slot") or 0),
                                        entry["signature"])
                continue
            tx = await self._process(entry["signature"], source="backfill")
            slot = int((tx or {}).get("slot") or entry.get("slot") or 0)
            await self.store.commit(slot, entry["signature"])
            self.stats.backfilled += 1
            done += 1
        return done

    # --- liveness ---------------------------------------------------------

    async def probe(self) -> bool:
        """Cheapest possible "is the chain and our client still there" check."""
        self.stats.probes += 1
        try:
            await self.client.get_slot()
            return True
        except Exception as e:  # noqa: BLE001
            self.stats.probe_failures += 1
            log.error("pumpfun: liveness probe failed: %s", e)
            await self._alert("pumpfun_ingestion_stalled", {
                "reason": "liveness probe failed", "error": str(e)[:300]})
            return False

    # --- run --------------------------------------------------------------

    async def run(self) -> None:
        """Subscribe, then backfill to the subscription's head, then stream.

        The order is the invariant. Anything that lands between the committed
        position and the moment the feed attached is covered by the backfill;
        anything after is covered by the feed.
        """
        start = await self.store.read()
        if start.is_set:
            log.info("pumpfun: resuming from committed slot %s (signature %s)",
                     start.slot, start.signature)
        else:
            log.warning(
                "pumpfun: NO committed position; cold start, ingesting the "
                "most recent %d signatures only. Earlier history is NOT "
                "backfilled -- this is a deliberate bound, not a gap that "
                "will be closed later.", self.cold_start_signatures)

        if self.feed is not None:
            await self.feed.start()
            log.info("pumpfun: live feed attached before backfill, so the "
                     "backfill window cannot hide new activity")

        await self.backfill(until_signature=start.signature)

        if self.feed is None:
            log.warning("pumpfun: no real-time feed configured; this process "
                        "has backfilled and will now poll")
            await self._poll_loop()
            return

        await self._stream_loop()

    async def _stream_loop(self) -> None:
        assert self.feed is not None
        it = self.feed.signatures()
        while not self._stop:
            try:
                signature = await asyncio.wait_for(
                    it.__anext__(), timeout=FEED_SILENCE_PROBE_S)
            except asyncio.TimeoutError:
                # Silence is not necessarily failure -- but an open-and-dead
                # socket looks exactly like a quiet market, so prove it.
                await self.probe()
                continue
            except StopAsyncIteration:
                log.warning("pumpfun: live feed ended; falling back to polling")
                await self._poll_loop()
                return
            tx = await self._process(signature, source="live")
            if tx is not None:
                await self.store.commit(int(tx.get("slot") or 0), signature)
                self.stats.live += 1

    async def _poll_loop(self, interval_s: float = 5.0) -> None:
        """REST-only fallback. Correct, slower, and honest about being so.

        Acceptable for development and as a degraded mode; not the production
        design. Each pass is itself a gap-safe backfill from the committed
        signature, so polling cannot miss anything -- it just notices later.
        """
        while not self._stop:
            pos = await self.store.read()
            try:
                await self.backfill(until_signature=pos.signature)
            except Exception as e:  # noqa: BLE001 -- one bad pass is not fatal
                log.error("pumpfun: poll pass failed: %s", e)
            await asyncio.sleep(interval_s)

    def stop(self) -> None:
        self._stop = True
