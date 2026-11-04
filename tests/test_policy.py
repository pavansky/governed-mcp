import pytest

from governed_mcp.policy import Action, Policy, PolicyError, fill

POLICY = Policy.from_dict(
    {
        "default": "require_approval",
        "rules": [
            {"tool": "get_*", "action": "allow"},
            {"tool": "delete_*", "action": "deny"},
            {"tool": "issue_refund", "action": "require_approval"},
            {"tool": "issue_refund", "when": {"path": "amount", "gt": 500}, "action": "deny"},
            {"tool": "close_tickets", "action": "allow"},
            {"tool": "close_tickets", "when": {"path": "ids", "count_gt": 3}, "action": "require_approval"},
            {"tool": "deploy", "when": {"path": "target.host", "matches": "^prod-"}, "action": "deny"},
            {"tool": "deploy", "action": "allow"},
        ],
    }
)


def act(tool, args=None, **kw):
    return POLICY.decide(tool, args or {}, **kw).action


def test_glob_rules():
    assert act("get_ticket") is Action.ALLOW
    assert act("delete_customer") is Action.DENY


def test_default_and_read_default():
    assert act("unknown_write") is Action.REQUIRE_APPROVAL
    assert act("unknown_read", read_only=True) is Action.ALLOW


def test_strictest_matching_rule_wins_regardless_of_order():
    assert act("issue_refund", {"amount": 100}) is Action.REQUIRE_APPROVAL
    assert act("issue_refund", {"amount": 501}) is Action.DENY
    reordered = Policy.from_dict({"rules": list(reversed([
        {"tool": "issue_refund", "action": "require_approval"},
        {"tool": "issue_refund", "when": {"path": "amount", "gt": 500}, "action": "deny"},
    ]))})
    assert reordered.decide("issue_refund", {"amount": 501}).action is Action.DENY


def test_count_and_nested_regex_conditions():
    assert act("close_tickets", {"ids": [1, 2, 3]}) is Action.ALLOW
    assert act("close_tickets", {"ids": [1, 2, 3, 4]}) is Action.REQUIRE_APPROVAL
    assert act("deploy", {"target": {"host": "prod-eu-1"}}) is Action.DENY
    assert act("deploy", {"target": {"host": "staging-1"}}) is Action.ALLOW


def test_uncomparable_value_does_not_satisfy_condition():
    # "lots" cannot be compared to 500: the deny rule does not apply, the
    # unconditional require_approval still does. Nothing falls open to allow.
    assert act("issue_refund", {"amount": "lots"}) is Action.REQUIRE_APPROVAL


def test_rule_tightens_only_never_opens_a_default():
    p = Policy.from_dict({"default": "deny", "rules": [{"tool": "x", "when": {"path": "a", "equals": 1}, "action": "require_approval"}]})
    assert p.decide("x", {"a": 2}).action is Action.DENY
    assert p.decide("x", {"a": 1}).action is Action.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "raw",
    [
        {"rules": [{"tool": "x"}]},
        {"rules": [{"tool": "x", "action": "maybe"}]},
        {"rules": [{"tool": "x", "action": "deny", "when": {"path": "a", "bogus": 1}}]},
        {"rules": [{"tool": "x", "action": "deny", "when": {"equals": 1}}]},
    ],
)
def test_invalid_policies_are_rejected(raw):
    with pytest.raises(PolicyError):
        Policy.from_dict(raw)


def test_fill_templates_keep_types():
    assert fill({"id": "{ticket_id}", "n": "{count}", "lit": "x"}, {"ticket_id": "T-1", "count": 3}) == {
        "id": "T-1",
        "n": 3,
        "lit": "x",
    }
    assert fill("{missing}", {}) is None
