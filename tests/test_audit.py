import json

from governed_mcp.audit import GENESIS, AuditLog
from governed_mcp.db import connect


def _log(audit: AuditLog, n: int = 5) -> None:
    for i in range(n):
        audit.append("call.decided", tool="t", i=i)


def test_empty_and_intact_chain_verifies(audit):
    assert audit.verify().ok and audit.head() == GENESIS
    _log(audit)
    res = audit.verify()
    assert res.ok and res.rows == 5


def test_edited_row_is_detected(audit):
    _log(audit)
    audit.conn.execute("UPDATE audit SET data = ? WHERE seq = 3", (json.dumps({"tool": "t", "i": 99}),))
    res = audit.verify()
    assert not res.ok and res.first_bad_seq == 3


def test_deleted_middle_row_is_detected(audit):
    _log(audit)
    audit.conn.execute("DELETE FROM audit WHERE seq = 2")
    res = audit.verify()
    assert not res.ok and res.first_bad_seq == 3


def test_rehashed_row_still_breaks_the_next_link(audit):
    _log(audit)
    # An attacker who recomputes row 2's own hash still breaks row 3's prev_hash.
    from governed_mcp.audit import _row_hash

    seq, ts, event, data, prev = audit.conn.execute(
        "SELECT seq, ts, event, data, prev_hash FROM audit WHERE seq = 2"
    ).fetchone()
    forged = json.dumps({"tool": "t", "i": -1})
    audit.conn.execute(
        "UPDATE audit SET data = ?, hash = ? WHERE seq = 2", (forged, _row_hash(seq, ts, event, forged, prev))
    )
    res = audit.verify()
    assert not res.ok and res.first_bad_seq == 3


def test_truncation_needs_an_anchor(audit):
    _log(audit)
    anchor = audit.head()
    audit.conn.execute("DELETE FROM audit WHERE seq = 5")
    assert audit.verify().ok  # a shorter chain is internally consistent...
    assert not audit.verify(expected_head=anchor).ok  # ...but not the one that was anchored


def test_concurrent_writers_keep_one_chain(db_path):
    import threading

    AuditLog(connect(db_path))

    def worker(k):
        log = AuditLog(connect(db_path))
        for i in range(25):
            log.append("e", worker=k, i=i)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    res = AuditLog(connect(db_path)).verify()
    assert res.ok and res.rows == 100
