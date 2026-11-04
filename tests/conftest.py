from __future__ import annotations

from pathlib import Path

import pytest

from governed_mcp.approvals import ApprovalStore
from governed_mcp.audit import AuditLog
from governed_mcp.config import Config
from governed_mcp.db import connect

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "governed.db"


@pytest.fixture
def store(db_path: Path) -> ApprovalStore:
    return ApprovalStore(connect(db_path))


@pytest.fixture
def audit(db_path: Path) -> AuditLog:
    return AuditLog(connect(db_path))


@pytest.fixture
def demo_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """The example policy, with all state in a temp dir."""
    monkeypatch.setenv("CRM_DB", str(tmp_path / "crm.json"))
    monkeypatch.delenv("CRM_DROP_WRITES", raising=False)
    cfg = Config.load(ROOT / "examples" / "governed.yaml")
    cfg.db = tmp_path / "governed.db"
    cfg.downstream.env["CRM_DB"] = str(tmp_path / "crm.json")
    return cfg
