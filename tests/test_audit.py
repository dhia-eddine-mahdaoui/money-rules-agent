import json
from pathlib import Path

from rules_agent.audit import AuditLog
from rules_agent.service import Workspace

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "rules.txt"


def _demo_workspace(tmp_path):
    ws = Workspace(tmp_path / "data")
    for line in EXAMPLES.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            ws.submit_rule(line)
    for rule in list(ws.state["rules"].values()):
        if rule["status"] == "pending_review":
            ws.approve_rule(rule["id"], actor="test")
    return ws


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


def test_every_rule_is_logged_with_text_and_parsed_spec(tmp_path):
    ws = _demo_workspace(tmp_path)
    types = [e["type"] for e in ws.audit.entries()]
    assert types.count("rule.submitted") == 3
    assert types.count("rule.parsed") == 3
    assert types.count("rule.approved") == 3
    parsed = [e for e in ws.audit.entries() if e["type"] == "rule.parsed"]
    assert all(e["data"]["validation_passed"] for e in parsed)
    assert ws.verify_audit()[0]


def test_unapproved_rule_proposals_wait_for_approval_and_are_traceable(tmp_path):
    ws = Workspace(tmp_path / "data")
    ws.submit_rule("Pay £850 to landlord from checking on the 1st")
    ws.run()
    proposal = next(iter(ws.state["proposals"].values()))
    assert proposal["status"] == "pending_approval"
    assert "awaiting its first human approval" in proposal["reasons"][0]
    assert not any(e["type"] == "transfer.executed" for e in ws.audit.entries())


def test_explain_traces_transfer_back_to_rule_text(tmp_path):
    ws = _demo_workspace(tmp_path)
    pid = next(p for p in ws.state["proposals"] if p.startswith("R1-TX-"))
    text = ws.explain(pid)
    assert "When my salary arrives, move 20% to savings" in text
    assert "rule.submitted" in text and "rule.parsed" in text and "proposal.created" in text


def test_approving_a_proposal_executes_it_and_rejecting_drops_it(tmp_path):
    ws = _demo_workspace(tmp_path)
    pending = sorted(pid for pid, p in ws.state["proposals"].items() if p["status"] == "pending_approval")
    assert len(pending) >= 2
    ws.decide(pending[0], "approved", actor="reviewer", note="looks right")
    ws.decide(pending[1], "rejected", actor="reviewer")
    assert ws.state["proposals"][pending[0]]["status"] == "approved"
    assert ws.state["proposals"][pending[1]]["status"] == "rejected"
    types = [e["type"] for e in ws.audit.entries()]
    assert "transfer.executed" in types
    assert ws.verify_audit()[0]


def test_simulation_is_deterministic(tmp_path):
    first = _demo_workspace(tmp_path / "a")
    second = _demo_workspace(tmp_path / "b")
    assert first.state["proposals"] == second.state["proposals"]
