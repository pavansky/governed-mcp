"""End-to-end demo with the stub model (no API key needed).

    uv run python -m examples.demo            # approves automatically, as "oncall-lead"
    uv run python -m examples.demo --manual   # stops and lets you approve with the CLI yourself

Coordinator -> triage sub-agent (reads) -> executor sub-agent (writes, held
for approval) -> a person approves with the CLI -> the executor sees the
verified result. Every arrow that is a tool call goes through the governed
gateway over stdio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil

from examples.stub_agents import ROOT, Agent, Sandbox, Turn, governed_client, triage, wait_for


async def main(manual: bool) -> int:
    state = ROOT / "examples" / ".governed" / "demo"
    shutil.rmtree(state, ignore_errors=True)
    box = Sandbox.create(state)
    turns: list[Turn] = []
    run_id = "run_demo"
    cli = f"uv run governed-mcp -c {box.config.relative_to(ROOT)}"

    print("coordinator: 'Triage T-1001 and fix what it needs' -> delegating to triage")
    async with governed_client(box, "triage", run_id) as tc:
        finding = await triage(Agent("triage", tc, turns, echo=True), "T-1001")
    print(f"coordinator: triage proposes {finding['proposed']} -> delegating to executor")

    async with governed_client(box, "executor", run_id) as ec:
        ex = Agent("executor", ec, turns, echo=True)
        await ex.call("add_comment", {"ticket_id": "T-1001", "body": "Triage: EU checkout down, sev1 candidate."})
        held = await ex.call("update_ticket", {"ticket_id": "T-1001", **finding["proposed"]})
        aid = held["approval_id"]
        print(f"\nexecutor: held for approval -> {aid}  ({'; '.join(held['reasons'])})")
        print(f"  ticket now: {box.crm()['tickets']['T-1001']['priority']} / {box.crm()['tickets']['T-1001']['status']}")
        print("  (the agent retries, as agents do)")
        await ex.call("update_ticket", {"ticket_id": "T-1001", **finding["proposed"]})

        print(f"\nperson: {cli} pending")
        if manual:
            print(f"        {cli} approve {aid} --as you --note 'sev1'")
            input("        run that in another terminal, then press Enter... ")
        else:
            print(f"person: {cli} approve {aid} --as oncall-lead --note sev1")
            code, out = box.cli_json("approve", aid, "--as", "oncall-lead", "--note", "sev1")
            print(f"  -> exit {code}, status={out.get('status')}, verified={out.get('result', {}).get('verified')}")

        status = await wait_for(ex, aid, tries=10)
    print(f"\nexecutor: approval_status -> {status['status']}")
    t = box.crm()["tickets"]["T-1001"]
    print(f"  ticket now: {t['priority']} / {t['status']}  (writes applied downstream: "
          f"{[w['tool'] for w in box.crm()['write_log']]})")
    code, verify = box.cli_json("audit", "verify")
    print(f"\naudit: {json.dumps(verify)}")
    print(f"       inspect with: {cli} audit tail")
    return 0 if status["status"] == "done" and verify.get("ok") else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--manual", action="store_true")
    raise SystemExit(asyncio.run(main(p.parse_args().manual)))
