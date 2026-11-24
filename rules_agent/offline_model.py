"""Offline stand-in for an LLM: a small deterministic intent/slot extractor.

It plays the role of the "structured output" model in the agent pipeline and implements the same
provider interface as a real LLM backend (`propose(text) -> dict`). It understands many more
paraphrases than the strict grammar ("whenever I get paid, stash a fifth in my rainy day fund")
by normalising the text, locating one trigger, splitting the rest into clauses, and filling
slots (verb, amount, source, destination) per clause.

Design rules that keep it safe:

* fail closed: any word that is not consumed by a slot or in a short filler list rejects the
  whole rule, so trailing instructions cannot ride along;
* missing slots produce a clarifying question, never a guessed value;
* "everything", "all my money", unsupported schedules and currencies are rejected;
* its output is only a proposal: the agent still runs JSON Schema, semantic and grounding checks.

Output contract (same as the LLM): {"spec": {...}} | {"clarify": "..."} | {"reject": ["..."]}.
"""

from __future__ import annotations

import re

from .schema import MAX_ACTIONS

name = "offline_model"

# --------------------------------------------------------------------------- lexicon
ACCOUNT_PATTERNS = [
    ("holiday_pot", r"holidays?(?: pot| fund| account| savings| jar| money)?|vacation(?: pot| fund| savings| account)?|travel (?:pot|fund)"),
    ("savings", r"savings(?: account| pot| pocket)?|saving account|rainy day (?:fund|pot)|nest egg"),
    ("checking", r"checking(?: account)?|chequing(?: account)?|current account|main account|everyday account|spending account|day[- ]to[- ]day account"),
]
SALARY_WORDS = {"salary", "wage", "wages", "paycheck", "paycheque", "pay", "payroll", "income", "earnings", "pay check", "pay cheque"}
GENERIC_TAGS = {"money", "cash", "funds", "payment", "payments", "deposit", "deposits", "transfer", "anything", "something", "it", "paid"}
TAG_ADJECTIVES = {"monthly", "regular", "usual", "normal", "main", "next", "latest", "new", "weekly"}

ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty-first": 21, "twenty-second": 22, "twenty-third": 23, "twenty-fourth": 24,
    "twenty-fifth": 25, "twenty-sixth": 26, "twenty-seventh": 27, "twenty-eighth": 28,
    "twenty-ninth": 29, "thirtieth": 30, "thirty-first": 31,
}
_ORD_RE = "|".join(sorted((re.escape(k) for k in ORDINALS), key=len, reverse=True))

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_NUMW = "|".join(sorted(list(_UNITS) + list(_TENS), key=len, reverse=True))
_NUM_WORDS_RE = re.compile(
    rf"\b(?:(?:a|{_NUMW})(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?\s+(?:hundred|thousand|grand)"
    rf"|(?:{_NUMW})(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|grand)"
    rf"(?=\s*(?:pounds?|quid|gbp|sterling|percent|per cent|%))"
)

VERBS = (
    "moving|moved|move|shifting|shifted|shift|sweeping|swept|sweep|transferring|transferred|transfer|"
    "sending|sent|send|putting|put|stashing|stashed|stash|sticking|stuck|stick|squirrel(?:ling|led)?|"
    "sock(?:ing|ed)?|tuck(?:ing|ed)?|setting|set|allocating|allocated|allocate|reserving|reserved|reserve|"
    "saving|saved|save|paying|paid|pay|giving|gave|give|wiring|wired|wire|settling|settle|diverting|diverted|divert|"
    "redirecting|redirect|routing|route|parking|park|depositing|deposit|adding|add|contributing|contribute|"
    "chucking|chuck|throwing|throw|dropping|drop|popping|pop|funnel(?:ling|led)?|assigning|assign|taking|take|"
    "withdrawing|withdraw|skimming|skim|siphon(?:ing|ed)?|cut|banking|bank"
)
PAYEE_VERBS = {"pay", "paying", "paid", "wire", "wiring", "wired", "settle", "settling", "send", "sending", "sent",
               "give", "giving", "gave", "transfer", "transferring", "transferred"}
PURE_PAY_VERBS = {"pay", "paying", "wire", "wiring", "wired", "settle", "settling"}
SAVE_VERBS = {"save", "saving", "saved", "stash", "stashed", "stashing", "squirrel", "squirrelling", "squirrelled",
              "sock", "socking", "socked", "tuck", "tucking", "tucked", "reserve", "reserved", "reserving",
              "allocate", "allocated", "allocating"}
