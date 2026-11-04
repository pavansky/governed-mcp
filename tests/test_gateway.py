"""The governor end to end, over real MCP, against the fake CRM in-process."""

import json
import os

from mcp import Client

from examples.crm_server import SEED
from examples.crm_server import mcp as crm
from governed_mcp.gateway import APPROVER
from governed_mcp.server import build_governor, make_server


def crm_state():
    """The CRM's records; the file only exists once something was written."""
    if not os.path.exists(os.environ["CRM_DB"]):
        return json.loads(json.dumps(SEED))
    with open(os.environ["CRM_DB"]) as f:
        return json.load(f)


async def call(client, name, args):
    r = await client.call_tool(name, args)
    return r.is_error, r.structured_content


async def test_reads_allowed_writes_held_deletes_denied(demo_config):
    async with Client(crm) as ds:
        gov = build_governor(demo_config, ds)
        async with Client(make_server(gov)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"get_ticket", "update_ticket", "list_pending", "approval_status"} <= names
            assert not {"approve", "reject"} & names

            err, out = await call(c, "get_ticket", {"ticket_id": "T-1001"})
            assert not err and out["result"]["id"] == "T-1001"

            err, out = await call(c, "update_ticket", {"ticket_id": "T-1001", "priority": "P1"})
            assert not err and out["status"] == "pending_approval"

            err, out = await call(c, "delete_customer", {"customer_id": "C-100"})
            assert err and out["status"] == "denied"

    assert crm_state()["tickets"]["T-1001"]["priority"] == "P3"
    assert "C-100" in crm_state()["customers"]


async def test_idempotency_key_is_hidden_from_the_agent_and_injected_downstream(demo_config):
    async with Client(crm) as ds:
        gov = build_governor(demo_config, ds)
        tools = {t.name: t for t in await gov.list_tools()}
        assert "idempotency_key" not in tools["add_comment"].input_schema["properties"]
        out = await gov.call("add_comment", {"ticket_id": "T-1001", "body": "looking", "idempotency_key": "x"})
        assert not out.is_error
        key = out.payload["idempotency_key"]
        assert key.startswith("gk_") and key in crm_state()["applied_keys"]

        # A retried allowed write is applied once downstream.
        again = await gov.call("add_comment", {"ticket_id": "T-1001", "body": "looking"})
        assert again.payload["result"].get("deduplicated") is True
    assert crm_state()["tickets"]["T-1001"]["comments"] == ["looking"]


async def test_approve_executes_once_and_verifies(demo_config):
    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        human = build_governor(demo_config, ds, role=APPROVER, identity="alice")
        held = await agent.call("update_ticket", {"ticket_id": "T-1001", "priority": "P1", "status": "in_progress"})
        aid = held.payload["approval_id"]

        done = await human.approve(aid, note="sev1")
        assert not done.is_error and done.payload["result"]["verified"] is True
        assert (await human.approve(aid)).payload["status"] == "not_claimed"

        # The agent's retry of the same step returns the stored result.
        retry = await agent.call("update_ticket", {"ticket_id": "T-1001", "priority": "P1", "status": "in_progress"})
        assert retry.payload["status"] == "done" and retry.payload["approval_id"] == aid

    s = crm_state()
    assert s["tickets"]["T-1001"]["priority"] == "P1"
    assert [w["tool"] for w in s["write_log"]] == ["update_ticket"]
    assert agent.audit.verify().ok


async def test_verification_catches_an_acknowledged_but_dropped_write(demo_config, monkeypatch):
    monkeypatch.setenv("CRM_DROP_WRITES", "update_ticket")
    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        human = build_governor(demo_config, ds, role=APPROVER, identity="alice")
        aid = (await agent.call("update_ticket", {"ticket_id": "T-1001", "status": "closed"})).payload["approval_id"]
        out = await human.approve(aid)
    assert out.is_error and out.payload["status"] == "failed"
    assert out.payload["result"]["status"] == "verify_failed"
    assert agent.store.get(aid).status == "failed"


async def test_agent_cannot_approve_or_smuggle_overrides(demo_config):
    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        aid = (await agent.call("update_ticket", {"ticket_id": "T-1002", "status": "closed"})).payload["approval_id"]
        assert (await agent.call("approve", {"approval_id": aid})).is_error
        assert (await agent.approve(aid)).is_error
        smuggled = await agent.call("update_ticket", {"ticket_id": "T-1002", "status": "closed", "bypass_approval": True})
        assert smuggled.is_error and "no override" in smuggled.payload["reason"]
        assert agent.store.get(aid).status == "pending"
    events = [r["event"] for r in agent.audit.rows()]
    assert "governance.denied" in events and events.count("call.denied") == 1


async def test_argument_rule_denies_large_refund_even_after_approval_request(demo_config):
    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        big = await agent.call("issue_refund", {"customer_id": "C-200", "amount": 5000, "reason": "dup"})
        small = await agent.call("issue_refund", {"customer_id": "C-200", "amount": 85, "reason": "dup"})
    assert big.is_error and big.payload["status"] == "denied"
    assert small.payload["status"] == "pending_approval"


async def test_tampered_stored_arguments_are_not_executed(demo_config):
    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        human = build_governor(demo_config, ds, role=APPROVER, identity="alice")
        aid = (await agent.call("issue_refund", {"customer_id": "C-200", "amount": 85, "reason": "dup"})).payload[
            "approval_id"
        ]
        agent.store.conn.execute(
            "UPDATE approvals SET args = ? WHERE id = ?",
            (json.dumps({"customer_id": "C-200", "amount": 450, "reason": "dup"}), aid),
        )
        out = await human.approve(aid)
    assert out.is_error and "digest" in out.payload["reason"]
    assert crm_state()["customers"]["C-200"]["balance"] == 85.0


async def test_policy_tightened_after_request_wins_at_execution(demo_config):
    from governed_mcp.policy import Policy

    async with Client(crm) as ds:
        agent = build_governor(demo_config, ds)
        aid = (await agent.call("update_ticket", {"ticket_id": "T-1003", "status": "closed"})).payload["approval_id"]
        human = build_governor(demo_config, ds, role=APPROVER, identity="alice")
        human.policy = Policy.from_dict({"rules": [{"tool": "update_ticket", "action": "deny"}]})
        out = await human.approve(aid)
    assert out.is_error and "policy now denies" in out.payload["reason"]
    assert crm_state()["tickets"]["T-1003"]["status"] == "open"
