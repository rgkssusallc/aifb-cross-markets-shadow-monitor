# cata-aifb-framework — pump.fun (Solana) ingestion for AIFB

A standalone package implementing AIFB's **first non-EVM** chain: discovery
(new launch) and graduation detection for pump.fun on Solana mainnet-beta,
landing rows in the shared `launches` shape every other launchpad uses.

**This has nothing to do with the arb-shadow project in the repository root.**
It shares the git repository and nothing else: no imports, no configuration, no
data. It is self-contained under `cata-aifb-framework/`.

Built to the handoff spec in `design/solana/` terms, and intended to drop into
the real AIFB repo — see **`MERGE_MANIFEST.md`** before merging, because the
package carries its own mirror copies of a few AIFB domain files that must not
overwrite the real ones.

---

## Status

| | |
|---|---|
| Scope | discovery + graduation only. Downstream (Research, Risk Veto, Analyst, execution) is **out of scope** — see the design doc's non-goals ledger |
| Tests | **74 passing**, no pytest required (`./run_tests.sh`) |
| Verified against | the live authoritative IDL and 4 **real captured mainnet transactions** |
| Live end-to-end | yes, read-only: 141 transactions through the full pipeline, 1 launch decoded |
| Not done | Geyser/gRPC feed (needs a provider decision), devnet, Tier-0 progress polling, USDC-quoted coins |
| Ships disabled | `PUMPFUN_COLLECTOR_ENABLED` must be exactly `"true"`; anything else idles with zero RPC calls |

Read `design/solana/PUMPFUN_ARCHITECTURE.md` for the reasoning. It is the
primary document; this README is the operational quickstart.

---

## What the verification actually found

The spec said its on-chain facts were "confirmed-real, confirmed-stable,
confirmed-**incomplete**" and told the implementer to fetch the live IDL
first. That was the right instruction. The live IDL has **47 instructions, 28
events, 7 accounts, 42 types and 94 errors**, and four gaps changed how
ingestion has to work:

- **`create_v2` is a second launch path.** Both launch fixtures captured from
  mainnet used it; neither invoked `create`. An indexer watching only
  `create` misses them.
- **`migrate_v2` is a second graduation path.** The captured graduation used
  it. An indexer built to the spec's four instructions would have missed that
  real graduation.
- **`CompletePumpAmmMigrationEvent` carries the PumpSwap `pool` address**, so
  the separate pool-discovery loop the spec anticipated is not needed.
- **`quote_mint` is on every relevant event**, so USDC-quoted coins must be
  filtered explicitly rather than assumed away.

Two facts would have silently inverted a reasonable implementation:

- **Native SOL is the zero pubkey `1111…1111`, not the wrapped-SOL mint.**
  pump-public-docs tells integrations to *pass* WSOL, which makes it look like
  the marker. A census of 85 consecutive live events found zero-pubkey ×82,
  USDC ×2, another quote ×1. Filtering on WSOL would have dropped every
  native coin and kept the USDC ones — the exact inverse of the intended
  scope.
- **On a token-quoted coin `sol_amount` is 0** and the real figure is in
  `quote_amount` (observed: `sol_amount=0, quote_amount=4044868`). Progress
  math reading `sol_amount` returns zero rather than failing.

And two only observable from live RPC:

- **pump.fun is usually reached by CPI** at stack depth 2+, not as a
  top-level instruction. Scanning only top-level instructions reports a quiet
  program.
- **Version-1 transactions are live**, so `getTransaction` needs
  `maxSupportedTransactionVersion >= 1` or it refuses them outright.

---

## Layout

```
src/
  adapters/
    data/
      solana_rpc_client.py        generic Solana JSON-RPC; multi-endpoint
                                  failover, throttle, retry-vs-bad-request.
                                  No chain logic. Sibling of the EVM client,
                                  not a subclass.
    launchpads/
      pumpfun/                    self-contained; nothing else imports it
        constants.py              every verified fact; discriminators DERIVED
                                  from Anchor's hash rule and checked against
                                  the IDL at import
        idl/pump.json             vendored authoritative IDL (dated)
        anchor_codec.py           IDL-driven Borsh + base58. No solders.
        events.py                 event extraction: CPI inner instructions,
                                  lookup-table program resolution, both
                                  event transports
        adapter.py                -> TokenLaunch / GraduationSignal
        config.py                 own config module (isolation rule)
        position_store.py         Redis, monotonic, slot + signature
        solana_provider.py        gap-safe subscribe-then-backfill
        publisher.py              the two dedicated Redis streams
        ops_reporter.py           operations heartbeat/alerts over HTTP
        main.py                   standalone entrypoint
        *_test.py                 colocated tests
        fixtures/                 REAL captured mainnet transactions
  domain/                         MIRRORS -- see MERGE_MANIFEST.md
tools/
  capture_fixtures.py             re-capture the real test fixtures
  dry_run.py                      full pipeline on live data, no Redis
design/solana/PUMPFUN_ARCHITECTURE.md
docs/OPERATIONS_PATCH.md          the mandatory edits outside this package
docker/chain-listener-pumpfun.yml the compose service block
```

---

## Run it

```sh
./run_tests.sh                      # 74 tests, no pytest, no conftest

# Full pipeline against live mainnet. Read-only; publishes nothing.
NON_EVM_SOL_RPC_URL=... python3 tools/dry_run.py --signatures=400

# Re-capture the real fixtures (after a protocol change)
NON_EVM_SOL_RPC_URL=... python3 tools/capture_fixtures.py

# The service itself. Idles unless PUMPFUN_COLLECTOR_ENABLED is exactly "true".
python3 -m src.adapters.launchpads.pumpfun.main
```

Endpoints resolve as `SOLANA_RPC_HTTP_URL`, then
`SOLANA_RPC_HTTP_URL_SECONDARY_1..5`, then `NON_EVM_SOL_RPC_URL`, then the
public node. Only hostnames are ever logged — provider URLs carry API keys in
the path, and the stats dict is attached to every operations heartbeat.

---

## Before enabling in production

1. **Confirm the chain-ID sentinel.** `SOLANA_MAINNET_CHAIN_ID = 900001` is
   *proposed*. It is load-bearing from the first production row; changing it
   afterwards is a data migration. Design doc §3.
2. **Register `pumpfun_collector` in `src/operations/main.py`'s
   `lifespan()`.** Without it the heartbeat is silently refused with
   `reason: "unknown_component"` and the service has no staleness monitoring
   at all. `docs/OPERATIONS_PATCH.md`.
3. **Register the three alert kinds** in `src/operations/kinds.py`.
4. **Decide on a Geyser/gRPC provider.** Until then it runs the REST polling
   fallback: gap-safe, but higher latency. Plain `logsSubscribe` is a known
   weak point at pump.fun's volume, and AIFB has already paid for the
   websocket version of that lesson twice (a 9.5-hour silent chain-listener
   outage and a 4-day collector stall).
5. **Merge per `MERGE_MANIFEST.md`**, then run `static_rules_test.py` — it is
   the canary that the isolation rule is still true.
