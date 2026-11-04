from governed_mcp.stepkeys import step_key


def test_same_step_same_key_regardless_of_arg_order():
    a = step_key("run1", "update_ticket", {"ticket_id": "T-1", "priority": "P1"})
    b = step_key("run1", "update_ticket", {"priority": "P1", "ticket_id": "T-1"})
    assert a == b and a.startswith("gk_")


def test_any_component_changes_the_key():
    base = step_key("run1", "update_ticket", {"ticket_id": "T-1"}, step="3")
    assert base != step_key("run2", "update_ticket", {"ticket_id": "T-1"}, step="3")
    assert base != step_key("run1", "add_comment", {"ticket_id": "T-1"}, step="3")
    assert base != step_key("run1", "update_ticket", {"ticket_id": "T-2"}, step="3")
    assert base != step_key("run1", "update_ticket", {"ticket_id": "T-1"}, step="4")


def test_client_supplied_idempotency_key_does_not_perturb_the_derived_one():
    plain = step_key("run1", "issue_refund", {"amount": 10})
    with_key = step_key("run1", "issue_refund", {"amount": 10, "idempotency_key": "made-up-by-model"})
    assert plain == with_key
