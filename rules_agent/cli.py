"""Command-line interface. Run with: python -m rules_agent --help"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .llm import LLMError, provider_from_env
from .ledger import ACCOUNT_LABELS
from .service import Workspace, label, money

EXAMPLE_RULES = Path(__file__).resolve().parents[1] / "examples" / "rules.txt"


def _print_rule(rule: dict) -> None:
    print(f"{rule['id']} [{rule['status']}] {rule['text']}")
    if rule["status"] == "invalid":
        if rule["question"]:
            print(f"    clarify: {rule['question']}")
        for message in rule["messages"]:
            print(f"    rejected: {message}")


def _print_proposals(workspace: Workspace, status: str | None) -> None:
    proposals = sorted(workspace.state["proposals"].values(), key=lambda p: (p["date"], p["id"]))
    if status:
        proposals = [p for p in proposals if p["status"] == status]
    if not proposals:
        print("No proposals.")
        return
    for p in proposals:
        print(f"{p['id']}  {p['status']:<16} {money(p['amount_gbp']):>10}  {label(p['from'])} -> "
              f"{label(p['to'])}  {p['date']}")
        if p["reasons"]:
            print(f"      {'; '.join(p['reasons'])}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rules_agent", description="Plain-English money rules (simulation only).")
    parser.add_argument("--data-dir", default=os.environ.get("RULES_DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="submit a plain-English rule")
    add.add_argument("text", nargs="+")
    add.add_argument("--no-llm", action="store_true", help="never use the optional LLM backend")

    sub.add_parser("rules", help="list rules")
    approve_rule = sub.add_parser("approve-rule", help="approve a rule so its small transfers auto-execute")
    approve_rule.add_argument("rule_id")
    sub.add_parser("run", help="simulate all live rules over the synthetic month")

    proposals = sub.add_parser("proposals", help="list proposed transfers")
    proposals.add_argument("--status")
    for name in ("approve", "reject"):
        decide = sub.add_parser(name, help=f"{name} a proposed transfer")
        decide.add_argument("proposal_id")
        decide.add_argument("--note", default="")
        decide.add_argument("--actor", default="cli")

    explain = sub.add_parser("explain", help="trace a transfer back to the rule text")
    explain.add_argument("proposal_id")

    audit = sub.add_parser("audit", help="show the audit trail")
    audit.add_argument("--tail", type=int, default=20)
    sub.add_parser("verify", help="verify the audit hash chain")
    sub.add_parser("demo", help="load the example rules, approve them and run the simulation")
    sub.add_parser("reset", help="delete local state and audit log in the data directory")

    web = sub.add_parser("web", help="serve the local web page (localhost only)")
    web.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "reset":
        data_dir = Path(args.data_dir)
        for name in ("state.json", "audit.jsonl"):
            (data_dir / name).unlink(missing_ok=True)
        print(f"Cleared state and audit log in {data_dir}")
        return 0
    try:
        provider = provider_from_env()
    except LLMError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 2
    workspace = Workspace(args.data_dir, provider=provider)

    if args.cmd == "add":
        rule = workspace.submit_rule(" ".join(args.text), use_llm=not args.no_llm)
        _print_rule(rule)
    elif args.cmd == "rules":
        if not workspace.state["rules"]:
            print("No rules yet. Add one with: add \"When my salary arrives, move 20% to savings\"")
        for rule in workspace.state["rules"].values():
            _print_rule(rule)
    elif args.cmd == "approve-rule":
        workspace.approve_rule(args.rule_id)
        print(f"{args.rule_id} approved; simulation re-run.")
        _print_proposals(workspace, None)
    elif args.cmd == "run":
        summary = workspace.run()
        print("Proposals by status:", summary["by_status"])
        for conflict in summary["conflicts"]:
            print(f"[{conflict['severity']}] {conflict['message']}")
        _print_proposals(workspace, None)
        balances = ", ".join(f"{ACCOUNT_LABELS[a]} {money(v)}" for a, v in summary["final_balances"].items())
        print("Projected end-of-month balances:", balances)
    elif args.cmd == "proposals":
        _print_proposals(workspace, args.status)
    elif args.cmd in ("approve", "reject"):
        decision = "approved" if args.cmd == "approve" else "rejected"
        workspace.decide(args.proposal_id, decision, actor=args.actor, note=args.note)
        print(f"{args.proposal_id} {decision}; simulation re-run.")
    elif args.cmd == "explain":
        print(workspace.explain(args.proposal_id))
    elif args.cmd == "audit":
        for entry in workspace.audit.entries()[-args.tail:]:
            print(f"#{entry['seq']:<4} {entry['ts']} {entry['type']:<18} {entry['data']}")
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
                _print_rule(workspace.submit_rule(line.strip()))
        for rule in list(workspace.state["rules"].values()):
            if rule["status"] == "pending_review":
                workspace.approve_rule(rule["id"], actor="demo-operator")
        summary = workspace.run()
        print("Proposals by status:", summary["by_status"])
        _print_proposals(workspace, None)
    elif args.cmd == "web":
        from .web import serve
        serve(workspace, "localhost", args.port)
    return 0