FILLERS = {
    "of", "a", "an", "it", "that", "this", "them", "to", "into", "onto", "in", "from", "for", "on", "up", "off", "out",
    "aside", "away", "back", "over", "then", "some", "every", "each", "month", "monthly", "pot", "account", "balance",
    "money", "funds", "cash", "amount", "towards", "toward", "also", "and", "be", "is", "are", "will", "should",
    "must", "can", "could", "would", "i", "we", "want", "need", "like", "go", "goes", "going", "let", "get", "gets",
    "make", "sure", "so", "do", "does", "as", "soon", "when", "if", "whenever", "once", "after", "at", "by", "with",
    "all", "any", "extra", "about", "around", "me", "myself", "day", "side", "own", "same", "again", "straight",
    "per", "or", "the", "my", "our", "just", "way",
}
VAGUE = re.compile(r"\b(?:a bit|a little|a chunk|a portion|a slice|part of|some of|a fraction|a share|something|"
                   r"the rest|rest|the remainder|remainder|rest of|whatever(?: is)? left|leftovers?|left ?overs?|spare|surplus|what'?s left)\b")
UNSAFE = re.compile(r"\b(?:everything|every penny|every last|the lot|whole balance|entire balance|full balance|"
                    r"all (?:of )?(?:the |my |our )?(?:money|funds|cash|savings|balance|accounts?|pennies)|"
                    r"drain|empty|clear out|wipe)\b")
ALL_WORD = re.compile(r"\b(?:all|full|whole|entire)\b")
UNSUPPORTED_SCHEDULE = re.compile(
    r"\b(?:weekly|every week|each week|daily|every day|each day|fortnightly|biweekly|bi-weekly|every other|quarterly|"
    r"yearly|annually|every year|every (?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:mon|tues|wednes|thurs|fri|satur|sun)days?|"
    r"weekends?|every \d+ (?:weeks?|days?|months?)|tomorrow|tonight|next (?:week|month|year|friday|monday)|today)\b"
)
MONTH_END = re.compile(r"\b(?:last day|end of (?:the )?month|month[- ]end|end of each month)\b")
CURRENCY = re.compile(r"[$€¥]|\b(?:dollars?|euros?|usd|eur|bucks|yen|bitcoin|btc|crypto)\b")

TOPUP = re.compile(
    r"\b(?:top(?:ping)?(?: it| @\w+)? up|top off|refill|replenish|restock|restore|fill(?: it)? up|"
    r"bring (?:it|@\w+) back(?: up)?|back up to|make up (?:the )?(?:shortfall|difference|gap)|"
    r"cover (?:the )?(?:shortfall|gap|difference)|(?:the )?(?:shortfall|difference|gap)|enough|what'?s missing|"
    r"whatever it takes)\b"
)


