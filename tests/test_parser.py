from rules_agent.parser import parse_rule


def test_salary_rule_becomes_typed_spec():
    result = parse_rule("When my salary arrives, move 20% to savings and £300 to the holiday pot")
    assert result.status == "ok"
    assert result.spec == {
        "trigger": {"type": "deposit", "tag": "salary"},
        "actions": [
            {"from": "checking", "to": "savings", "amount": {"mode": "percent", "percent": 20.0}},
            {"from": "checking", "to": "holiday_pot", "amount": {"mode": "fixed", "gbp": 300.0}},
        ],
    }


def test_balance_trigger_top_up_refills_triggering_account():
    result = parse_rule("If checking drops below £200, top it up from savings")
    assert result.status == "ok"
    assert result.spec["trigger"] == {"type": "balance_below", "account": "checking", "threshold_gbp": 200.0}
    assert result.spec["actions"] == [{"from": "savings", "to": "checking", "amount": {"mode": "shortfall"}}]


def test_monthly_payment_with_amount_and_source_is_accepted():
    result = parse_rule("Pay £850 to landlord from checking on the 1st")
    assert result.status == "ok"
    assert result.spec["trigger"] == {"type": "monthly", "day_of_month": 1}
    assert result.spec["actions"][0]["amount"] == {"mode": "fixed", "gbp": 850.0}
    assert result.spec["actions"][0]["to"] == "landlord"


def test_ambiguous_rent_rule_gets_a_clarifying_question_not_a_guess():
    result = parse_rule("Pay rent on the 1st")
    assert result.status == "clarify"
    assert result.spec is None
    assert "How much" not in result.question
    assert "Pay how much" in result.question


def test_vague_amount_asks_for_clarification():
    result = parse_rule("When my salary arrives, move some money to savings")
    assert result.status == "clarify"
    assert "How much" in result.question


def test_unknown_account_is_rejected_with_reason():
    result = parse_rule("When my salary arrives, move 10% to current")
    assert result.status == "reject"
    assert any("Unknown account 'current'" in m for m in result.messages)


def test_rule_without_trigger_is_rejected():
    result = parse_rule("Move some money to savings when I get paid")
    assert result.status == "reject"
    assert "No trigger" in result.messages[0]


def test_percentages_over_100_in_one_rule_are_rejected():
    result = parse_rule("When my salary arrives, move 70% to savings and 40% to holiday pot")
    assert result.status == "reject"
    assert any("100%" in m for m in result.messages)


def test_day_outside_1_to_28_is_rejected():
    result = parse_rule("Pay £850 to landlord from checking on the 31st")
    assert result.status == "reject"
    assert "1 to 28" in result.messages[0]


def test_top_up_needs_a_balance_trigger():
    result = parse_rule("When my salary arrives, top it up from savings")
    assert result.status == "reject"
    assert "drops below" in result.messages[0]


def test_empty_rule_is_rejected():
    assert parse_rule("   ").status == "reject"
