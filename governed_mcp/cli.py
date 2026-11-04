"""governed-mcp command line.

    governed-mcp serve   -c governed.yaml [--role agent|approver] [--as NAME]
    governed-mcp tools   -c governed.yaml          # downstream tools and their posture
    governed-mcp pending -c governed.yaml
    governed-mcp show    -c governed.yaml APPROVAL_ID
    governed-mcp approve -c governed.yaml APPROVAL_ID [--as NAME] [--note TEXT]
    governed-mcp reject  -c governed.yaml APPROVAL_ID [--as NAME] [--note TEXT]
    governed-mcp audit   -c governed.yaml verify [--anchor HASH] | tail [-n N] | head
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from .approvals import PENDING
from .config import Config
from .gateway import AGENT, APPROVER
from .server import build_governor, open_downstream, serve_stdio


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _approver(args: argparse.Namespace) -> str:
    return args.as_ or getpass.getuser()


async def _approve(cfg: Config, approval_id: str, who: str, note: str) -> int:
    async with open_downstream(cfg) as downstream:
        gov = build_governor(cfg, downstream, role=APPROVER, identity=who)
        out = await gov.approve(approval_id, note=note)
    _print(out.payload)
    return 1 if out.is_error else 0


async def _tools(cfg: Config) -> int:
    async with open_downstream(cfg) as downstream:
        gov = build_governor(cfg, downstream, role=AGENT, identity="cli")
        for name, tool in (await gov.downstream_tools()).items():
            ro = gov._is_read_only(tool)
            posture = cfg.policy.decide(name, {}, read_only=ro).action.label
            ann = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
            print(f"{posture:17} {name:24} {'read' if ro else 'write':5} {ann}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="governed-mcp", description="Governance gateway for MCP tool calls.")
    p.add_argument("-c", "--config", default="governed.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the governed MCP server on stdio")
    s.add_argument("--role", choices=[AGENT, APPROVER], default=AGENT)
    s.add_argument("--as", dest="as_", help="identity recorded in the audit log")

    sub.add_parser("tools", help="list downstream tools with their unconditional posture")
    sub.add_parser("pending", help="list calls waiting for a decision")
    sh = sub.add_parser("show", help="show one approval")
    sh.add_argument("approval_id")

    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("approval_id")
        d.add_argument("--as", dest="as_", help="approver identity (default: $USER)")
        d.add_argument("--note", default="")

    a = sub.add_parser("audit", help="inspect or verify the audit chain")
    a.add_argument("action", choices=["verify", "tail", "head"])
    a.add_argument("-n", type=int, default=20)
    a.add_argument("--anchor", help="expected head hash (detects truncation)")

    args = p.parse_args(argv)
    cfg = Config.load(args.config)

    if args.cmd == "serve":
        role = args.role
        asyncio.run(serve_stdio(cfg, role=role, identity=args.as_ or (getpass.getuser() if role == APPROVER else None)))
        return 0
    if args.cmd == "tools":
        return asyncio.run(_tools(cfg))
    if args.cmd == "approve":
        return asyncio.run(_approve(cfg, args.approval_id, _approver(args), args.note))

    # The rest never touch the downstream server.
    gov = build_governor(cfg, downstream=None, role=APPROVER, identity=_approver(args) if "as_" in args else None)
    if args.cmd == "pending":
        _print([x.public() for x in gov.store.list(PENDING)])
        return 0
    if args.cmd == "show":
        found = gov.store.get(args.approval_id)
        _print(found.public() if found else {"error": "not found"})
        return 0 if found else 1
    if args.cmd == "reject":
        out = gov.reject(args.approval_id, note=args.note)
        _print(out.payload)
        return 1 if out.is_error else 0
    if args.cmd == "audit":
        if args.action == "verify":
            res = gov.audit.verify(expected_head=args.anchor)
            _print({"ok": res.ok, "rows": res.rows, "first_bad_seq": res.first_bad_seq, "detail": res.detail})
            return 0 if res.ok else 2
        if args.action == "head":
            print(gov.audit.head())
            return 0
        rows = gov.audit.rows()
        for r in rows[-args.n :]:
            print(json.dumps(r, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
