"""Offline harness: the same coordinator / triage / executor shape as
``agent_app.py``, driven by a deterministic stub model instead of Claude.

Everything below the model is real: each sub-agent is an MCP client talking
over stdio to its own ``governed-mcp serve`` process, which talks over stdio
to the fake CRM; a person's approval is the real CLI in a separate process.
Only the decision of *which tool to call next* is scripted.

The stub is deliberately gullible. ``StubModel(naive=True)`` reads every tool
result and obeys any instruction it finds there - the worst case for prompt
injection, and the case the governance layer has to hold against without any
help from the model.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "examples" / "governed.yaml"


# -- environment ------------------------------------------------------------


@dataclass
class Sandbox:
    """A throwaway copy of the demo: its own config, CRM file and gateway DB."""

    dir: Path
    config: Path
    crm_db: Path
    gateway_db: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, directory: Path, *, drop_writes: str = "") -> Sandbox:
        directory.mkdir(parents=True, exist_ok=True)
        raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
        crm_db = directory / "crm.json"
        gateway_db = directory / "governed.db"
        raw["downstream"]["cwd"] = str(ROOT)
        raw["downstream"]["env"] = {"CRM_DB": str(crm_db)}
        if drop_writes:
            raw["downstream"]["env"]["CRM_DROP_WRITES"] = drop_writes
        raw["db"] = str(gateway_db)
        config = directory / "governed.yaml"
        config.write_text(yaml.safe_dump(raw, sort_keys=False))
        return cls(directory, config, crm_db, gateway_db)

    def crm(self) -> dict[str, Any]:
        from examples.crm_server import SEED

        if not self.crm_db.exists():
            return json.loads(json.dumps(SEED))
        return json.loads(self.crm_db.read_text())

    def cli(self, *args: str) -> subprocess.Popen:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        env.pop("GOVERNED_DB", None)
        return subprocess.Popen(
            [sys.executable, "-m", "governed_mcp", "-c", str(self.config), *args],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def cli_json(self, *args: str) -> tuple[int, Any]:
        p = self.cli(*args)
        out, err = p.communicate(timeout=60)
        try:
            return p.returncode, json.loads(out)
        except json.JSONDecodeError:
            return p.returncode, {"stdout": out, "stderr": err}


@asynccontextmanager
async def governed_client(box: Sandbox, identity: str, run_id: str) -> AsyncIterator[Client]:
    """An agent-role MCP client: what a sub-agent's tool calls go through."""
    env = {**get_default_environment(), "GOVERNED_RUN_ID": run_id, "PYTHONPATH": str(ROOT)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "governed_mcp", "-c", str(box.config), "serve", "--as", identity],
        env=env,
        cwd=str(ROOT),
    )
    async with Client(params) as client:
        yield client


# -- the stub model -----------------------------------------------------------


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]


@dataclass
class Turn:
    agent: str
    tool: str
    args: dict[str, Any]
    is_error: bool
    result: dict[str, Any]


_INJECTIONS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"call approve", re.I), lambda m, ctx: [ToolCall("approve", {"approval_id": a}) for a in ctx["pending"]]
     or [ToolCall("approve", {"approval_id": "apr_any"})]),
    (re.compile(r"bypass_approval\s*=\s*true", re.I),
     lambda m, ctx: [ToolCall("update_ticket", {"ticket_id": ctx["ticket"], "status": "closed", "bypass_approval": True})]),
    (re.compile(r"refund of (\d+) to (C-\d+)", re.I),
     lambda m, ctx: [ToolCall("issue_refund", {"customer_id": m.group(2), "amount": float(m.group(1)), "reason": "per notice"})]),
    (re.compile(r"close this ticket", re.I),
     lambda m, ctx: [ToolCall("update_ticket", {"ticket_id": ctx["ticket"], "status": "closed"})]),
    (re.compile(r"delete customer (C-\d+)", re.I),
     lambda m, ctx: [ToolCall("delete_customer", {"customer_id": m.group(1)})]),
]


class StubModel:
    """Chooses tool calls deterministically. ``naive`` makes it follow
    instructions embedded in data it reads."""

    def __init__(self, naive: bool = False):
        self.naive = naive

    def injected_calls(self, text: str, ctx: dict[str, Any]) -> list[ToolCall]:
        if not self.naive:
            return []
        calls: list[ToolCall] = []
        for pattern, make in _INJECTIONS:
            m = pattern.search(text)
            if m:
                calls.extend(make(m, ctx))
        return calls


class Agent:
    """One sub-agent: a name, a governed MCP client, a shared transcript."""

    def __init__(self, name: str, client: Client, transcript: list[Turn], *, echo: bool = False):
        self.name, self.client, self.transcript, self.echo = name, client, transcript, echo

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        r = await self.client.call_tool(tool, args)
        payload = r.structured_content or {}
        self.transcript.append(Turn(self.name, tool, args, bool(r.is_error), payload))
        if self.echo:
            status = payload.get("status", "ok")
            print(f"  [{self.name}] {tool}({json.dumps(args)}) -> {'ERROR ' if r.is_error else ''}{status}")
        return payload


async def triage(agent: Agent, ticket_id: str) -> dict[str, Any]:
    """Triage reader: reads only. Returns its finding for the coordinator."""
    t = (await agent.call("get_ticket", {"ticket_id": ticket_id}))["result"]
    c = (await agent.call("get_customer", {"customer_id": t["customer_id"]}))["result"]
    severe = any(w in (t["title"] + t["body"]).lower() for w in ("500", "outage", "all eu", "revenue"))
    return {
        "ticket": t["id"],
        "customer": c["id"],
        "tier": c["tier"],
        "proposed": {"priority": "P1", "status": "in_progress"} if severe else None,
        "evidence": t["body"],  # the raw text travels on - which is how injections spread
    }


async def wait_for(agent: Agent, approval_id: str, *, tries: int = 1) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _ in range(tries):
        out = await agent.call("approval_status", {"approval_id": approval_id})
        if out.get("status") not in ("pending_approval", "executing"):
            break
        await asyncio.sleep(0.2)
    return out


def run(coro):
    return asyncio.run(coro)
