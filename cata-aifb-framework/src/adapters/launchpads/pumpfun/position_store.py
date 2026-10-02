"""Durable ingestion position, in Redis, advanced monotonically.

THE INVARIANT, and it is the whole file: the stored position means
"EVERYTHING AT OR BEFORE THIS HAS BEEN FULLY PROCESSED AND PUBLISHED". Not
"seen", not "fetched" -- published. A position written before the publish
succeeds turns a crash into silent data loss, which is the one failure this
design exists to prevent, because on restart the gap is never revisited and
nothing anywhere reports a problem.

WHY A SIGNATURE AND NOT JUST A SLOT. `getSignaturesForAddress` paginates with
`before`/`until` cursors that are SIGNATURES, not slot numbers. A slot is
therefore not sufficient to resume a backfill precisely -- a slot can hold
many pump.fun transactions and there is no "start from the 4th transaction in
slot N" cursor. But a signature alone is not sufficient either: it carries no
ordering, so nothing can be compared to it to enforce monotonicity, and a
stale signature cannot be detected. So BOTH are stored: the slot is the
monotonic guard, the signature is the resume cursor.

MONOTONIC-ONLY WRITES, VIA LUA. Several things can try to write a position:
the live feed and the backfill run concurrently by design (subscribe first,
then backfill, so nothing is missed in between). A plain SET would let the
backfill, which is working through OLDER history, overwrite the live feed's
newer position -- and that silently rewinds the committed watermark, which on
the next restart replays or, worse, skips. The Lua script makes
compare-and-set atomic on the server, so the position can only ever move
forward regardless of who writes or in what order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# Namespaced to pump.fun. Sharing a key namespace with the EVM bridge would
# couple two services that the isolation rule says must be separable.
KEY_PREFIX = "pumpfun-bridge"

# Set the slot and its signature together, and ONLY if the slot moves forward.
# Returning the stored slot either way lets the caller log a rejected write
# rather than assume success.
_SET_MAX_LUA = """
local cur = tonumber(redis.call('HGET', KEYS[1], 'slot') or '-1')
local new = tonumber(ARGV[1])
if new > cur then
  redis.call('HSET', KEYS[1], 'slot', ARGV[1], 'signature', ARGV[2])
  return {1, ARGV[1]}
end
return {0, tostring(cur)}
"""


@dataclass
class Position:
    slot: int
    signature: Optional[str] = None

    @property
    def is_set(self) -> bool:
        return self.slot > 0


@dataclass
class PositionStore:
    """Redis-backed position for one chain_id.

    redis is any client exposing async eval/hgetall. Injected rather than
    constructed so tests use a hand-rolled fake instead of a live server.
    """
    redis: Any
    chain_id: int
    prefix: str = KEY_PREFIX
    rejected_writes: int = 0

    @property
    def key(self) -> str:
        return f"{self.prefix}:last_processed_slot:{self.chain_id}"

    async def read(self) -> Position:
        """The committed position, or an unset Position.

        An unset position is NOT the same as "start from now". main.py decides
        what a cold start means and says so in a log line; silently starting
        at the head is how a restart loses the whole outage window.
        """
        raw = await self.redis.hgetall(self.key)
        if not raw:
            return Position(slot=0)
        got = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in dict(raw).items()
        }
        try:
            slot = int(got.get("slot") or 0)
        except (TypeError, ValueError):
            log.warning("position store: unparseable slot %r at %s; treating "
                        "as unset", got.get("slot"), self.key)
            return Position(slot=0)
        return Position(slot=slot, signature=got.get("signature") or None)

    async def commit(self, slot: int, signature: str | None) -> bool:
        """Advance the position. Returns False if the write was a rewind.

        Call this ONLY after every event from that slot has been published.
        """
        if slot <= 0:
            return False
        res = await self.redis.eval(
            _SET_MAX_LUA, 1, self.key, str(int(slot)), signature or "")
        moved = bool(int((res or [0])[0]))
        if not moved:
            # Not an error: a concurrent backfill writing an older slot is the
            # designed behaviour. Counted so an unexpected flood is visible.
            self.rejected_writes += 1
            log.debug("position store: refused rewind to slot %s at %s",
                      slot, self.key)
        return moved
