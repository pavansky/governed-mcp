"""The governor: every tool call an agent makes passes through here.

The agent proposes; this module decides. In order, for each call:

1. **Shape.** The tool must exist downstream and every argument must be one
   its input schema declares. An injected ``bypass_approval: true`` or
   ``_override`` is not a flag anyone honours - it is an unknown argument and
   the call is denied for it.
2. **Policy.** ``allow``, ``require_approval`` or ``deny``, strictest
   matching rule wins (policy.py).
3. **Step key.** Derived from the run, the step, the tool and the arguments
   (stepkeys.py). A held call is stored under it, so a retry finds the
   approval it already asked for, and a finished step returns its stored
   result without executing again.
4. **Execute.** Writes carry the step key downstream as the idempotency key.
5. **Verify.** If the policy names a read-after-write check for the tool,
   the gateway reads the record back and compares; a write the downstream
   acknowledged but did not apply is reported as failed, not as success.
6. **Audit.** Each decision, request, approval, execution and verification
   is a row in the hash-chained log (audit.py).

Approving is not something the agent can do. The governor is built for a
*role*: the ``agent`` role sees the downstream tools plus ``list_pending``
and ``approval_status``; only the ``approver`` role (a person's MCP client
or the CLI, run under their name) gets ``approve`` and ``reject``. An agent
that is talked into calling ``approve`` finds no such tool, and the attempt
is audited.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import mcp_types as types

from .approvals import DONE, EXECUTING, EXPIRED, FAILED, PENDING, REJECTED, ApprovalStore, ClaimError
from .audit import AuditLog
from .canonical import digest
from .policy import Action, Policy, fill
from .stepkeys import step_key

AGENT, APPROVER = "agent", "approver"
STEP_META_KEY = "governed/step"


class Downstream(Protocol):
    async def list_tools(self) -> types.ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult: ...


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


GOVERNANCE_TOOLS: dict[str, types.Tool] = {
    "list_pending": types.Tool(
        name="list_pending",
        description="List tool calls waiting for a human decision.",
        input_schema=_obj({}, []),
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "approval_status": types.Tool(
        name="approval_status",
        description=(
            "Check a held call by its approval_id. Returns its status (pending, executing, done, failed, "
            "rejected, expired) and, once done, the downstream result."
        ),
        input_schema=_obj({"approval_id": {"type": "string"}}, ["approval_id"]),
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "approve": types.Tool(
        name="approve",
        description="Approve a held call. It executes once, now, with exactly the arguments that were held.",
        input_schema=_obj({"approval_id": {"type": "string"}, "note": {"type": "string"}}, ["approval_id"]),
    ),
    "reject": types.Tool(
        name="reject",
        description="Reject a held call. It will not execute; the agent is told why.",
        input_schema=_obj({"approval_id": {"type": "string"}, "note": {"type": "string"}}, ["approval_id"]),
    ),
}
ROLE_TOOLS = {AGENT: ("list_pending", "approval_status"), APPROVER: tuple(GOVERNANCE_TOOLS)}


@dataclass
class Outcome:
    """What the caller is told. ``payload`` is JSON-serialisable."""

    payload: dict[str, Any]
    is_error: bool = False

    def to_mcp(self) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(self.payload, indent=2, default=str))],
            structured_content=self.payload,
            is_error=self.is_error,
        )


def _parse(result: types.CallToolResult) -> Any:
    if result.structured_content is not None:
        sc = result.structured_content
        # MCPServer wraps non-object returns as {"result": ...}
        return sc
    texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    text = "\n".join(texts)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


class Governor:
    def __init__(
        self,
        downstream: Downstream,
        policy: Policy,
        store: ApprovalStore,
        audit: AuditLog,
        *,
        role: str = AGENT,
        identity: str = "agent",
        run_id: str | None = None,
    ):
        if role not in ROLE_TOOLS:
            raise ValueError(f"role must be one of {sorted(ROLE_TOOLS)}")
        self.downstream = downstream
        self.policy = policy
        self.store = store
        self.audit = audit
        self.role = role
        self.identity = identity
        self.run_id = run_id or "run_" + secrets.token_hex(6)
        self._tools: dict[str, types.Tool] | None = None

    # -- discovery --------------------------------------------------------

    async def downstream_tools(self) -> dict[str, types.Tool]:
        if self._tools is None:
            listing = await self.downstream.list_tools()
            self._tools = {t.name: t for t in listing.tools}
        return self._tools

    def _is_read_only(self, tool: types.Tool) -> bool:
        return bool(tool.annotations and tool.annotations.read_only_hint)

    async def list_tools(self) -> list[types.Tool]:
        out: list[types.Tool] = []
        for tool in (await self.downstream_tools()).values():
            # Tell the agent the posture up front; it saves it guessing, and
            # nothing here is a secret. Unconditional posture only - argument
            # rules are decided per call.
            posture = self.policy.decide(tool.name, {}, read_only=self._is_read_only(tool)).action
            schema = dict(tool.input_schema)
            props = dict(schema.get("properties") or {})
            if self.policy.idempotency_arg in props:
                # The gateway supplies the key; hide it so the model does not
                # invent one per retry.
                props.pop(self.policy.idempotency_arg)
                schema["properties"] = props
                schema["required"] = [r for r in schema.get("required", []) if r != self.policy.idempotency_arg]
            out.append(
                tool.model_copy(
                    update={
                        "description": f"[policy: {posture.label}] {tool.description or ''}".strip(),
                        "input_schema": schema,
                    }
                )
            )
        out.extend(GOVERNANCE_TOOLS[n] for n in ROLE_TOOLS[self.role])
        return out

    # -- calls ------------------------------------------------------------

    async def call(self, name: str, args: dict[str, Any] | None, *, step: str = "") -> Outcome:
        args = dict(args or {})
        if name in GOVERNANCE_TOOLS:
            return await self._governance(name, args)

        tools = await self.downstream_tools()
        tool = tools.get(name)
        if tool is None:
            self.audit.append("call.denied", tool=name, caller=self.identity, run=self.run_id, why="unknown tool")
            return Outcome({"status": "denied", "tool": name, "reason": "unknown tool"}, is_error=True)

        declared = set((tool.input_schema.get("properties") or {}).keys())
        declared.discard(self.policy.idempotency_arg)
        unknown = sorted(k for k in args if k not in declared and k != self.policy.idempotency_arg)
        if unknown:
            self.audit.append(
                "call.denied", tool=name, caller=self.identity, run=self.run_id, why="undeclared arguments", args=unknown
            )
            return Outcome(
                {
                    "status": "denied",
                    "tool": name,
                    "reason": f"arguments not in the tool's schema: {unknown}. There is no override argument.",
                },
                is_error=True,
            )
        # A key the agent made up is not the step's key; drop it.
        args.pop(self.policy.idempotency_arg, None)

        read_only = self._is_read_only(tool)
        decision = self.policy.decide(name, args, read_only=read_only)
        key = step_key(self.run_id, name, args, step=step, exclude=self.policy.idempotency_arg)
        self.audit.append(
            "call.decided",
            tool=name,
            caller=self.identity,
            run=self.run_id,
            step_key=key,
            args_digest=digest(args),
            action=decision.action.label,
            reasons=list(decision.reasons),
        )

        if decision.action is Action.DENY:
            return Outcome(
                {"status": "denied", "tool": name, "reasons": list(decision.reasons), "retryable": False},
                is_error=True,
            )
        if decision.action is Action.ALLOW:
            ok, payload = await self._execute(tool, args, key)
            return Outcome(payload, is_error=not ok)
        return self._hold(tool, args, key, list(decision.reasons))

    def _hold(self, tool: types.Tool, args: dict[str, Any], key: str, reasons: list[str]) -> Outcome:
        approval, created = self.store.request(
            step_key=key, tool=tool.name, args=args, reasons=reasons, requested_by=self.identity
        )
        if created:
            self.audit.append(
                "approval.requested",
                approval_id=approval.id,
                tool=tool.name,
                step_key=key,
                args_digest=approval.args_digest,
                caller=self.identity,
                run=self.run_id,
            )
            return Outcome(
                {
                    "status": "pending_approval",
                    "approval_id": approval.id,
                    "tool": tool.name,
                    "reasons": reasons,
                    "message": (
                        "This call is held for a human decision. Do not retry it or try to achieve the same "
                        "change another way. Report the approval_id and check approval_status later."
                    ),
                }
            )
        self.audit.append(
            "approval.repeat", approval_id=approval.id, status=approval.status, step_key=key, run=self.run_id
        )
        return self._status_outcome(approval.id, repeat=True)

    def _status_outcome(self, approval_id: str, *, repeat: bool = False) -> Outcome:
        a = self.store.get(approval_id)
        if a is None:
            return Outcome({"status": "not_found", "approval_id": approval_id}, is_error=True)
        payload: dict[str, Any] = {"approval_id": a.id, "tool": a.tool, "status": a.status}
        if repeat:
            payload["note"] = "This step was already requested; nothing new was queued or executed."
        if a.status == PENDING:
            payload["status"] = "pending_approval"
        elif a.status in (DONE, FAILED):
            payload["result"] = a.result
        elif a.status == REJECTED:
            payload["rejected_by"] = a.decided_by
            payload["reason"] = a.note
        return Outcome(payload, is_error=a.status in (FAILED, REJECTED, EXPIRED))

    async def _execute(self, tool: types.Tool, args: dict[str, Any], key: str) -> tuple[bool, dict[str, Any]]:
        send = dict(args)
        props = tool.input_schema.get("properties") or {}
        is_write = not self._is_read_only(tool)
        if is_write and self.policy.idempotency_arg in props:
            send[self.policy.idempotency_arg] = key
        try:
            result = await self.downstream.call_tool(tool.name, send)
        except Exception as exc:  # transport failure: unknown whether it applied
            self.audit.append("call.error", tool=tool.name, step_key=key, error=repr(exc), run=self.run_id)
            return False, {"status": "error", "tool": tool.name, "error": repr(exc), "idempotency_key": key}
        parsed = _parse(result)
        payload: dict[str, Any] = {"status": "error" if result.is_error else "ok", "tool": tool.name, "result": parsed}
        if is_write:
            payload["idempotency_key"] = key
        ok = not result.is_error
        if ok and is_write and tool.name in self.policy.verify:
            verified, detail = await self._verify(tool.name, args)
            payload["verified"] = verified
            if not verified:
                ok = False
                payload["status"] = "verify_failed"
                payload["verify_detail"] = detail
        self.audit.append(
            "call.executed",
            tool=tool.name,
            step_key=key,
            ok=ok,
            verified=payload.get("verified"),
            run=self.run_id,
            caller=self.identity,
        )
        return ok, payload

    async def _verify(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        spec = self.policy.verify[tool_name]
        read_args = {k: v for k, v in fill(spec.args, args).items() if v is not None}
        expect = {k: v for k, v in fill(spec.expect, args).items() if v not in (None, "")}
        try:
            got = _parse(await self.downstream.call_tool(spec.read, read_args))
        except Exception as exc:
            return False, {"error": f"read-back failed: {exc!r}"}
        if not isinstance(got, dict):
            return False, {"error": "read-back did not return an object", "got": got}
        mismatches = {k: {"expected": v, "got": got.get(k)} for k, v in expect.items() if got.get(k) != v}
        self.audit.append("call.verified", tool=tool_name, read=spec.read, ok=not mismatches, mismatches=mismatches)
        return (not mismatches), {"mismatches": mismatches} if mismatches else {}

    # -- governance tools ---------------------------------------------------

    async def _governance(self, name: str, args: dict[str, Any]) -> Outcome:
        if name not in ROLE_TOOLS[self.role]:
            self.audit.append(
                "governance.denied", tool=name, caller=self.identity, role=self.role, run=self.run_id, args=args
            )
            return Outcome(
                {
                    "status": "denied",
                    "tool": name,
                    "reason": f"'{name}' is for human approvers; the {self.role} role cannot call it.",
                },
                is_error=True,
            )
        if name == "list_pending":
            return Outcome({"pending": [a.public() for a in self.store.list(PENDING)]})
        if name == "approval_status":
            return self._status_outcome(str(args.get("approval_id", "")))
        if name == "approve":
            return await self.approve(str(args.get("approval_id", "")), note=str(args.get("note", "")))
        if name == "reject":
            return self.reject(str(args.get("approval_id", "")), note=str(args.get("note", "")))
        raise AssertionError(name)

    async def approve(self, approval_id: str, note: str = "") -> Outcome:
        if self.role != APPROVER:
            return await self._governance("approve", {"approval_id": approval_id})
        try:
            a = self.store.claim(approval_id, self.identity, note)
        except ClaimError as exc:
            self.audit.append("approval.claim_lost", approval_id=approval_id, approver=self.identity, why=str(exc))
            return Outcome({"status": "not_claimed", "approval_id": approval_id, "reason": str(exc)}, is_error=True)
        self.audit.append("approval.approved", approval_id=a.id, approver=self.identity, note=note or None)

        # What runs is what was approved: re-hash the stored arguments.
        if digest(a.args) != a.args_digest:
            self.store.finish(a.id, ok=False, result={"error": "stored arguments changed after the request"})
            self.audit.append("approval.tampered", approval_id=a.id)
            return Outcome({"status": "failed", "approval_id": a.id, "reason": "argument digest mismatch"}, True)

        # The policy may have tightened since the request; a deny now wins.
        tools = await self.downstream_tools()
        tool = tools.get(a.tool)
        decision = self.policy.decide(a.tool, a.args, read_only=bool(tool and self._is_read_only(tool)))
        if tool is None or decision.action is Action.DENY:
            why = "tool no longer exists" if tool is None else f"policy now denies: {list(decision.reasons)}"
            self.store.finish(a.id, ok=False, result={"error": why})
            self.audit.append("approval.refused_at_execution", approval_id=a.id, why=why)
            return Outcome({"status": "failed", "approval_id": a.id, "reason": why}, is_error=True)

        ok, payload = await self._execute(tool, a.args, a.step_key)
        self.store.finish(a.id, ok=ok, result=payload)
        self.audit.append("approval.finished", approval_id=a.id, ok=ok)
        return Outcome({"approval_id": a.id, "status": DONE if ok else FAILED, "result": payload}, is_error=not ok)

    def reject(self, approval_id: str, note: str = "") -> Outcome:
        if self.role != APPROVER:
            self.audit.append("governance.denied", tool="reject", caller=self.identity, role=self.role)
            return Outcome({"status": "denied", "reason": "only approvers can reject"}, is_error=True)
        try:
            a = self.store.reject(approval_id, self.identity, note)
        except ClaimError as exc:
            return Outcome({"status": "not_rejected", "approval_id": approval_id, "reason": str(exc)}, is_error=True)
        self.audit.append("approval.rejected", approval_id=a.id, approver=self.identity, note=note or None)
        return Outcome({"approval_id": a.id, "status": REJECTED, "note": note or None})


__all__ = ["AGENT", "APPROVER", "EXECUTING", "Governor", "Outcome", "STEP_META_KEY"]
