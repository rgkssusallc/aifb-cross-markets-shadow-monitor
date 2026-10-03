# Running the shadow monitor on Docker

One service: a long-running, **read-only** measurement worker. It opens
outbound connections, POSTs events to a webhook you own, and writes to one
volume. Nothing connects in to it, and nothing in the image can trade — there
is no signer, no wallet and no private key.

## Quick start

```sh
cp docker/.env.example docker/.env
# edit docker/.env -- EVM_BASE_RPC_URL is required, the rest is optional
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

Stop it with `down`. The measurements survive in the `arb-shadow-data` volume.

## The two things that catch people out

**`EVM_BASE_RPC_URL` is required.** Every candidate in `candidates.py` trades
on Base, so without it no route can be quoted. The container refuses to start
and says so (exit 78) rather than running and reporting a quiet market —
because "no pair had two usable venues" reads like a market condition, not a
missing variable. `https://mainnet.base.org` works and is rate-limited; a
provider endpoint is better for a continuous run, since the monitor polls once
a second and the code batches JSON-RPC calls.

**Inside a container, `localhost` is the container.** A webhook pointed at
`http://localhost:3000/api/arb` reaches nothing, while the monitor looks
perfectly healthy — this is the usual reason a local deployment delivers
nothing. Use `host.docker.internal` for your own machine:

```
SHADOW_WEBHOOK_URL=http://host.docker.internal:3000/api/arb
```

The compose file adds `host.docker.internal:host-gateway` so that name also
works on Linux, where Docker does not provide it by default. If your frontend
is another compose service, use its service name instead.

## Is it actually working?

```sh
docker compose -f docker/docker-compose.yml ps        # look for (healthy)
docker inspect --format '{{.State.Health.Status}}' arb-shadow-monitor
```

Health is **measured, not assumed**: the monitor rewrites `/data/status.json`
on every heartbeat, and the healthcheck fails the container if that file goes
stale. A process that is alive but has stopped sampling is the expensive
failure here — it looks exactly like a quiet market, and a sibling system once
sat in that state for 9.5 hours without telling anyone. A PID is not evidence
of work.

```sh
# what the healthcheck sees
docker compose -f docker/docker-compose.yml exec monitor \
  python /app/docker/healthcheck.py
# healthy: status 2s old, cycles=28, samples=55, open=0
```

## Getting the data out

The volume holds `monitor.db` (sqlite, the durable record), `events.jsonl`
(every event, replayable) and `status.json` (current state).

```sh
C=arb-shadow-monitor
docker cp $C:/data/events.jsonl ./events.jsonl
docker cp $C:/data/monitor.db   ./monitor.db

# or read in place
docker compose -f docker/docker-compose.yml exec monitor \
  sh -c 'tail -5 /data/events.jsonl'
```

A **named volume** is used rather than a bind mount on purpose. The container
runs as a non-root user (uid 10001); a bind-mounted host directory on Linux
arrives owned by the host user, and sqlite then fails to create its database
with a permission error that reads like a code bug. To bind-mount anyway, swap
the volume line for `- ./data:/data` and add
`user: "${UID:-1000}:${GID:-1000}"` to the service.

## Changing what it measures

Everything is environment-driven; see `.env.example` for the full list.

```sh
SHADOW_CANDIDATES=10          # the measured top N from candidates.py
SHADOW_PAIRS=WETH-USDC,...    # explicit override
SHADOW_SIZE_USD=1000          # edge is size-dependent -- see below
```

`SHADOW_SIZE_USD` is not cosmetic. Every bps figure the monitor reports is
only true at that size, and most candidate pools cannot absorb much more than
$1,000 — 27 of the 41 screened assets were rejected for exactly that. Raising
it without re-screening produces real-looking numbers for trades that cannot
fill.

```sh
docker compose -f docker/docker-compose.yml exec monitor python candidates.py
```
prints the candidate table with the measurement behind each one.

## Behind a TLS-inspecting proxy

A corporate proxy re-signs TLS with a private CA, and both the build and the
run will fail certificate verification. Disabling verification is not an
option; supply the CA instead.

Build:
```sh
docker build -f docker/Dockerfile \
  --secret id=pipca,src=/path/to/ca-bundle.crt \
  -t arb-shadow-monitor:local .
```
The secret is a mount, not a layer, so the certificate never enters the image.

At runtime the app's HTTP client uses `certifi`, so the CA has to be in that
bundle. Mount it and prepend a one-liner, or bake a derived image:

```sh
docker compose -f docker/docker-compose.yml run --rm \
  -v /path/to/ca-bundle.crt:/tmp/ca.crt:ro monitor \
  sh -c 'cat /tmp/ca.crt >> "$(python -c "import certifi;print(certifi.where())")" \
         && exec /app/docker/entrypoint.sh'
```

## Running something else in the image

Any command after the image name replaces the monitor, so the container is
easy to poke at:

```sh
docker compose -f docker/docker-compose.yml run --rm monitor python -V
docker compose -f docker/docker-compose.yml run --rm monitor \
  sh -c 'for f in tests/test_*.py; do python "$f" | tail -1; done'
```

## What was verified, and what was not

Built and run for real while writing this:

- image builds; `keccak` is proven to have a hashing backend **during the
  build**, so a missing one cannot surface later as a dead chain
- runs as uid 10001, `/app` not writable, `/data` writable
- a real measurement run: venues connected, ~1.1 samples/s, all five event
  kinds produced including `arbfeedall`
- sqlite, JSONL and status snapshot persist in the volume
- healthcheck returns healthy while sampling, and unhealthy for both a stale
  snapshot and a missing one
- missing `EVM_BASE_RPC_URL` exits 78 with an explanation
- an override command replaces the monitor as expected

Not verified: Docker Desktop on macOS or Windows (only Linux was available),
and `host.docker.internal` reaching a real frontend — that depends on your
machine. The webhook path itself was verified earlier against a live console
outside Docker.
