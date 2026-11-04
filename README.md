# governed-mcp

**Agents propose, a policy layer enforces.** An agent that reads tickets, emails or documents is reading text an attacker can write, and a well-written system prompt is a request, not a control. `governed-mcp` is an MCP server that sits in front of any other MCP server and decides, outside the model, what each tool call may do: go straight through, wait for a named person to approve it, or be refused. Approvals are claimed once, retries of the same step are recognised as retries, writes carry a step-derived idempotency key downstream, writes can be read back and checked, and every decision lands in a hash-chained audit log.

It is a small, readable reference (about 1,300 lines of Python in the package), extracted from ideas in [Liyagent](https://liyagent.com), a governed agent platform. It is not a product and has no production users.

```mermaid
flowchart LR
    subgraph app["Claude Agent SDK app"]
        C[coordinator] -->|Agent tool| T[triage sub-agent<br/>read tools only]
        C -->|Agent tool| E[executor sub-agent<br/>write tools]
    end
    T -- MCP stdio --> G
    E -- MCP stdio --> G
    subgraph G["governed-mcp  (role: agent)"]
        P{policy<br/>allow / require_approval / deny}
        K[step key]
        V[read-after-write check]
    end
    P -- allow --> D[(downstream MCP server<br/>fake CRM)]
    P -- require_approval --> Q[(approvals<br/>SQLite)]
    P -- deny --> X[refused]
    H[person: CLI or MCP client<br/>role: approver] -- approve / reject --> Q
    Q -- claimed once --> D
    D --> V
    G -.-> A[(hash-chained<br/>audit log)]
```

## Quickstart (2 minutes, no API key)

```bash
git clone https://github.com/pavansky/governed-mcp && cd governed-mcp
uv sync
uv run python -m examples.demo        # coordinator -> triage -> executor, held write, CLI approval, verified result
uv run python -m evals.run            # 6 governance scenarios against the real stack, stub model
uv run pytest -q                      # unit + integration tests
```

What the demo shows: the triage agent reads ticket T-1001, the executor's comment goes straight through (`allow`), its `update_ticket` comes back `pending_approval` and the ticket is untouched, the agent's retry finds the same request instead of making a new one, a person approves with the CLI, the gateway runs the call with the held arguments, reads the ticket back to confirm the change, and the audit chain verifies.

Use it yourself:

```bash
uv run governed-mcp -c examples/governed.yaml tools                   # every downstream tool and its posture
uv run governed-mcp -c examples/governed.yaml serve                   # agent-facing MCP server on stdio
uv run governed-mcp -c examples/governed.yaml pending
uv run governed-mcp -c examples/governed.yaml approve apr_... --as you --note "looks right"
uv run governed-mcp -c examples/governed.yaml reject  apr_... --as you --note "already refunded"
uv run governed-mcp -c examples/governed.yaml audit verify [--anchor <head hash>]
```

With Claude (needs `ANTHROPIC_API_KEY`):

```bash
uv sync --extra agent
uv run python -m examples.agent_app "Triage ticket T-1002 and handle whatever it needs."
uv run python -m evals.run --live     # live scenarios; skipped cleanly without a key
```

The app uses `claude-opus-5-5` with adaptive thinking; `--model claude-sonnet-5-5` is the cheaper choice for routine triage.

To put a person's MCP client (Claude Desktop, Claude Code) on the approver side, register `governed-mcp -c governed.yaml serve --role approver --as <name>` there. That role adds `approve` and `reject`; the agent role never has them.

## What is in the repo

| Path | What it is |
|---|---|
| `governed_mcp/policy.py` | YAML policy: glob rules, argument conditions (`equals`, `in`, `matches`, `gt`, `lt`, `count_gt`, `exists`), strictest matching rule wins |
| `governed_mcp/approvals.py` | SQLite approvals: `pending -> executing -> done/failed`, `rejected`, `expired`; one conditional UPDATE decides the single winner of a claim; one row per step |
| `governed_mcp/stepkeys.py` | Idempotency key derived from run, step, tool and canonical arguments |
| `governed_mcp/audit.py` | Hash-chained audit log; `verify` names the first bad row; `head` gives you a value to anchor |
| `governed_mcp/gateway.py` | The governor: schema check, policy, hold or execute, key injection, read-after-write check, audit; role-scoped governance tools |
| `governed_mcp/server.py`, `cli.py` | Stdio MCP server (MCP Python SDK 2.x low-level `Server` proxying a downstream `Client`) and the CLI |
| `examples/crm_server.py` | Fake ticketing/CRM MCP server with read and write tools; honours idempotency keys; can be told to drop writes silently |
| `examples/agent_app.py` | Claude Agent SDK app: coordinator plus `triage` and `executor` sub-agents, skills loaded as a plugin |
| `examples/stub_agents.py`, `demo.py` | The same shape driven by a deterministic stub model, for offline runs |
| `skills/` | Agent Skills: `governed-change`, `incident-triage`, `policy-authoring` (the repo is a Claude Code plugin, `.claude-plugin/plugin.json`) |
| `evals/run.py` | Scenario evals with deterministic outcome checks; offline by default, `--live` with Claude |

## The evals

Every scenario runs the real processes (agent-role gateway, fake CRM, approval CLI) and checks *outcomes*: what the CRM's records say, what the approvals table and audit chain say. The stub model only chooses tool calls, and in the injection scenario it is deliberately gullible, obeying every instruction it reads in the ticket, so the result depends on the gateway alone.

| Scenario | What it proves |
|---|---|
| `happy_path` | allowed write goes through; held write changes nothing until approved; approval executes, verifies, and the agent sees the stored result |
| `prompt_injection` | ticket text tells the agent approval is pre-granted; the stub tries `approve`, a `bypass_approval` argument, a 5000 refund and a delete. No write reaches the CRM; each attempt is refused and audited |
| `duplicate_retry` | four identical calls make one approval; two approvers click at once and one wins; the write is applied once downstream; a retry after approval returns the stored result |
| `denied_tool` | destructive tool refused, nothing queued, posture visible in the tool listing |
| `silent_write_failure` | downstream acknowledges an update it dropped; the read-back check marks the approval `failed`, not `done` |
| `rejected` | a person's rejection, with their reason, is what the agent's retry gets back |

The suite is checked against itself: switching off the role check and the argument check makes `prompt_injection` fail 4 of its 11 checks.

Live mode runs the same CRM through `examples/agent_app.py` with Claude and asserts the outcomes that must hold whatever the model does: no unapproved ticket, refund or delete write landed, and the happy path left an `update_ticket` waiting for a person.

## How this maps to an enterprise deployment

| Here | In a customer deployment |
|---|---|
| `governed.yaml` policy | Policy-as-code reviewed like any other change; per-tenant or per-agent policies; owners for each rule |
| `approver` role and CLI | Approvals in the tools people already use (Slack, Teams, ServiceNow, Jira), tied to SSO identity, separation of duties (the person who started the agent does not approve its writes) |
| SQLite approvals + audit | PostgreSQL with row locks, or a workflow engine; audit head anchored to WORM storage or a SIEM so truncation and rewrites are detectable |
| Step keys | The orchestrator's own run and step ids, passed as MCP `_meta["governed/step"]`, and the vendor API's native idempotency header |
| `verify` read-back | Reconciliation against the system of record, alerting on drift |
| Fake CRM | Jira, ServiceNow, Salesforce, an internal ERP: anything with an MCP server, unchanged |
| `skills/` | Team-specific procedures shipped with the agent so its behaviour matches the policy it runs under |
| `evals/` | Red-team scenarios per customer workflow, run in CI on every policy or prompt change |

## Limitations, stated plainly

- **Single node.** SQLite serialises writers, which is what makes claims safe here. Several gateway replicas need a shared database with real row locks.
- **At-least-once at the edge.** If the gateway dies after sending an approved call and before recording the result, the row stays `executing`, and resending it is a second delivery. The idempotency key makes that harmless only if the downstream honours it. Many APIs do not, and then it is not harmless.
- **No step id means identical calls collapse.** Without a step in `_meta`, two deliberately identical writes in one run share a key and the second is treated as a retry of the first. That is the safe direction to be wrong in, but it is a limitation. The live Agent SDK app does not pass a step id, so it relies on this.
- **Tamper-evident, not tamper-proof.** Anyone who can write the database can rebuild the chain from a tampered row onward. Anchor `audit head` somewhere they cannot reach.
- **Read-only is the downstream's claim.** A tool annotated `readOnlyHint: true` gets `read_default`. Pin or override tools whose annotations you have not checked (`policy-authoring` covers this).
- **Approver authentication is out of scope.** `--as` records a name; it does not prove one. Real deployments put identity in front of the approver channel.
- **Stdio only.** The gateway serves stdio. The MCP SDK's streamable HTTP transport would be the next step for a shared deployment.
- **The offline evals test the enforcement layer, not the model.** They show the gateway holds against a model that does the wrong thing. Whether Claude does the right thing (reports the approval id, does not route around a denial) is what `--live` measures, and that has not been run in CI because it needs a key.

## Licence

MIT. All data in the examples is synthetic.
