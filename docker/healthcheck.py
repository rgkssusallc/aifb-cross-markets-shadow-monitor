"""Is the monitor still MEASURING, not merely still running?

A process that is alive but has stopped sampling is the expensive failure in
this project: it looks identical to a quiet market, and a sibling system once
sat in exactly that state for 9.5 hours without telling anyone. So the health
signal is the freshness of the status snapshot the monitor rewrites on every
heartbeat -- evidence of work, not evidence of a PID.

Exit 0 = healthy, 1 = unhealthy. Docker reads the exit code only.
"""
from __future__ import annotations

import json
import os
import sys
import time

STATUS = os.environ.get("SHADOW_STATUS_FILE", "/data/status.json")

# How stale the snapshot may get before the container is called unhealthy.
# Derived from the heartbeat interval rather than guessed: the monitor writes
# on every heartbeat, so anything beyond a few missed heartbeats is a stall.
# Mirrors the sibling convention of interval x 3, with a floor for slow venues.
HEARTBEAT_S = float(os.environ.get("SHADOW_HEARTBEAT_S", "5"))
MAX_AGE_S = max(90.0, HEARTBEAT_S * 3)


def fail(msg: str) -> None:
    print(f"unhealthy: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if not os.path.exists(STATUS):
        # Before the first heartbeat there is nothing to judge. start-period in
        # the Dockerfile covers venue connect(), so reaching here afterwards
        # means the loop never produced anything.
        fail(f"{STATUS} does not exist yet")

    age = time.time() - os.path.getmtime(STATUS)
    if age > MAX_AGE_S:
        fail(f"{STATUS} is {age:.0f}s old (limit {MAX_AGE_S:.0f}s); the "
             "monitor is running but has stopped sampling")

    try:
        with open(STATUS) as fh:
            state = json.load(fh)
    except (OSError, ValueError) as e:
        # The writer uses os.replace, so a reader sees the old file or the new
        # one, never a torn one. A parse failure here is therefore a real
        # problem rather than a race.
        fail(f"{STATUS} is unreadable: {e}")

    cycles = state.get("cycles")
    print(f"healthy: status {age:.0f}s old, cycles={cycles}, "
          f"samples={state.get('samples')}, "
          f"open={state.get('open_count')}")
    sys.exit(0)


if __name__ == "__main__":
    main()
