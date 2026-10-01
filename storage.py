"""Append-only SQLite logging for shadow runs.

Three tables, three purposes:

  opportunities  one row per positive-net-edge event, from open to close.
                 lifetime_ms is the field that decides your fate: compare its
                 distribution against your measured round-trip latency.
  rejections     aggregated counts by reason. A high skew-rejection rate means
                 your data plumbing is the problem, not the market.
  latency        measured round-trips per venue, so lifetimes can be judged
                 against a real number rather than a guess.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_key       TEXT NOT NULL,
    path            TEXT NOT NULL,
    venues          TEXT NOT NULL,
    t_open          REAL NOT NULL,
    t_close         REAL,
    lifetime_ms     REAL,
    size_usd        REAL NOT NULL,
    edge_open_bps   REAL NOT NULL,
    peak_edge_bps   REAL NOT NULL,
    edge_close_bps  REAL,
    capacity_usd    REAL,
    gross_bps       REAL,
    fee_bps         REAL,
    slippage_bps    REAL,
    fixed_bps       REAL,
    max_skew_ms     REAL,
    exhausted       INTEGER DEFAULT 0,
    samples         INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_opp_cycle ON opportunities(cycle_key);
CREATE INDEX IF NOT EXISTS idx_opp_open  ON opportunities(t_open);

CREATE TABLE IF NOT EXISTS rejections (
    cycle_key   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    last_ts     REAL,
    PRIMARY KEY (cycle_key, reason)
);

CREATE TABLE IF NOT EXISTS latency (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    venue   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    rtt_ms  REAL NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lat_venue ON latency(venue, kind);

CREATE TABLE IF NOT EXISTS peg_watch (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset     TEXT NOT NULL,
    venue     TEXT NOT NULL,
    mid       REAL NOT NULL,
    dev_bps   REAL NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_peg ON peg_watch(asset, ts);
"""


def _f(x: Any) -> float | None:
    if x is None:
        return None
    return float(x)


@dataclass
class OpenOpportunity:
    """An opportunity being tracked from first detection until it closes."""
    cycle_key: str
    path: tuple[str, ...]
    venues: tuple[str, ...]
    t_open: float
    size_usd: Decimal
    edge_open_bps: Decimal
    peak_edge_bps: Decimal
    peak_breakdown: dict[str, Decimal]
    capacity_usd: Decimal | None = None
    max_skew_ms: float = 0.0
    exhausted: bool = False
    samples: int = 1
    last_edge_bps: Decimal = Decimal(0)

    def observe(self, edge_bps: Decimal, breakdown: dict[str, Decimal],
                skew_ms: float, exhausted: bool,
                capacity_usd: Decimal | None) -> None:
        self.samples += 1
        self.last_edge_bps = edge_bps
        self.max_skew_ms = max(self.max_skew_ms, skew_ms)
        self.exhausted = self.exhausted or exhausted
        if edge_bps > self.peak_edge_bps:
            self.peak_edge_bps = edge_bps
            self.peak_breakdown = breakdown
            if capacity_usd is not None:
                self.capacity_usd = capacity_usd


