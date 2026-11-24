"""Event-sourced execution: idempotency, stable ids, replayable state, traceability."""

import json
from pathlib import Path

import pytest

from rules_agent import projection
from rules_agent.audit import AuditLog
from rules_agent.service import Workspace

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "rules.txt"


def _demo_workspace(tmp_path, run=True):
    ws = Workspace(tmp_path / "data")
    for line in EXAMPLES.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            ws.submit_rule(line, actor="operator")
    for rule in list(ws.state["rules"].values()):
        if rule["status"] == "pending_review":
            ws.approve_rule(rule["id"], actor="approver")
    if run:
        ws.run()
    return ws


def _count(ws, kind=None):
    entries = ws.audit.entries()
    return len(entries) if kind is None else sum(e["type"] == kind for e in entries)


def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("a", {"x": 1})
    log.append("b", {"y": "£2"})
    assert log.verify()[0] is True
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["data"]["x"] = 999
    lines[0] = json.dumps(tampered)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, message = log.verify()
    assert ok is False and "entry 1" in message


def test_every_rule_is_logged_with_text_parse_trace_and_spec(tmp_path):
    ws = _demo_workspace(tmp_path)
    assert _count(ws, "rule.submitted") == _count(ws, "rule.parsed") == _count(ws, "rule.approved") == 4
    parsed = [e for e in ws.audit.entries() if e["type"] == "rule.parsed"]
    assert all(e["data"]["validation_passed"] and e["data"]["trace"] for e in parsed)
    assert ws.verify_audit()[0]


def test_processing_a_period_twice_creates_nothing_new(tmp_path):
    ws = _demo_workspace(tmp_path)
    ids, events = set(ws.state["proposals"]), _count(ws)
    again = ws.run()
    assert again["processed"] is False
    assert set(ws.state["proposals"]) == ids and _count(ws) == events


def test_proposal_ids_are_stable_across_independent_runs(tmp_path):
    first, second = _demo_workspace(tmp_path / "a"), _demo_workspace(tmp_path / "b")
    assert first.state["proposals"].keys() == second.state["proposals"].keys()
    for pid, p in first.state["proposals"].items():
        q = second.state["proposals"][pid]
        assert (p["amount_gbp"], p["from"], p["to"], p["status"]) == (q["amount_gbp"], q["from"], q["to"], q["status"])


def test_approval_applies_once_and_does_not_rerun_the_month(tmp_path):
    ws = _demo_workspace(tmp_path)
    before = {pid: dict(p) for pid, p in ws.state["proposals"].items()}
    pending = sorted(pid for pid, p in before.items() if p["status"] == "pending_approval")
    target = pending[0]
    executed_before = _count(ws, "transfer.executed")  # the small rule R4 auto-executed already
    result = ws.decide(target, "approved", actor="approver", note="checked")
    assert result["applied"] is True
    after = ws.state["proposals"]
    assert after.keys() == before.keys()  # nothing created or withdrawn
    for pid in before:
        if pid != target:
            assert after[pid]["status"] == before[pid]["status"] and after[pid]["amount_gbp"] == before[pid]["amount_gbp"]
    assert after[target]["status"] == "approved" and after[target]["applied"]
    assert _count(ws, "transfer.executed") == executed_before + 1 and _count(ws, "proposal.withdrawn") == 0
    balance = ws.state["balances"]["checking"]
    # repeating the same decision is a no-op: no new events, balance unchanged
    events = _count(ws)
    assert ws.decide(target, "approved", actor="approver")["applied"] is False
    assert _count(ws) == events and ws.state["balances"]["checking"] == balance


def test_decisions_are_final(tmp_path):
    ws = _demo_workspace(tmp_path)
    pid = sorted(pid for pid, p in ws.state["proposals"].items() if p["status"] == "pending_approval")[0]
    executed_before = _count(ws, "transfer.executed")
    ws.decide(pid, "rejected", actor="approver")
    with pytest.raises(ValueError, match="final"):
        ws.decide(pid, "approved", actor="approver")
    assert ws.state["proposals"][pid]["status"] == "rejected" and _count(ws, "transfer.executed") == executed_before


