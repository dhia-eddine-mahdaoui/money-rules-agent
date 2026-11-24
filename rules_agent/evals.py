"""Evaluation harness for the rule-understanding agent.

Labelled sentences live in evals/rule_cases.json. Each case has an expected outcome
(ok with an exact spec, clarify, or reject). Metrics:

* exact-spec accuracy: of cases expected to parse, how many produced exactly the labelled spec;
* clarify recall / precision: asks a question when it should, and only then;
* reject recall: refuses what must be refused;
* unsafe-accept rate: of cases that must NOT be accepted (clarify or reject), how many were
  accepted. This must be 0;
* false-reject rate: valid rules that were refused (a usability cost, not a safety failure).

A second block runs rule pairs through the conflict detector.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .agent import RuleAgent
from .safety import static_conflicts

CASES_PATH = Path(__file__).resolve().parents[1] / "evals" / "rule_cases.json"


def load_cases(path: Path | None = None) -> dict:
    return json.loads((path or CASES_PATH).read_text(encoding="utf-8"))


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def run_cases(cases: list[dict], agent: RuleAgent) -> list[dict]:
    rows = []
    for case in cases:
        result = agent.parse(case["text"])
        passed = result.status == case["expect"] and (case["expect"] != "ok" or result.spec == case["spec"])
        rows.append({
            "id": case["id"], "category": case["category"], "text": case["text"], "expect": case["expect"],
            "got": result.status, "source": result.source, "passed": passed,
            "spec_exact": case["expect"] == "ok" and result.status == "ok" and result.spec == case["spec"],
            "detail": result.question or "; ".join(result.messages) or "",
            "expected_spec": case.get("spec"), "got_spec": result.spec,
        })
    return rows


def metrics(rows: list[dict]) -> dict:
    ok_cases = [r for r in rows if r["expect"] == "ok"]
    clarify_cases = [r for r in rows if r["expect"] == "clarify"]
    reject_cases = [r for r in rows if r["expect"] == "reject"]
    must_not_accept = clarify_cases + reject_cases
    asked = [r for r in rows if r["got"] == "clarify"]
    return {
        "cases": len(rows),
        "status_accuracy": _rate(sum(r["got"] == r["expect"] for r in rows), len(rows)),
        "exact_spec_accuracy": _rate(sum(r["spec_exact"] for r in ok_cases), len(ok_cases)),
        "false_reject_rate": _rate(sum(r["got"] == "reject" for r in ok_cases), len(ok_cases)),
        "clarify_recall": _rate(sum(r["got"] == "clarify" for r in clarify_cases), len(clarify_cases)),
        "clarify_precision": _rate(sum(r["expect"] == "clarify" for r in asked), len(asked)),
        "reject_recall": _rate(sum(r["got"] == "reject" for r in reject_cases), len(reject_cases)),
        "unsafe_accepts": sum(r["got"] == "ok" for r in must_not_accept),
        "unsafe_accept_rate": _rate(sum(r["got"] == "ok" for r in must_not_accept), len(must_not_accept)),
        "counts": {"ok": len(ok_cases), "clarify": len(clarify_cases), "reject": len(reject_cases)},
    }


def run_conflicts(scenarios: list[dict], agent: RuleAgent) -> list[dict]:
    out = []
    for sc in scenarios:
        rules = []
        for i, text in enumerate(sc["rules"], start=1):
            result = agent.parse(text)
            if result.status == "ok":
                rules.append({"id": f"R{i}", "spec": result.spec})
        found = static_conflicts(rules)
        severities = {c["severity"] for c in found}
        got = "error" if "error" in severities else "warning" if "warning" in severities else "none"
        out.append({"id": sc["id"], "rules": sc["rules"], "expect": sc["expect"], "got": got, "parsed": len(rules),
                    "passed": got == sc["expect"] and len(rules) == len(sc["rules"])})
    return out


def run_all(path: Path | None = None) -> dict:
    data = load_cases(path)
    full = run_cases(data["cases"], RuleAgent())
    split = {name: metrics([r for r, c in zip(full, data["cases"]) if c.get("split") == name])
             for name in ("dev", "holdout")}
    grammar_only = run_cases(data["cases"], RuleAgent(use_model=False))
    by_category = defaultdict(lambda: Counter())
    for row in full:
        by_category[row["category"]]["total"] += 1
        by_category[row["category"]]["passed"] += row["passed"]
    conflicts = run_conflicts(data.get("conflicts", []), RuleAgent())
    return {
        "full": {"metrics": metrics(full), "rows": full},
        "grammar_only": {"metrics": metrics(grammar_only), "rows": grammar_only},
        "split": split,
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "conflicts": {"rows": conflicts, "passed": sum(c["passed"] for c in conflicts), "total": len(conflicts)},
    }


def format_report(report: dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = []
    for name, title in (("grammar_only", "Strict grammar only"), ("full", "Full agent (grammar, offline model, validation)")):
        m = report[name]["metrics"]
        lines += [
            f"{title}: {m['cases']} cases ({m['counts']['ok']} ok, {m['counts']['clarify']} clarify, {m['counts']['reject']} reject)",
            f"  exact-spec accuracy  {pct(m['exact_spec_accuracy'])}",
            f"  false-reject rate    {pct(m['false_reject_rate'])}",
            f"  clarify recall       {pct(m['clarify_recall'])}   precision {pct(m['clarify_precision'])}",
            f"  reject recall        {pct(m['reject_recall'])}",
            f"  unsafe-accept rate   {pct(m['unsafe_accept_rate'])} ({m['unsafe_accepts']} accepted)",
            f"  status accuracy      {pct(m['status_accuracy'])}",
        ]
    for name, m in report["split"].items():
        lines.append(f"  {name} split ({m['cases']} cases): exact-spec {pct(m['exact_spec_accuracy'])}, status {pct(m['status_accuracy'])}, "
                     f"clarify recall {pct(m['clarify_recall'])}, unsafe accepts {m['unsafe_accepts']}")
    lines.append("By category (full agent): " + ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in report["by_category"].items()))
    c = report["conflicts"]
    lines.append(f"Conflict detection: {c['passed']}/{c['total']} scenarios")
    failures = [r for r in report["full"]["rows"] if not r["passed"]]
    if failures:
        lines.append("Failures:")
        for r in failures:
            lines.append(f"  {r['id']} expected {r['expect']}, got {r['got']}: {r['text']!r}")
    return "\n".join(lines)
