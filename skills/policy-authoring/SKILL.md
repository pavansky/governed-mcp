---
name: policy-authoring
description: Write or review a governed-mcp policy (governed.yaml) for a customer's MCP server - which tools are allowed, which need human approval, which are denied, argument rules, and read-after-write checks. Use when onboarding a new MCP server behind governed-mcp, or when asked whether an agent's tool access is safe.
---

# Authoring a governed-mcp policy

Work from the downstream server's real tool list, not its README. Point a
draft `governed.yaml` at the server (just `downstream:` and `default:
require_approval`) and list what it exposes, with the posture the draft gives
each tool:

```bash
uv run governed-mcp -c governed.yaml tools
```

## Procedure

1. **Classify every tool** as read, reversible write, or irreversible write.
   Do not trust names or annotations blindly: a `get_` tool that marks a
   message read is a write.
2. **Start strict.** `default: require_approval`, `read_default: allow`.
   Then loosen individual low-risk writes (`add_comment`) to `allow`.
3. **Deny what is never delegated** (deletes, permission changes, payments
   above a ceiling) with `action: deny`. Rules combine strictest-wins, so a
   deny can never be overridden by a later allow - you cannot write a policy
   that "un-denies" by accident.
4. **Add argument rules** where risk depends on the call:
   `{path: amount, gt: 500}`, `{path: ids, count_gt: 50}`,
   `{path: target.host, matches: "^prod-"}`. Ops: `equals`, `not_equals`,
   `in`, `matches`, `gt`, `lt`, `count_gt`, `exists`. A value that cannot be
   compared does not satisfy a condition, so make sure the strict outcome is
   the rule *without* a condition.
5. **Add `verify`** for writes whose effect a read tool can confirm.
   Templates `"{arg}"` copy the write's arguments; empty values are skipped.
6. **Check idempotency support.** If the downstream accepts an idempotency
   key under another name, set `idempotency_arg`. If it accepts none, say so
   in the review: a gateway crash between send and record can then repeat a
   write.
7. **Prove it.** Add an eval scenario (see `evals/run.py`) that tries the
   riskiest call with a gullible stub and asserts on downstream state.

## Review checklist

- [ ] Every irreversible tool is `deny` or `require_approval`
- [ ] No tool is `allow` only because of its name
- [ ] Limits are expressed as deny rules on arguments, not as prompt text
- [ ] The approver role is served only to people (`serve --role approver --as <name>`), never to an agent
- [ ] Verification exists for the writes a customer would notice going missing