# --------------------------------------------------------------------------- normalisation
def numbers_in_text(text: str) -> set[float]:
    """All numeric values a rule text can legitimately mention (digits, number words, ordinals, fractions)."""
    t = canon(text)
    values = {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", t)}
    values |= {float(v) for k, v in ORDINALS.items() if re.search(rf"\b{k}\b", t)}
    return values


def _words_to_digits(t: str) -> str:
    def repl(m: re.Match) -> str:
        total = cur = 0
        for tok in re.split(r"[\s-]+", m.group(0)):
            if tok in _UNITS:
                cur += _UNITS[tok]
            elif tok in _TENS:
                cur += _TENS[tok]
            elif tok == "a":
                cur = max(cur, 1)
            elif tok == "hundred":
                cur = max(cur, 1) * 100
            elif tok in ("thousand", "grand"):
                total += max(cur, 1) * 1000
                cur = 0
        return str(total + cur)

    return _NUM_WORDS_RE.sub(repl, t)


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def canon(text: str) -> str:
    t = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = " ".join(t.split())
    t = _words_to_digits(t)
    t = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*k\b", lambda m: _fmt(float(m.group(1).replace(",", "")) * 1000), t)
    t = re.sub(r"£\s*(\d[\d,]*(?:\.\d+)?)", lambda m: "£" + _fmt(float(m.group(1).replace(",", ""))), t)
    t = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*(?:pounds?|quid|gbp|sterling)\b", lambda m: "£" + _fmt(float(m.group(1).replace(",", ""))), t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", lambda m: _fmt(float(m.group(1))) + "%", t)
    for pattern, pct in ((r"\b(?:three quarters|3/4)\b", 75), (r"\b(?:(?:a|one) )?quarter\b", 25), (r"\b(?:(?:a|one) )?half\b", 50),
                         (r"\b(?:(?:a|one) )?fifth\b", 20), (r"\b(?:(?:a|one) )?tenth\b", 10)):
        t = re.sub(pattern, f"{pct}%", t)
    t = re.sub(r"\b(?:a|one|two) thirds?\b", "UNSUPPORTED_FRACTION", t)
    t = re.sub(r"\s*<\s*(?=£)", " below ", t)
    t = re.sub(r"\b(?:less than|fewer than|lower than|under|beneath|short of|falls? short of)\b", "below", t)
    t = re.sub(r"\b(?:i'd like to|i would like to|i'd like|i would like|i want to|i want|i need to|i need|can you|could you|would you|"
               r"please|kindly|make sure that|make sure|make it so that|set up a rule to|set up a rule that|rule:|automatically|"
               r"always|immediately|straight ?away|right away|directly|simply)\b", " ", t)
    t = re.sub(r"\b(?:should|must|will|shall)\s+(?:be\s+)?(?=(?:moved|sent|transferred|put|saved|paid|shifted|swept|set|stashed)\b)", " ", t)
    t = re.sub(r"\bgo(?:es)?\s+(?:in)?to\b", "to", t)
    for key, pattern in ACCOUNT_PATTERNS:
        t = re.sub(rf"\b(?:{pattern})\b", f"@{key}", t)
    t = re.sub(r"\b(?:my|the|our|your)\s+", "", t)
    return " ".join(t.split())


# --------------------------------------------------------------------------- triggers
_DAY = r"(?P<d>\d{1,2})(?:st|nd|rd|th)?(?![\d%.]|\s*(?:%|percent|pounds?|quid))"
_WHEN = r"(?:when|whenever|once|as soon as|after|every time|each time|the moment|if)"
_ACC = r"(?P<acc>@\w+|[a-z][a-z ]{1,25}?)"
_NUM = r"(?P<n>\d+(?:\.\d+)?)"

BAL_PATTERNS = [
    re.compile(rf"\b(?:if|when|whenever|once|should|in case)\s+(?:balance (?:of|in|on)\s+)?{_ACC}(?: balance)?\s+(?:ever\s+)?"
               rf"(?:(?:drops?|falls?|goes|go|dips?|sinks?|slips?|gets?|is|runs?|ends? up|is left|stays?|has|have|dropped|fell|looks?|are|be)\s+)*"
               rf"(?:back\s+|down\s+|to\s+|just\s+)*below\s+£{_NUM}"),
    re.compile(rf"\b(?:if|when|whenever|once)\s+(?:there(?:'s| is)?|i have|i've got|i've|i'm left with|i am left with)\s+below\s+£{_NUM}"
               rf"\s+(?:left\s+)?(?:in|on)\s+{_ACC}(?=[,;.]|$| then)"),
]
ABOVE_RE = re.compile(r"\b(?:above|over|exceeds?|more than|higher than|greater than|at least)\s+£\d")
DEPOSIT_PATTERNS = [
    ("payday", re.compile(rf"\b(?:{_WHEN}|on)\s+(?:i(?: am|'m| get| got|'ve been| have been| was)?\s+paid|pay ?day|i get my pay)\b")),
    ("get", re.compile(rf"\b{_WHEN}\s+i\s+(?:get|got|receive|received|am sent|am given|'m sent)\s+(?:an? |any )?(?P<tag>[a-z]+)(?: payment| pay| deposit| cheque| check| transfer)?(?=[,;.]|$| then| i | move| put| send| transfer| save| set| stash| pay)")),
    ("arrives", re.compile(rf"\b{_WHEN}\s+(?:an? |any |some )?(?P<tag>[a-z]+(?: [a-z]+)?)\s+(?:arrives?|arrived|lands?|landed|comes? in|comes? through|came in|"
                           rf"hits?|(?:is|are) paid|gets? paid|(?:is|are) deposited|gets? deposited|(?:is|are) credited|gets? credited|gets? in|(?:is|are) in|clears?|cleared|shows? up|appears?|"
                           rf"(?:is|are) received|has arrived|have arrived|(?:is|are) paid in|gets? here|(?:is|are) sent|comes)(?: (?:in(?:to)?|to|in) @checking)?")),
]
MONTHLY_PATTERNS = [
    re.compile(rf"\b(?:on|every|each|by)\s+(?:day\s+)?{_DAY}(?:\s+of\s+(?:every |each )?month)?"),
    re.compile(rf"\b(?:on|every|each)\s+(?P<w>{_ORD_RE})\b(?:\s+day)?(?:\s+of\s+(?:every |each )?month)?"),
    re.compile(rf"\b(?P<w>{_ORD_RE})\s+(?:day\s+)?of\s+(?:every |each )?month"),
    re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)\s+(?:day\s+)?of\s+(?:every |each )?month"),
]


