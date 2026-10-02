"""Publish domain events to pump.fun's own Redis streams.

Message shape is identical to the existing convention --
`XADD <stream> {"data": "<dataclass.to_json()>"}` with an approximate maxlen
trim -- so a consumer written for PONS/Arc needs no new parsing, only a new
stream name.

PUBLISH FAILURE MUST NOT BE SWALLOWED. This is the one place in the ingestion
path where an exception is the correct behaviour: the provider commits the
durable position only when the handler returns, so raising here is what turns
a Redis outage into a retry instead of a permanent gap. Every other failure in
this service is bounded-and-skipped; this one is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.adapters.launchpads.pumpfun.config import (
    STREAM_GRADUATIONS, STREAM_LAUNCHES, STREAM_MAXLEN,
)
from src.domain.graduation_signal import GraduationSignal
from src.domain.token_launch import TokenLaunch

log = logging.getLogger(__name__)


@dataclass
class RedisStreamPublisher:
    """Thin XADD wrapper. redis is injected so tests use a fake."""
    redis: Any
    maxlen: int = STREAM_MAXLEN
    launches: int = 0
    graduations: int = 0

    async def _xadd(self, stream: str, blob: str) -> None:
        await self.redis.xadd(stream, {"data": blob},
                              maxlen=self.maxlen, approximate=True)

    async def publish_launch(self, launch: TokenLaunch) -> None:
        await self._xadd(STREAM_LAUNCHES, launch.to_json())
        self.launches += 1
        log.info("pumpfun launch published token=%s symbol=%s curve=%s",
                 launch.token, launch.token_symbol, launch.curve)

    async def publish_graduation(self, signal: GraduationSignal) -> None:
        """Graduations go to their OWN stream.

        Separate because the ingestion service has no Postgres access and a
        graduation carries no launch_id -- the consumer resolves it by
        (chain_id, token_address). It also decouples ordering: a coin can be
        created and complete its curve in ONE transaction (observed on
        mainnet), so a graduation can legitimately arrive before its launch
        row exists, and the consumer must be free to handle that rather than
        have it hidden inside a single queue's ordering.
        """
        await self._xadd(STREAM_GRADUATIONS, signal.to_json())
        self.graduations += 1
        log.info("pumpfun graduation published token=%s event=%s pool=%s",
                 signal.token_address, getattr(signal, "_event", "?"),
                 getattr(signal, "_pool", None))
