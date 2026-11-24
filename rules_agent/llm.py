"""Optional LLM backend behind a small interface.

The LLM is only consulted when the deterministic parser rejects a rule outright. Its output is
treated as untrusted: it must pass the same JSON Schema and semantic validation as the
deterministic parser. Clarifying questions are never guessed by the model.

Configuration (environment variables, no keys are stored in the repository):

    RULES_LLM_PROVIDER=openai_compatible     (default: none)
    RULES_LLM_BASE_URL=https://<host>/v1
    RULES_LLM_KEY=<credential, read from the environment only>
    RULES_LLM_MODEL=<model id>
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

from .parser import ParseResult, parse_rule as parse_deterministic
from .schema import RULE_SPEC_SCHEMA, validate_spec

SYSTEM_PROMPT = (
    "You convert one personal-finance routing rule into a JSON object that matches the JSON Schema below. "
    "Output only the JSON object. If the rule is ambiguous or unsupported, output "
    '{"clarify": "<one short question>"} instead. Never invent amounts or accounts.\n\nSchema:\n'
    + json.dumps(RULE_SPEC_SCHEMA)
)

Transport = Callable[[str, dict, str], dict]


class LLMError(Exception):
    """Raised when the provider is misconfigured or returns unusable output."""


def _http_post(url: str, body: dict, credential: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, base_url: str, credential: str, model: str, transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.model = model
        self.transport = transport or _http_post

    def propose(self, text: str) -> dict:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        response = self.transport(f"{self.base_url}/chat/completions", body, self.credential)
        try:
            output = json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("provider returned output that is not a JSON object") from exc
        if not isinstance(output, dict):
            raise LLMError("provider returned a JSON value that is not an object")
        return output


def provider_from_env(env=None) -> OpenAICompatibleProvider | None:
    env = os.environ if env is None else env
    kind = env.get("RULES_LLM_PROVIDER", "none").strip().lower()
    if kind in ("", "none"):
        return None
    if kind == "openai_compatible":
        required = ("RULES_LLM_BASE_URL", "RULES_LLM_KEY", "RULES_LLM_MODEL")
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise LLMError("missing environment variables: " + ", ".join(missing))
        return OpenAICompatibleProvider(env["RULES_LLM_BASE_URL"], env["RULES_LLM_KEY"], env["RULES_LLM_MODEL"])
    raise LLMError(f"unknown RULES_LLM_PROVIDER '{kind}'")


def parse_rule(text: str, provider: OpenAICompatibleProvider | None = None) -> ParseResult:
    result = parse_deterministic(text)
    if result.status != "reject" or provider is None:
        return result
    try:
        proposal = provider.propose(text)
    except (LLMError, OSError, ValueError) as exc:
        return ParseResult("reject", messages=[f"Deterministic parser rejected the rule; LLM unavailable: {exc}"],
                           source="llm")
    if set(proposal) == {"clarify"}:
        return ParseResult("clarify", question=str(proposal["clarify"]), source="llm")
    errors = validate_spec(proposal)
    if errors:
        return ParseResult("reject", messages=["LLM output failed validation:"] + errors, source="llm")
    return ParseResult("ok", spec=proposal, source="llm")
