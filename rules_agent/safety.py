"""Safety checks: static rule-set analysis (conflicts, loops) and per-transfer runtime checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .schema import INTERNAL_ACCOUNTS


@dataclass(frozen=True)
class Limits:
    max_per_transfer_gbp: float = 1000.0
    approval_threshold_gbp: float = 250.0
    min_balance_gbp: float = 0.0


def static_conflicts(rules: list[dict]) -> list[dict]:
    """rules: [{"id": "R1", "spec": {...}}]. Severity 'error' blocks the involved rules."""
    return _over_allocation(rules) + _loops(rules)


def _over_allocation(rules: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for rule in rules:
        trigger = rule["spec"]["trigger"]
        if trigger["type"] != "deposit":
            continue
        for action in rule["spec"]["actions"]:
            if action["amount"]["mode"] == "percent":
                key = (trigger["tag"], action["from"])
                groups[key].append((rule["id"], action["amount"]["percent"]))
    conflicts = []
    for (tag, source), items in sorted(groups.items()):
        total = sum(percent for _, percent in items)
        if total > 100 + 1e-9:
            conflicts.append(
                {
                    "kind": "over_allocation",
                    "severity": "error",
                    "rule_ids": sorted({rid for rid, _ in items}),
                    "message": f"Percentage moves on '{tag}' deposits from {source} add up to {total:g}% (maximum 100%).",
                }
            )
    return conflicts


def _loops(rules: list[dict]) -> list[dict]:
    """Find cycles among internal accounts formed by the rules' transfers.

    A cycle made only of balance-triggered rules can ping-pong money indefinitely, so it is an
    error. A cycle that involves a scheduled or deposit rule is reported as a warning.
    """
    edges = []
    for rule in rules:
        trigger_type = rule["spec"]["trigger"]["type"]
        for action in rule["spec"]["actions"]:
            if action["to"] in INTERNAL_ACCOUNTS and action["from"] != action["to"]:
                edges.append((action["from"], action["to"], rule["id"], trigger_type))

    found: dict[tuple, dict] = {}

    def walk(start: str, node: str, visited: list[str], path: list[tuple]) -> None:
        for src, dst, rule_id, trigger_type in edges:
            if src != node:
                continue
            step = path + [(src, dst, rule_id, trigger_type)]
            if dst == start:
                accounts = [start] + [edge[1] for edge in step]
                rule_ids = sorted({edge[2] for edge in step})
                all_balance = all(edge[3] == "balance_below" for edge in step)
                key = tuple(sorted(rule_ids))
                found.setdefault(
                    key,
                    {
                        "kind": "loop",
                        "severity": "error" if all_balance else "warning",
                        "rule_ids": rule_ids,
                        "message": (
                            "Money can loop " + " -> ".join(accounts) + f" via rules {', '.join(rule_ids)}"
                            + (" (balance top-ups only: unbounded ping-pong)." if all_balance else ".")
                        ),
                    },
                )
            elif dst not in visited and dst > start:
                walk(start, dst, visited + [dst], step)

    for start in sorted(INTERNAL_ACCOUNTS):
        walk(start, start, [start], [])
    return list(found.values())


def check_transfer(transfer: dict, balances: dict[str, float], limits: Limits) -> list[dict]:
    """Runtime checks for one proposed transfer against projected balances."""
    violations = []
    amount = transfer["amount_gbp"]
    source = transfer["from"]
    if amount <= 0:
        violations.append({"code": "non_positive", "message": "Amount must be above zero."})
    if amount > limits.max_per_transfer_gbp:
        violations.append(
            {
                "code": "max_per_transfer",
                "message": f"£{amount:,.2f} exceeds the £{limits.max_per_transfer_gbp:,.0f} per-transfer limit.",
            }
        )
    if source in balances:
        after = round(balances[source] - amount, 2)
        if after < limits.min_balance_gbp - 1e-9:
            violations.append(
                {
                    "code": "overdraft",
                    "message": f"{source} would fall to £{after:,.2f} (minimum £{limits.min_balance_gbp:,.2f}).",
                }
            )
    return violations


def approval_reasons(transfer: dict, rule_is_approved: bool, limits: Limits) -> list[str]:
    reasons = []
    if not rule_is_approved:
        reasons.append("rule is awaiting its first human approval")
    if transfer["amount_gbp"] > limits.approval_threshold_gbp:
        reasons.append(f"amount is above the £{limits.approval_threshold_gbp:,.0f} approval threshold")
    return reasons
