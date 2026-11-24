import copy

import pytest

from rules_agent.llm import LLMError, OpenAICompatibleProvider, parse_rule, provider_from_env
from rules_agent.schema import validate_spec

VALID = {
    "trigger": {"type": "deposit", "tag": "salary"},
    "actions": [{"from": "checking", "to": "savings", "amount": {"mode": "percent", "percent": 20}}],
}


def test_valid_spec_has_no_errors():
    assert validate_spec(VALID) == []


def test_unknown_top_level_key_is_rejected():
    spec = copy.deepcopy(VALID)
    spec["notes"] = "guessed"
    assert any("notes" in e or "Additional" in e for e in validate_spec(spec))


@pytest.mark.parametrize("percent", [0, 101, -5])
def test_percent_bounds_are_enforced(percent):
    spec = copy.deepcopy(VALID)
    spec["actions"][0]["amount"]["percent"] = percent
    assert validate_spec(spec)


def test_percentage_on_monthly_trigger_is_semantic_error():
    spec = {
        "trigger": {"type": "monthly", "day_of_month": 1},
        "actions": [{"from": "checking", "to": "savings", "amount": {"mode": "percent", "percent": 10}}],
    }
    assert any("deposit trigger" in e for e in validate_spec(spec))


def test_shortfall_must_refill_triggering_account():
    spec = {
        "trigger": {"type": "balance_below", "account": "checking", "threshold_gbp": 200},
        "actions": [{"from": "savings", "to": "holiday_pot", "amount": {"mode": "shortfall"}}],
    }
    assert any("refill the triggering account" in e for e in validate_spec(spec))


def test_same_source_and_destination_is_rejected():
    spec = copy.deepcopy(VALID)
    spec["actions"][0]["to"] = "checking"
    assert any("both 'checking'" in e for e in validate_spec(spec))


class FakeTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def __call__(self, url, body, credential):
        self.calls.append((url, body))
        return {"choices": [{"message": {"content": self.content}}]}


def _provider(content):
    return OpenAICompatibleProvider("https://llm.example.invalid/v1", "test-key", "test-model",
                                    transport=FakeTransport(content))


def test_llm_output_that_passes_validation_is_accepted():
    provider = _provider('{"trigger": {"type": "monthly", "day_of_month": 1}, '
                         '"actions": [{"from": "checking", "to": "rent", "amount": {"mode": "fixed", "gbp": 850}}]}')
    result = parse_rule("Pay rent monthly from checking", provider=provider)
    assert result.status == "ok"
    assert result.source == "llm"
    assert provider.transport.calls[0][0].endswith("/chat/completions")


def test_llm_output_failing_validation_is_rejected():
    provider = _provider('{"trigger": {"type": "monthly", "day_of_month": 31}, "actions": []}')
    result = parse_rule("Pay rent monthly from checking", provider=provider)
    assert result.status == "reject"
    assert "LLM output failed validation" in result.messages[0]


def test_llm_clarify_is_passed_through():
    provider = _provider('{"clarify": "Which account should pay the rent?"}')
    result = parse_rule("Pay rent monthly", provider=provider)
    assert result.status == "clarify"
    assert result.question == "Which account should pay the rent?"


def test_non_json_llm_output_is_rejected_cleanly():
    provider = _provider("sure! here is your rule")
    result = parse_rule("Pay rent monthly", provider=provider)
    assert result.status == "reject"
    assert "LLM unavailable" in result.messages[0]


def test_llm_is_not_consulted_when_deterministic_parser_succeeds():
    provider = _provider('{"should": "not be used"}')
    result = parse_rule("Pay £850 to landlord from checking on the 1st", provider=provider)
    assert result.status == "ok"
    assert provider.transport.calls == []


def test_provider_from_env_is_off_by_default_and_fails_loudly_when_misconfigured():
    assert provider_from_env({}) is None
    assert provider_from_env({"RULES_LLM_PROVIDER": "none"}) is None
    with pytest.raises(LLMError, match="RULES_LLM_KEY"):
        provider_from_env({"RULES_LLM_PROVIDER": "openai_compatible", "RULES_LLM_BASE_URL": "https://x.invalid/v1",
                           "RULES_LLM_MODEL": "m"})