class _Stop(Exception):
    def __init__(self, kind: str, payload):
        self.kind, self.payload = kind, payload


def _reject(msg):
    raise _Stop("reject", [msg])


def _clarify(question):
    raise _Stop("clarify", question)


def _account(name: str | None) -> str | None:
    if not name:
        return None
    return name[1:] if name.startswith("@") else None


def _unknown_account(name: str) -> str:
    return f"Unknown account '{name.strip()}'. Known accounts: checking, savings, holiday pot."


def _normalise_tag(raw: str) -> str:
    words = [w for w in raw.split() if w not in TAG_ADJECTIVES]
    joined = " ".join(words)
    if joined in SALARY_WORDS or (words and all(w in SALARY_WORDS for w in words)):
        return "salary"
    if not words or joined in GENERIC_TAGS:
        _clarify("Which kind of deposit should trigger this? Rules need a named deposit, for example 'salary'.")
    tag = "_".join(words)
    if not re.fullmatch(r"[a-z_]{3,20}", tag):
        _reject(f"Deposit name '{raw}' must be 3-20 letters, for example 'salary'.")
    return tag


def _find_trigger(t: str):
    """Returns (trigger, remainder). At most one trigger may be present."""
    found: list[tuple[int, int, dict]] = []
    if UNSUPPORTED_SCHEDULE.search(t):
        _reject("Only monthly schedules on a fixed day (1 to 28) are supported, not weekly, daily or one-off schedules.")
    for pat in BAL_PATTERNS:
        for m in pat.finditer(t):
            account = m.group("acc").strip()
            if not account.startswith("@"):
                _reject(_unknown_account(account))
            found.append((m.start(), m.end(), {"type": "balance_below", "account": _account(account), "threshold_gbp": float(m.group("n"))}))
    if not found and re.search(rf"\b(?:if|when|whenever|once)\b[^,;]*@\w+[^,;]*{ABOVE_RE.pattern}", t):
        _reject("Only 'drops below' balance triggers are supported, not 'goes above'.")
    for kind, pat in DEPOSIT_PATTERNS:
        for m in pat.finditer(t):
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue
            tag = "salary" if kind == "payday" else _normalise_tag(m.group("tag"))
            found.append((m.start(), m.end(), {"type": "deposit", "tag": tag}))
    for pat in MONTHLY_PATTERNS:
        for m in pat.finditer(t):
            day = int(m.group("d")) if m.groupdict().get("d") else ORDINALS[m.group("w")]
            if not 1 <= day <= 28:
                _reject(f"Day {day} is not supported; use a day from 1 to 28.")
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue
            found.append((m.start(), m.end(), {"type": "monthly", "day_of_month": day}))
    if not found:
        if MONTH_END.search(t) or re.search(r"\bmonthly\b|\bevery month\b|\beach month\b", t):
            _clarify("Which day of the month (1 to 28) should this run on? Month-end is not supported.")
        return None, t
    unique = {repr(sorted(f[2].items())) for f in found}
    if len(unique) > 1:
        _reject("A rule can have only one trigger; split this into separate rules.")
    found.sort(key=lambda f: f[0])
    remainder = t
    for s, e, _ in sorted(found, key=lambda f: -f[0]):
        remainder = remainder[:s] + " ; " + remainder[e:]
    return found[0][2], remainder


