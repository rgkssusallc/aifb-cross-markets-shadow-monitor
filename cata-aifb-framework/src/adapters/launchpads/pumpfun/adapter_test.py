"""PumpfunAdapter, tested against REAL captured mainnet-beta transactions.

Every fixture under fixtures/ is an unmodified `getTransaction` response,
captured by tools/capture_fixtures.py. That is the point: a synthetic payload
is written to match the decoder's own assumptions, so the pair agrees with
itself and disagrees with the chain -- which is the exact failure a decoder
test exists to catch. These four transactions were chosen because each one
proves something the handoff spec got wrong or did not know:

  create_event.json     a launch via create_v2 (not `create`)
  complete_event.json   a launch AND its graduation in ONE transaction
  migration_event.json  a graduation via migrate_v2, carrying the pool address
  trade_event.json      a sell on a NON-SOL-quoted coin, reached by CPI

Run: python src/adapters/launchpads/pumpfun/adapter_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from src.adapters.launchpads.pumpfun import constants as C  # noqa: E402
from src.adapters.launchpads.pumpfun.adapter import (  # noqa: E402
    PumpfunAdapter, to_wire,
)
from src.adapters.launchpads.pumpfun.events import (  # noqa: E402
    invoked_instruction_names,
)

FIXTURES = Path(__file__).with_name("fixtures")


def load(stem: str) -> dict:
    with (FIXTURES / f"{stem}.json").open() as fh:
        return json.load(fh)["transaction_response"]


def test_fixtures_are_real_captured_mainnet_transactions_not_synthetic():
    """Guards the standard, not the code.

    If someone replaces a fixture with a hand-written payload, the decoder
    starts being tested against its own assumptions and this suite stops
    meaning anything. Each fixture must carry the provenance the capture tool
    writes.
    """
    for stem in ("create_event", "complete_event", "migration_event",
                 "trade_event"):
        with (FIXTURES / f"{stem}.json").open() as fh:
            blob = json.load(fh)
        assert blob.get("_source", "").startswith("mainnet-beta"), (
            f"{stem}.json has no mainnet provenance marker; a synthetic "
            "fixture would make this whole suite self-confirming")
        assert blob.get("_signature"), f"{stem}.json records no signature"
        assert int(blob.get("_captured_at_slot") or 0) > 0


def test_decode_recognizes_a_real_create_event_and_sets_curve_not_pool():
    async def _run():
        adapter = PumpfunAdapter()
        result = await adapter.decode(load("create_event"))
        assert len(result.launches) == 1, result
        launch = result.launches[0]
        assert launch.launchpad == "pumpfun"
        assert launch.chain_id == C_SENTINEL
        # The repo convention: a bonding-curve launchpad sets curve, never pool.
        assert launch.curve is not None and launch.pool is None
        assert launch.token.endswith("pump") or len(launch.token) > 30
        assert launch.token_symbol == "smoltits"
        assert launch.block_number > 0 and launch.block_timestamp > 0
        assert result.undecodable == 0
    asyncio.run(_run())


C_SENTINEL = 900001


def test_decode_handles_a_launch_created_through_create_v2_not_create():
    """The spec listed only `create`. Real launches use create_v2.

    An indexer watching only the `create` discriminator would have missed
    both of the launch fixtures captured here.
    """
    async def _run():
        tx = load("create_event")
        assert "create_v2" in invoked_instruction_names(tx), (
            "fixture should exercise the v2 launch path")
        assert "create" not in invoked_instruction_names(tx)
        result = await PumpfunAdapter().decode(tx)
        assert len(result.launches) == 1
    asyncio.run(_run())


def test_decode_returns_both_a_launch_and_a_graduation_from_one_transaction():
    """A coin can be created and fill its curve in a single transaction.

    This is why decode() returns lists rather than an Optional[TokenLaunch]:
    a one-event-per-transaction decoder drops the graduation of a token it
    just discovered.
    """
    async def _run():
        result = await PumpfunAdapter().decode(load("complete_event"))
        assert len(result.launches) == 1, result
        assert len(result.graduations) == 1, result
        assert result.launches[0].token == result.graduations[0].token_address
        assert result.launches[0].token_symbol == "DISNEY"
    asyncio.run(_run())


def test_decode_recognizes_a_migration_and_captures_the_pumpswap_pool():
    """The spec expected pool discovery to need a separate polling step.

    CompletePumpAmmMigrationEvent carries `pool` directly, so it does not.
    """
    async def _run():
        tx = load("migration_event")
        assert "migrate_v2" in invoked_instruction_names(tx)
        result = await PumpfunAdapter().decode(tx)
        assert not result.launches
        assert len(result.graduations) == 1
        grad = result.graduations[0]
        assert getattr(grad, "_event") == C.EVENT_MIGRATED
        pool = getattr(grad, "_pool")
        assert pool and len(pool) > 30, (
            "the migration event must yield the PumpSwap pool address")
        assert grad.graduated_at_onchain and "T" in grad.graduated_at_onchain
    asyncio.run(_run())


def test_decode_ignores_a_trade_because_a_trade_is_neither_launch_nor_graduation():
    async def _run():
        result = await PumpfunAdapter().decode(load("trade_event"))
        assert result.empty, result
        assert result.undecodable == 0, (
            "foreign programs' Program data lines must be attributed away, "
            "not counted as our undecodable events")
    asyncio.run(_run())


def test_decode_ignores_a_transaction_that_does_not_mention_the_pumpfun_program_id():
    async def _run():
        tx = {"slot": 1, "meta": {"logMessages": [], "err": None},
              "transaction": {"message": {"accountKeys": ["SomeOtherProgram"],
                                          "instructions": []},
                              "signatures": ["sig"]}}
        result = await PumpfunAdapter().decode(tx)
        assert result.empty
    asyncio.run(_run())


def test_decode_refuses_a_failed_transaction_even_though_its_logs_survive():
    """A reverted create must never be published as a launch.

    Failed transactions keep their log messages, so decoding before checking
    meta.err would happily emit a launch for a token that was never created.
    """
    async def _run():
        tx = load("create_event")
        assert (await PumpfunAdapter().decode(tx)).launches, "sanity"
        failed = json.loads(json.dumps(tx))
        failed["meta"]["err"] = {"InstructionError": [0, {"Custom": 7}]}
        result = await PumpfunAdapter().decode(failed)
        assert result.empty, "a failed transaction changed no state"
    asyncio.run(_run())


def test_a_non_sol_quoted_launch_is_skipped_rather_than_half_supported():
    """USDC-quoted coins are live. Their SOL-denominated fields read zero.

    So they are counted and dropped, not ingested with silently-zero
    reserves. The quote mint is rewritten here rather than captured because
    the point under test is the FILTER, and the filter's input is one field.
    """
    async def _run():
        tx = json.loads(json.dumps(load("create_event")))
        adapter = PumpfunAdapter(native_quote_only=True)
        base = await adapter.decode(tx)
        assert base.launches, "sanity: fixture must normally yield a launch"

        forced = PumpfunAdapter(native_quote_only=True)
        fields = {"mint": "M" * 43, "quote_mint": C.USDC_MINT,
                  "creator": "C" * 43, "name": "x", "symbol": "x",
                  "uri": "u", "bonding_curve": "B" * 43, "timestamp": 1}
        out = type(base)()
        forced._on_create(out, fields, 1, 1, "sig", "signer", [])
        assert not out.launches
        assert out.skipped_non_native == ["M" * 43]
    asyncio.run(_run())


def test_native_sol_is_the_zero_pubkey_not_the_wrapped_sol_mint():
    """The fact that would have inverted the whole filter.

    pump-public-docs tells integrations to PASS the WSOL mint, so WSOL looks
    like the marker for a native coin. The events carry the zero pubkey. A
    filter written against WSOL would drop every native coin and keep the
    USDC ones.
    """
    assert C.is_native_sol_quote("11111111111111111111111111111111")
    assert C.is_native_sol_quote(C.WSOL_MINT)
    assert C.is_native_sol_quote(None)
    assert not C.is_native_sol_quote(C.USDC_MINT)
    # And the real fixture must actually be the zero pubkey, so this is not
    # just an assertion about a constant.
    from src.adapters.launchpads.pumpfun.anchor_codec import decode_event
    from src.adapters.launchpads.pumpfun.events import iter_event_payloads
    tx = load("create_event")
    seen = []
    for disc, payload in iter_event_payloads(tx):
        name, fields = decode_event(disc, payload, C.IDL)
        if name == C.EVENT_CREATE:
            seen.append(fields["quote_mint"])
    assert seen == ["11111111111111111111111111111111"], seen


def test_deployer_prefers_the_creator_field_over_the_transaction_signer():
    """`user` signs; `creator` is who the protocol treats as the creator.

    They differ whenever a coin is launched through a bot or proxy, which is
    common. Deployer-reputation work downstream wants `creator`.
    """
    async def _run():
        adapter = PumpfunAdapter()
        out_type = type(await adapter.decode(load("create_event")))
        out = out_type()
        adapter._on_create(out, {
            "mint": "M" * 43, "creator": "CREATOR", "user": "SIGNER",
            "quote_mint": "11111111111111111111111111111111",
            "bonding_curve": "B" * 43, "timestamp": 7,
            "name": "n", "symbol": "s", "uri": "u",
        }, 5, 5, "sig", "FEEPAYER", [])
        assert out.launches[0].deployer == "CREATOR"

        out2 = out_type()
        adapter._on_create(out2, {
            "mint": "M" * 43, "user": "SIGNER",
            "quote_mint": "11111111111111111111111111111111",
            "timestamp": 7,
        }, 5, 5, "sig", "FEEPAYER", [])
        assert out2.launches[0].deployer == "SIGNER", (
            "pre-`creator` history must still resolve a deployer")
    asyncio.run(_run())


def test_block_timestamp_is_never_silently_replaced_with_wall_clock_now():
    """A launch backdated to whenever the indexer ran is invisibly wrong.

    Every downstream age calculation depends on this, so a missing timestamp
    must stay missing (0) rather than become `now`.
    """
    import time as _time

    async def _run():
        adapter = PumpfunAdapter()
        out_type = type(await adapter.decode(load("create_event")))
        out = out_type()
        adapter._on_create(out, {
            "mint": "M" * 43, "creator": "C",
            "quote_mint": "11111111111111111111111111111111",
        }, 9, None, "sig", "signer", [])
        ts = out.launches[0].block_timestamp
        assert ts == 0, f"expected 0 for an unknown timestamp, got {ts}"
        assert abs(ts - _time.time()) > 1000
    asyncio.run(_run())


def test_token_metadata_is_sanitized_because_it_is_attacker_controlled():
    """Anyone can mint a coin whose name contains NUL bytes.

    The EVM side crashed a Postgres insert on exactly this. SPL metadata is
    no less exposed.
    """
    async def _run():
        adapter = PumpfunAdapter()
        out_type = type(await adapter.decode(load("create_event")))
        out = out_type()
        adapter._on_create(out, {
            "mint": "M" * 43, "creator": "C",
            "quote_mint": "11111111111111111111111111111111",
            "name": "ev\x00il\x1f", "symbol": "a" * 500, "uri": "u\x00ri",
            "timestamp": 1,
        }, 1, 1, "sig", "signer", [])
        launch = out.launches[0]
        assert "\x00" not in (launch.token_name or "")
        assert "\x1f" not in (launch.token_name or "")
        assert len(launch.token_symbol or "") <= 32
        assert "\x00" not in (launch.metadata_uri or "")
        # And it must survive serialisation, which is where it crashed before.
        json.loads(launch.to_json())
    asyncio.run(_run())


def test_large_u64_values_are_stringified_so_json_cannot_round_them():
    """Reserve values exceed 2**53; JSON numbers are doubles.

    Left as numbers they lose low-order digits silently on any round trip.
    """
    assert to_wire(2 ** 64 - 1) == str(2 ** 64 - 1)
    assert to_wire(12345) == 12345
    assert to_wire(True) is True, "bool must not be stringified as an int"
    assert to_wire({"a": [2 ** 60]}) == {"a": [str(2 ** 60)]}


def test_raw_payload_is_json_serialisable_for_every_fixture():
    """`raw` is the audit trail and goes into a JSONB column.

    A u128, a bytes field or a nested vec that cannot serialise would fail at
    insert time, i.e. after the measurement was taken and lost.
    """
    async def _run():
        adapter = PumpfunAdapter()
        for stem in ("create_event", "complete_event"):
            for launch in (await adapter.decode(load(stem))).launches:
                blob = launch.to_json()
                again = json.loads(blob)
                assert again["raw"]["event"] == C.EVENT_CREATE
                assert again["raw"]["idl_vendored_at"] == C.IDL_VENDORED_AT
    asyncio.run(_run())


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"pass {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
