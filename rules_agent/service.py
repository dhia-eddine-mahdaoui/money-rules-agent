"""Workflow service used by the CLI and the web page.

State is never stored separately: every command appends events to the hash-chained log and the
in-memory state is a fold over that log (projection.py). Commands are idempotent:

* processing a ledger period twice creates nothing new (proposal ids are stable),
* deciding a proposal twice with the same decision is a no-op, a conflicting second decision is refused,
* an approved proposal executes exactly once, against current balances, without recomputing the month.
"""

from __future__ import annotations

import copy
import threading
from collections import Counter
from pathlib import Path

from . import engine, projection
from .agent import RuleAgent
from .audit import AuditLog
from .ledger import ACCOUNT_LABELS
from .safety import Limits, check_transfer, static_conflicts

EXECUTED = ("auto_executed", "approved")


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


def _next_period(period: str) -> tuple[int, int]:
    year, month = (int(x) for x in period.split("-"))
    return (year + 1, 1) if month == 12 else (year, month + 1)


class Workspace:
    def __init__(self, data_dir, provider=None, limits: Limits | None = None, year: int = 2026, month: int = 11):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.dir / "events.jsonl")
        self.agent = RuleAgent(provider)
        self.limits = limits or Limits()
        self.start_period = f"{year:04d}-{month:02d}"
        self.lock = threading.RLock()
        self._size = -1
        self.state = projection.new_state(self.start_period)
        self.refresh()

    # -- event plumbing ---------------------------------------------------
    def refresh(self) -> None:
        """Re-fold from the log when another process appended to it."""
        with self.lock:
            size = self.audit.size()
            if size != self._size:
                self.state = projection.fold(self.audit.entries(), self.start_period)
                self._size = size

    def _emit(self, event_type: str, data: dict) -> dict:
        entry = self.audit.append(event_type, data)
        projection.apply(self.state, entry)
        self._size = self.audit.size()
        return entry

    # -- rules ------------------------------------------------------------
    def conflicts_for(self, extra: dict | None = None) -> list[dict]:
        rules = [{"id": r["id"], "spec": r["spec"]} for r in engine.live_rules(self.state)]
        if extra:
            rules.append(extra)
        return static_conflicts(rules)

    def submit_rule(self, text: str, actor: str = "cli-user", use_model: bool = True) -> dict:
        with self.lock:
            self.refresh()
            result = RuleAgent(self.agent.provider, use_model).parse(text)
            rule_id = f"R{self.state['rule_counter'] + 1}"
            conflicts = []
            if result.status == "ok":
                conflicts = [c for c in self.conflicts_for({"id": rule_id, "spec": result.spec}) if rule_id in c["rule_ids"]]
            self._emit("rule.submitted", {"rule_id": rule_id, "text": text, "actor": actor})
            self._emit("rule.parsed", {
                "rule_id": rule_id, "status": result.status, "source": result.source, "code": result.code,
                "validation_passed": result.status == "ok", "spec": result.spec, "messages": result.messages,
                "question": result.question, "trace": result.trace, "conflicts": conflicts,
            })
            return self.state["rules"][rule_id]

    def approve_rule(self, rule_id: str, actor: str = "cli-approver") -> dict:
        with self.lock:
            self.refresh()
            rule = self.state["rules"].get(rule_id)
            if rule is None:
                raise KeyError(f"no rule {rule_id}")
            if rule["status"] == "active":
                return {"applied": False, "message": f"{rule_id} is already approved"}
            if rule["status"] != "pending_review":
                raise ValueError(f"{rule_id} is '{rule['status']}', not awaiting approval")
            if actor == rule["submitted_by"]:
                raise ValueError("a rule cannot be approved by the person who submitted it")
            errors = [c["message"] for c in self.conflicts_for() if c["severity"] == "error" and rule_id in c["rule_ids"]]
            if errors:
                raise ValueError("rule conflicts with other rules: " + "; ".join(errors))
            self._emit("rule.approved", {"rule_id": rule_id, "actor": actor})
            return {"applied": True, "message": f"{rule_id} approved"}

    # -- ledger processing ------------------------------------------------
    def run(self) -> dict:
        """Process the current ledger period once. Re-running is a no-op."""
        with self.lock:
            self.refresh()
            period = self.state["period"]
            if period in self.state["processed"]:
                return {"processed": False, "period": period, "created": 0, "by_status": self.counts()}
            year, month = (int(x) for x in period.split("-"))
            events = engine.process_period(engine.live_rules(self.state), self.state["balances"], self.limits, year, month,
                                           existing_ids=frozenset(self.state["proposals"]))
            for kind, data in events:
                self._emit(kind, data)
            return {"processed": True, "period": period, "created": events[-1][1]["proposals"], "by_status": self.counts()}

    def next_month(self) -> dict:
        with self.lock:
            self.refresh()
            if self.state["period"] not in self.state["processed"]:
                return self.run()
            year, month = _next_period(self.state["period"])
            self._emit("ledger.period_opened", {"period": f"{year:04d}-{month:02d}"})
            return self.run()

    def counts(self) -> dict:
        return dict(sorted(Counter(p["status"] for p in self.state["proposals"].values()).items()))

    def preview(self, spec: dict) -> list[dict]:
        """What a rule would have proposed for the current period (nothing is stored)."""
        with self.lock:
            self.refresh()
            period = self.state["period"]
            processed = self.state["processed"].get(period)
            opening = processed["opening_balances"] if processed else self.state["balances"]
            year, month = (int(x) for x in period.split("-"))
            rule = {"id": "R0", "status": "pending_review", "spec": spec}
            events = engine.process_period([rule], opening, self.limits, year, month)
            return [d for kind, d in events if kind == "proposal.created"]

    # -- human decisions --------------------------------------------------
    def decide(self, proposal_id: str, decision: str, actor: str = "cli-approver", note: str = "") -> dict:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        with self.lock:
            self.refresh()
            p = self.state["proposals"].get(proposal_id)
            if p is None:
                raise KeyError(f"no proposal {proposal_id}")
            if p["decision"] is not None:
                if p["decision"]["decision"] == decision:
                    return {"applied": False, "status": p["status"], "message": f"{proposal_id} was already {decision}; nothing changed"}
                raise ValueError(f"{proposal_id} was already {p['decision']['decision']}; decisions are final")
            if p["status"] != "pending_approval":
                raise ValueError(f"{proposal_id} is '{p['status']}'; only proposals awaiting a decision can be decided")
            self._emit("proposal.decision", {"proposal_id": proposal_id, "rule_id": p["rule_id"], "decision": decision,
                                             "actor": actor, "note": note})
            if decision == "approved":
                violations = check_transfer({"amount_gbp": p["amount_gbp"], "from": p["from"], "to": p["to"]},
                                            self.state["balances"], self.limits)
                if violations:
                    self._emit("proposal.blocked", {"proposal_id": proposal_id, "stage": "execution",
                                                    "reasons": ["blocked at execution: " + v["message"] for v in violations]})
                else:
                    self._emit("transfer.executed", {
                        "proposal_id": proposal_id, "rule_id": p["rule_id"], "amount_gbp": p["amount_gbp"], "from": p["from"],
                        "to": p["to"], "date": p["date"], "status": "approved", "simulated": True})
            return {"applied": True, "status": self.state["proposals"][proposal_id]["status"],
                    "message": f"{proposal_id} {self.state['proposals'][proposal_id]['status']}"}

    # -- trace ------------------------------------------------------------
    def trace(self, proposal_id: str) -> dict:
        """Everything that led to one transfer, from the rule text to the execution event."""
        with self.lock:
            self.refresh()
            p = self.state["proposals"].get(proposal_id)
            if p is None:
                raise KeyError(f"no proposal {proposal_id}")
            rule = self.state["rules"][p["rule_id"]]
            wanted = {
                "rule.submitted": lambda d: d["rule_id"] == rule["id"],
                "rule.parsed": lambda d: d["rule_id"] == rule["id"],
                "rule.approved": lambda d: d["rule_id"] == rule["id"],
                "proposal.created": lambda d: d["proposal_id"] == proposal_id,
                "proposal.decision": lambda d: d["proposal_id"] == proposal_id,
                "transfer.executed": lambda d: d["proposal_id"] == proposal_id,
                "proposal.blocked": lambda d: d["proposal_id"] == proposal_id,
            }
            entries = [e for e in self.audit.entries() if e["type"] in wanted and wanted[e["type"]](e["data"])]
            by_type = {e["type"]: e for e in entries}
            steps = [{"kind": "rule_text", "title": "Rule as written", "seq": by_type["rule.submitted"]["seq"],
                      "text": rule["text"], "actor": rule["submitted_by"], "at": rule["submitted_at"]}]
            parsed = by_type["rule.parsed"]
            steps.append({"kind": "parse", "title": "Understood as", "seq": parsed["seq"], "source": rule["source"],
                          "trace": rule["trace"], "summary": describe_spec(rule["spec"]) if rule["spec"] else "",
                          "spec": rule["spec"]})
            if "rule.approved" in by_type:
                steps.append({"kind": "rule_approval", "title": "Rule approved by a human", "seq": by_type["rule.approved"]["seq"],
                              "actor": rule["approved_by"], "at": rule["approved_at"]})
            else:
                steps.append({"kind": "rule_approval", "title": "Rule not yet approved", "seq": None, "actor": None, "at": None})
            created = by_type["proposal.created"]
            steps.append({"kind": "trigger", "title": "Ledger event that triggered it", "seq": created["seq"], "text": p["trigger"],
                          "date": p["date"]})
            steps.append({"kind": "proposal", "title": "Transfer proposed and checked", "seq": created["seq"], "id": p["id"],
                          "amount": p["amount_gbp"], "from": p["from"], "to": p["to"],
                          "first_status": created["data"]["status"], "reasons": created["data"]["reasons"],
                          "violations": created["data"]["violations"]})
            if p["decision"]:
                steps.append({"kind": "decision", "title": "Human decision", "seq": p["decision"]["seq"], **p["decision"]})
            if "transfer.executed" in by_type:
                ex = by_type["transfer.executed"]
                steps.append({"kind": "execution", "title": "Simulated transfer executed", "seq": ex["seq"], "at": ex["ts"],
                              "status": ex["data"]["status"]})
            elif "proposal.blocked" in by_type:
                steps.append({"kind": "blocked", "title": "Blocked", "seq": by_type["proposal.blocked"]["seq"],
                              "reasons": by_type["proposal.blocked"]["data"]["reasons"]})
            ok, verdict = self.audit.verify()
            return {"proposal": copy.deepcopy(p), "rule": copy.deepcopy(rule), "steps": steps,
                    "audit": [{"seq": e["seq"], "type": e["type"], "hash": e["hash"], "prev": e["prev_hash"], "ts": e["ts"]}
                              for e in entries], "chain_ok": ok, "chain": verdict}

    def explain(self, proposal_id: str) -> str:
        t = self.trace(proposal_id)
        p, rule = t["proposal"], t["rule"]
        lines = [
            f"Transfer {p['id']}: {money(p['amount_gbp'])} from {label(p['from'])} to {label(p['to'])} on {p['date']}",
            f"Status: {p['status']} (simulated, no real money moves)",
            f"Caused by rule {rule['id']}: \"{rule['text']}\" (submitted by {rule['submitted_by']})",
        ]
        for step in t["steps"]:
            if step["kind"] == "parse" and rule["spec"]:
                path = " -> ".join(f"{s['stage']}: {s['outcome']}" for s in step["trace"])
                lines.append(f"  Parsed as: {step['summary']}")
                lines.append(f"  Agent path: {path} (source: {step['source']})")
            elif step["kind"] == "rule_approval":
                lines.append(f"  {step['title']}" + (f" by {step['actor']} at {step['at']}" if step["actor"] else ""))
            elif step["kind"] == "trigger":
                lines.append(f"Trigger event: {step['text']}")
            elif step["kind"] == "proposal":
                lines.append("Reasons: " + "; ".join(step["reasons"]))
                lines.append("Safety checks: " + ("passed" if not step["violations"] else "; ".join(v["message"] for v in step["violations"])))
            elif step["kind"] == "decision":
                lines.append(f"Human decision: {step['decision']} by {step['actor']} at {step['at']}"
                             + (f" ({step['note']})" if step["note"] else ""))
            elif step["kind"] == "execution":
                lines.append(f"Executed once at {step['at']} as '{step['status']}'")
            elif step["kind"] == "blocked":
                lines.append("Blocked: " + "; ".join(step["reasons"]))
        lines.append("Audit trail:")
        for e in t["audit"]:
            lines.append(f"  #{e['seq']:<3} {e['type']:<18} hash {e['hash'][:12]}")
        lines.append(f"Chain: {t['chain']}")
        return "\n".join(lines)

    def verify_audit(self) -> tuple[bool, str]:
        return self.audit.verify()