# --------------------------------------------------------------------------- actions
def _tag_words(trigger: dict) -> str:
    if trigger["type"] == "deposit":
        words = {trigger["tag"].replace("_", " ")} | (SALARY_WORDS if trigger["tag"] == "salary" else set())
        return "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    return r"deposit|payment"


def _parse_clause(clause: str, trigger: dict, previous: dict | None):
    s = " " + clause + " "
    if "UNSUPPORTED_FRACTION" in s:
        _clarify("Please give an exact percentage or a £ amount instead of a fraction like a third.")
    if UNSAFE.search(s):
        _reject("Moving everything or an entire balance is not allowed. Give an explicit percentage or £ amount.")
    if VAGUE.search(s):
        _clarify("How much exactly? Give a percentage like '10%' or an amount like '£100'.")
    tagre = _tag_words(trigger)
    s = re.sub(rf"\b(?:of|from|out of)\s+(?:that|it|this|each|every|any|what|deposit|payment|{tagre})\b(?:\s+(?:arrives|comes in|you get))?", " ", s)

    if TOPUP.search(s):
        if trigger["type"] != "balance_below":
            _reject("'Top it up' needs an 'If <account> drops below £<amount>' trigger.")
        src = re.search(r"\b(?:from|out of|using)\s+(@\w+)", s)
        if not src:
            unknown = re.search(r"\b(?:from|out of|using)\s+([a-z][a-z ]*)", s)
            if unknown:
                _reject(_unknown_account(unknown.group(1)))
            _clarify("Top up from which account? For example 'top it up from savings'.")
        dst = (re.search(r"\b(?:to|into)\s+(@\w+)", s) or re.search(r"top(?:ping)?(?: up)?\s+(@\w+)", s)
               or re.search(r"(?:refill|replenish|restock)\s+(@\w+)", s))
        target = _account(dst.group(1)) if dst else trigger["account"]
        amounts = re.findall(r"£(\d+(?:\.\d+)?)", s)
        if any(abs(float(a) - trigger["threshold_gbp"]) > 1e-9 for a in amounts):
            _clarify(f"Top-ups restore the trigger threshold (£{_fmt(trigger['threshold_gbp'])}). Did you mean a different amount?")
        if "%" in s:
            _reject("A top-up restores a shortfall and cannot use a percentage.")
        return {"from": _account(src.group(1)), "to": target, "amount": {"mode": "shortfall"}}, None

    verb_m = re.search(rf"\b({VERBS})\b", s)
    verb = verb_m.group(1) if verb_m else None
    if verb_m:
        s = s[:verb_m.start()] + " " + s[verb_m.end():]
    if verb is None:
        if previous is not None:
            verb = previous["verb"]
        elif re.search(r"[£%]", s):
            verb = "move"  # "10% to savings, 5% to holiday pot": a bare amount means move
        else:
            _reject(f"Could not understand '{clause.strip()}'.")
    if verb in ("drop", "cut", "bank", "add", "take", "set") and not re.search(r"[£%]", s):
        _reject(f"Could not understand '{clause.strip()}'.")
    if ">" in s or "<" in s:
        _reject(f"Could not understand '{clause.strip()}'.")

    if VAGUE.search(s):
        _clarify("How much exactly? Give a percentage like '10%' or an amount like '£100'.")
    if "%" in s and trigger["type"] != "deposit":
        _reject("Percentages need a deposit trigger (a percentage of what arrived); use a fixed £ amount instead.")
    if re.search(r"\d+(?:\.\d+)?%\s+of\s+@\w+", s):
        _reject("Percentages apply to the triggering deposit, not to an account balance.")
    percents = re.findall(r"(\d+(?:\.\d+)?)%", s)
    moneys = re.findall(r"£(\d+(?:\.\d+)?)", s)
    if len(percents) + len(moneys) > 1:
        _clarify("This action names more than one amount. Which one should be used?")
    s = re.sub(r"\d+(?:\.\d+)?%|£\d+(?:\.\d+)?", " ", s)

    src_m = re.search(r"\b(?:from|out of)\s+(@\w+)", s)
    source = _account(src_m.group(1)) if src_m else None
    if src_m:
        s = s[:src_m.start()] + " " + s[src_m.end():]
    unknown_src = re.search(r"\b(?:from|out of)\s+([a-z][a-z ]*)", s)
    if unknown_src:
        _reject(_unknown_account(unknown_src.group(1)))
    dst_m =re.search(r"\b(?:to|into|in|towards?|for|onto)\s+(@\w+)", s)
    dest = _account(dst_m.group(1)) if dst_m else None
    if dst_m:
        s = s[:dst_m.start()] + " " + s[dst_m.end():]
    elif verb in SAVE_VERBS:
        dest = "savings"
    elif source is None:
        loose = re.findall(r"@\w+", s)
        if len(loose) == 1 and verb not in PURE_PAY_VERBS:
            dest = _account(loose[0])
            s = s.replace(loose[0], " ")
    if "@" in s:
        _reject("Could not understand this action; name one source and one destination account.")

    residual = [w for w in re.findall(r"[a-z][a-z'_-]*", s) if w not in FILLERS]
    payee = None
    if residual:
        if verb in PAYEE_VERBS and dest is None and len(residual) <= 3:
            payee = "_".join(residual)
            if not re.fullmatch(r"[a-z][a-z_]{1,29}", payee) or payee in ("account", "bank", "everyone", "anyone", "someone", "anybody"):
                _reject(f"Payee name '{' '.join(residual)}' is not usable; use letters and spaces.")
        elif dest is None and re.search(r"\b(?:to|into|in|towards?|for|onto)\b", clause) and verb not in PAYEE_VERBS:
            _reject(_unknown_account(" ".join(residual)))
        else:
            _reject(f"Could not understand '{clause.strip()}'.")
    if not percents and not moneys:
        if ALL_WORD.search(clause):
            _clarify("Do you mean the whole deposit? Please state it explicitly as '100%' (it will still need approval).")
        _clarify("How much should move? Give a percentage like '10%' or an amount like '£100'.")
    if payee is not None:
        if source is None:
            if previous and previous.get("source") and previous.get("payee_ok"):
                source = previous["source"]
            else:
                _clarify("Pay from which account? For example 'pay £850 to landlord from checking'.")
        target = payee
    else:
        if dest is None:
            _clarify("Move the money to which account? For example 'to savings'.")
        target = dest
        if source is None and previous and previous.get("source") and not previous.get("payee_ok"):
            source = previous["source"]
        if source is None:
            if trigger["type"] == "deposit":
                source = "checking"
            else:
                _clarify("Which account should the money come from? For example 'from savings'.")
    amount = ({"mode": "percent", "percent": float(percents[0])} if percents else {"mode": "fixed", "gbp": float(moneys[0])})
    return {"from": source, "to": target, "amount": amount}, {"verb": verb, "source": source, "payee_ok": payee is not None}


