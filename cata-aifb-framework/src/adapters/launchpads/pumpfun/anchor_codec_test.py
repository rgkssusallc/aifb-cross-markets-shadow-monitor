"""Borsh/Anchor codec tests.

Split from adapter_test.py on purpose. This file tests a CODEC, and a codec is
the one place a constructed buffer is legitimate evidence: the question is
whether the parser implements Borsh, which is a fixed specification, so
round-tripping a buffer this file encodes proves exactly that. Whether the
LAYOUT matches pump.fun is a different question, answered in adapter_test.py
against real captured mainnet transactions.

The bounds and sanity checks get most of the attention here, because those are
the behaviours that turn a protocol change into a loud failure instead of a
plausible wrong number.

Run: python src/adapters/launchpads/pumpfun/anchor_codec_test.py
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from src.adapters.launchpads.pumpfun import constants as C  # noqa: E402
from src.adapters.launchpads.pumpfun.anchor_codec import (  # noqa: E402
    BorshError, Cursor, b58decode, b58encode, decode_event, decode_type,
    parse_program_data_logs,
)

TYPES: dict[str, dict] = {}


def test_base58_round_trips_arbitrary_bytes():
    for raw in (b"", b"\x00", b"\x00\x00\x01", bytes(range(32)),
                b"\xff" * 32):
        assert b58decode(b58encode(raw)) == raw, raw


def test_base58_preserves_leading_zero_bytes_as_leading_ones():
    """Solana's convention. Dropping them changes the pubkey.

    The native-SOL sentinel is 32 zero bytes and encodes to 32 '1's; an
    implementation that trims them produces a different, wrong address.
    """
    assert b58encode(b"\x00" * 32) == "1" * 32
    assert b58decode("1" * 32) == b"\x00" * 32
    assert b58encode(b"\x00" * 32) == C.NATIVE_QUOTE_SENTINEL


def test_base58_rejects_a_character_outside_the_alphabet():
    for bad in ("0", "O", "I", "l", "hello!"):
        try:
            b58decode(bad)
        except BorshError:
            continue
        raise AssertionError(f"{bad!r} should not decode")


def test_cursor_raises_rather_than_zero_padding_a_short_buffer():
    """The central safety property.

    int.from_bytes on a short slice returns a confident wrong number. A
    protocol that grew a field must surface as an exception, not as a plausible
    value, so every read is bounds-checked.
    """
    cur = Cursor(b"\x01\x02\x03")
    try:
        cur.uint(8)
    except BorshError as e:
        assert "exhausted" in str(e)
        return
    raise AssertionError("an 8-byte read from a 3-byte buffer must raise")


def test_cursor_refuses_an_implausible_length_prefix_instead_of_allocating():
    """A misaligned buffer yields a garbage length.

    Honouring it tries to allocate gigabytes and kills the process, turning a
    decode bug into an outage.
    """
    cur = Cursor(struct.pack("<I", 0xFFFFFFF) + b"abc")
    try:
        cur.string()
    except BorshError as e:
        assert "implausible" in str(e)
        return
    raise AssertionError("an absurd length prefix must be refused")


def test_bool_byte_other_than_zero_or_one_is_rejected():
    try:
        Cursor(b"\x02").boolean()
    except BorshError:
        return
    raise AssertionError("only 0 and 1 are valid Borsh bools")


def test_primitive_decoding_is_little_endian_and_signed_where_declared():
    assert decode_type(Cursor(b"\x01\x00\x00\x00"), "u32", TYPES) == 1
    assert decode_type(Cursor(b"\xff" * 8), "u64", TYPES) == 2 ** 64 - 1
    # i64 is used for every timestamp in this IDL; an unsigned read of a
    # negative value yields ~1.8e19 instead of a date.
    assert decode_type(Cursor(b"\xff" * 8), "i64", TYPES) == -1
    assert decode_type(Cursor(struct.pack("<q", 1790912966)), "i64",
                       TYPES) == 1790912966


def test_string_decoding_survives_invalid_utf8_rather_than_raising():
    """Token metadata is attacker-controlled and need not be valid UTF-8.

    Raising here would let one malformed name stop ingestion; TokenLaunch
    does the real sanitising.
    """
    buf = struct.pack("<I", 4) + b"\xff\xfe\x41\x42"
    out = decode_type(Cursor(buf), "string", TYPES)
    assert "A" in out and "B" in out


def test_vec_and_option_and_array_shapes_decode():
    vec = struct.pack("<I", 3) + struct.pack("<3I", 7, 8, 9)
    assert decode_type(Cursor(vec), {"vec": "u32"}, TYPES) == [7, 8, 9]
    assert decode_type(Cursor(b"\x00"), {"option": "u32"}, TYPES) is None
    assert decode_type(Cursor(b"\x01" + struct.pack("<I", 5)),
                       {"option": "u32"}, TYPES) == 5
    arr = struct.pack("<2I", 1, 2)
    assert decode_type(Cursor(arr), {"array": ["u32", 2]}, TYPES) == [1, 2]


def test_unknown_event_discriminator_is_reported_not_guessed():
    try:
        decode_event(b"\x00" * 8, b"", C.IDL)
    except BorshError as e:
        assert "discriminator" in str(e)
        return
    raise AssertionError("an unknown discriminator must not resolve")


def test_a_grown_struct_keeps_its_known_fields_and_reports_the_excess():
    """pump.fun has evolved by APPENDING fields -- quote_mint, then holder
    rewards, then virtual_quote_reserves. Failing closed on the whole event
    would stop ingestion on an additive change that costs us nothing, so
    trailing bytes are reported rather than fatal.
    """
    disc = C.event_discriminator(C.EVENT_COMPLETE)
    payload = (b"\x01" * 32 + b"\x02" * 32 + b"\x03" * 32
               + struct.pack("<q", 1790912966) + b"\x04" * 32
               + b"EXTRA-FIELD-FROM-THE-FUTURE")
    name, fields = decode_event(disc, payload, C.IDL)
    assert name == C.EVENT_COMPLETE
    assert fields["timestamp"] == 1790912966
    assert fields["_undecoded_trailing_bytes"] == len(
        b"EXTRA-FIELD-FROM-THE-FUTURE")


def test_a_shrunk_struct_fails_loudly_instead_of_decoding_partially():
    disc = C.event_discriminator(C.EVENT_COMPLETE)
    try:
        decode_event(disc, b"\x01" * 40, C.IDL)
    except BorshError as e:
        assert "exhausted" in str(e)
        return
    raise AssertionError("a truncated event must raise")


def test_program_data_lines_are_attributed_to_the_emitting_program():
    """One transaction interleaves several programs' data lines.

    Decoding all of them against pump.fun's IDL is noise at best and, on an
    8-byte discriminator collision, a fabricated event at worst.
    """
    import base64
    ours = base64.b64encode(b"OURS").decode()
    theirs = base64.b64encode(b"THEIRS").decode()
    logs = [
        "Program OTHER invoke [1]",
        f"Program data: {theirs}",
        "Program OTHER success",
        f"Program {C.PUMPFUN_PROGRAM_ID} invoke [1]",
        f"Program data: {ours}",
        f"Program {C.PUMPFUN_PROGRAM_ID} success",
    ]
    assert parse_program_data_logs(logs, C.PUMPFUN_PROGRAM_ID) == [b"OURS"]
    # Unfiltered keeps both, which is what the old behaviour did.
    assert len(parse_program_data_logs(logs)) == 2


def test_nested_invocations_do_not_confuse_program_attribution():
    """pump.fun is commonly reached by CPI, so nesting is the normal case."""
    import base64
    ours = base64.b64encode(b"INNER").decode()
    logs = [
        "Program ROUTER invoke [1]",
        f"Program {C.PUMPFUN_PROGRAM_ID} invoke [2]",
        f"Program data: {ours}",
        f"Program {C.PUMPFUN_PROGRAM_ID} success",
        "Program ROUTER success",
    ]
    assert parse_program_data_logs(logs, C.PUMPFUN_PROGRAM_ID) == [b"INNER"]


def test_malformed_base64_in_a_log_line_is_skipped_not_fatal():
    logs = [f"Program {C.PUMPFUN_PROGRAM_ID} invoke [1]",
            "Program data: !!!not-base64!!!",
            f"Program {C.PUMPFUN_PROGRAM_ID} success"]
    assert parse_program_data_logs(logs, C.PUMPFUN_PROGRAM_ID) == []


def test_every_idl_discriminator_matches_anchors_own_derivation():
    """The IDL is vendored, so it could be edited. This re-derives all of it.

    75 instructions and events were checked this way when the file was
    vendored; keeping the check in the suite means a tampered or
    partially-refreshed IDL fails here rather than at the first bad decode.
    """
    for ev in C.IDL["events"]:
        assert ev["discriminator"] == list(
            C.event_discriminator(ev["name"])), ev["name"]
    for ix in C.IDL["instructions"]:
        assert ix["discriminator"] == list(
            C.instruction_discriminator(ix["name"])), ix["name"]


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
