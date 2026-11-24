"""Workflow service used by both the CLI and the web page. Every state change is written to the audit log."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import engine
from .audit import AuditLog
from .ledger import ACCOUNT_LABELS
from .llm import parse_rule
from .safety import Limits

EXECUTED = engine.APPLIED_STATUSES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def label(account: str) -> str:
    return ACCOUNT_LABELS.get(account, account)


def money(value: float) -> str:
    return f"£{value:,.2f}"


def describe_spec(spec: dict) -> str:
    trigger = spec["trigger"]
    if trigger["type"] == "deposit":
        head = f"when a '{trigger['tag']}' deposit arrives"
    elif trigger["type"] == "balance_below":
        head = f"when {label(trigger['account'])} drops below {money(trigger['threshold_gbp'])}"
    else:
        head = f"on day {trigger['day_of_month']} of each month"
    steps = []
    for action in spec["actions"]:
        mode = action["amount"]["mode"]
        if mode == "percent":
            amount = f"{action['amount']['percent']:g}% of the trigger amount"
        elif mode == "fixed":
            amount = money(action["amount"]["gbp"])
        else:
            amount = "enough to restore the threshold"
        steps.append(f"{amount} from {label(action['from'])} to {label(action['to'])}")
    return f"{head}: " + "; ".join(steps)


class Workspace:
    def __init__(self, data_dir, provider=None, limits: Limits | None = None, year: int = 2026, month: int = 11):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.state_path = self.dir / "state.json"
        self.provider = provider
        self.limits = limits or Limits()
        self.year = year
        self.month = month
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = engine.new_state()

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # Rules -----------------------------------------------------------------
    def submit_rule(self, text: str, use_llm: bool = True) -> dict:
        result = parse_rule(text, provider=self.provider if use_llm else None)
        rule = engine.add_rule(self.state, text, result)
        self.audit.append("rule.submitted", {"rule_id": rule["id"], "text": text})
        self.audit.append(
            "rule.parsed",
            {
                "rule_id": rule["id"],
                "status": result.status,
                "source": result.source,
                "validation_passed": result.status == "ok",
                "spec": result.spec,
                "messages": result.messages,
                "question": result.question,
            },
        )
        self._save()
        return rule

    def approve_rule(self, rule_id: str, actor: str = "cli") -> dict:
        engine.approve_rule(self.state, rule_id, actor, _now())
        self.audit.append("rule.approved", {"rule_id": rule_id, "actor": actor})
        return self.run()

    # Simulation ------------------------------------------------------------
    def run(self) -> dict:
        proposals, conflicts = engine.run_simulation(self.state, self.limits, self.year, self.month)
        counts = Counter(p["status"] for p in proposals.values())
        self.audit.append(
            "simulation.run",
            {
                "period": f"{self.year:04d}-{self.month:02d}",
                "by_status": dict(sorted(counts.items())),
                "conflicts": conflicts,
            },
        )
        self._log_status_changes()
        self._save()
        return {"by_status": dict(counts), "conflicts": conflicts, "final_balances": self.state["last_run"]["final_balances"]}

    def _log_status_changes(self) -> None:
        last = self.state["last_status"]
        for pid, proposal in self.state["proposals"].items():
            previous = last.get(pid)
            if previous == proposal["status"]:
                continue
            last[pid] = proposal["status"]
            self.audit.append(
                "proposal.created" if previous is None else "proposal.status",
                {
                    "proposal_id": pid,
                    "rule_id": proposal["rule_id"],
                    "from_status": previous,
                    "to_status": proposal["status"],
                    "amount_gbp": proposal["amount_gbp"],
                    "from_account": proposal["from"],
                    "to_account": proposal["to"],
                    "date": proposal["date"],
                    "reasons": proposal["reasons"],
                },
            )
            if proposal["status"] in EXECUTED and previous not in EXECUTED:
                self.audit.append(
                    "transfer.executed",
                    {
                        "proposal_id": pid,
                        "rule_id": proposal["rule_id"],
                        "amount_gbp": proposal["amount_gbp"],
                        "from_account": proposal["from"],
                        "to_account": proposal["to"],
                        "date": proposal["date"],
                        "simulated": True,
                    },
                )
        for pid in [p for p in last if p not in self.state["proposals"]]:
            self.audit.append(
                "proposal.withdrawn",
                {
                    "proposal_id": pid,
                    "last_status": last.pop(pid),
                    "reason": "no longer produced by the simulation after an upstream decision or rule change",
                },
            )

    # Human decisions -------------------------------------------------------
    def decide(self, proposal_id: str, decision: str, actor: str = "cli", note: str = "") -> dict:
        engine.decide(self.state, proposal_id, decision, actor, note, _now())
        self.audit.append(
            "proposal.decision",
            {"proposal_id": proposal_id, "decision": decision, "actor": actor, "note": note},
        )
        return self.run()

    # Trace -----------------------------------------------------------------
    def explain(self, proposal_id: str) -> str:
        proposal = self.state["proposals"].get(proposal_id)
        if proposal is None:
            raise KeyError(f"no proposal {proposal_id}")
        rule = self.state["rules"][proposal["rule_id"]]
        related = [
            e
            for e in self.audit.entries()
            if e["data"].get("proposal_id") == proposal_id
            or (e["type"].startswith("rule.") and e["data"].get("rule_id") == rule["id"])
        ]
        lines = [
            f"Transfer {proposal_id}: {money(proposal['amount_gbp'])} from {label(proposal['from'])} "
            f"to {label(proposal['to'])} on {proposal['date']}",
            f"Status: {proposal['status']} (simulated, no real money moves)",
            f"Caused by rule {rule['id']}: \"{rule['text']}\"",
        ]
        if rule["spec"]:
            lines.append(f"  Parsed as: {describe_spec(rule['spec'])}")
        lines.append(f"  Rule source: {rule['source']}; status: {rule['status']}"
                     + (f" (approved by {rule['approved_by']})" if rule["approved_by"] else ""))
        lines.append(f"Trigger event: {proposal['trigger']}")
        lines.append("Reasons: " + "; ".join(proposal["reasons"]))
        lines.append("Safety checks: " + ("passed" if not proposal["violations"] else
                                          "; ".join(v["message"] for v in proposal["violations"])))
        decision = self.state["decisions"].get(proposal_id)
        if decision:
            lines.append(f"Human decision: {decision['decision']} by {decision['actor']} at {decision['at']}"
                         + (f" ({decision['note']})" if decision["note"] else ""))
        lines.append("Audit trail:")
        for entry in related:
            lines.append(f"  #{entry['seq']:<3} {entry['type']:<18} hash {entry['hash'][:12]}")
        return "\n".join(lines)

    def verify_audit(self) -> tuple[bool, str]:
        return self.audit.verify()
