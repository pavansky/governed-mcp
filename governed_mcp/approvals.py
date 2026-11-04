"""Human approvals for held tool calls.

An approval holds one tool call exactly as the agent asked for it, with a
digest of its arguments taken at request time. Its life is::

    pending ──approve──▶ executing ──▶ done
       │                     └──────▶ failed
       ├──reject──▶ rejected
       └──(ttl)───▶ expired

Two properties carry the weight:

* **Claimed once.** ``claim`` is a single conditional UPDATE
  (``WHERE status = 'pending'``). However many approvers click, however many
  processes race, exactly one sees ``rowcount == 1`` and goes on to execute;
  the rest are told it was already claimed. SQLite serialises writers, so
  this holds across threads and processes sharing the file.
* **One per step.** ``step_key`` is UNIQUE. An agent that retries the same
  step finds the approval it already asked for (pending, done, rejected...)
  rather than queueing a second one for a person to approve twice.

What is executed is what was approved: the gateway re-hashes the stored
arguments before sending and refuses on mismatch, so a row edited in the
database after a person looked at it is not sent.

Execution is at-least-once *from the gateway's side* if the process dies
between sending and ``finish``: the row stays ``executing``. That is why the
same step key is also sent downstream as the idempotency key - a downstream
that dedupes on it turns the retry into a no-op. ``stale_executing`` lists
such rows for an operator.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .canonical import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    step_key TEXT NOT NULL UNIQUE,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL,
    note TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status);
"""

PENDING, EXECUTING, DONE, FAILED, REJECTED, EXPIRED = "pending", "executing", "done", "failed", "rejected", "expired"


@dataclass(frozen=True)
class Approval:
    id: str
    step_key: str
    tool: str
    args: dict[str, Any]
    args_digest: str
    status: str
    reasons: list[str]
    requested_by: str
    requested_at: float
    expires_at: float
    decided_by: str | None
    decided_at: float | None
    note: str | None
    result: Any

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "reasons": self.reasons,
            "requested_by": self.requested_by,
            "expires_in_s": max(0, int(self.expires_at - time.time())) if self.status == PENDING else None,
            "decided_by": self.decided_by,
            "note": self.note,
            "result": self.result,
        }


class ClaimError(RuntimeError):
    pass


_COLS = (
    "id, step_key, tool, args, args_digest, status, reasons, requested_by, "
    "requested_at, expires_at, decided_by, decided_at, note, result"
)


def _row(r: tuple) -> Approval:
    return Approval(
        id=r[0],
        step_key=r[1],
        tool=r[2],
        args=json.loads(r[3]),
        args_digest=r[4],
        status=r[5],
        reasons=json.loads(r[6]),
        requested_by=r[7],
        requested_at=r[8],
        expires_at=r[9],
        decided_by=r[10],
        decided_at=r[11],
        note=r[12],
        result=json.loads(r[13]) if r[13] is not None else None,
    )


class ApprovalStore:
    def __init__(self, conn: sqlite3.Connection, ttl_s: int = 3600):
        self.conn = conn
        self.ttl_s = ttl_s
        conn.executescript(SCHEMA)

    # -- requests ---------------------------------------------------------

    def request(
        self, *, step_key: str, tool: str, args: dict[str, Any], reasons: list[str], requested_by: str
    ) -> tuple[Approval, bool]:
        """Hold a call for approval. Returns ``(approval, created)``; when the
        step already asked, returns the existing row and ``created=False``."""
        now = time.time()
        aid = "apr_" + secrets.token_hex(6)
        # An expired request no longer answers for its step: move it aside so
        # the step can ask again. Rejected ones keep the key - a person said no.
        self.expire()
        self.conn.execute(
            "UPDATE approvals SET step_key = step_key || ':expired:' || id WHERE step_key = ? AND status = ?",
            (step_key, EXPIRED),
        )
        cur = self.conn.execute(
            f"INSERT INTO approvals ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL) "
            "ON CONFLICT(step_key) DO NOTHING",
            (
                aid,
                step_key,
                tool,
                json.dumps(args, sort_keys=True),
                digest(args),
                PENDING,
                json.dumps(reasons),
                requested_by,
                now,
                now + self.ttl_s,
            ),
        )
        created = cur.rowcount == 1
        found = self.by_step(step_key)
        assert found is not None
        return found, created

    def get(self, approval_id: str) -> Approval | None:
        self.expire()
        r = self.conn.execute(f"SELECT {_COLS} FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row(r) if r else None

    def by_step(self, step_key: str) -> Approval | None:
        r = self.conn.execute(f"SELECT {_COLS} FROM approvals WHERE step_key = ?", (step_key,)).fetchone()
        return _row(r) if r else None

    def list(self, status: str | None = PENDING) -> list[Approval]:
        self.expire()
        if status is None:
            cur = self.conn.execute(f"SELECT {_COLS} FROM approvals ORDER BY requested_at")
        else:
            cur = self.conn.execute(
                f"SELECT {_COLS} FROM approvals WHERE status = ? ORDER BY requested_at", (status,)
            )
        return [_row(r) for r in cur]

    # -- decisions --------------------------------------------------------

    def expire(self) -> int:
        cur = self.conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE status = ? AND expires_at <= ?",
            (EXPIRED, time.time(), PENDING, time.time()),
        )
        return cur.rowcount

    def claim(self, approval_id: str, approver: str, note: str = "") -> Approval:
        """Move pending -> executing. Exactly one caller wins."""
        self.expire()
        cur = self.conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, note = ? "
            "WHERE id = ? AND status = ? AND expires_at > ?",
            (EXECUTING, approver, time.time(), note or None, approval_id, PENDING, time.time()),
        )
        if cur.rowcount != 1:
            current = self.get(approval_id)
            state = current.status if current else "not found"
            raise ClaimError(f"approval {approval_id} cannot be claimed: {state}")
        got = self.get(approval_id)
        assert got is not None
        return got

    def reject(self, approval_id: str, approver: str, note: str = "") -> Approval:
        self.expire()
        cur = self.conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ? AND status = ?",
            (REJECTED, approver, time.time(), note or None, approval_id, PENDING),
        )
        if cur.rowcount != 1:
            current = self.get(approval_id)
            raise ClaimError(f"approval {approval_id} cannot be rejected: {current.status if current else 'not found'}")
        got = self.get(approval_id)
        assert got is not None
        return got

    def finish(self, approval_id: str, *, ok: bool, result: Any) -> None:
        self.conn.execute(
            "UPDATE approvals SET status = ?, result = ? WHERE id = ? AND status = ?",
            (DONE if ok else FAILED, json.dumps(result, default=str), approval_id, EXECUTING),
        )

    def stale_executing(self, older_than_s: float = 300) -> list[Approval]:
        cur = self.conn.execute(
            f"SELECT {_COLS} FROM approvals WHERE status = ? AND decided_at < ?",
            (EXECUTING, time.time() - older_than_s),
        )
        return [_row(r) for r in cur]
