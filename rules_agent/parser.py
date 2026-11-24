"""Deterministic parser for a documented subset of plain-English rules.

Supported forms (case-insensitive, trailing full stop optional):

    When my <tag> arrives, <actions>
    If [my] <account> drops|falls|goes below £<amount>, <actions>
    On the <day>st|nd|rd|th [of each month], <actions>      (or <actions> on the <day>st)

Actions, joined with " and " or ";":

    move <n>% [from <account>] to <account>
    move £<amount> [from <account>] to <account>
    top it up from <account>                 (only with an "if ... drops below" trigger)
    pay £<amount> [to] <payee> from <account>

Accounts: checking, savings, holiday pot. A deposit trigger lands in checking, so it is the
default source for "move" actions. Anything the parser does not recognise is rejected, and
missing information produces a clarifying question instead of a guessed value.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .offline_model import SALARY_WORDS, TAG_ADJECTIVES
from .schema import INTERNAL_ACCOUNTS, MAX_ACTIONS, validate_spec

ACCOUNT_ALIASES = {"checking": "checking", "savings": "savings", "holiday pot": "holiday_pot"}
DEPOSIT_LANDING_ACCOUNT = "checking"

RE_DEPOSIT = re.compile(r"^when my (?P<tag>[a-z][a-z ]*?) (?:arrives|lands|comes in)$")
RE_BALANCE = re.compile(r"^if (?:my )?(?P<acc>[a-z][a-z ]*?) (?:drops|falls|goes) below £(?P<th>\d+(?:\.\d{1,2})?)$")
RE_MONTHLY = re.compile(r"^on (?:the )?(?P<day>\d{1,2})(?:st|nd|rd|th)?(?: of (?:each |every )?month)?$")
RE_MONTHLY_SUFFIX = re.compile(
    r"^(?P<actions>.+?)\s+on (?:the )?(?P<day>\d{1,2})(?:st|nd|rd|th)?(?: of (?:each |every )?month)?$"
)
RE_PCT_MOVE = re.compile(r"^move (?P<pct>\d+(?:\.\d+)?)% (?:from (?P<src>[a-z][a-z ]*?) )?to (?P<dst>[a-z][a-z ]*?)$")
RE_FIXED_MOVE = re.compile(
    r"^move £(?P<amt>\d+(?:\.\d{1,2})?) (?:from (?P<src>[a-z][a-z ]*?) )?to (?P<dst>[a-z][a-z ]*?)$"
)
RE_TOP_UP = re.compile(r"^top (?:it )?up from (?P<src>[a-z][a-z ]*?)$")
RE_PAY = re.compile(
    r"^pay £(?P<amt>\d+(?:\.\d{1,2})?) (?:(?:to|for) )?(?P<payee>[a-z][a-z ]*?) from (?P<src>[a-z][a-z ]*?)$"
)


@dataclass
class ParseResult:
    status: str  # "ok" | "clarify" | "reject"
    spec: dict | None = None
    messages: list = field(default_factory=list)
    question: str | None = None
    source: str = "grammar"
    code: str = ""  # reject only: "unsupported" (phrasing not understood, a model may try) or "invalid" (final)
    trace: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _reject(messages: list[str], code: str | None = None) -> ParseResult:
    if code is None:
        unsupported = ("Unknown account", "Could not understand", "No trigger found")
        code = "unsupported" if any(m.startswith(unsupported) for m in messages) else "invalid"
    return ParseResult("reject", messages=messages, code=code)


# Questions asked when a clause starts like an action but does not match any pattern. The grammar
# cannot tell what is missing, so the model stage gets a chance to read the sentence.
FALLTHROUGH_QUESTIONS = (
    "How much should move, and to which account? For example 'move 10% to savings' or 'move £100 to savings'.",
    "Pay how much, and from which account? For example 'pay £850 to landlord from checking'.",
    "Top up from which account? For example 'top it up from savings'.",
)


def _clarify(question: str) -> ParseResult:
    return ParseResult("clarify", question=question, code="unsupported" if question in FALLTHROUGH_QUESTIONS else "")


def normalize(text: str) -> str:
    t = " ".join(text.strip().lower().split()).rstrip(".!")
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\bthe (?=[a-z])", "", t)
    return t


def parse_rule(text: str) -> ParseResult:
    t = normalize(text)
    if not t:
        return _reject(["Rule text is empty."])
    for trigger_text, actions_text in _candidate_splits(t):
        trigger, error = _parse_trigger(trigger_text)
        if trigger is None and error is None:
            continue
        if error is not None:
            return _reject([error])
        return _parse_actions(trigger, actions_text)
    return _reject(
        [
            "No trigger found. Start the rule with 'When my <tag> arrives,' or "
            "'If <account> drops below £<amount>,' or end it with 'on the <day>st'."
        ]
    )


def _candidate_splits(t: str) -> list[tuple[str, str]]:
    candidates = []
    if "," in t:
        head, tail = t.split(",", 1)
        candidates.append((head.strip(), tail.strip()))
    match = RE_MONTHLY_SUFFIX.match(t)
    if match:
        candidates.append((f"on {match.group('day')}", match.group("actions").rstrip(", ").strip()))
    return candidates


def _account(name: str) -> str | None:
    return ACCOUNT_ALIASES.get(" ".join(name.split()))


def _unknown_account(name: str) -> str:
    return f"Unknown account '{name.strip()}'. Known accounts: checking, savings, holiday pot."


def _parse_trigger(text: str) -> tuple[dict | None, str | None]:
    """Returns (trigger, None) on success, (None, error) when matched but invalid, (None, None) when not matched."""
    match = RE_DEPOSIT.match(text)
    if match:
        words = [w for w in match.group("tag").split() if w not in TAG_ADJECTIVES]
        tag = "salary" if words and all(w in SALARY_WORDS for w in words) else "_".join(words)
        if not re.fullmatch(r"[a-z_]{3,20}", tag):
            return None, f"Deposit tag '{tag}' must be 3-20 letters, for example 'salary'."
        return {"type": "deposit", "tag": tag}, None
    match = RE_BALANCE.match(text)
    if match:
        account = _account(match.group("acc"))
        if account is None:
            return None, _unknown_account(match.group("acc"))
        return {"type": "balance_below", "account": account, "threshold_gbp": float(match.group("th"))}, None
    match = RE_MONTHLY.match(text)
    if match:
        day = int(match.group("day"))
        if not 1 <= day <= 28:
            return None, f"Day {day} is not supported; use a day from 1 to 28."
        return {"type": "monthly", "day_of_month": day}, None
    return None, None


def _parse_actions(trigger: dict, actions_text: str) -> ParseResult:
    parts = [p.strip() for p in re.split(r";| and ", actions_text) if p.strip()]
    if not parts:
        return _clarify("What should happen when this triggers? For example 'move 10% to savings'.")
    if len(parts) > MAX_ACTIONS:
        return _reject([f"A rule can have at most {MAX_ACTIONS} actions; this one has {len(parts)}."])
    actions, messages, question = [], [], None
    for part in parts:
        if re.match(r"^(?:£|\d)", part):
            part = "move " + part  # "... and £300 to holiday pot" reuses the verb
        action, error, asked = _parse_action(part, trigger)
        if asked and question is None:
            question = asked
        if error:
            messages.append(error)
        if action:
            actions.append(action)
    if messages:
        return _reject(messages)
    if question:
        return _clarify(question)
    spec = {"trigger": trigger, "actions": actions}
    errors = validate_spec(spec)
    if errors:
        return _reject(errors)
    return ParseResult("ok", spec=spec)


def _source(src_text: str | None, trigger: dict) -> tuple[str | None, str | None, str | None]:
    """Returns (account, error, question)."""
    if src_text:
        account = _account(src_text)
        return (account, None, None) if account else (None, _unknown_account(src_text), None)
    if trigger["type"] == "deposit":
        return DEPOSIT_LANDING_ACCOUNT, None, None
    return None, None, "Which account should the money come from? For example 'move £100 from savings to checking'."


def _parse_action(part: str, trigger: dict):
    """Returns (action, error, question). Exactly one of them is normally set."""
    match = RE_PCT_MOVE.match(part)
    if match:
        if trigger["type"] != "deposit":
            return None, "Percentage amounts need a deposit trigger (a percentage of what arrived); use a fixed £ amount instead.", None
        dst = _account(match.group("dst"))
        if dst is None:
            return None, _unknown_account(match.group("dst")), None
        src, error, asked = _source(match.group("src"), trigger)
        if error or asked:
            return None, error, asked
        return {"from": src, "to": dst, "amount": {"mode": "percent", "percent": float(match.group("pct"))}}, None, None

    match = RE_FIXED_MOVE.match(part)
    if match:
        dst = _account(match.group("dst"))
        if dst is None:
            return None, _unknown_account(match.group("dst")), None
        src, error, asked = _source(match.group("src"), trigger)
        if error or asked:
            return None, error, asked
        return {"from": src, "to": dst, "amount": {"mode": "fixed", "gbp": float(match.group("amt"))}}, None, None

    match = RE_TOP_UP.match(part)
    if match:
        if trigger["type"] != "balance_below":
            return None, "'Top it up' needs an 'If <account> drops below £<amount>' trigger.", None
        src = _account(match.group("src"))
        if src is None:
            return None, _unknown_account(match.group("src")), None
        return {"from": src, "to": trigger["account"], "amount": {"mode": "shortfall"}}, None, None

    match = RE_PAY.match(part)
    if match:
        payee_name = " ".join(match.group("payee").split())
        if _account(payee_name):
            return None, "Moves between your own accounts use 'move', for example 'move £100 to savings'.", None
        payee = payee_name.replace(" ", "_")
        if not re.fullmatch(r"[a-z][a-z_]{1,29}", payee):
            return None, f"Payee name '{payee_name}' is not usable; use letters and spaces.", None
        src = _account(match.group("src"))
        if src is None:
            return None, _unknown_account(match.group("src")), None
        return {"from": src, "to": payee, "amount": {"mode": "fixed", "gbp": float(match.group("amt"))}}, None, None

    if part.startswith("move "):
        return None, None, "How much should move, and to which account? For example 'move 10% to savings' or 'move £100 to savings'."
    if part.startswith("pay "):
        return None, None, "Pay how much, and from which account? For example 'pay £850 to landlord from checking'."
    if part.startswith("top"):
        return None, None, "Top up from which account? For example 'top it up from savings'."
    return None, (
        f"Could not understand action '{part}'. Supported: 'move 20% to savings', 'move £300 to holiday pot', "
        "'top it up from savings', 'pay £850 to landlord from checking'."
    ), None