class Store:
    def __init__(self, path: str = "shadow.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def record_opportunity(self, opp: OpenOpportunity, t_close: float) -> None:
        b = opp.peak_breakdown
        self.conn.execute(
            """INSERT INTO opportunities
               (cycle_key, path, venues, t_open, t_close, lifetime_ms, size_usd,
                edge_open_bps, peak_edge_bps, edge_close_bps, capacity_usd,
                gross_bps, fee_bps, slippage_bps, fixed_bps, max_skew_ms,
                exhausted, samples)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opp.cycle_key, json.dumps(opp.path), json.dumps(opp.venues),
                opp.t_open, t_close, (t_close - opp.t_open) * 1000.0,
                _f(opp.size_usd), _f(opp.edge_open_bps), _f(opp.peak_edge_bps),
                _f(opp.last_edge_bps), _f(opp.capacity_usd),
                _f(b.get("gross")), _f(b.get("fee")), _f(b.get("slippage")),
                _f(b.get("fixed")), opp.max_skew_ms,
                1 if opp.exhausted else 0, opp.samples,
            ),
        )
        self.conn.commit()

    def record_rejection(self, cycle_key: str, reason: str) -> None:
        # Reasons carry sizes and timings; bucket them so the table stays small.
        bucket = reason.split(" ")[0] if reason else "unknown"
        self.conn.execute(
            """INSERT INTO rejections (cycle_key, reason, n, last_ts)
               VALUES (?,?,1,?)
               ON CONFLICT(cycle_key, reason)
               DO UPDATE SET n = n + 1, last_ts = excluded.last_ts""",
            (cycle_key, bucket, time.time()),
        )

    def record_latency(self, venue: str, kind: str, rtt_ms: float) -> None:
        self.conn.execute(
            "INSERT INTO latency (venue, kind, rtt_ms, ts) VALUES (?,?,?,?)",
            (venue, kind, rtt_ms, time.time()),
        )
        self.conn.commit()

    def record_peg(self, asset: str, venue: str, mid: Decimal,
                   dev_bps: Decimal) -> None:
        self.conn.execute(
            "INSERT INTO peg_watch (asset, venue, mid, dev_bps, ts) VALUES (?,?,?,?,?)",
            (asset, venue, _f(mid), _f(dev_bps), time.time()),
        )
        self.conn.commit()

    # --- reporting --------------------------------------------------------

    def summary(self) -> str:
        """The go/no-go numbers, read straight out of the log."""
        c = self.conn.cursor()
        out: list[str] = []

        n, = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        out.append(f"opportunities logged: {n}")
        if n:
            row = c.execute(
                """SELECT AVG(lifetime_ms), MIN(lifetime_ms), MAX(lifetime_ms),
                          AVG(peak_edge_bps), MAX(peak_edge_bps),
                          AVG(capacity_usd), AVG(max_skew_ms)
                   FROM opportunities"""
            ).fetchone()
            out.append(
                f"  lifetime_ms  avg {row[0]:.0f}  min {row[1]:.0f}  max {row[2]:.0f}"
            )
            out.append(f"  peak_edge    avg {row[3]:.2f}bps  max {row[4]:.2f}bps")
            cap = row[5]
            out.append(f"  capacity     avg ${cap:,.0f}" if cap else "  capacity     n/a")
            out.append(f"  max_skew     avg {row[6]:.0f}ms")

            med = c.execute(
                """SELECT lifetime_ms FROM opportunities
                   ORDER BY lifetime_ms LIMIT 1
                   OFFSET (SELECT COUNT(*) FROM opportunities) / 2"""
            ).fetchone()
            if med:
                out.append(f"  median lifetime {med[0]:.0f}ms")

            out.append("\ntop cycles by count:")
            for key, cnt, pk in c.execute(
                """SELECT cycle_key, COUNT(*), MAX(peak_edge_bps)
                   FROM opportunities GROUP BY cycle_key
                   ORDER BY COUNT(*) DESC LIMIT 10"""
            ):
                out.append(f"  {cnt:5d}x  peak {pk:+7.2f}bps  {key}")

        out.append("\nrejections by reason:")
        for reason, tot in c.execute(
            "SELECT reason, SUM(n) FROM rejections GROUP BY reason ORDER BY SUM(n) DESC"
        ):
            out.append(f"  {tot:8d}  {reason}")

        rows = list(c.execute(
            """SELECT venue, kind, COUNT(*), AVG(rtt_ms), MAX(rtt_ms)
               FROM latency GROUP BY venue, kind"""
        ))
        if rows:
            out.append("\nmeasured latency:")
            for venue, kind, cnt, avg, mx in rows:
                out.append(f"  {venue:12s} {kind:10s} n={cnt:<5d} avg {avg:.0f}ms  max {mx:.0f}ms")
            out.append(
                "\nCompare median lifetime against max latency above. If most "
                "opportunities die faster than your round-trip, they were never yours."
            )
        return "\n".join(out)


def main() -> None:
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "shadow.db"
    store = Store(path)
    print(store.summary())
    store.close()


if __name__ == "__main__":
    main()
