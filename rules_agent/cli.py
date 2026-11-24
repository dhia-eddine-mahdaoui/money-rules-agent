"""Command-line interface. Run with: python -m rules_agent --help"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import RuleAgent
from .ledger import ACCOUNT_LABELS
from .llm import LLMError, provider_from_env
from .service import Workspace, describe_spec, label, money

EXAMPLE_RULES = Path(__file__).resolve().parents[1] / "examples" / "rules.txt"


def _print_rule(rule: dict) -> None:
    print(f"{rule['id']} [{rule['status']}] {rule['text']}")
    if rule["spec"]:
        print(f"    understood: {describe_spec(rule['spec'])}  (via {rule['source']})")
    if rule["status"] == "invalid":
        if rule["question"]:
            print(f"    clarify: {rule['question']}")
        for message in rule["messages"]:
            print(f"    rejected: {message}")
    for conflict in rule.get("conflicts", []):
        print(f"    {conflict['severity']}: {conflict['message']}")


def _print_proposals(workspace: Workspace, status: str | None) -> None:
    proposals = sorted(workspace.state["proposals"].values(), key=lambda p: (p["date"], p["id"]))
    if status:
        proposals = [p for p in proposals if p["status"] == status]
    if not proposals:
        print("No proposals.")
        return
    for p in proposals:
        print(f"{p['id']}  {p['status']:<16} {money(p['amount_gbp']):>10}  {label(p['from'])} -> {label(p['to'])}  {p['date']}")
        if p["reasons"]:
            print(f"      {'; '.join(p['reasons'])}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rules_agent", description="Plain-English money rules (simulation only).")
    parser.add_argument("--data-dir", default=os.environ.get("RULES_DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="submit a plain-English rule")
    add.add_argument("text", nargs="+")
    add.add_argument("--actor", default="cli-user")
    add.add_argument("--no-model", action="store_true", help="strict grammar only: skip the offline model and any LLM")

    check = sub.add_parser("parse", help="dry run: show how the agent reads a rule, without storing it")
    check.add_argument("text", nargs="+")

    sub.add_parser("rules", help="list rules")
    approve_rule = sub.add_parser("approve-rule", help="approve a rule (a different person from the submitter)")
    approve_rule.add_argument("rule_id")
    approve_rule.add_argument("--actor", default="cli-approver")
    sub.add_parser("run", help="process the current ledger month once (idempotent)")
    sub.add_parser("next-month", help="open and process the next ledger month")

    proposals = sub.add_parser("proposals", help="list proposed transfers")
    proposals.add_argument("--status")
    for name in ("approve", "reject"):
        decide = sub.add_parser(name, help=f"{name} a proposed transfer (applied once, final)")
        decide.add_argument("proposal_id")
        decide.add_argument("--note", default="")
        decide.add_argument("--actor", default="cli-approver")

    explain = sub.add_parser("explain", help="trace a transfer back to the rule text")
    explain.add_argument("proposal_id")

    audit = sub.add_parser("audit", help="show the audit trail")
    audit.add_argument("--tail", type=int, default=20)
    sub.add_parser("verify", help="verify the audit hash chain")
    sub.add_parser("demo", help="load the example rules, approve them and process the month")
    sub.add_parser("reset", help="delete the event log in the data directory (demo logins are kept)")

    ev = sub.add_parser("eval", help="run the parsing evals")
    ev.add_argument("--html", help="also write a styled results page to this file")
    ev.add_argument("--json", action="store_true", help="print the full report as JSON")

    web = sub.add_parser("web", help="serve the web UI (localhost only, login required)")
    web.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "reset":
        (Path(args.data_dir) / "events.jsonl").unlink(missing_ok=True)
        print(f"Cleared the event log in {args.data_dir}")
        return 0
    if args.cmd == "eval":
        from . import evals
        report = evals.run_all()
        print(json.dumps(report, indent=2) if args.json else evals.format_report(report))
        if args.html:
            from .web import render_eval_page
            Path(args.html).write_text(render_eval_page(report), encoding="utf-8")
        return 1 if report["full"]["metrics"]["unsafe_accepts"] else 0
    try:
        provider = provider_from_env()
    except LLMError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "parse":
        result = RuleAgent(provider).parse(" ".join(args.text))
        for step in result.trace:
            print(f"  {step['stage']:<14} {step['outcome']}" + (f"  ({step['detail']})" if step["detail"] else ""))
        print(f"=> {result.status} via {result.source}")
        if result.spec:
            print(f"   {describe_spec(result.spec)}")
            print(json.dumps(result.spec))
        if result.question:
            print(f"   question: {result.question}")
        for message in result.messages:
            print(f"   rejected: {message}")
        return 0
    workspace = Workspace(args.data_dir, provider=provider)

    try:
        if args.cmd == "add":
            _print_rule(workspace.submit_rule(" ".join(args.text), actor=args.actor, use_model=not args.no_model))
        elif args.cmd == "rules":
            if not workspace.state["rules"]:
                print("No rules yet. Add one with: add \"When my salary arrives, move 20% to savings\"")
            for rule in workspace.state["rules"].values():
                _print_rule(rule)
        elif args.cmd == "approve-rule":
            print(workspace.approve_rule(args.rule_id, actor=args.actor)["message"] + ".")
        elif args.cmd in ("run", "next-month"):
            result = workspace.run() if args.cmd == "run" else workspace.next_month()
            if result["processed"]:
                print(f"Processed {result['period']}: {result['created']} proposals created.")
            else:
                print(f"{result['period']} was already processed; nothing changed.")
            print("Proposals by status:", result["by_status"])
            for conflict in workspace.conflicts_for():
                print(f"[{conflict['severity']}] {conflict['message']}")
            _print_proposals(workspace, None)
            print("Current balances:", ", ".join(f"{ACCOUNT_LABELS[a]} {money(v)}" for a, v in workspace.state["balances"].items()))
        elif args.cmd == "proposals":
            _print_proposals(workspace, args.status)
        elif args.cmd in ("approve", "reject"):
            decision = "approved" if args.cmd == "approve" else "rejected"
            print(workspace.decide(args.proposal_id, decision, actor=args.actor, note=args.note)["message"] + ".")
        elif args.cmd == "explain":
            print(workspace.explain(args.proposal_id))
        elif args.cmd == "audit":
            for entry in workspace.audit.entries()[-args.tail:]:
                print(f"#{entry['seq']:<4} {entry['ts']} {entry['type']:<20} {json.dumps(entry['data'], ensure_ascii=False)[:140]}")
        elif args.cmd == "verify":
            ok, message = workspace.verify_audit()
            print(message)
            return 0 if ok else 1
        elif args.cmd == "demo":
            if workspace.state["rules"]:
                print("Data directory already has rules. Run 'reset' first.", file=sys.stderr)
                return 1
            for line in EXAMPLE_RULES.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    _print_rule(workspace.submit_rule(line.strip(), actor="demo-operator"))
            for rule in list(workspace.state["rules"].values()):
                if rule["status"] == "pending_review":
                    workspace.approve_rule(rule["id"], actor="demo-approver")
            result = workspace.run()
            print("Proposals by status:", result["by_status"])
            _print_proposals(workspace, None)
        elif args.cmd == "web":
            from .web import serve
            serve(workspace, "localhost", args.port)
    except (KeyError, ValueError) as exc:
        print(f"Refused: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    return 0
