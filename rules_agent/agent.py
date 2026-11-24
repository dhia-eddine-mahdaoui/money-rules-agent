"""Rule-understanding agent: guard, deterministic grammar, model proposal, validation.

    text -> guard -> grammar --ok/clarify/invalid--> result
                        | unsupported phrasing
                        v
                  model (offline extractor by default, optional LLM provider)
                        |  proposal is untrusted
                        v
          JSON Schema + semantic validation + grounding check -> result

Every stage appends to a trace that is stored with the rule, so a reviewer can see which stage
produced the spec and why other stages declined. The agent never returns "ok" for a spec that
failed validation, and never keeps a number that the rule text does not contain.
"""

from __future__ import annotations

import re

from . import offline_model
from .llm import LLMError, Provider
from .parser import ParseResult, parse_rule as parse_grammar
from .schema import validate_spec

MAX_TEXT_LEN = 300
INJECTION = re.compile(
    r"ignore (?:all |any |the |your )?(?:previous|prior|above|earlier)|disregard|system prompt|you are now|new instructions|"
    r"override|jailbreak|developer mode|pretend|act as|reveal|pass(?:word|phrase|code)|\bapi[ _-]?key\b|\btoken\b|\bsudo\b|"
    r"<\s*script|drop\s+table|\bselect\b.+\bfrom\b|\{\{|\$\(|`|https?://|www\.|\S+@\S+\.\S+",
    re.IGNORECASE,
)
LONG_NUMBER = re.compile(r"\d[\d \-]{7,}\d")  # account, card or phone numbers


def _step(trace: list, stage: str, outcome: str, detail: str = "") -> None:
    trace.append({"stage": stage, "outcome": outcome, "detail": detail})


def guard(text: str) -> str | None:
    """Return a rejection reason for input that must never reach a parser or a model, else None."""
    if not text or not text.strip():
        return "Rule text is empty."
    if len(text) > MAX_TEXT_LEN:
        return f"Rule text is longer than {MAX_TEXT_LEN} characters; split it into separate rules."
    if any(ord(c) < 32 for c in text):
        return "Rule text contains control characters."
    if INJECTION.search(text):
        return "Rule text contains instructions, links or credentials-like words aimed at the assistant; rules may only describe money routing."
    if LONG_NUMBER.search(text):
        return "Account, card or phone numbers are not accepted; rules use your named accounts or a plain payee name."
    return None


def grounded(spec: dict, text: str) -> list[str]:
    """Every number in the spec must be stated in the rule text (never invent an amount)."""
    allowed = offline_model.numbers_in_text(text)
    values: list[tuple[str, float]] = []
    trigger = spec["trigger"]
    if trigger["type"] == "balance_below":
        values.append(("threshold", trigger["threshold_gbp"]))
    elif trigger["type"] == "monthly":
        values.append(("day", float(trigger["day_of_month"])))
    for action in spec["actions"]:
        amount = action["amount"]
        if amount["mode"] == "percent":
            values.append(("percent", amount["percent"]))
        elif amount["mode"] == "fixed":
            values.append(("amount", amount["gbp"]))
    return [f"{label} {value:g} does not appear in the rule text" for label, value in values
            if not any(abs(value - a) < 1e-9 for a in allowed)]


def _interpret(output: dict) -> tuple[str, dict | str | list]:
    if not isinstance(output, dict):
        raise LLMError("model output is not an object")
    keys = set(output)
    if keys == {"clarify"}:
        return "clarify", str(output["clarify"])
    if keys == {"reject"}:
        reasons = output["reject"]
        return "reject", [str(r) for r in (reasons if isinstance(reasons, list) else [reasons])]
    if keys == {"spec"}:
        return "spec", output["spec"]
    if keys == {"trigger", "actions"}:
        return "spec", output
    raise LLMError(f"unexpected keys in model output: {sorted(keys)}")


class RuleAgent:
    """`provider` is optional; without one the offline model is used."""

    def __init__(self, provider: Provider | None = None, use_model: bool = True):
        self.provider = provider
        self.offline = offline_model.OfflineModel()
        self.use_model = use_model

    # -- model stage -------------------------------------------------------
    def _ask(self, model, text: str, trace: list):
        try:
            kind, payload = _interpret(model.propose(text))
        except (LLMError, OSError, ValueError) as exc:
            _step(trace, model.name, "unavailable", str(exc))
            return None
        _step(trace, model.name, {"spec": "proposed spec", "clarify": "asked a question", "reject": "declined"}[kind],
              payload if isinstance(payload, str) else "; ".join(payload) if kind == "reject" else "")
        return kind, payload

    def _check(self, spec, text: str, trace: list, model_name: str) -> str | None:
        errors = validate_spec(spec)
        if errors:
            _step(trace, "validation", "failed", "; ".join(errors))
            return "; ".join(errors)
        ungrounded = grounded(spec, text)
        if ungrounded:
            _step(trace, "grounding", "failed", "; ".join(ungrounded))
            return "; ".join(ungrounded)
        _step(trace, "validation", "passed", "JSON Schema, semantic checks and grounding")
        return None

    def parse(self, text: str) -> ParseResult:
        trace: list = []
        reason = guard(text)
        if reason:
            _step(trace, "guard", "rejected", reason)
            return ParseResult("reject", messages=[reason], source="guard", code="invalid", trace=trace)
        _step(trace, "guard", "passed")

        result = parse_grammar(text)
        if result.code != "unsupported":
            _step(trace, "grammar", {"ok": "matched", "clarify": "asked a question", "reject": "rejected"}[result.status],
                  result.question or "; ".join(result.messages))
            result.trace = trace
            return result
        _step(trace, "grammar", "phrasing not covered", "; ".join(result.messages))
        if not self.use_model:
            result.trace = trace
            return result

        models = [self.provider, self.offline] if self.provider is not None else [self.offline]
        answers = []
        for model in models:
            answer = self._ask(model, text, trace)
            if answer is not None:
                answers.append((model, answer))
            if model is self.provider and answer is not None and answer[0] == "spec":
                break  # offline model is only consulted as a cross-check below
        if not answers:
            return ParseResult("reject", messages=["No model was available to interpret this rule."], source="agent",
                               code="unsupported", trace=trace)

        model, (kind, payload) = answers[0]
        if kind == "reject":
            return ParseResult("reject", messages=list(payload), source=model.name, code="invalid", trace=trace)
        if kind == "clarify":
            return ParseResult("clarify", question=payload, source=model.name, trace=trace)

        problem = self._check(payload, text, trace, model.name)
        if problem:
            return ParseResult("reject", messages=[f"{model.name} proposal failed validation: {problem}"], source=model.name,
                               code="invalid", trace=trace)
        if model is self.provider:
            cross = self._ask(self.offline, text, trace)
            if cross and cross[0] == "spec" and validate_spec(cross[1]) == [] and cross[1] != payload:
                _step(trace, "agreement", "disagree", "the LLM and the offline model produced different specs")
                return ParseResult(
                    "clarify",
                    question="This rule can be read in more than one way. Please rephrase it using explicit amounts and accounts, "
                             "for example 'move 20% to savings'.",
                    source="agreement_check", trace=trace)
            if cross and cross[0] == "spec":
                _step(trace, "agreement", "agree", "both models produced the same spec")
        return ParseResult("ok", spec=payload, source=model.name, trace=trace)


def parse_rule(text: str, provider: Provider | None = None, use_model: bool = True) -> ParseResult:
    return RuleAgent(provider, use_model).parse(text)
