"""GraduationSignal -- published when a token leaves its bonding curve.

!!! MIRROR FILE !!! See token_launch.py's header and MERGE_MANIFEST.md: the
real AIFB repo already owns src/domain/graduation_signal.py; delete this on
merge rather than overwriting that.

WHY IT CARRIES NO launch_id. The ingestion service has no Postgres access by
design, so at publish time there is no row id to reference -- the consumer
resolves the launch by (chain_id, token_address) against `launches`. That is
also why this goes to a SEPARATE Redis stream from new launches: a graduation
can arrive before the consumer has written the launch row (a coin can be
created and complete its curve in ONE transaction -- observed on mainnet, see
the complete_event fixture), and two streams let the consumer handle that
ordering explicitly instead of having it hidden inside one queue.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class GraduationSignal:
    chain_id: int
    token_address: str
    graduated_at_onchain: Optional[str]
    tx_hash: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, blob: str) -> "GraduationSignal":
        return cls(**json.loads(blob))
