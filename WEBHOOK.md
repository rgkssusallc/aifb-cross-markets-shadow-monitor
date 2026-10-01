# Webhook integration

What to implement on the frontend side to receive shadow-logger events.

## One setup step first

The logger runs in a cloud container whose outbound traffic goes through an
egress proxy with an allowlist. **Your app's hostname has to be added to the
environment's Network access allowed-domains list**, the same way
`api.flowx.finance` was. Until it is, every POST fails with
`ProxyError: 403 Forbidden` and the logger reports
`webhook sent 0 failed N`.

That is also why webhook is the only workable transport from the cloud: the
container's network is outbound-only, so nothing can connect *in* to it. A
WebSocket or SSE server next to the logger is unreachable from a browser. Run
the logger on your own machine instead and the status-file and JSONL
transports become available too.

## The contract

```
POST https://your-app/api/arb
Content-Type: application/json
X-Shadow-Token: <the --secret value>

<one event object>

-> any 2xx. The body is ignored.
```

One event per request. Verify `X-Shadow-Token` and reject anything else: the
endpoint is internet-reachable, and an unauthenticated one accepts fabricated
opportunities from whoever finds it.

### Delivery guarantees, stated plainly

- **At-most-once. There are no retries.** A failed POST is counted and
  dropped. This is deliberate: sampling rate decides whether brief
  dislocations are visible at all, and a retry queue competing with the
  measurement loop would cost more than the lost notification.
- **Order is preserved among delivered events, but gaps are possible.** A
  failure leaves a hole; it is not backfilled.
- **A full queue drops rather than blocks.** Verified against a receiver that
  slept 2s on every third request: the loop's sampling rate was unaffected.
- **Therefore the webhook is a feed, not a ledger.** If you need a complete
  record, run with `--jsonl=events.jsonl` as well and treat that file as the
  source of truth. The counters are printed on exit
  (`webhook sent N failed N dropped N`) so loss is never silent.

## The four event kinds

Every event carries `kind`, `ts` (epoch seconds, float) and `iso`.

### `open` / `close` — one excursion

```json
{"kind":"open","ts":1790894786.13,"iso":"2026-10-01T22:46:26",
 "id":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
 "route":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
 "size_usd":1000.0,"gross_bps":5.10,"net_bps":-11.81}
```

```json
{"kind":"close","id":"<same id>","route":"...","size_usd":1000.0,
 "lifetime_ms":50234.0,"peak_gross_bps":6.85,"samples":47}
```

**Pair them on `id`, not on `route`.** An earlier version of this used
different identity fields on the two events and every excursion looked
orphaned.

`lifetime_ms` is the field that decides everything. An excursion shorter than
your round trip was never yours. Reference points from live measurement: the
Coinbase round trip is ~60ms, and a Coinbase→wallet→Coinbase transfer cycle
took 12–53s.

### `heartbeat` — current state, every `--heartbeat` seconds

```json
{"kind":"heartbeat","cycles":22,"samples":35,"open_count":1,
 "best_gross_bps":5.10,
 "routes":[{"route":"SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus",
   "size_usd":1000.0,"gross_bps":1.80,"net_bps":-14.43,
   "fee_bps":-15.00,"slip_bps":-1.21,"gas_bps":-0.019,
   "assumptions":["USDC=USD (market, basis +0.00bps)"]}]}
```

Use it for liveness and for the cost breakdown. `assumptions` lists the
unproven equivalences a route depends on — see below, it belongs on screen.

### `summary` — the distribution, every `--summary` seconds

```json
{"kind":"summary","gross_bps_by_route":{
  "SUI-USDC:coinbase:SUI-USDC:ws=>sui:flowx:cetus":
    {"p50":-0.71,"p95":5.1,"p99":5.1,"max":5.1,"samples":15}}}
```

**This is the one to lead the dashboard with.** See "what to put on screen".

## Reference receivers

Express:

```js
app.post('/api/arb', express.json(), (req, res) => {
  if (req.get('X-Shadow-Token') !== process.env.SHADOW_WEBHOOK_SECRET) {
    return res.sendStatus(401);
  }
  const ev = req.body;
  switch (ev.kind) {
    case 'open':      openExcursion(ev.id, ev); break;
    case 'close':     closeExcursion(ev.id, ev.lifetime_ms, ev); break;
    case 'heartbeat': setLiveState(ev); break;
    case 'summary':   setDistribution(ev.gross_bps_by_route); break;
  }
  res.sendStatus(200);            // answer fast; do work asynchronously
});
```

FastAPI:

```python
@app.post("/api/arb")
async def arb(ev: dict, x_shadow_token: str = Header(None)):
    if x_shadow_token != os.environ["SHADOW_WEBHOOK_SECRET"]:
        raise HTTPException(401)
    await handle(ev)              # do not block the response on slow work
    return {"ok": True}
```

Answer quickly either way. A slow receiver cannot slow the logger — that was
tested — but it will fill the queue and start losing events.

## What to put on screen

Three things earn the space. An instantaneous net-edge ticker does not: it is
negative essentially always, which trains you to ignore the screen.

1. **The `summary` percentile tail per route, with your break-even drawn on
   it.** That is the entire question in one chart. For context, an operator
   running this pair live measured a 3-day p99 of +43.8bps, against a
   break-even near 16.7bps on pre-positioned inventory and ~45bps if funds
   move per trade.

2. **A lifetime histogram of closed excursions**, with markers at 60ms, 1s and
   30s. A 90-second sample here gave 6/6 excursions surviving 60ms but only
   2/6 surviving 30s — that split is the whole argument for pre-positioned
   inventory, and it is invisible in any point-in-time view.

3. **The `assumptions` field, visibly.** A route tagged `USDC=USD` depends on
   a basis nobody has guaranteed. It must not look as solid as a route that
   stands on its own.

## Running it

```bash
PYTHONPATH=. python3 monitor.py --pair=SUI-USDC --size=1000 --seconds=3600 \
  --webhook=https://your-app/api/arb --secret=$SHADOW_WEBHOOK_SECRET \
  --jsonl=events.jsonl \
  --interval=1.0 --heartbeat=10 --summary=120
```

`--interval=1.0` is deliberate. FlowX does not rate limit — 60 of 60 requests
succeeded at every rate tried — but each quote takes ~500ms and a leg needs
two, so spinning faster buys nothing against a 500ms upstream.
