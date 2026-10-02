# Merging `cata-aifb-framework` into the AIFB repo

This package was built standalone so it can be developed, tested and reviewed
on its own. That means it carries its own copies of a few AIFB domain files.
**Those copies must not overwrite the real ones.** This is the drop-in list.

## Drop in as-is (new files, nothing to collide with)

```
src/adapters/data/solana_rpc_client.py
src/adapters/data/solana_rpc_client_test.py
src/adapters/launchpads/pumpfun/              (the whole directory)
design/solana/PUMPFUN_ARCHITECTURE.md
tools/capture_fixtures.py
```

## DELETE from this package; the real repo already owns them

| This package | Real repo | Action |
|---|---|---|
| `src/domain/token_launch.py` | exists | delete ours, import theirs |
| `src/domain/graduation_signal.py` | exists | delete ours, import theirs |
| `src/config.py` | exists | delete ours, use theirs |

Each is marked `!!! MIRROR FILE !!!` in its own docstring. They were
reproduced from the handoff spec so field names, optionality and the
sanitisation behaviour match. **If the real file has drifted, the real file
wins** — then re-run `adapter_test.py`, which exercises the sanitisation and
the JSON round-trip and will catch a shape mismatch.

## MERGE, do not replace

`src/domain/chains.py` — the real file owns the PONS and Arc entries. Add only
the Solana block:

```python
SOLANA_MAINNET_CHAIN_ID = 900001   # NOT a real chain ID -- internal sentinel
SOLANA_DEVNET_CHAIN_ID  = 900002
```

plus the matching `NATIVE_CURRENCY_BY_CHAIN`,
`NATIVE_CURRENCY_SYMBOL_BY_CHAIN` and `USD_PRICING_POOL_BY_CHAIN` rows. See
this package's `chains.py` for the values and the comment explaining why they
are sentinels. **The value needs owner sign-off before the first production
row** (design doc §3).

## Changes OUTSIDE the package — mandatory

`docs/OPERATIONS_PATCH.md` has the exact edits:
- `src/operations/main.py` — register `pumpfun_collector` in `lifespan()`,
  without which the heartbeat is silently refused;
- `src/operations/kinds.py` — register three alert kinds.

`docker/docker-compose.yml` — add the `chain-listener-pumpfun` service block
from `docker/chain-listener-pumpfun.yml`.

`requirements.txt` — merge the pins from this package's `requirements.txt`.

## After merging

```bash
python src/adapters/launchpads/pumpfun/static_rules_test.py
```

That one is the canary. It asserts over the AST that nothing outside the
package imports it and that it imports only shared infra — so if the merge
wired something the wrong way round, this fails rather than the rule quietly
ceasing to be true.
