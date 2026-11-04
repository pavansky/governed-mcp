import json

from governed_mcp.audit import AuditLog
from governed_mcp.cli import main
from governed_mcp.db import connect


def _config(tmp_path):
    cfg = tmp_path / "governed.yaml"
    cfg.write_text("downstream: {command: python, args: ['-c', 'pass']}\ndb: governed.db\n")
    return cfg


def test_audit_verify_exit_codes(tmp_path, capsys):
    cfg = _config(tmp_path)
    log = AuditLog(connect(tmp_path / "governed.db"))
    for i in range(3):
        log.append("e", i=i)
    head = log.head()
    assert main(["-c", str(cfg), "audit", "verify", "--anchor", head]) == 0
    log.conn.execute("UPDATE audit SET event = 'x' WHERE seq = 2")
    assert main(["-c", str(cfg), "audit", "verify"]) == 2
    out = capsys.readouterr().out
    assert json.loads(out[out.rindex("{"):])["first_bad_seq"] == 2


def test_pending_and_reject_without_downstream(tmp_path, capsys):
    from governed_mcp.approvals import ApprovalStore

    cfg = _config(tmp_path)
    store = ApprovalStore(connect(tmp_path / "governed.db"))
    a, _ = store.request(step_key="gk_x", tool="t", args={}, reasons=[], requested_by="agent")
    assert main(["-c", str(cfg), "pending"]) == 0
    assert a.id in capsys.readouterr().out
    assert main(["-c", str(cfg), "reject", a.id, "--as", "alice", "--note", "no"]) == 0
    assert main(["-c", str(cfg), "reject", a.id, "--as", "alice"]) == 1
    assert store.get(a.id).decided_by == "alice"
