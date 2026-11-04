from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """One connection per thread/process. Autocommit mode so the explicit
    BEGIN IMMEDIATE in the audit log is the only transaction; WAL so readers
    never block the writer; a busy timeout so concurrent writers queue
    instead of failing."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
