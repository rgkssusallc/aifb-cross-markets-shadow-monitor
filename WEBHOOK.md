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

## The five event kinds

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

### `arbfeedall` — every candidate as a table row, every `--feed` seconds

A periodic full snapshot of all candidates, profitable or not. The other four
kinds are unchanged by this one: `open`/`close` remain the thing to alert on,
`arbfeedall` is for the always-on table.

```json
{"kind":"arbfeedall","ts":1790903568.0,"iso":"2026-10-02T01:12:48",
 "size_usd":1000.0,
 "rows":[{
  "asset":"SUI","quote":"USDC",
  "gap_bps":5.72919821994756,"net_bps":-15.001132783080005,
  "good_for_usd":0.0,
  "buy_at":1.1688,"buy_venue":"coinbase:SUI-USDC:ws","ask_size_usd":59732.0,
  "sell_at":1.1690693995200188,"sell_venue":"sui:flowx:cetus","bid_size_usd":null,
  "venues":"coinbase:SUI-USDC:ws->sui:flowx:cetus",
  "size_usd":1000.0,
  "quoted_iso":"2026-10-02T01:12:48","quoted_age_ms":1224.6,
  "depth_basis":{"ask":"book walked to 10bps",
                 "bid":"curve: leg cannot quote arbitrary sizes"},
  "assumptions":["USDC=USD (market, basis +0.00bps)"],
  "warnings":[],"ok":true,"reason":null}]}
```

Columns: `asset`/`quote` · `gap_bps` (gross, the market before our costs) ·
`net_bps` (after fees, slippage and gas — the number to trust) ·
`good_for_usd` · `buy_at`+`buy_venue` · `ask_size_usd` · `sell_at`+`sell_venue`
· `bid_size_usd` · `venues` · `quoted_iso`+`quoted_age_ms`.

Four things are not guessable from the shape:

- **`good_for_usd` is capacity, not the size quoted** — the largest notional at
  which net edge is still positive. `0.0` when nothing is profitable, which is
  a real answer rather than missing data; render it as `—`. It is only searched
  when the probed size is already net-positive, because solving it costs quotes
  and there is no point paying to confirm a negative.
- **`null` and `0` mean different things.** `null` is *not measurable* — an
  aggregator leg is pinned to the sizes already fetched and genuinely cannot
  answer without more network calls. `0.0` is *measured as zero*. Do not
  coalesce them: a fabricated depth number reads as a measurement, which is
  worse than a blank.
- **`ask_size_usd` and `bid_size_usd` share one definition** — USD notional
  tradeable within 10bps of slippage. It is the only way to put an order book
  and a bonding curve in the same column, and `depth_basis` says which was
  used: `book walked to 10bps`, `book: empty`, `book: no touch`,
  `curve bisected to 10bps`, `curve: >= N at 10bps` (search ceiling hit, so a
  lower bound), `curve: no price`, `curve: leg cannot quote arbitrary sizes`.
  Show it on hover over the size cell.
- **`warnings` are invariant violations where `net_bps` survives.** The one you
  will see is `positive slippage: fee/slip split unreliable, net is sound`,
  which fires when an aggregator's tiny probe and its real quote route through
  different pools at different fee tiers. Render it as a caution, not an error,
  and do not hide the row.

`ok:false` carries a short `reason` and nulls everywhere else: the route could
not be quoted this tick. **Keep the row on screen, greyed.** A candidate that
vanishes reads as a UI bug, and "we could not quote this" is information.

`assumptions` lists the unproven equivalences the row leans on.
`WETH=ETH (contract, 1:1)` is proven on chain; `USDC=USD (market, basis +Xbps)`
is not — a positive net that depends on a peg holding is a weaker claim than
one that does not.

Rows arrive in route-enumeration order, not sorted, and the same `asset`
appears once per directed venue pair. Sort client-side on `net_bps`.

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
    case 'arbfeedall': setFeedRows(ev.rows, ev.size_usd); break;
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

**Fail closed when the secret is not configured, and say so with 503.** Both
receivers above compare against the environment variable directly, so an
unset `SHADOW_WEBHOOK_SECRET` either throws or — worse, with a `.get()` —
compares `None` to `None` and accepts everything. Returning 503 instead of
401 for that case is the better signal, because it separates "this endpoint
is not configured yet" from "your token is wrong", and those need opposite
fixes. The logger's preflight distinguishes them and prints the remedy:

| Response | Meaning |
|---|---|
| proxy 403 | the host is not in the environment's allowed domains |
| proxy 502 | the host **is** allowed; nothing is listening upstream |
| 403 with `allowedHosts` in the body | a Vite dev server rejected the tunnel's `Host` before the API saw it — add the host to `server.allowedHosts`, or point the tunnel at the API port |
| 503 | endpoint live but fail-closed, usually no secret configured |
| 401 / 403 | reachable, token rejected |

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
export SHADOW_WEBHOOK_SECRET=...        # read from the environment, not argv
PYTHONPATH=. python3 monitor.py --pair=SUI-USDC --size=1000 --seconds=3600 \
  --webhook=https://your-app/api/arb \
  --jsonl=events.jsonl \
  --interval=1.0 --heartbeat=10 --summary=120 --feed=15
```

The secret is read from `SHADOW_WEBHOOK_SECRET` rather than a `--secret` flag
on purpose: anything in argv is visible to every process on the box via `ps`.
A `--secret` is still accepted as a fallback, but prefer the environment.

`--interval=1.0` is deliberate. FlowX does not rate limit — 60 of 60 requests
succeeded at every rate tried — but each quote takes ~500ms and a leg needs
two, so spinning faster buys nothing against a 500ms upstream.
