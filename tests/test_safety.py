from rules_agent.safety import Limits, approval_reasons, check_transfer, static_conflicts


def _deposit_rule(rid, percent, to="savings"):
    return {"id": rid, "spec": {
        "trigger": {"type": "deposit", "tag": "salary"},
        "actions": [{"from": "checking", "to": to, "amount": {"mode": "percent", "percent": percent}}],
    }}


def _top_up_rule(rid, account, source):
    return {"id": rid, "spec": {
        "trigger": {"type": "balance_below", "account": account, "threshold_gbp": 200},
        "actions": [{"from": source, "to": account, "amount": {"mode": "shortfall"}}],
    }}


def test_percentages_on_same_deposit_source_over_100_are_an_error():
    conflicts = static_conflicts([_deposit_rule("R1", 60), _deposit_rule("R2", 50, to="holiday_pot")])
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "over_allocation"
    assert conflicts[0]["severity"] == "error"
    assert conflicts[0]["rule_ids"] == ["R1", "R2"]


def test_percentages_within_100_are_not_flagged():
    assert static_conflicts([_deposit_rule("R1", 60), _deposit_rule("R2", 40, to="holiday_pot")]) == []


def test_ping_pong_between_two_balance_rules_is_an_error_loop():
    rules = [
        _top_up_rule("R1", "checking", "savings"),
        {"id": "R2", "spec": {
            "trigger": {"type": "balance_below", "account": "savings", "threshold_gbp": 100},
            "actions": [{"from": "checking", "to": "savings", "amount": {"mode": "shortfall"}}],
        }},
    ]
    loops = [c for c in static_conflicts(rules) if c["kind"] == "loop"]
    assert loops and loops[0]["severity"] == "error"
    assert set(loops[0]["rule_ids"]) == {"R1", "R2"}


def test_loop_through_a_deposit_rule_is_only_a_warning():
    rules = [
        _deposit_rule("R1", 20),  # checking -> savings
        _top_up_rule("R2", "checking", "savings"),  # savings -> checking
    ]
    loops = [c for c in static_conflicts(rules) if c["kind"] == "loop"]
    assert loops and loops[0]["severity"] == "warning"


def test_overdraft_is_blocked():
    transfer = {"amount_gbp": 500.0, "from": "checking", "to": "savings"}
    violations = check_transfer(transfer, {"checking": 300.0}, Limits())
    assert [v["code"] for v in violations] == ["overdraft"]


def test_per_transfer_limit_is_enforced():
    transfer = {"amount_gbp": 1500.0, "from": "savings", "to": "checking"}
    violations = check_transfer(transfer, {"savings": 9000.0}, Limits(max_per_transfer_gbp=1000))
    assert [v["code"] for v in violations] == ["max_per_transfer"]


def test_approval_needed_for_unapproved_rule_or_large_amount():
    limits = Limits(approval_threshold_gbp=250)
    small = {"amount_gbp": 100.0}
    large = {"amount_gbp": 480.0}
    assert approval_reasons(small, rule_is_approved=True, limits=limits) == []
    assert approval_reasons(small, rule_is_approved=False, limits=limits)
    assert any("threshold" in r for r in approval_reasons(large, rule_is_approved=True, limits=limits))
