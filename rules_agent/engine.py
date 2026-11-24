"""Simulation engine: runs live rules over the synthetic ledger and produces proposed transfers.

The run is a pure function of (rules, ledger, decisions). Pending proposals are not applied to
projected balances; approved ones are. Re-running after a decision therefore recomputes
downstream effects, and a proposal that would now overdraw an account is blocked.
"""

from __future__ import annotations

import calendar

from .ledger import ACCOUNT_LABELS, OPENING_BALANCES_GBP, generate_month
from .safety import Limits, approval_reasons, check_transfer, static_conflicts
from .schema import INTERNAL_ACCOUNTS

LIVE_STATUSES = ("pending_review", "active")
APPLIED_STATUSES = ("auto_executed", "approved")


def new_state() -> dict:
    return {
        "rules": {},
        "rule_counter": 0,
        "decisions": {},
        "proposals": {},
        "conflicts": [],
        "last_status": {},
        "last_run": None,
    }


def _rule_order(rule: dict) -> int:
    return int(rule["id"][1:])


def add_rule(state: dict, text: str, result) -> dict:
    state["rule_counter"] += 1
    rule_id = f"R{state['rule_counter']}"
    rule = {
        "id": rule_id,
        "text": text,
        "status": "pending_review" if result.status == "ok" else "invalid",
        "spec": result.spec,
        "source": result.source,
        "messages": list(result.messages),
        "question": result.question,
        "approved_by": None,
        "approved_at": None,
    }
    state["rules"][rule_id] = rule
    return rule


def approve_rule(state: dict, rule_id: str, actor: str, when: str) -> None:
    rule = state["rules"].get(rule_id)
    if rule is None:
        raise KeyError(f"no rule {rule_id}")
    if rule["status"] != "pending_review":
        raise ValueError(f"{rule_id} is '{rule['status']}', not awaiting approval")
    rule["status"] = "active"
    rule["approved_by"] = actor
    rule["approved_at"] = when


def decide(state: dict, proposal_id: str, decision: str, actor: str, note: str, when: str) -> None:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    proposal = state["proposals"].get(proposal_id)
    if proposal is None:
        raise KeyError(f"no proposal {proposal_id}")
    if proposal["status"] not in ("pending_approval", "approved", "rejected"):
        raise ValueError(f"{proposal_id} is '{proposal['status']}'; only proposals awaiting a decision can be decided")
    state["decisions"][proposal_id] = {"decision": decision, "actor": actor, "note": note, "at": when}


def run_simulation(state: dict, limits: Limits, year: int = 2026, month: int = 11, transactions=None):
    txs = list(transactions) if transactions is not None else generate_month(year, month)
    rules = sorted(
        (r for r in state["rules"].values() if r["status"] in LIVE_STATUSES), key=_rule_order
    )
    conflicts = static_conflicts([{"id": r["id"], "spec": r["spec"]} for r in rules])
    blocked = {rid: c["message"] for c in conflicts if c["severity"] == "error" for rid in c["rule_ids"]}

    balances = dict(OPENING_BALANCES_GBP)
    armed = {r["id"]: True for r in rules}
    proposals: dict[str, dict] = {}
    days_in_month = calendar.monthrange(year, month)[1]

    events = [(tx.date, 0, idx, ("tx", tx)) for idx, tx in enumerate(txs)]
    events += [
        (f"{year:04d}-{month:02d}-{day:02d}", 1, day, ("day", day)) for day in range(1, days_in_month + 1)
    ]
    events.sort(key=lambda e: e[:3])

    def fire(rule: dict, ref: str, date: str, trigger_text: str, amount_in, threshold=None) -> None:
        for idx, action in enumerate(rule["spec"]["actions"], start=1):
            mode = action["amount"]["mode"]
            if mode == "percent":
                amount = round(amount_in * action["amount"]["percent"] / 100, 2)
            elif mode == "fixed":
                amount = round(action["amount"]["gbp"], 2)
            else:
                amount = round(max(0.0, threshold - balances[action["to"]]), 2)
            if amount <= 0:
                continue
            proposal_id = f"{rule['id']}-{ref}-a{idx}"
            transfer = {"amount_gbp": amount, "from": action["from"], "to": action["to"]}
            violations = check_transfer(transfer, balances, limits)
            decision_record = state["decisions"].get(proposal_id)
            decision = decision_record["decision"] if decision_record else None
            if rule["id"] in blocked:
                status, reasons = "blocked", [blocked[rule["id"]]]
            elif decision == "rejected":
                status, reasons = "rejected", [f"rejected by {decision_record['actor']}"]
            elif violations:
                status, reasons = "blocked", [v["message"] for v in violations]
            elif decision == "approved":
                status, reasons = "approved", [f"approved by {decision_record['actor']}"]
            else:
                reasons = approval_reasons(transfer, rule["status"] == "active", limits)
                if reasons:
                    status = "pending_approval"
                else:
                    status = "auto_executed"
                    reasons = ["within approval threshold and rule already approved"]
            applied = status in APPLIED_STATUSES
            if applied:
                balances[action["from"]] = round(balances[action["from"]] - amount, 2)
                if action["to"] in INTERNAL_ACCOUNTS:
                    balances[action["to"]] = round(balances[action["to"]] + amount, 2)
            proposals[proposal_id] = {
                "id": proposal_id,
                "rule_id": rule["id"],
                "date": date,
                "trigger": trigger_text,
                "from": action["from"],
                "to": action["to"],
                "amount_gbp": amount,
                "status": status,
                "reasons": reasons,
                "applied": applied,
                "violations": violations,
            }

    for _, _, _, (kind, obj) in events:
        if kind == "tx":
            tx = obj
            balances[tx.account] = round(balances[tx.account] + tx.amount_gbp, 2)
            ref, date = tx.id, tx.date
            description = f"{tx.id} ({tx.description}, {tx.date})"
            if tx.amount_gbp > 0 and tx.tag:
                for rule in rules:
                    trigger = rule["spec"]["trigger"]
                    if trigger["type"] == "deposit" and trigger["tag"] == tx.tag:
                        fire(rule, ref, date, f"{description}: deposit of £{tx.amount_gbp:,.2f} tagged '{tx.tag}'",
                             tx.amount_gbp)
        else:
            day = obj
            date = f"{year:04d}-{month:02d}-{day:02d}"
            ref = f"sched-{date}"
            description = f"scheduled day {day} ({date})"
            for rule in rules:
                trigger = rule["spec"]["trigger"]
                if trigger["type"] == "monthly" and trigger["day_of_month"] == day:
                    fire(rule, ref, date, description, None)

        for rule in rules:
            trigger = rule["spec"]["trigger"]
            if trigger["type"] != "balance_below":
                continue
            balance = balances[trigger["account"]]
            threshold = trigger["threshold_gbp"]
            if balance < threshold and armed[rule["id"]]:
                armed[rule["id"]] = False  # edge-triggered: fires once per crossing
                label = ACCOUNT_LABELS[trigger["account"]]
                fire(rule, ref, date,
                     f"{description} left {label} at £{balance:,.2f}, below £{threshold:,.2f}",
                     None, threshold=threshold)
            elif balance >= threshold:
                armed[rule["id"]] = True

    state["proposals"] = proposals
    state["conflicts"] = conflicts
    state["last_run"] = {
        "period": f"{year:04d}-{month:02d}",
        "transactions": len(txs),
        "final_balances": balances,
    }
    return proposals, conflicts
