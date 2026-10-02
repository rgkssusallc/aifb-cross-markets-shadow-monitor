# pump.fun on Solana mainnet-beta — ingestion architecture

AIFB's first non-EVM chain. pump.fun is a bonding-curve launchpad on Solana
mainnet-beta, program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, and this
document covers discovery (new launch) and graduation detection only —
everything downstream is in the non-goals ledger below.

This doc is kept deliberately separate from
`design/core/LAUNCHPAD_ADAPTER_ARCHITECTURE.md`, matching the code's own
isolation: the pump.fun package shares no production file with PONS or Arc,
and the docs should not imply otherwise.

Solana has no `eth_getLogs`, no topics and no flat log records. The ingestion
layer is therefore a redesign rather than a port. Everything else — the
`launches` schema, the domain dataclasses, the isolation rule, the alert sink,
the testing conventions — is reused exactly as-is.

---

## 1. pump.fun — verified on-chain facts (2026-10-02)

Nothing below is from training data. Two independent sources were used: the
authoritative IDL at `github.com/pump-fun/pump-public-docs` (`idl/pump.json`,
fetched and vendored into the package at
`src/adapters/launchpads/pumpfun/idl/pump.json`), and live `mainnet-beta` RPC.

### How each claim was established

| Claim | How it was verified |
|---|---|
| Program ID `6EF8rre…wF6P` | Read from the IDL's own `address` field, not typed in |
| PumpSwap `pAMMBay…fXEA` | Sibling `idl/pump_amm.json` in the same repo |
| 47 instructions / 28 events / 7 accounts / 42 types / 94 errors | Counted in the vendored IDL |
| All 75 instruction+event discriminators | **Re-derived** as `sha256("global:"+name)[:8]` and `sha256("event:"+name)[:8]` and compared to the IDL: 75 match, 0 mismatch |
| `create`/`buy`/`sell`/`migrate` discriminators | Match the handoff spec's values byte-for-byte |
| Bonding-curve PDA seeds `["bonding-curve", mint]` | Read from the IDL's `pda.seeds` for `create.bonding_curve` |
| `BondingCurve` account layout | Independently confirmed: the layout computed from the IDL gives 125 bytes, and `getProgramAccounts` with `dataSize: 125` plus a `memcmp` on the `complete` flag at offset 48 returned **10,004** graduated curves. A wrong layout returns zero |
| Native-SOL quote sentinel | Census of 85 consecutive live `CreateEvent`/`TradeEvent` payloads |
| CPI invocation depth | Read from real transactions |
| Version-1 transactions | `getTransaction` error −32015 on live signatures |

### What the spec got right

Program ID, PumpSwap as the migration target (**not** Raydium — that is the
retired model), the four core discriminators, and the PDA seeds. All confirmed
exactly.

### What was incomplete, and where it changes correctness

The spec said its list was "confirmed-real, confirmed-stable,
confirmed-incomplete". It was right, and four of the gaps matter:

1. **`create_v2` is a second launch path.** An indexer watching only `create`
   misses every v2 launch. Both launch fixtures captured for the test suite
   were created via `create_v2`; neither invoked `create`.
2. **`migrate_v2` is a second graduation path.** The graduation fixture
   captured from mainnet used `migrate_v2`. An indexer built to the spec's
   four instructions would have missed that real graduation entirely.
3. **`CompletePumpAmmMigrationEvent` carries `pool`.** The spec expected the
   post-graduation PumpSwap pool address to need a separate discovery step
   "mirroring PONS/Arc's pool-discovery pattern". It does not — the address is
   in the event payload. One less polling loop to build and operate.
4. **`quote_mint` is on `CreateEvent`, `CompleteEvent`, `TradeEvent` and the
   `BondingCurve` account.** Multi-quote coins are live now, so "native SOL
   only" has to be an explicit filter rather than an assumption.

Field counts also differ substantially: live `CreateEvent` has 19 fields (the
spec described 6) and `TradeEvent` has 34 (the spec described 10). This is why
the codec is IDL-driven — Borsh is positional, so a struct that gained a field
decodes every later field at the wrong offset and still returns
plausible-looking integers.

