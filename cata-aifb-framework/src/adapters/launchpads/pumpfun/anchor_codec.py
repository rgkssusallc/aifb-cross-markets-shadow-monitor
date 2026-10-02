"""Borsh/Anchor decoding, driven by the IDL rather than by hand-written offsets.

WHY IDL-DRIVEN. The live `TradeEvent` carries 34 fields and `CreateEvent` 19
(the handoff spec described 10 and 6 -- it said plainly that its list was
"confirmed-incomplete", and it was right). Hand-written offsets against a
moving 34-field struct are a bug waiting to happen, and the bug is the silent
kind: Borsh is a positional format with no field names on the wire, so a
struct that has gained one field decodes every subsequent field at the wrong
offset and still returns plausible-looking integers. Reading the layout from
the vendored IDL means a protocol change produces either a correct decode or a
loud length error, never a quietly shifted one.

HOW ANCHOR PUTS EVENTS ON THE WIRE -- two shapes, both of which occur:

 1. `Program data: <base64>` log lines. Payload is
    `[8-byte event discriminator][borsh fields]`.
 2. SELF-CPI event instructions. Anchor's newer `emit_cpi!` writes the event
    as an inner instruction to the program itself, whose data is
    `[8-byte "anchor:event" marker][8-byte event discriminator][borsh fields]`.
    The marker is sha256("anchor:event")[:8], computed in constants.py rather
    than pasted.

Both are handled. Relying on only the log form loses events whenever a caller
truncates logs, and log truncation is a real Solana behaviour ("Log truncated")
rather than a hypothetical -- which is exactly why Anchor moved to CPI events.

WHAT IS DELIBERATELY NOT DONE. No `solders`/`anchorpy` dependency. Those pull
a large native-wheel tree for what is, for a read-only decoder, a few hundred
lines of positional parsing plus base58. The tradeoff is stated in the design
doc under "Dependencies"; if signing is ever needed, that calculus changes and
`solders` becomes the right answer.
"""
from __future__ import annotations

import base64
from typing import Any

# Bitcoin/Solana base58 alphabet. Solana pubkeys and signatures are base58 and
# the alphabet deliberately omits 0, O, I and l.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

PUBKEY_LEN = 32

# A string or vec length this large is a decode gone wrong, not real data. An
# unbounded length read from a misaligned buffer otherwise tries to allocate
# gigabytes before failing, which turns a decode bug into a process kill.
MAX_SANE_LEN = 1 << 20


class BorshError(ValueError):
    """The buffer did not match the layout the IDL describes."""


