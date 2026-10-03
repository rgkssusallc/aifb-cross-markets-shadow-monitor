#!/bin/sh
# Map environment variables onto monitor.py's flags.
#
# Why a script rather than a long `command:` in compose: the secret must NOT
# become a command-line argument. monitor.py reads SHADOW_WEBHOOK_SECRET from
# the environment precisely because anything in argv is visible to every
# process on the box via `ps`, so this entrypoint never passes --secret.
set -eu

# Anything passed after the image name replaces the monitor entirely, so
# `docker run <image> python -V` and `docker compose run monitor sh` behave the
# way anyone would expect. Without this the entrypoint ignored its arguments
# and started a full measurement run instead, which makes the container
# effectively impossible to poke at.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

DATA_DIR="${SHADOW_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# The monitor cannot do anything without an RPC endpoint for the chain its
# candidates live on. Checked here rather than discovered per-venue, because
# the per-venue failure is one REFUSED line per pair buried in the log and the
# run then ends with "no pair had two usable venues", which reads like a
# market problem rather than a missing variable.
if [ -z "${EVM_BASE_RPC_URL:-}" ] && [ -z "${EVM_RPC_URL:-}" ]; then
  echo "arb-shadow: FATAL no Base RPC endpoint. Every candidate in" >&2
  echo "  candidates.py trades on Base, so without this nothing can be" >&2
  echo "  quoted. Set EVM_BASE_RPC_URL in docker/.env (see .env.example)." >&2
  exit 78          # EX_CONFIG: a configuration fault, not a crash
fi

# monitor.py takes a RUN DURATION and exits when it elapses (default 180s).
# For a service that is wrong: the container would exit every three minutes,
# and each restart re-validates every venue on chain and loses the in-flight
# excursions it was timing. A year is the practical "run until stopped", and
# `restart: unless-stopped` covers an actual crash.
SECONDS_TO_RUN="${SHADOW_SECONDS:-31536000}"

set -- python -u monitor.py \
  --size="${SHADOW_SIZE_USD:-1000}" \
  --seconds="${SECONDS_TO_RUN}" \
  --interval="${SHADOW_INTERVAL_S:-1.0}" \
  --heartbeat="${SHADOW_HEARTBEAT_S:-5}" \
  --summary="${SHADOW_SUMMARY_S:-60}" \
  --feed="${SHADOW_FEED_S:-15}" \
  --db="${DATA_DIR}/monitor.db" \
  --jsonl="${DATA_DIR}/events.jsonl" \
  --status="${DATA_DIR}/status.json"

# Which pairs to watch. --candidates runs the measured top N from
# candidates.py and is the normal way in; --pairs overrides it with an
# explicit list; SHADOW_PAIR is the single-pair escape hatch.
if [ -n "${SHADOW_PAIRS:-}" ]; then
  set -- "$@" --pairs="${SHADOW_PAIRS}"
elif [ -n "${SHADOW_PAIR:-}" ]; then
  set -- "$@" --pair="${SHADOW_PAIR}"
else
  set -- "$@" --candidates="${SHADOW_CANDIDATES:-10}"
fi

if [ -n "${SHADOW_WEBHOOK_URL:-}" ]; then
  set -- "$@" --webhook="${SHADOW_WEBHOOK_URL}"
  # Refuse to start on an unreachable endpoint when asked. A webhook that was
  # never reachable looks exactly like a quiet market, and the counters that
  # would reveal it are only printed at the end of a run.
  if [ "${SHADOW_REQUIRE_WEBHOOK:-0}" = "1" ]; then
    set -- "$@" --require-webhook=1
  fi
fi

echo "arb-shadow: read-only monitor, no signer, no trading."
echo "arb-shadow: data dir ${DATA_DIR}"
if [ -n "${SHADOW_WEBHOOK_URL:-}" ]; then
  # Host only. A webhook URL can carry a token in its path.
  echo "arb-shadow: webhook host $(printf '%s' "${SHADOW_WEBHOOK_URL}" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
  if [ -z "${SHADOW_WEBHOOK_SECRET:-}" ]; then
    echo "arb-shadow: WARNING no SHADOW_WEBHOOK_SECRET set -- your endpoint" \
         "will receive unauthenticated POSTs, and anyone who finds it can" \
         "feed your UI fabricated opportunities." >&2
  fi
else
  echo "arb-shadow: no webhook configured; events land in ${DATA_DIR} only"
fi

exec "$@"
