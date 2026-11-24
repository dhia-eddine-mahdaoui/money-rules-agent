"""JSON Schema for typed rule specs, plus the semantic checks a schema cannot express."""

from __future__ import annotations

from jsonschema import Draft202012Validator

INTERNAL_ACCOUNTS = ("checking", "savings", "holiday_pot")
MAX_ACTIONS = 5
MAX_AMOUNT_GBP = 100_000

RULE_SPEC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "RuleSpec",
    "type": "object",
    "additionalProperties": False,
    "required": ["trigger", "actions"],
    "properties": {
        "trigger": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "tag"],
                    "properties": {
                        "type": {"const": "deposit"},
                        "tag": {"type": "string", "pattern": "^[a-z_]{3,20}$"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "account", "threshold_gbp"],
                    "properties": {
                        "type": {"const": "balance_below"},
                        "account": {"enum": list(INTERNAL_ACCOUNTS)},
                        "threshold_gbp": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_AMOUNT_GBP},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "day_of_month"],
                    "properties": {
                        "type": {"const": "monthly"},
                        "day_of_month": {"type": "integer", "minimum": 1, "maximum": 28},
                    },
                },
            ]
        },
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ACTIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from", "to", "amount"],
                "properties": {
                    "from": {"enum": list(INTERNAL_ACCOUNTS)},
                    "to": {"type": "string", "pattern": "^[a-z][a-z_]{1,29}$"},
                    "amount": {
                        "oneOf": [
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["mode", "percent"],
                                "properties": {
                                    "mode": {"const": "percent"},
                                    "percent": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                                },
                            },
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["mode", "gbp"],
                                "properties": {
                                    "mode": {"const": "fixed"},
                                    "gbp": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_AMOUNT_GBP},
                                },
                            },
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["mode"],
                                "properties": {"mode": {"const": "shortfall"}},
                            },
                        ]
                    },
                },
            },
        },
    },
}

_VALIDATOR = Draft202012Validator(RULE_SPEC_SCHEMA)
Draft202012Validator.check_schema(RULE_SPEC_SCHEMA)


def validate_spec(spec) -> list[str]:
    """Return a list of human-readable errors; an empty list means the spec is valid."""
    schema_errors = sorted(_VALIDATOR.iter_errors(spec), key=lambda e: [str(p) for p in e.absolute_path])
    if schema_errors:
        return [f"{'/'.join(str(p) for p in e.absolute_path) or 'spec'}: {e.message}" for e in schema_errors]
    return _semantic_errors(spec)


def _semantic_errors(spec: dict) -> list[str]:
    errors: list[str] = []
    trigger = spec["trigger"]
    kind = trigger["type"]
    total_percent = 0.0
    for i, action in enumerate(spec["actions"]):
        where = f"actions/{i}"
        mode = action["amount"]["mode"]
        if action["from"] == action["to"]:
            errors.append(f"{where}: source and destination are both '{action['from']}'")
        if mode == "percent":
            total_percent += action["amount"]["percent"]
            if kind != "deposit":
                errors.append(f"{where}: percentage amounts need a deposit trigger")
        if mode == "shortfall":
            if kind != "balance_below":
                errors.append(f"{where}: 'top it up' needs an 'if <account> drops below' trigger")
            elif action["to"] != trigger["account"]:
                errors.append(f"{where}: a top-up must refill the triggering account '{trigger['account']}'")
    if total_percent > 100 + 1e-9:
        errors.append(f"percentages in this rule add up to {total_percent:g}% (maximum is 100%)")
    return errors
