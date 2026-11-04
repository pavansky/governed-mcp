"""Hash-chained audit log.

Each row stores the hash of the row before it, and its own hash covers that
link plus everything it records. Changing, removing or reordering any row
breaks every hash after it, which ``verify`` reports with the first bad
sequence number.

This is tamper-*evident*, not tamper-proof: someone with write access to the
database can rewrite the whole chain from the tampered row onward. Anchoring
the head hash somewhere the writer cannot reach (a WORM bucket, a ticket, a
signed checkpoint) is what turns evidence into proof; ``head()`` returns the
value to anchor. Likewise, cutting rows off the *end* leaves a valid shorter
chain; only a comparison with an anchored head (``verify(expected_head=...)``)
notices that.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .canonical import canonical, digest

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows: int
    first_bad_seq: int | None = None
    detail: str = ""


def _row_hash(seq: int, ts: float, event: str, data: str, prev_hash: str) -> str:
    return digest({"seq": seq, "ts": ts, "event": event, "data": data, "prev": prev_hash})


class AuditLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(SCHEMA)

    def append(self, event: str, **data: Any) -> str:
        payload = canonical(data)
        # BEGIN IMMEDIATE takes the write lock before reading the head, so two
        # writers cannot both chain onto the same previous row.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
            ts = round(time.time(), 6)
            h = _row_hash(seq, ts, event, payload, prev)
            self.conn.execute(
                "INSERT INTO audit (seq, ts, event, data, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
                (seq, ts, event, payload, prev, h),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return h

    def head(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def rows(self, event_prefix: str = "") -> list[dict[str, Any]]:
        import json

        cur = self.conn.execute(
            "SELECT seq, ts, event, data FROM audit WHERE event LIKE ? ORDER BY seq", (event_prefix + "%",)
        )
        return [{"seq": s, "ts": t, "event": e, **json.loads(d)} for s, t, e, d in cur]

    def verify(self, expected_head: str | None = None) -> VerifyResult:
        result = self._walk()
        if result.ok and expected_head is not None and self.head() != expected_head:
            # The chain is internally consistent but does not end where the
            # anchor says it should: rows were cut off or rewritten wholesale.
            return VerifyResult(False, result.rows, None, "chain head does not match the anchored head")
        return result

    def _walk(self) -> VerifyResult:
        prev = GENESIS
        expected_seq = 1
        n = 0
        for seq, ts, event, data, prev_hash, h in self.conn.execute(
            "SELECT seq, ts, event, data, prev_hash, hash FROM audit ORDER BY seq"
        ):
            n += 1
            if seq != expected_seq:
                return VerifyResult(False, n, seq, f"gap: expected seq {expected_seq}, found {seq}")
            if prev_hash != prev:
                return VerifyResult(False, n, seq, "prev_hash does not match the previous row")
            if _row_hash(seq, ts, event, data, prev_hash) != h:
                return VerifyResult(False, n, seq, "row contents do not match its hash")
            prev = h
            expected_seq += 1
        return VerifyResult(True, n)