def b58encode(raw: bytes) -> str:
    """Base58 with Solana's leading-zero convention (one '1' per zero byte)."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = 0
    for b in raw:
        if b != 0:
            break
        pad += 1
    return "1" * pad + (out or "")


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise BorshError(f"invalid base58 character {ch!r}")
        n = n * 58 + _B58_INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in text:
        if ch != "1":
            break
        pad += 1
    return b"\x00" * pad + body


class Cursor:
    """A position in a byte buffer. Every read is bounds-checked.

    Bounds checking is the whole point: an under-length buffer must raise so a
    protocol change surfaces as a decode failure, rather than int.from_bytes
    silently zero-padding a short slice into a confident wrong number.
    """

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self.buf = buf
        self.pos = pos

    @property
    def remaining(self) -> int:
        return len(self.buf) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0:
            raise BorshError(f"negative read length {n}")
        if self.remaining < n:
            raise BorshError(
                f"buffer exhausted: wanted {n} bytes at offset {self.pos}, "
                f"only {self.remaining} left (total {len(self.buf)}). The IDL "
                "layout and the on-chain data disagree -- re-vendor the IDL.")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little", signed=False)

    def sint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little", signed=True)

    def boolean(self) -> bool:
        v = self.take(1)[0]
        if v > 1:
            raise BorshError(f"bool byte was {v}, which is not 0 or 1")
        return v == 1

    def pubkey(self) -> str:
        return b58encode(self.take(PUBKEY_LEN))

    def length(self) -> int:
        n = self.uint(4)
        if n > MAX_SANE_LEN:
            raise BorshError(
                f"length prefix {n} at offset {self.pos - 4} is implausible; "
                "refusing to allocate. The buffer is almost certainly "
                "misaligned against the IDL layout.")
        return n

    def string(self) -> str:
        raw = self.take(self.length())
        # Token metadata is attacker-controlled; never let a bad byte raise
        # here. TokenLaunch does the real sanitising, this just survives.
        return raw.decode("utf-8", errors="replace")


_UINTS = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16}
_SINTS = {"i8": 1, "i16": 2, "i32": 4, "i64": 8, "i128": 16}


def decode_type(cur: Cursor, ty: Any, types: dict[str, dict]) -> Any:
    """Decode one IDL-described type at the cursor.

    Integers wider than 64 bits are returned as Python ints (arbitrary
    precision, so u128 is exact) -- but see `to_wire` in adapter.py: they are
    stringified before they reach JSON, because u64 reserve values exceed
    IEEE-754's exact range and JSON numbers are doubles.
    """
    if isinstance(ty, str):
        if ty in _UINTS:
            return cur.uint(_UINTS[ty])
        if ty in _SINTS:
            return cur.sint(_SINTS[ty])
        if ty == "bool":
            return cur.boolean()
        if ty == "string":
            return cur.string()
        if ty in ("pubkey", "publicKey"):
            return cur.pubkey()
        if ty == "bytes":
            return cur.take(cur.length())
        raise BorshError(f"unsupported primitive type {ty!r}")

    if isinstance(ty, dict):
        if "defined" in ty:
            name = ty["defined"]
            if isinstance(name, dict):
                name = name.get("name")
            return decode_defined(cur, str(name), types)
        if "option" in ty:
            return decode_type(cur, ty["option"], types) if cur.boolean() else None
        if "coption" in ty:
            return (decode_type(cur, ty["coption"], types)
                    if cur.uint(4) else None)
        if "vec" in ty:
            n = cur.length()
            return [decode_type(cur, ty["vec"], types) for _ in range(n)]
        if "array" in ty:
            inner, count = ty["array"]
            return [decode_type(cur, inner, types) for _ in range(int(count))]
    raise BorshError(f"unsupported type shape {ty!r}")


def decode_defined(cur: Cursor, name: str, types: dict[str, dict]) -> Any:
    """Decode a named IDL type: struct, enum, or an Anchor Option* wrapper."""
    spec = types.get(name)
    if spec is None:
        raise BorshError(
            f"type {name!r} is not in the vendored IDL; it cannot be decoded "
            "without guessing its layout")
    kind = spec.get("type", {})
    k = kind.get("kind")

    if k == "struct":
        out: dict[str, Any] = {}
        for f in kind.get("fields") or []:
            out[f["name"]] = decode_type(cur, f["type"], types)
        return out

    if k == "enum":
        variants = kind.get("variants") or []
        idx = cur.take(1)[0]
        if idx >= len(variants):
            raise BorshError(
                f"enum {name} variant index {idx} out of range "
                f"({len(variants)} variants)")
        v = variants[idx]
        vname = v.get("name")
        vfields = v.get("fields") or []
        if not vfields:
            # pump.fun's OptionBool/OptionU64 are enums whose "None" variant
            # carries nothing and whose "Some" carries one value. Returning the
            # bare variant name for a payload-less variant keeps None as None.
            return None if vname in ("None", "none") else vname
        vals = [decode_type(cur, (f["type"] if isinstance(f, dict) else f),
                            types) for f in vfields]
        return vals[0] if len(vals) == 1 else {vname: vals}

    if k == "type":
        return decode_type(cur, kind.get("alias") or kind.get("type"), types)

    raise BorshError(f"unsupported IDL type kind {k!r} for {name!r}")


def decode_event(disc: bytes, payload: bytes, idl: dict) -> tuple[str, dict]:
    """Resolve an 8-byte event discriminator and decode its Borsh payload.

    Returns (event_name, fields). Raises BorshError when the discriminator is
    unknown -- the caller decides whether an unknown event is worth logging.
    Trailing bytes are tolerated and reported: a protocol that APPENDS a field
    (which is exactly how pump.fun has evolved -- quote_mint, then
    holder_rewards, then virtual_quote_reserves) stays decodable for every
    field we already know, instead of failing closed on the whole event.
    """
    want = list(disc)
    name = None
    for ev in idl.get("events") or []:
        if ev.get("discriminator") == want:
            name = ev["name"]
            break
    if name is None:
        raise BorshError(f"no event in the IDL has discriminator {want}")
    types = {t["name"]: t for t in idl.get("types") or []}
    cur = Cursor(payload)
    fields = decode_defined(cur, name, types)
    if cur.remaining:
        # Not an error. Recorded so a growing struct is visible in the audit
        # trail and shows up as a prompt to re-vendor, rather than silently.
        fields = dict(fields)
        fields["_undecoded_trailing_bytes"] = cur.remaining
    return name, fields


def parse_program_data_logs(logs: list[str],
                            program_id: str | None = None) -> list[bytes]:
    """Extract payloads from `Program data:` log lines, optionally per program.

    Anchor's older emit! path. Each payload is discriminator+fields.

    ATTRIBUTION MATTERS. A single pump.fun transaction also runs the fee
    program and, on a migration, PumpSwap -- and each writes its OWN
    `Program data:` lines into the same flat log array. Decoding every line
    against pump.fun's IDL produced a stream of "no event has this
    discriminator" noise on real fixtures, and noise is the benign outcome:
    an 8-byte discriminator collision across two programs would decode
    another program's event as one of ours and publish a fabricated launch.

    So when program_id is given, the invoke/success markers the runtime emits
    are used to track which program is executing, and only that program's
    lines are returned. The runtime guarantees these markers bracket every
    invocation, including nested ones, which is what makes a simple stack
    sufficient here.
    """
    out: list[bytes] = []
    stack: list[str] = []
    for line in logs or []:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split(" ", 2)[1])
            continue
        if line.startswith("Program ") and (
                line.endswith(" success") or " failed" in line):
            if stack:
                stack.pop()
            continue
        if not line.startswith("Program data: "):
            continue
        if program_id is not None and (not stack or stack[-1] != program_id):
            continue
        blob = line[len("Program data: "):].strip()
        try:
            out.append(base64.b64decode(blob, validate=True))
        except Exception:  # noqa: BLE001 -- a malformed line is not fatal
            continue
    return out
