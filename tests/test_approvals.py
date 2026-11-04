import multiprocessing as mp
import threading
import time

import pytest

from governed_mcp.approvals import DONE, EXECUTING, EXPIRED, PENDING, REJECTED, ApprovalStore, ClaimError
from governed_mcp.db import connect


def _req(store, key="gk_1", **kw):
    return store.request(step_key=key, tool="update_ticket", args={"ticket_id": "T-1"}, reasons=["r"],
                         requested_by="agent", **kw)


def test_one_approval_per_step(store):
    a, created = _req(store)
    b, created_again = _req(store)
    assert created and not created_again
    assert a.id == b.id and len(store.list(PENDING)) == 1


def test_lifecycle(store):
    a, _ = _req(store)
    claimed = store.claim(a.id, "alice")
    assert claimed.status == EXECUTING and claimed.decided_by == "alice"
    store.finish(a.id, ok=True, result={"ok": True})
    assert store.get(a.id).status == DONE and store.get(a.id).result == {"ok": True}
    with pytest.raises(ClaimError):
        store.claim(a.id, "bob")


def test_rejected_cannot_be_claimed_and_keeps_its_step(store):
    a, _ = _req(store)
    store.reject(a.id, "alice", "no")
    with pytest.raises(ClaimError):
        store.claim(a.id, "bob")
    again, created = _req(store)
    assert not created and again.status == REJECTED


def test_expired_cannot_be_claimed_but_step_may_ask_again(db_path):
    store = ApprovalStore(connect(db_path), ttl_s=0)
    a, _ = _req(store)
    time.sleep(0.01)
    with pytest.raises(ClaimError):
        store.claim(a.id, "alice")
    assert store.get(a.id).status == EXPIRED
    store.ttl_s = 3600
    b, created = _req(store)
    assert created and b.id != a.id and b.status == PENDING


def test_claim_once_under_thread_contention(db_path):
    store = ApprovalStore(connect(db_path))
    a, _ = _req(store)
    barrier = threading.Barrier(16)
    wins, losses = [], []

    def approver(i):
        mine = ApprovalStore(connect(db_path))  # own connection, as separate approvers would have
        barrier.wait()
        try:
            mine.claim(a.id, f"approver-{i}")
            wins.append(i)
        except ClaimError:
            losses.append(i)

    threads = [threading.Thread(target=approver, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(losses) == 15
    assert store.get(a.id).decided_by == f"approver-{wins[0]}"


def _proc_claim(db_path, approval_id, i, start, out):
    store = ApprovalStore(connect(db_path))
    start.wait()
    try:
        store.claim(approval_id, f"p{i}")
        out.put(i)
    except ClaimError:
        out.put(None)


def test_claim_once_across_processes(db_path):
    store = ApprovalStore(connect(db_path))
    a, _ = _req(store)
    ctx = mp.get_context("spawn")
    start, out = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_proc_claim, args=(str(db_path), a.id, i, start, out)) for i in range(6)]
    for p in procs:
        p.start()
    start.set()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert len([r for r in results if r is not None]) == 1


def test_concurrent_requests_for_one_step_make_one_row(db_path):
    ApprovalStore(connect(db_path))
    barrier = threading.Barrier(8)
    ids = []

    def asker():
        s = ApprovalStore(connect(db_path))
        barrier.wait()
        ids.append(_req(s)[0].id)

    threads = [threading.Thread(target=asker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1