### Two facts that invert naive implementations

**Native SOL is the ZERO PUBKEY, not the wrapped-SOL mint.**
pump-public-docs tells integrations to *pass* `So111…112` as the quote mint,
which makes WSOL look like the marker of a native coin. The events carry
something else. Census over 85 consecutive live events:

```
11111111111111111111111111111111               x82   <- native SOL
EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v   x2    <- USDC
A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS   x1    <- another quote
```

A filter written against WSOL would have discarded every native coin — i.e.
essentially all of them — while silently keeping the USDC ones: the exact
inverse of the intended scope.

**On a token-quoted coin, `sol_amount` is 0 and the real figure is in
`quote_amount`.** Observed directly on a live USDC-quoted sell:
`sol_amount=0, quote_amount=4044868`. Any progress or volume arithmetic
reading `sol_amount` or `real_sol_reserves` therefore reports **zero** for
USDC coins rather than failing — a silent wrong answer. This is the concrete
reason USDC-quoted coins are filtered out rather than half-supported.

### Two facts learned only from live RPC

**pump.fun is usually reached by CPI.** The first live transaction inspected
was a `sell` invoked at stack depth 2 from a router program; pump.fun never
appeared as a top-level instruction. An extractor scanning only
`message.instructions` sees a fraction of real activity and reports a quiet
program. Anchor's own `emit_cpi!` events are inner instructions too, so this
is doubly true.

**Instruction program IDs must be resolved through the address lookup
tables.** Instructions reference programs by *index* into a list that is, for
a versioned transaction, the static keys **plus** the lookup-table addresses
in a defined order (static, loaded-writable, loaded-readonly). Resolving
against the static keys alone either misses or — worse — attributes an
instruction to the wrong program.

### The distinction the spec collapsed

`CompleteEvent` and `CompletePumpAmmMigrationEvent` are **different moments**:
the curve filling, and liquidity actually arriving in a PumpSwap pool. They
can be separated in time and the second can fail to happen. Treating
"complete" as "graduated" publishes a graduation for a token with no pool to
trade on. Both are modelled; only the migration event carries `pool`.

### Known staleness, stated plainly

The protocol is actively versioning (`buy_v2`, `sell_v2`,
`buy_exact_quote_in_v2`, holder-rewards coins replacing cashback, and
PumpSwap's `virtual_quote_reserves` becoming a possibly-**negative** `i128`
as of 30 September). The vendored IDL is dated in `constants.IDL_VENDORED_AT`
and verified at import; a refreshed IDL that moves a discriminator fails the
process at startup rather than at the first bad decode.

---

## 2. Hard isolation from PONS/Arc's production files

Following Arc's precedent and accepted tradeoff exactly.

**Isolated** — all under `src/adapters/launchpads/pumpfun/`: `config.py`
(its own module, not a variant of `BridgeConfig`), `adapter.py`,
`constants.py`, `anchor_codec.py`, `events.py`, `solana_provider.py`,
`position_store.py`, `publisher.py`, `ops_reporter.py`, `main.py`, the
vendored IDL, and the fixtures.

**Genuinely shared** — the only things crossing the boundary: the
`TokenLaunch`/`GraduationSignal` dataclasses, `src/domain/chains.py`,
`src/config.py` helpers, and `src/adapters/data/solana_rpc_client.py` (which
sits at the same generic-infra tier as `rpc_http_client.py` and contains no
launch-decoding logic).

**The accepted cost**: loop-shape duplication between `bridge/main.py` and
`pumpfun/main.py`, and a ~60-line local copy of the operations HTTP client
rather than importing `src/tier0/ops_reporter.py`. In exchange, changing
anything about pump.fun never requires touching PONS or Arc's production
files.

**Enforced, not hoped for.** `static_rules_test.py` asserts it over the AST,
modelled on `src/operations/static_rules_test.py`: nothing outside the package
imports it, it imports only itself and the shared infra above, and it never
imports `src.bridge`, `src.adapters.launchpads.pons`, `src.adapters.launchpads.arc`,
`src.tier0`, `src.collectors` or `src.telegram`. Verified by injecting a
`src.bridge.config` import, which fails two of its tests.

---

