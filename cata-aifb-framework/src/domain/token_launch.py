"""TokenLaunch -- the launchpad-agnostic in-flight event.

!!! MIRROR FILE !!!
This package is standalone, so it carries its own copy of the AIFB domain
contracts in order to be runnable and testable on its own. In the real AIFB
repo `src/domain/token_launch.py` ALREADY EXISTS and is the authority. When
this package is merged, DELETE this file and import the real one -- do not
overwrite the real one with this. The shape here is reproduced from the
handoff spec so that field names, optionality and the sanitisation behaviour
match; if the real file has since drifted, the real file wins. See
MERGE_MANIFEST.md at the package root for the full drop-in / discard list.

WHY THE SANITISATION IS NOT OPTIONAL. Free-text fields here are
attacker-controlled. On the EVM side a malicious contract's `name()` returned
NUL bytes and crashed a Postgres insert -- a real incident, not a theoretical
one. The Solana equivalent is at least as exposed: SPL token metadata (name,
symbol, uri) is written by whoever creates the mint, and pump.fun lets anyone
create a mint with any string. One live fixture captured while building this
package is named "my tits aren't that smol" with a typographic apostrophe,
which is a harmless reminder that these strings are arbitrary user input and
go straight into a database and a UI.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Caps chosen to be generous for real tokens and still bounded. A token whose
# name is a megabyte is an attack or a bug; either way it must not reach the
# database.
MAX_NAME_LEN = 128
MAX_SYMBOL_LEN = 32
MAX_URI_LEN = 512


def sanitize_text(value: Any, limit: int) -> Optional[str]:
    """Strip control characters and cap length. Returns None for empty.

    NUL is the one that caused the production incident, but every C0/C1
    control character is removed: they serve no purpose in a token name and
    several of them break terminals, logs and CSV exports further downstream.
    Surrogates are dropped too -- they survive a `errors="replace"` decode but
    cannot be encoded to UTF-8 later, which moves the crash to a worse place.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    cleaned = "".join(
        ch for ch in value
        if (ch == " " or not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F))
        and not 0xD800 <= ord(ch) <= 0xDFFF
    ).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


@dataclass
class TokenLaunch:
    """One new launch, in the shape every downstream consumer expects.

    `curve` vs `pool` is a real distinction, not a naming preference: a
    bonding-curve launchpad sets `curve` and leaves `pool` None, and only a
    launchpad whose token has a real DEX pool at the moment of launch sets
    `pool`. pump.fun is a bonding-curve model, so it always sets `curve`.
    """
    chain_id: int
    launchpad: str
    token: str
    deployer: str
    block_number: int
    block_timestamp: int
    tx_hash: str
    token_name: Optional[str] = None
    token_symbol: Optional[str] = None
    curve: Optional[str] = None
    graduation_threshold: Optional[str] = None
    metadata_uri: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    # EVM/PONS-v1-specific fields, left None by every Solana launch. Present
    # so the dataclass shape matches the real one and a consumer doing
    # TokenLaunch(**payload) cannot fail on a missing key.
    pool: Optional[str] = None
    dex_factory: Optional[str] = None
    dex_id: Optional[str] = None
    position_id: Optional[str] = None
    restrictions_end_block: Optional[int] = None
    initial_buy_amount: Optional[str] = None
    pair_token: Optional[str] = None
    launch_config_id: Optional[str] = None
    website: Optional[str] = None
    twitter: Optional[str] = None
    telegram: Optional[str] = None

    def __post_init__(self) -> None:
        self.token_name = sanitize_text(self.token_name, MAX_NAME_LEN)
        self.token_symbol = sanitize_text(self.token_symbol, MAX_SYMBOL_LEN)
        self.metadata_uri = sanitize_text(self.metadata_uri, MAX_URI_LEN)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, blob: str) -> "TokenLaunch":
        return cls(**json.loads(blob))
