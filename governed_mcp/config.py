"""Gateway configuration: one YAML file names the downstream server, the
state database and the policy.

    downstream:
      command: python                 # "python" means this interpreter
      args: ["-m", "examples.crm_server"]
      env: {CRM_DB: .governed/crm.json}
    db: .governed/governed.db
    policy:
      default: require_approval
      rules: [...]

Relative paths (``db``, ``downstream.cwd``) are resolved against the config
file's directory, which is also the downstream's working directory unless
``cwd`` says otherwise. ``${VAR}`` is expanded in string values.
Environment overrides: ``GOVERNED_DB`` for the database path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .policy import Policy


@dataclass
class DownstreamSpec:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass
class Config:
    downstream: DownstreamSpec
    db: Path
    policy: Policy
    base_dir: Path

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path).resolve()
        raw = yaml.safe_load(path.read_text()) or {}
        return cls.from_dict(raw, base_dir=path.parent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base_dir: Path) -> Config:
        ds = raw.get("downstream") or {}
        if "command" not in ds:
            raise ValueError("config needs downstream.command")
        command = os.path.expandvars(ds["command"])
        if command in ("python", "python3"):
            command = sys.executable
        cwd = Path(os.path.expandvars(ds.get("cwd") or ".")).expanduser()
        cwd = cwd if cwd.is_absolute() else base_dir / cwd
        db = Path(os.environ.get("GOVERNED_DB") or os.path.expandvars(raw.get("db", ".governed/governed.db")))
        db = db if db.is_absolute() else base_dir / db
        return cls(
            downstream=DownstreamSpec(
                command=command,
                args=[os.path.expandvars(str(a)) for a in ds.get("args") or []],
                env={k: os.path.expandvars(str(v)) for k, v in (ds.get("env") or {}).items()},
                cwd=str(cwd),
            ),
            db=db,
            policy=Policy.from_dict(raw.get("policy") or {}),
            base_dir=base_dir,
        )
