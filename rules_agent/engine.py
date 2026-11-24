"""Ledger processing: turns rules plus one month of ledger events into events (it never mutates state).

`process_period` walks the month once, in date order, and returns the events to append:
`proposal.created` (with a stable id), `transfer.executed` for transfers that are auto-approved,
and a final `ledger.processed`. Pending proposals do not move money. Approving one later is a
separate command (service.decide) that executes only that proposal against the then-current
balances; nothing is recomputed.

Proposal ids are `<rule>-<ledger ref>-a<n>`, so the same rule and ledger event always yield the
same id and a second processing of the same period creates nothing new.
"""

from __future__ import annotations

import calendar

from .ledger import ACCOUNT_LABELS, generate_month
from .safety import Limits, approval_reasons, check_transfer, static_conflicts
from .schema import INTERNAL_ACCOUNTS

LIVE_STATUSES = ("pending_review", "active")


def _rule_order(rule: dict) -> int:
    return int(rule["id"][1:]) if rule["id"][1:].isdigit() else 10**9


def live_rules(state: dict) -> list[dict]:
    return sorted((r for r in state["rules"].values() if r["status"] in LIVE_STATUSES), key=_rule_order)


def process_period(rules: list[dict], balances_in: dict, limits: Limits, year: int, month: int,
                   existing_ids=frozenset(), transactions=None) -> list[tuple[str, dict]]:
    txs = list(transactions) if transactions is not None else generate_month(year, month)
    period = f"{year:04d}-{month:02d}"
    conflicts = static_conflicts([{"id": r["id"], "spec": r["spec"]} for r in rules])
    blocked = {rid: c["message"] for c in conflicts if c["severity"] == "error" for rid in c["rule_ids"]}

    balances = dict(balances_in)
    armed = {r["id"]: balances[r["spec"]["trigger"]["account"]] >= r["spec"]["trigger"]["threshold_gbp"]
             for r in rules if r["spec"]["trigger"]["type"] == "balance_below"}
    events: list[tuple[str, dict]] = []
    created = 0
    days_in_month = calendar.monthrange(year, month)[1]

    timeline = [(tx.date, 0, idx, ("tx", tx)) for idx, tx in enumerate(txs)]
    timeline += [(f"{period}-{day:02d}", 1, day, ("day", day)) for day in range(1, days_in_month + 1)]
    timeline.sort(key=lambda e: e[:3])

    def fire(rule: dict, ref: str, date: str, trigger_text: str, amount_in, threshold=None) -> None:
        nonlocal created
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
            pid = f"{rule['id']}-{ref}-a{idx}"
            if pid in existing_ids:
                continue
            transfer = {"amount_gbp": amount, "from": action["from"], "to": action["to"]}
            violations = check_transfer(transfer, balances, limits)
            if rule["id"] in blocked:
                status, reasons = "blocked", [blocked[rule["id"]]]
            elif violations:
                status, reasons = "blocked", [v["message"] for v in violations]
            else:
                reasons = approval_reasons(transfer, rule["status"] == "active", limits)
                if reasons:
                    status = "pending_approval"
                else:
                    status, reasons = "auto_executed", ["within approval threshold and rule already approved"]
            events.append(("proposal.created", {
                "proposal_id": pid, "rule_id": rule["id"], "ref": ref, "date": date, "trigger": trigger_text,
                "from": action["from"], "to": action["to"], "amount_gbp": amount, "status": status,
                "reasons": reasons, "violations": violations, "period": period,
            }))
            created += 1
            if status == "auto_executed":
                balances[action["from"]] = round(balances[action["from"]] - amount, 2)
                if action["to"] in INTERNAL_ACCOUNTS:
                    balances[action["to"]] = round(balances[action["to"]] + amount, 2)
                events.append(("transfer.executed", {
                    "proposal_id": pid, "rule_id": rule["id"], "amount_gbp": amount, "from": action["from"],
                    "to": action["to"], "date": date, "status": "auto_executed", "simulated": True,
                }))

    net_change = 0.0
    for _, _, _, (kind, obj) in timeline:
        if kind == "tx":
            tx = obj
            balances[tx.account] = round(balances[tx.account] + tx.amount_gbp, 2)
            net_change += tx.amount_gbp
            ref, date = tx.id, tx.date
            description = f"{tx.id} ({tx.description}, {tx.date})"
            if tx.amount_gbp > 0 and tx.tag:
                for rule in rules:
                    trigger = rule["spec"]["trigger"]
                    if trigger["type"] == "deposit" and trigger["tag"] == tx.tag:
                        fire(rule, ref, date, f"{description}: deposit of £{tx.amount_gbp:,.2f} tagged '{tx.tag}'", tx.amount_gbp)
        else:
            day = obj
            date = f"{period}-{day:02d}"
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
                fire(rule, ref, date,
                     f"{description} left {ACCOUNT_LABELS[trigger['account']]} at £{balance:,.2f}, below £{threshold:,.2f}",
                     None, threshold=threshold)
            elif balance >= threshold:
                armed[rule["id"]] = True

    events.append(("ledger.processed", {
        "period": period, "opening_balances": dict(balances_in), "transactions": len(txs),
        "net_change_gbp": round(net_change, 2), "proposals": created, "conflicts": conflicts,
    }))
    return events
