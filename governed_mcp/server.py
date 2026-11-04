"""The MCP face of the governor: a stdio MCP server that proxies a
downstream MCP server's tools through ``Governor``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .approvals import ApprovalStore
from .audit import AuditLog
from .config import Config
from .db import connect
from .gateway import AGENT, STEP_META_KEY, Governor


@asynccontextmanager
async def open_downstream(cfg: Config) -> AsyncIterator[Client]:
    env = {**get_default_environment(), **cfg.downstream.env}
    params = StdioServerParameters(
        command=cfg.downstream.command, args=cfg.downstream.args, env=env, cwd=cfg.downstream.cwd
    )
    async with Client(params) as client:
        yield client


def build_governor(cfg: Config, downstream: Any, *, role: str = AGENT, identity: str | None = None) -> Governor:
    cfg.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db)
    return Governor(
        downstream,
        cfg.policy,
        ApprovalStore(conn, ttl_s=cfg.policy.approval_ttl_s),
        AuditLog(conn),
        role=role,
        identity=identity or os.environ.get("GOVERNED_IDENTITY") or role,
        run_id=os.environ.get("GOVERNED_RUN_ID") or None,
    )


def make_server(gov: Governor) -> Server:
    async def on_list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await gov.list_tools())

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        meta = params.meta or {}
        step = str(meta.get(STEP_META_KEY, "")) if isinstance(meta, dict) else ""
        outcome = await gov.call(params.name, params.arguments, step=step)
        return outcome.to_mcp()

    return Server(
        "governed-mcp",
        version="0.1.0",
        instructions=(
            "Tools here are governed. A call may come back as pending_approval: that means a person must "
            "decide. Do not retry it or route around it; report the approval_id. Denied calls are final."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(cfg: Config, *, role: str = AGENT, identity: str | None = None) -> None:
    async with open_downstream(cfg) as downstream:
        gov = build_governor(cfg, downstream, role=role, identity=identity)
        server = make_server(gov)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
