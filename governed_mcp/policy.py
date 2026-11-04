"""Policy: what an agent may do with each downstream tool.

A policy is a list of rules. Each rule matches tools by glob and, optionally,
by a condition on the call's arguments, and names one of three actions:

* ``allow`` - the call goes straight through;
* ``require_approval`` - the call is held, exactly as asked, until a person
  approves or rejects it;
* ``deny`` - refused, whoever asks.

Every matching rule is considered and the strictest wins
(``deny`` > ``require_approval`` > ``allow``). That makes rules additive and
order-independent: adding a rule can only tighten what the policy already
said, never quietly open a hole above it. A call that no rule matches gets
``default`` - except a tool the downstream server annotates
``readOnlyHint: true``, which gets ``read_default`` (normally ``allow``).

Example::

    default: require_approval
    read_default: allow
    rules:
      - tool: "delete_*"
        action: deny
      - tool: issue_refund
        when: {path: amount, gt: 500}
        action: deny
      - tool: add_comment
        action: allow
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Action(IntEnum):
    # Ordered by strictness so max() picks the strictest.
    ALLOW = 0
    REQUIRE_APPROVAL = 1
    DENY = 2

    @classmethod
    def parse(cls, value: str) -> Action:
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise PolicyError(f"unknown action {value!r} (allow | require_approval | deny)") from exc

    @property
    def label(self) -> str:
        return self.name.lower()


class PolicyError(ValueError):
    pass


_OPS = {"equals", "not_equals", "in", "matches", "gt", "lt", "count_gt", "exists"}
_MISSING = object()


def _lookup(args: dict[str, Any], path: str) -> Any:
    cur: Any = args
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


@dataclass(frozen=True)
class Condition:
    path: str
    op: str
    value: Any

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Condition:
        if not isinstance(raw, dict) or "path" not in raw:
            raise PolicyError(f"condition needs a 'path': {raw!r}")
        ops = [k for k in raw if k != "path"]
        if len(ops) != 1 or ops[0] not in _OPS:
            raise PolicyError(f"condition needs exactly one of {sorted(_OPS)}: {raw!r}")
        op = ops[0]
        if op == "matches":
            re.compile(raw[op])
        return cls(path=raw["path"], op=op, value=raw[op])

    def holds(self, args: dict[str, Any]) -> bool:
        got = _lookup(args, self.path)
        if self.op == "exists":
            return (got is not _MISSING) == bool(self.value)
        if got is _MISSING:
            return False
        try:
            if self.op == "equals":
                return got == self.value
            if self.op == "not_equals":
                return got != self.value
            if self.op == "in":
                return got in self.value
            if self.op == "matches":
                return re.search(self.value, str(got)) is not None
            if self.op == "gt":
                return float(got) > float(self.value)
            if self.op == "lt":
                return float(got) < float(self.value)
            if self.op == "count_gt":
                return hasattr(got, "__len__") and len(got) > int(self.value)
        except (TypeError, ValueError):
            # A value that cannot be compared does not satisfy the condition;
            # the rule then simply does not apply.
            return False
        return False


@dataclass(frozen=True)
class Rule:
    tool: str
    action: Action
    when: Condition | None = None
    reason: str = ""

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        return self.when is None or self.when.holds(args)


@dataclass(frozen=True)
class Decision:
    action: Action
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerifySpec:
    """Read-after-write check: after ``tool`` succeeds, call ``read`` and
    compare fields. Values in ``args``/``expect`` may be ``"{arg_name}"``
    templates filled from the write's own arguments."""

    read: str
    args: dict[str, Any]
    expect: dict[str, Any]


@dataclass
class Policy:
    default: Action = Action.REQUIRE_APPROVAL
    read_default: Action = Action.ALLOW
    rules: list[Rule] = field(default_factory=list)
    verify: dict[str, VerifySpec] = field(default_factory=dict)
    idempotency_arg: str = "idempotency_key"
    approval_ttl_s: int = 3600

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Policy:
        raw = raw or {}
        rules = []
        for i, r in enumerate(raw.get("rules") or []):
            if "tool" not in r or "action" not in r:
                raise PolicyError(f"rule {i} needs 'tool' and 'action'")
            rules.append(
                Rule(
                    tool=str(r["tool"]),
                    action=Action.parse(r["action"]),
                    when=Condition.parse(r["when"]) if r.get("when") else None,
                    reason=str(r.get("reason", "")),
                )
            )
        verify = {
            name: VerifySpec(read=v["read"], args=dict(v.get("args") or {}), expect=dict(v.get("expect") or {}))
            for name, v in (raw.get("verify") or {}).items()
        }
        return cls(
            default=Action.parse(raw.get("default", "require_approval")),
            read_default=Action.parse(raw.get("read_default", "allow")),
            rules=rules,
            verify=verify,
            idempotency_arg=str(raw.get("idempotency_arg", "idempotency_key")),
            approval_ttl_s=int(raw.get("approval_ttl_s", 3600)),
        )

    def decide(self, tool: str, args: dict[str, Any], *, read_only: bool = False) -> Decision:
        hits = [r for r in self.rules if r.matches(tool, args)]
        if not hits:
            base = self.read_default if read_only else self.default
            why = "read-only tool, read_default" if read_only else "no rule matched, default"
            return Decision(base, (f"{why}: {base.label}",))
        action = max(r.action for r in hits)
        reasons = tuple(
            r.reason or f"rule tool={r.tool}{' when ' + r.when.path + ' ' + r.when.op if r.when else ''}: {r.action.label}"
            for r in hits
            if r.action == action
        )
        return Decision(action, reasons)


def fill(template: Any, args: dict[str, Any]) -> Any:
    """Fill ``"{name}"`` placeholders from ``args``. A whole-string placeholder
    keeps the argument's type; anything else is left as written."""
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        got = _lookup(args, template[1:-1])
        return None if got is _MISSING else got
    if isinstance(template, dict):
        return {k: fill(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, args) for v in template]
    return template
