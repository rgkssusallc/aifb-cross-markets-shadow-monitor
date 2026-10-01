"""Reference receiver for the shadow logger's webhook. Zero dependencies.

Two ways to use this:

  1. RUN IT STANDALONE to see the feed before touching your app:
         python3 examples/webhook_receiver.py 3001
     then point cloudflared at port 3001 instead of your dev server, and run
     the logger with --webhook=https://<tunnel>/api/arb. It prints every event
     and serves the accumulated state at GET /api/arb/state, so you can shape
     your UI against real data without writing a backend first.

  2. COPY THE HANDLER into your own app. The `handle()` function below is the
     whole contract; everything around it is plumbing. A FastAPI and an
     Express version are at the bottom of this file.

The state model is the useful part. Raw events are a firehose of little use to
a UI; what a dashboard wants is:

  routes      latest cost breakdown per route, from `heartbeat`
  distribution  the percentile tail per route, from `summary`
  excursions  open ones, and recently closed ones WITH LIFETIME

Lifetime is the field that decides everything: an excursion shorter than your
round trip was never yours. Measured reference points -- the Coinbase round
trip is ~60ms, and a Coinbase->wallet->Coinbase transfer cycle took 12-53s.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ.get("SHADOW_WEBHOOK_SECRET", "")

# Keep the last N closed excursions. Enough for a histogram, bounded so a long
# run cannot grow this without limit.
KEEP_CLOSED = 500

STATE: dict = {
    "started_at": time.time(),
    "last_event_at": None,
    "counts": {},
    "routes": {},          # route -> latest breakdown
    "distribution": {},    # route -> {p50,p95,p99,max,samples}
    "open": {},            # id -> open event
    "closed": [],          # most recent first, each with lifetime_ms
}


def handle(ev: dict) -> None:
    """The entire contract. Everything else here is plumbing."""
    kind = ev.get("kind")
    STATE["last_event_at"] = ev.get("ts", time.time())
    STATE["counts"][kind] = STATE["counts"].get(kind, 0) + 1

    if kind == "heartbeat":
        for r in ev.get("routes", []):
            STATE["routes"][r["route"]] = r

    elif kind == "summary":
        STATE["distribution"] = ev.get("gross_bps_by_route", {})

    elif kind == "open":
        # Key on `id`, never on `route`. open and close agree on id; an
        # earlier version of the logger disagreed on route and every
        # excursion looked orphaned.
        STATE["open"][ev["id"]] = ev

    elif kind == "close":
        STATE["open"].pop(ev.get("id"), None)
        STATE["closed"].insert(0, ev)
        del STATE["closed"][KEEP_CLOSED:]

    elif kind == "hello":
        print("  preflight received")


def summarise() -> str:
    """One line per event, so a terminal tail is actually readable."""
    c = STATE["counts"]
    return (f"hb={c.get('heartbeat', 0)} sum={c.get('summary', 0)} "
            f"open={c.get('open', 0)} close={c.get('close', 0)} "
            f"| live={len(STATE['open'])} closed={len(STATE['closed'])}")


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        # The browser fetches state cross-origin during development.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Shadow-Token")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/api/arb/state", "/state"):
            return self._json(200, STATE)
        self._json(404, {"error": "try GET /api/arb/state"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/api/arb", "/arb"):
            return self._json(404, {"error": "post to /api/arb"})
        # Reject anything without the token. This endpoint is reachable from
        # the internet through the tunnel, and an open one accepts fabricated
        # opportunities from whoever finds it.
        if SECRET and self.headers.get("X-Shadow-Token") != SECRET:
            return self._json(401, {"error": "bad or missing X-Shadow-Token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            ev = json.loads(self.rfile.read(n))
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": f"bad json: {e}"})

        # Answer FIRST, then do the work. A slow receiver cannot slow the
        # logger -- that is tested -- but it will fill the queue and start
        # losing events, so keep the response immediate.
        self._json(200, {"ok": True})
        try:
            handle(ev)
        except Exception as e:  # noqa: BLE001 -- never die on one bad event
            print(f"  handler error on {ev.get('kind')}: "
                  f"{type(e).__name__}: {e}")
            return
        line = f"  {ev.get('iso', '')} {str(ev.get('kind')):9s}"
        if ev.get("kind") == "close":
            line += (f" lifetime {ev.get('lifetime_ms', 0) / 1000:.1f}s  "
                     f"peak {ev.get('peak_gross_bps', 0):+.2f}bps")
        elif ev.get("kind") == "open":
            line += f" gross {ev.get('gross_bps', 0):+.2f}bps"
        elif ev.get("kind") == "summary":
            for route, d in (ev.get("gross_bps_by_route") or {}).items():
                line += (f"\n      {route[:58]}  p50 {d.get('p50')} "
                         f"p95 {d.get('p95')} p99 {d.get('p99')} "
                         f"max {d.get('max')} n={d.get('samples')}")
        print(line + ("" if ev.get("kind") == "summary" else
                      f"   [{summarise()}]"))

    def log_message(self, *a: object) -> None:
        pass          # the event lines above are the log


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3001
    if not SECRET:
        print("WARNING no SHADOW_WEBHOOK_SECRET set -- accepting unauthenticated "
              "posts. Fine on localhost, not behind a public tunnel.")
    print(f"listening on :{port}")
    print(f"  POST /api/arb         <- point --webhook here")
    print(f"  GET  /api/arb/state   <- poll this from your UI\n")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- dropping this into your own app -------------------------------------
#
# FastAPI:
#
#   from fastapi import APIRouter, Header, HTTPException
#   router = APIRouter()
#
#   @router.post("/api/arb")
#   async def arb(ev: dict, x_shadow_token: str | None = Header(None)):
#       if x_shadow_token != os.environ["SHADOW_WEBHOOK_SECRET"]:
#           raise HTTPException(401)
#       handle(ev)                      # the function above, unchanged
#       return {"ok": True}
#
#   @router.get("/api/arb/state")
#   async def state():
#       return STATE
#
# Express:
#
#   app.post('/api/arb', express.json(), (req, res) => {
#     if (req.get('X-Shadow-Token') !== process.env.SHADOW_WEBHOOK_SECRET)
#       return res.sendStatus(401);
#     res.sendStatus(200);              // answer first
#     handle(req.body);                 // then update state
#   });
#   app.get('/api/arb/state', (_req, res) => res.json(STATE));
#
# Either way: answer fast, verify the token, and pair open/close on `id`.