## 3. Chain ID sentinel — OPEN DECISION, needs owner confirmation

`chain_id` is a plain `int` everywhere in AIFB: `launches.chain_id` is
`INT NOT NULL`, `TokenLaunch.chain_id` is `int`, every per-chain dispatch dict
is keyed by `int`. Solana has no EVM chain ID.

**Proposed**, in `src/domain/chains.py`, documented there as *not a real chain
ID*:

```python
SOLANA_MAINNET_CHAIN_ID = 900001
SOLANA_DEVNET_CHAIN_ID  = 900002
```

Deliberately far outside the real EVM chain-ID range so it can never collide
with a chain added later. **It becomes load-bearing the moment a production
row is written with it** — changing it afterwards is a data migration, not an
edit. Needs sign-off before the first production row.

**`launches` needs no schema change.** Every column that would hold a Solana
value is already `TEXT` (`token_address`, `deployer_address`, `curve_address`,
`tx_hash`, `graduated_pool_address` — all fit a base58 pubkey or signature)
or already chain-agnostic (`block_number BIGINT` fits a slot,
`block_timestamp TIMESTAMPTZ` fits a block time). Row identity is
`UNIQUE (chain_id, token_address)`: the sentinel plus the base58 mint.

---

## 4. The `LaunchpadAdapter` ABC — deliberately not implemented

`PumpfunAdapter` does **not** implement the ABC. Its `decode(log)`,
`watched_addresses()` and `watched_topics()` are typed around an EVM log dict
(`topics`, `data`, `address`); Solana has no such object, and one transaction
can carry several events of different kinds at once. Forcing it through would
mean `watched_topics()` returning `[]` forever and `decode()` taking something
that is not a log.

The ABC earns its keep where one loop drives several launchpads generically —
Arc's `main.py` driving Tolly and CircleWarp. Nothing drives an EVM and a
non-EVM launchpad from one loop, so there is no generic dispatch to preserve,
and widening a working contract for a single outlier leaves a seam nobody
uses. This matches the spec's own recommendation (option 1).

`decode()` returns a `DecodeResult` with **lists**, not an
`Optional[TokenLaunch]`, because real transactions carry combinations: the
captured `complete_event` fixture is one transaction containing a launch *and*
its graduation. A one-event-per-transaction decoder would drop the graduation
of a token it had just discovered.

If the owner prefers the ABC, the fix is to widen `decode()`'s parameter type
— not to reshape this class.

---

## 5. Real-time feed — provider choice and the REST backfill path

**The REST backfill is required regardless of transport**, because it is what
a restarted process catches up through. It walks
`getSignaturesForAddress(program, before, until)` pages, then
`getTransaction` per signature.

**Geyser/gRPC ("Yellowstone") should be the primary live feed.** Plain
`logsSubscribe` over a public or lightly-provisioned endpoint is a known weak
point at pump.fun's volume, and provider documentation (Shyft, Helius)
recommends Geyser explicitly for this reason. AIFB has already paid for the
websocket version of this lesson: the chain-listener's Alchemy WSS key hit a
monthly cap and sat retrying a dead connection every ~30s for **9.5 hours**
before anyone noticed, and the time-series collector stalled for **4 days**.
Both were "silently stopped producing, told nobody".

So `RealtimeFeed` is a `Protocol` and the provider is swappable.
**Which Geyser provider to buy is an owner decision** (cost/contract).
Until one is configured, `main.py` logs a warning and runs the REST polling
fallback — correct and gap-safe, but higher latency than the intended design.
A plain `logsSubscribe` is acceptable for local/devnet development only.

### Gap-safety, as implemented

1. **Start the feed first, then backfill to its head.** Backfilling first
   leaves a hole exactly the width of the backfill, and the hole looks like a
   quiet market.
2. **Resume from the persisted position, never from "now".** The single most
   important property; see the outages above.
3. **Commit only after publishing, monotonically.** A publish failure raises,
   which prevents the commit and turns a Redis outage into a retry instead of
   a gap. Everything else is bounded-and-skipped; this one is not.
