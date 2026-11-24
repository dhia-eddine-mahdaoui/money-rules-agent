"""Event-sourced state: the workspace state is a pure fold over audit-log events.

Applying the same event twice is harmless (every handler is idempotent), so a retried or raced
command cannot execute a transfer or process a ledger period twice.
"""

from __future__ import annotations

from .ledger import OPENING_BALANCES_GBP
from .schema import INTERNAL_ACCOUNTS


def new_state(period: str = "2026-11") -> dict:
    return {
        "rules": {},
        "rule_counter": 0,
        "proposals": {},
        "balances": dict(OPENING_BALANCES_GBP),
        "period": period,
        "processed": {},
        "events": 0,
    }


def fold(entries: list[dict], period: str = "2026-11") -> dict:
    state = new_state(period)
    for entry in entries:
        apply(state, entry)
    return state


def apply(state: dict, entry: dict) -> None:
    kind, data = entry["type"], entry["data"]
    state["events"] = entry["seq"]
    handler = _HANDLERS.get(kind)
    if handler:
        handler(state, data, entry)


def _rule_submitted(state, d, e):
    if d["rule_id"] in state["rules"]:
        return
    state["rule_counter"] += 1
    state["rules"][d["rule_id"]] = {
        "id": d["rule_id"], "text": d["text"], "status": "parsing", "spec": None, "source": None,
        "messages": [], "question": None, "trace": [], "conflicts": [], "code": "",
        "submitted_by": d.get("actor"), "submitted_at": e["ts"], "approved_by": None, "approved_at": None,
    }


def _rule_parsed(state, d, e):
    rule = state["rules"].get(d["rule_id"])
    if rule is None or rule["status"] != "parsing":
        return
    rule.update(
        status="pending_review" if d["status"] == "ok" else "invalid",
        spec=d.get("spec"), source=d.get("source"), messages=d.get("messages", []), question=d.get("question"),
        trace=d.get("trace", []), conflicts=d.get("conflicts", []), code=d.get("code", ""), parse_status=d["status"],
    )


def _rule_approved(state, d, e):
    rule = state["rules"].get(d["rule_id"])
    if rule and rule["status"] == "pending_review":
        rule.update(status="active", approved_by=d["actor"], approved_at=e["ts"])


def _period_opened(state, d, e):
    state["period"] = d["period"]


def _proposal_created(state, d, e):
    pid = d["proposal_id"]
    if pid in state["proposals"]:
        return
    state["proposals"][pid] = {
        "id": pid, "rule_id": d["rule_id"], "ref": d["ref"], "date": d["date"], "trigger": d["trigger"],
        "from": d["from"], "to": d["to"], "amount_gbp": d["amount_gbp"], "status": d["status"],
        "reasons": list(d["reasons"]), "violations": list(d.get("violations", [])), "applied": False,
        "decision": None, "created_seq": e["seq"], "period": d["period"],
    }


def _decision(state, d, e):
    p = state["proposals"].get(d["proposal_id"])
    if p is None or p["decision"] is not None or p["status"] != "pending_approval":
        return
    p["decision"] = {"decision": d["decision"], "actor": d["actor"], "note": d.get("note", ""), "at": e["ts"], "seq": e["seq"]}
    if d["decision"] == "rejected":
        p["status"] = "rejected"
        p["reasons"] = p["reasons"] + [f"rejected by {d['actor']}"]


def _executed(state, d, e):
    p = state["proposals"].get(d["proposal_id"])
    if p is None or p["applied"]:
        return
    balances = state["balances"]
    balances[p["from"]] = round(balances[p["from"]] - p["amount_gbp"], 2)
    if p["to"] in INTERNAL_ACCOUNTS:
        balances[p["to"]] = round(balances[p["to"]] + p["amount_gbp"], 2)
    p["applied"] = True
    p["status"] = d["status"]
    p["executed_seq"] = e["seq"]
    if d["status"] == "approved":
        p["reasons"] = p["reasons"] + [f"approved by {p['decision']['actor']}"]


def _blocked(state, d, e):
    p = state["proposals"].get(d["proposal_id"])
    if p is None or p["applied"]:
        return
    p["status"] = "blocked"
    p["reasons"] = p["reasons"] + list(d["reasons"])
    p["blocked_seq"] = e["seq"]


def _processed(state, d, e):
    if d["period"] in state["processed"]:
        return
    state["balances"]["checking"] = round(state["balances"]["checking"] + d["net_change_gbp"], 2)
    state["processed"][d["period"]] = {
        "opening_balances": d["opening_balances"], "transactions": d["transactions"],
        "proposals": d["proposals"], "seq": e["seq"], "at": e["ts"],
    }


_HANDLERS = {
    "rule.submitted": _rule_submitted,
    "rule.parsed": _rule_parsed,
    "rule.approved": _rule_approved,
    "ledger.period_opened": _period_opened,
    "proposal.created": _proposal_created,
    "proposal.decision": _decision,
    "transfer.executed": _executed,
    "proposal.blocked": _blocked,
    "ledger.processed": _processed,
}