def test_state_is_a_pure_fold_over_the_log(tmp_path):
    ws = _demo_workspace(tmp_path)
    pending = sorted(pid for pid, p in ws.state["proposals"].items() if p["status"] == "pending_approval")
    ws.decide(pending[0], "approved", actor="approver")
    ws.decide(pending[1], "rejected", actor="approver")
    assert projection.fold(ws.audit.entries()) == ws.state
    reopened = Workspace(tmp_path / "data")  # a new process sees exactly the same state
    assert reopened.state == ws.state


def test_four_eyes_rule_approval_and_unapproved_rules_wait(tmp_path):
    ws = Workspace(tmp_path / "data")
    rule = ws.submit_rule("Pay £850 to landlord from checking on the 1st", actor="alice")
    with pytest.raises(ValueError, match="submitted it"):
        ws.approve_rule(rule["id"], actor="alice")
    ws.run()
    proposal = next(iter(ws.state["proposals"].values()))
    assert proposal["status"] == "pending_approval"
    assert "awaiting its first human approval" in proposal["reasons"][0]


def test_small_transfers_from_approved_rules_auto_execute_once(tmp_path):
    ws = Workspace(tmp_path / "data")
    rule = ws.submit_rule("Move £40 from checking to savings on the 3rd", actor="alice")
    ws.approve_rule(rule["id"], actor="bob")
    ws.run()
    [p] = ws.state["proposals"].values()
    assert p["status"] == "auto_executed" and p["applied"]
    ws.run()
    assert _count(ws, "transfer.executed") == 1 and ws.state["balances"]["savings"] == 3040.0


def test_execution_rechecks_balances_and_blocks_instead_of_overdrawing(tmp_path):
    ws = _demo_workspace(tmp_path)
    rent = "R3-sched-2026-11-01-a1"
    ws.state["balances"]["checking"] = 100.0  # money went elsewhere since the proposal was made
    ws.decide(rent, "approved", actor="approver")
    p = ws.state["proposals"][rent]
    assert p["status"] == "blocked" and not p["applied"] and "blocked at execution" in p["reasons"][-1]
    assert ws.state["balances"]["checking"] == 100.0


def test_next_month_opens_and_processes_a_new_period(tmp_path):
    ws = _demo_workspace(tmp_path)
    result = ws.next_month()
    assert result["processed"] and result["period"] == "2026-12"
    assert ws.state["processed"].keys() == {"2026-11", "2026-12"}
    assert any(pid.startswith("R3-sched-2026-12-01") for pid in ws.state["proposals"])


def test_preview_stores_nothing(tmp_path):
    ws = Workspace(tmp_path / "data")
    rule = ws.submit_rule("Pay £850 to landlord from checking on the 1st", actor="alice")
    events = _count(ws)
    assert ws.preview(rule["spec"])[0]["amount_gbp"] == 850.0
    assert _count(ws) == events and not ws.state["proposals"]


def test_explain_traces_transfer_back_to_rule_text(tmp_path):
    ws = _demo_workspace(tmp_path)
    pid = next(p for p in ws.state["proposals"] if p.startswith("R1-TX-"))
    ws.decide(pid, "approved", actor="approver", note="matches budget")
    text = ws.explain(pid)
    assert "When my salary arrives, move 20% to savings" in text
    assert "approved by approver" in text and "matches budget" in text and "chain intact" in text
    trace = ws.trace(pid)
    assert [s["kind"] for s in trace["steps"]] == ["rule_text", "parse", "rule_approval", "trigger", "proposal", "decision", "execution"]
    assert trace["chain_ok"]


def test_conflicting_rules_cannot_be_approved(tmp_path):
    ws = Workspace(tmp_path / "data")
    a = ws.submit_rule("When my salary arrives, move 70% to savings", actor="alice")
    b = ws.submit_rule("When my salary arrives, move 40% to holiday pot", actor="alice")
    assert any(c["severity"] == "error" for c in b["conflicts"])
    with pytest.raises(ValueError, match="conflicts"):
        ws.approve_rule(a["id"], actor="bob")