4. **Process a backfill oldest-first.** Pages arrive newest-first, so the
   range is collected and then reversed. Committing while walking
   newest-first would mark the newest slot done while older transactions in
   the same range were unprocessed — and a crash then loses them permanently.
5. **Bounded decode retries, then skip *with an alert*.** A permanently
   malformed transaction must not wedge the pipeline, but a skip nobody hears
   about is the same as a silent gap.
6. **Liveness is separate from correctness.** After 45s of feed silence a
   cheap `getSlot` probe runs; failing it raises
   `pumpfun_ingestion_stalled` rather than reconnecting forever.

### Position store — why a signature *and* a slot

`getSignaturesForAddress` paginates with `before`/`until` cursors that are
**signatures, not slots**, so a slot alone cannot resume a backfill precisely
(a slot holds many transactions and there is no "start at the 4th" cursor).
But a signature alone carries no ordering, so nothing can enforce
monotonicity against it. Both are stored: the slot is the monotonic guard, the
signature is the resume cursor.

Writes go through a Lua compare-and-set (`_SET_MAX_LUA`), reusing Bridge's
idea for the same reason: the live feed and the backfill write concurrently by
design, and the backfill is working through *older* history. A plain `SET`
would let it rewind the committed watermark.

Key namespace: `pumpfun-bridge:last_processed_slot:{chain_id}`.

**Cold start is bounded**, not "start at the head" and not "replay all
history": the most recent `PUMPFUN_COLD_START_SIGNATURES` (default 1000)
signatures are ingested and the choice is logged as a warning. Starting at the
head silently loses the outage window; replaying millions of transactions
never finishes.

---

## 6. Redis streams — dedicated to pump.fun

| Stream | Carries |
|---|---|
| `pumpfun-token-launches` | `TokenLaunch.to_json()` |
| `pumpfun-graduation-signals` | `GraduationSignal.to_json()` |

`XADD <stream> {"data": "<json>"}`, `maxlen≈100_000` approximate — identical
to the existing convention, so a consumer written for PONS/Arc needs a new
stream name and no new parsing. Not shared with PONS/Arc: a shared stream
would make one chain's consumer lag another chain's producer.

Two streams rather than one, for the reason the EVM side has two: the
ingestion service has no Postgres access by design, so a graduation carries no
`launch_id` and the consumer resolves it by `(chain_id, token_address)`. It
also decouples ordering — a coin can be created and complete its curve in
**one transaction** (observed; see the `complete_event` fixture), so a
graduation can legitimately arrive before its launch row exists.

---

## 7. Devnet — out of scope this pass

`SOLANA_DEVNET_CHAIN_ID = 900002` and the devnet RPC default exist in config
so the split mirrors Arc's testnet-in-parallel pattern, but no devnet
deployment, fixtures or verification were done. pump.fun's devnet deployment
was not confirmed to exist at the same program ID, and claiming devnet support
without verifying it would be exactly the kind of unchecked assumption this
package's verification work exists to avoid.

---

## 8. Explicitly not built this pass

A non-goals ledger, in the manner `ARC_ARCHITECTURE.md` uses for CircleWarp's
unbuilt graduation polling. Each of these is *absent*, not *half-done*:

- **Research dossier collectors, Risk Veto, Analyst kNN corpus, trade
  execution.** Out of scope by the brief. Rows land in `launches`; nothing
  downstream is wired.
- **Tier-0-style progress polling** (bonding-curve completion %). Would be a
  new standalone loop, *not* a change inside `src/tier0/watcher.py`, whose
  polling mechanics are `eth_call` selectors and not reusable. Everything
  needed is available (`BondingCurve.real_quote_reserves`,
  `Global.initial_virtual_*`, and the `complete` flag at offset 48 that this
  pass used to find graduated curves), so this is a tractable next pass.
  `graduation_progress`, `real_quote_reserve` and `graduation_threshold` are
  left NULL rather than estimated.
- **PumpSwap pool discovery as a separate step.** Not needed: the migration
  event carries `pool`. It is published in the graduation signal's payload;
  writing it to `launches.graduated_pool_address` is the consumer's side and
  is not implemented here.
