# Required edits to `src/operations/` — the only files outside the package

The pump.fun collector reports over the HTTP contract, so it does not import
`operations`. But operations has to be told the component and the alert kinds
exist, or reporting is silently discarded.

## 1. Register the component — `src/operations/main.py`

In `lifespan()`, beside the existing entries:

```python
_state["expected"]["pumpfun_collector"] = 60   # seconds; see PumpfunConfig
```

**Without this the heartbeat write is refused with
`reason: "unknown_component"` and the service has NO staleness monitoring at
all.** The response is still HTTP 200, so nothing looks wrong from the
outside — `ops_reporter.py` detects that exact body and logs an error naming
this edit, but the only real fix is here.

`STALE_MULTIPLE = 3` means 180s of silence becomes a `job_not_run` alert.

## 2. Register the alert kinds — `src/operations/kinds.py`

Add to `REGISTRY`, each with an explicit `AutoResolve` choice:

| Kind | Raised when | Suggested AutoResolve | Why |
|---|---|---|---|
| `pumpfun_ingestion_stalled` | liveness probe fails after feed silence | `HARM_UNDONE` | Resolves itself once the feed recovers; the gap is closed by the backfill |
| `pumpfun_transaction_skipped` | a transaction failed its bounded decode/publish retries and was skipped | `NEVER` | A skipped transaction is permanent data loss for that item and a human should look at it |
| `pumpfun_backfill_truncated` | a backfill hit the page ceiling, so the gap is wider than one run can close | `NEVER` | Means history was not fully recovered; needs a decision, not a timer |

An unregistered kind is still **stored** — never silently dropped — but is
flagged `payload.unregistered_kind` and never auto-resolves. That is a
visible "not finished properly" signal rather than a hard failure, which is
why this step is cheap to forget and worth doing.
