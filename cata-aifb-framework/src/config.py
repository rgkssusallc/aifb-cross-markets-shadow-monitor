"""Small env helpers shared by every service. Generic infra, not chain code.

!!! MIRROR FILE !!! The real AIFB repo owns src/config.py. On merge, use that
one -- these helpers exist so this package runs standalone. See
MERGE_MANIFEST.md.
"""
from __future__ import annotations

import os


def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = env_str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    """A kill switch must be OFF unless it says exactly "true".

    Deliberately strict, matching the time-series-collector's convention:
    "1", "yes", "TRUE " and a typo all mean DISABLED. A kill switch that can
    be accidentally enabled by a plausible-looking value is not a kill switch.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip() == "true"


def env_url_chain(prefix: str, *extra: str) -> list[str]:
    """Resolve a priority list of endpoint URLs from env.

    Follows the {PREFIX}_HTTP_URL / {PREFIX}_HTTP_URL_SECONDARY_1..N shape the
    existing bridge config already uses, so operators do not have to learn a
    second convention. `extra` names are appended as further fallbacks.
    """
    out: list[str] = []
    primary = env_str(f"{prefix}_HTTP_URL")
    if primary:
        out.append(primary)
    for i in range(1, 6):
        v = env_str(f"{prefix}_HTTP_URL_SECONDARY_{i}")
        if v:
            out.append(v)
    for name in extra:
        v = env_str(name)
        if v:
            out.append(v)
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]
