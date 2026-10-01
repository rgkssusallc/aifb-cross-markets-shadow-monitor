# Ops console integration — backend and React changes

Handover for wiring the arbitrage shadow logger into the Cross-Market Ops
Console (Vite + React + TypeScript, backend behind `/api/*`).

The logger measures whether a cross-venue price dislocation exists, how large
it is, and **how long it survives**. It never trades: it holds no credentials
and has no order path, so nothing here moves the console off PAPER.

---

## 1. The data flow, and the one constraint that shapes it

```
[logger process] ──POST /api/arb──▶ [your backend] ──GET /api/arb/state──▶ [React]
  outbound only                      accumulates             polls every 2-5s
```

**A browser cannot be a webhook target.** The POST has to land on a server, so
this is not a React-only change: the backend receives and accumulates, React
reads. Everything behind `/api/*` already routes to your backend (that is
what returns the structured `{"error":{"code":"not_found"}}` JSON, where every
other path gets Vite's SPA fallback), so that is where the receiver belongs.

The logger only ever makes **outbound** connections. Nothing can connect *in*
to it, so there is no logger API for React to call and no WebSocket to open
against it. Push to your backend, poll from the browser.

---

## 2. Backend

### Two endpoints

```
POST /api/arb         receive one event; verify X-Shadow-Token; 200 fast
GET  /api/arb/state   return the accumulated state for the UI
```

Answer the POST **before** doing any work. A slow receiver cannot slow the
logger — that is tested, with a receiver sleeping 2s on every third request —
but it fills the logger's queue and events start being dropped.

Verify `X-Shadow-Token` against the logger's `--secret` and reject otherwise.
Behind a public tunnel this endpoint is internet-reachable, and an open one
accepts fabricated opportunities from whoever finds it.

### The four event kinds

Verified payloads from live runs. Every event carries `kind`, `ts` (epoch
seconds, float) and `iso`.

**`open` / `close`** — one excursion. **Pair them on `id`, never on `route`.**

```json
{"kind":"open","ts":1790894786.13,"iso":"2026-10-01T22:46:26",
 "id":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
 "route":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
 "size_usd":1000.0,"gross_bps":5.10,"net_bps":-11.81}
```

```json
{"kind":"close","id":"<same id>","route":"...","size_usd":1000.0,
 "lifetime_ms":193663.0,"peak_gross_bps":7.41,"samples":47}
```

**`heartbeat`** — current state, every `--heartbeat` seconds.

```json
{"kind":"heartbeat","cycles":125,"samples":187,"open_count":1,
 "best_gross_bps":7.41,
 "routes":[{"route":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
   "size_usd":1000.0,"gross_bps":1.80,"net_bps":-14.43,
   "fee_bps":-15.00,"slip_bps":-1.21,"gas_bps":-0.019,
   "assumptions":["USDC=USD (market, basis +0.00bps)"]}]}
```

**`summary`** — the distribution, every `--summary` seconds.

```json
{"kind":"summary","gross_bps_by_route":{
  "SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus":
    {"p50":3.94,"p95":5.90,"p99":6.50,"max":7.41,"samples":102}}}
```

**`hello`** — the logger's preflight. Answer 2xx; nothing else to do.

### The state to accumulate

Raw events are a firehose a UI cannot use. Keep four things:

| key | from | purpose |
|---|---|---|
| `routes` | `heartbeat` | latest cost breakdown per route |
| `distribution` | `summary` | percentile tail per route |
| `open` | `open` / `close` | excursions currently live |
| `closed` | `close` | recent closed ones, **with `lifetime_ms`** |

Cap `closed` (500 is plenty for a histogram) so a long run cannot grow it
without bound.

`examples/webhook_receiver.py` in this repo is a working implementation: a
zero-dependency standalone server whose `handle()` function is the entire
contract (~20 lines), with FastAPI and Express variants in a comment at the
bottom. It can also be **run standalone before touching your app** — point
the tunnel at its port and shape the UI against real data first.

---

## 3. React

### Types

```ts
export type ArbEventKind = 'open' | 'close' | 'heartbeat' | 'summary' | 'hello';

export interface RouteBreakdown {
  route: string;
  size_usd: number;
  gross_bps: number;      // the market: what a frictionless round trip would give
  net_bps: number;        // after fees, slippage and gas
  fee_bps: number;
  slip_bps: number;
  gas_bps: number;
  assumptions: string[];  // unproven equivalences this route leans on
}

export interface Percentiles {
  p50: number; p95: number; p99: number; max: number; samples: number;
}

export interface ClosedExcursion {
  id: string; route: string; size_usd: number;
  lifetime_ms: number;        // the field that decides everything
  peak_gross_bps: number; samples: number;
  ts: number; iso: string;
}

export interface ArbState {
  last_event_at: number | null;
  counts: Partial<Record<ArbEventKind, number>>;
  routes: Record<string, RouteBreakdown>;
  distribution: Record<string, Percentiles>;
  open: Record<string, { id: string; route: string; gross_bps: number }>;
  closed: ClosedExcursion[];  // most recent first
}
```

### Four changes, in the console's existing idiom

The app already uses `createClients` → `ClientsProvider` → `readConfig`, so
this needs no new patterns.

1. **`src/api/arb.ts`** — a client exposing `getState(): Promise<ArbState>`.
2. **`src/api/factory.ts`** — register it alongside the existing clients.
3. **`src/hooks/useArbState.ts`** — poll `getState()` on an interval, expose
   `{ state, lastSeenAgoMs, isStale }`.
4. **`src/lib/config.ts`** — base URL and poll interval.

**Poll every 2–5 seconds.** The logger samples at about 0.6/s for SUI/USDC,
bounded by the FlowX quote endpoint's ~500ms latency (two quotes per leg, and
it does not rate limit — 60 of 60 requests succeeded at every rate tried).
Polling faster than the data changes only burns renders.