def propose(text: str) -> dict:
    try:
        t = canon(text)
        if CURRENCY.search(t):
            _reject("Only GBP (£) amounts are supported.")
        trigger, remainder = _find_trigger(t)
        parts = [p.strip(" ,.:") for p in re.split(r"\s*(?:;|,|&|\band then\b|\bthen\b|\balso\b|\bplus\b|\band\b)\s*", remainder)]
        parts = [p for p in parts if re.search(r"[a-z£%0-9]", p) and not re.fullmatch(r"(?:every|each|month|monthly|on|when|if|and|then| )+", p)]
        if trigger is None:
            if not parts:
                _reject("No rule found.")
            _clarify("When should this happen? For example 'when my salary arrives', 'if checking drops below £200' or 'on the 1st'.")
        if not parts:
            _clarify("What should happen when this triggers? For example 'move 10% to savings'.")
        if len(parts) > MAX_ACTIONS:
            _reject(f"A rule can have at most {MAX_ACTIONS} actions; this one has {len(parts)}.")
        actions, previous, question = [], None, None
        for part in parts:
            try:
                action, previous = _parse_clause(part, trigger, previous)
                actions.append(action)
            except _Stop as stop:
                if stop.kind == "reject":
                    raise
                question = question or stop.payload
        if question:
            _clarify(question)
        return {"spec": {"trigger": trigger, "actions": actions}}
    except _Stop as stop:
        return {"clarify": stop.payload} if stop.kind == "clarify" else {"reject": stop.payload}


class OfflineModel:
    """Provider-interface wrapper so the agent treats it like any other model backend."""

    name = "offline_model"

    def propose(self, text: str) -> dict:
        return propose(text)