- **Multi-quote (USDC-paired) coins.** Detected and explicitly **skipped**,
  counted in `DecodeResult.skipped_non_native`. Supporting them means
  auditing every SOL-denominated field, since `sol_amount` reads 0 for them.
  `PUMPFUN_ALLOW_TOKEN_QUOTES=true` disables the filter but nothing
  downstream has been adjusted for it.
- **Devnet.** See §7.
- **A Geyser/gRPC feed implementation.** The interface exists; the
  implementation needs a provider decision first. Runs the REST fallback
  meanwhile.
- **Holder-rewards and mayhem-mode semantics.** The flags are decoded and
  carried in `raw`; nothing interprets them.
- **`solders`/`anchorpy`.** Not added. A read-only decoder needs positional
  Borsh parsing plus base58, which is a few hundred lines, against a large
  native-wheel dependency tree. If signing is ever needed that calculus
  changes and `solders` becomes the right answer.

---

## 9. Open decisions

| # | Decision | Default taken | Why it needs an owner |
|---|---|---|---|
| 1 | `SOLANA_MAINNET_CHAIN_ID` value | `900001` | Load-bearing from the first production row; changing it later is a data migration |
| 2 | Geyser/gRPC provider | none; REST fallback | Cost and contract |
| 3 | Mainnet-only vs. +devnet | mainnet only | Scope and verification effort (§7) |
| 4 | `LaunchpadAdapter` ABC fit | not implemented (§4) | Architectural preference; cheap to reverse now, expensive later |
| 5 | `deployer` = `creator` or `user` | `creator`, falling back to `user` | They differ routinely (bot/proxy launches). Deployer-reputation work downstream depends on which one is stored |
| 6 | Publish `CompleteEvent` as a graduation, or only the migration | both published | Affects what `launches.graduated` means: "left the curve" vs "has a tradeable pool" |

---

## 10. Operations integration

- **Heartbeat**: `POST /api/v1/operations/heartbeat`,
  `component: "pumpfun_collector"`, interval 60s.
- **MANDATORY, easy to miss**: the component must be pre-registered in
  `src/operations/main.py`'s `lifespan()` as
  `_state["expected"]["pumpfun_collector"] = 60`, or the write is **silently
  refused** with `reason: "unknown_component"`. `ops_reporter.py` detects that
  exact body and logs an error naming the fix, because a heartbeat that looks
  delivered and is not is worse than none. This is the one file outside the
  package that must change — acceptable, since operations' own rule is
  "nothing imports operations", not "operations knows nothing of other
  services".
- **Alert kinds** to add to `src/operations/kinds.py`'s `REGISTRY`, each with
  an explicit `AutoResolve`: `pumpfun_ingestion_stalled`,
  `pumpfun_transaction_skipped`, `pumpfun_backfill_truncated`. An unregistered
  kind is still stored but is flagged `payload.unregistered_kind` and never
  auto-resolves — a visible "not finished properly" signal.
- **Staleness**: `STALE_MULTIPLE = 3`, so 180s without a heartbeat becomes a
  `job_not_run` alert. A failing-but-running tick still counts as ran and
  reports its own failure through an alert kind, keeping "stopped" and
  "broken" distinguishable.
- **Never Telegram or Discord.** Those are trading-signal channels by explicit
  repo rule.

Exact edits: `docs/OPERATIONS_PATCH.md`.

---

## 11. Verification status

| Component | How verified |
|---|---|
| IDL + discriminators | Re-derived; 75/75 match. Checked again at import and in the suite |
| `BondingCurve` layout | `dataSize: 125` + `memcmp` returned 10,004 graduated curves |
| Event decoding | 4 real captured mainnet transactions, 0 undecodable |
| Launch path | Real `create_v2` launch decoded |
| Graduation path | Real `migrate_v2` graduation decoded, pool address extracted |
| Launch+graduation in one tx | Real transaction, both emitted |
| Native-SOL sentinel | 85-event census |
| Isolation rule | AST tests; verified by injecting a violation |
| Gap-safety | 11 provider tests on hand-rolled fakes |
| **Live end-to-end run** | **Not done** — needs Redis and the operations service; see README |
| **Geyser feed** | **Not done** — no provider configured |
| **Devnet** | **Not done** — out of scope |