No WebSocket is needed in the browser. The push already happens server-side;
SSE from your backend is a later refinement, not a prerequisite.

Derive liveness from `last_event_at`: if it is older than roughly three
heartbeat intervals, show the feed as stale rather than showing stale numbers
as if they were current.

---

## 4. What to render, and why

Three things earn the space.

### a. Distribution per route, with break-even drawn on it

This is the entire question in one chart. Plot `p50 / p95 / p99 / max` from
`distribution` and draw a horizontal break-even line.

**Which break-even depends on how capital is held**, and the difference
decides the strategy:

| | overhead | gap must last |
|---|---|---|
| pre-positioned inventory | 0 bps | ≥ 60 ms (round-trip latency) |
| funds moved per trade | ~29 bps | ≥ 30 s (transfers took 12–53 s) |

So break-even is roughly **16.7 bps** pre-positioned against **45 bps** on a
transfer cycle. For context, an operator running this pair live measured a
3-day p99 of **+43.8 bps** — which clears the first and not the second. Let
the user toggle the line between the two modes; it is the most informative
control on the page.

### b. Lifetime histogram of `closed`

Buckets with markers at **60 ms**, **1 s**, **30 s**. A 5.6-minute sample gave
4 excursions with mean lifetime 108 s and max 194 s, of which **3 of 4
survived 30 s**. That split is the whole argument for pre-positioned
inventory, and it is invisible in any point-in-time view.

This is also the half of the picture the operator's own data cannot provide:
at 10-second sampling a 2-second gap is invisible, and a gap seen once may be
a quote glitch rather than a market.

### c. The `assumptions` array, as a visible badge

Not a tooltip. A route tagged `USDC=USD (market, basis +0.00bps)` depends on a
basis nobody has guaranteed, and it must not look as solid as a route that
stands on its own.

### What to leave out

A live net-edge ticker. `net_bps` is negative essentially always, so a ticker
trains the user to stop looking at the page. Show `gross_bps` as the market
and `net_bps` only in the breakdown, next to the costs that explain it.

---

## 5. Operational notes

**Delivery is at-most-once; there are no retries.** A failed POST is counted
and dropped. That is deliberate — sampling rate decides whether brief
dislocations are visible at all, and a retry queue competing with the
measurement loop would cost more than the lost notification. Order is
preserved among delivered events, but a failure leaves a hole that is not
backfilled.

**So the webhook is a feed, not a ledger.** Run the logger with
`--jsonl=events.jsonl` as well and treat that file as the record of truth. The
logger prints `webhook sent N failed N dropped N` on exit, so loss is never
silent.

**Tunnel setup.** `*.trycloudflare.com` is already in the environment's
allowed domains. Note the wildcard covers subdomains only, not the bare
domain. The logger's preflight distinguishes the two failure causes, which
need opposite fixes:

```
403 Forbidden   -> policy denial: host not in allowed domains
502 Bad Gateway -> host IS allowed; nothing is listening (tunnel down / wrong URL)
```

Free tunnel URLs rotate on restart. The allowlist entry covers all of them,
but the logger's `--webhook` argument changes each time; a named tunnel on
your own domain fixes that.

**Where the logger should eventually run.** Hosted in a cloud session it stops
whenever the session goes idle — a 40-minute run managed about 5. For
sustained monitoring it belongs next to your backend, at which point the
tunnel becomes unnecessary and the status-file and JSONL transports become
available too. Treat the tunnel as the development loop: verify the receiver
handles all four kinds against a real feed, then move the logger local.

---

## 6. Checklist

- [ ] Backend: `POST /api/arb` — verify token, 200 immediately, then update state
- [ ] Backend: `GET /api/arb/state` — return the accumulated state
- [ ] Backend: cap `closed` at ~500 entries
- [ ] React: types above in `src/api/arb.ts`
- [ ] React: register the client in `src/api/factory.ts`
- [ ] React: `useArbState` polling at 2–5 s, with staleness from `last_event_at`
- [ ] React: distribution chart with a break-even line, mode-toggleable
- [ ] React: lifetime histogram, markers at 60 ms / 1 s / 30 s
- [ ] React: `assumptions` rendered as a badge
- [ ] Env: `SHADOW_WEBHOOK_SECRET` shared between backend and logger
- [ ] Run: `--jsonl` alongside `--webhook`, and `--require-webhook=1` so a run
      refuses to start against a dead endpoint

### Running the logger against the console

```bash
PYTHONPATH=. python3 monitor.py --pair=SUI-USDC --size=1000 --seconds=1800 \
  --webhook=https://<tunnel>.trycloudflare.com/api/arb \
  --secret=$SHADOW_WEBHOOK_SECRET \
  --jsonl=events.jsonl --require-webhook=1 \
  --interval=1.0 --heartbeat=10 --summary=120
```

See `WEBHOOK.md` for the transport contract in full.
