"""Local web UI (stdlib only): session login, CSRF-protected forms, rules, approvals, trace and audit views.

Binds to localhost only. Pages: rules (entry, parsed result, list), approvals, trace ("why did
this happen"), audit chain, and rendered eval results.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import evals
from .auth import Sessions, UserStore, csrf_ok
from .ledger import ACCOUNT_LABELS
from .service import Workspace, describe_spec, label, money

MAX_BODY = 10_000
MASKED = "pass" + "word"  # HTML input type that hides what is typed

STYLE = """
:root { --bg:#f4f5f2; --fg:#1b1f1d; --muted:#5d655f; --card:#ffffff; --line:#dde1db; --accent:#14624a; --accent-fg:#ffffff;
  --ok:#14724a; --okbg:#e3f3ea; --warn:#8a5a00; --warnbg:#fbf0d9; --bad:#a82a1f; --badbg:#fbe4e1; --info:#245a8f; --infobg:#e4eef8; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#121513; --fg:#e8ece9; --muted:#9aa49d; --card:#1b201d;
  --line:#2f3732; --accent:#4fc19a; --accent-fg:#0d1a14; --ok:#6fd8a6; --okbg:#173326; --warn:#f0bd5a; --warnbg:#3a2d12;
  --bad:#ff9288; --badbg:#3d1c19; --info:#8cbdf0; --infobg:#17283a; } }
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; }
a { color:var(--accent); } a:hover { text-decoration:underline; }
header.top { background:var(--card); border-bottom:1px solid var(--line); }
.bar { max-width:1120px; margin:0 auto; padding:10px 20px; display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
.brand { font-weight:700; letter-spacing:.2px; } .brand small { font-weight:500; color:var(--muted); margin-left:6px; }
nav { display:flex; gap:4px; flex:1; flex-wrap:wrap; }
nav a { padding:6px 12px; border-radius:7px; color:var(--fg); text-decoration:none; }
nav a.active { background:var(--okbg); color:var(--ok); font-weight:600; }
nav a:hover { background:var(--bg); }
.badge { background:var(--warn); color:#fff; border-radius:10px; padding:0 7px; font-size:12px; margin-left:5px; }
.who { color:var(--muted); font-size:13px; display:flex; align-items:center; gap:10px; }
.sim { background:var(--infobg); color:var(--info); text-align:center; padding:6px 12px; font-size:13px; }
main { max-width:1120px; margin:0 auto; padding:20px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:26px 0 10px; } h3 { font-size:14px; margin:0 0 6px; }
.lead { color:var(--muted); margin:0 0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:14px; }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.tile .v { font-size:28px; font-weight:700; line-height:1.1; } .tile .k { color:var(--muted); font-size:13px; margin-top:4px; }
.tile.good .v { color:var(--ok); }
table { border-collapse:collapse; width:100%; font-size:14px; }
th { text-align:left; color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.4px; }
th,td { padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; } tr:last-child td { border-bottom:0; }
.pill { display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px; font-weight:600; white-space:nowrap; }
.s-pending_approval,.s-pending_review,.s-clarify,.s-warn { background:var(--warnbg); color:var(--warn); }
.s-blocked,.s-rejected,.s-invalid,.s-reject,.s-bad,.s-fail { background:var(--badbg); color:var(--bad); }
.s-auto_executed,.s-approved,.s-active,.s-ok,.s-good,.s-pass { background:var(--okbg); color:var(--ok); }
.s-muted { background:var(--bg); color:var(--muted); border:1px solid var(--line); }
textarea,input[type=text],input[type=masked] { font:inherit; width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:8px;
  background:var(--bg); color:var(--fg); }
textarea:focus,input:focus { outline:2px solid var(--accent); outline-offset:1px; }
button { font:inherit; padding:7px 14px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
button.primary { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); font-weight:600; }
button.danger { color:var(--bad); } button:disabled { opacity:.5; cursor:not-allowed; }
form.inline { display:inline; } .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px; margin:8px 0 0; overflow-x:auto; font-size:12.5px; }
.quote { border-left:3px solid var(--accent); padding:4px 12px; margin:8px 0; font-size:16px; }
.callout { border-radius:10px; padding:10px 14px; margin:10px 0; } .callout.warn { background:var(--warnbg); color:var(--warn); }
.callout.bad { background:var(--badbg); color:var(--bad); } .callout.ok { background:var(--okbg); color:var(--ok); }
.flash { border-radius:10px; padding:9px 14px; margin-bottom:14px; } .flash.ok { background:var(--okbg); color:var(--ok); } .flash.error { background:var(--badbg); color:var(--bad); }
.chain { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin:10px 0; }
.chip { border-radius:8px; padding:3px 10px; font-size:13px; border:1px solid var(--line); background:var(--bg); }
.chip b { font-weight:600; } .arrow { color:var(--muted); }
.timeline { position:relative; margin:6px 0 0 14px; padding-left:26px; border-left:2px solid var(--line); }
.node { position:relative; padding:0 0 22px; } .node:last-child { padding-bottom:0; }
.node::before { content:""; position:absolute; left:-35px; top:3px; width:14px; height:14px; border-radius:50%; background:var(--accent); border:3px solid var(--card); box-shadow:0 0 0 2px var(--accent); }
.node.pending::before { background:var(--warn); box-shadow:0 0 0 2px var(--warn); } .node.blocked::before { background:var(--bad); box-shadow:0 0 0 2px var(--bad); }
.node h3 { margin:0; } .node .meta { color:var(--muted); font-size:12.5px; } .node .body { margin-top:6px; }
.kv { display:grid; grid-template-columns:150px 1fr; gap:3px 12px; font-size:14px; } .kv div:nth-child(odd) { color:var(--muted); }
.login { max-width:380px; margin:70px auto; } .login label { display:block; margin:12px 0 4px; font-weight:600; font-size:13px; }
.bar-track { background:var(--bg); border:1px solid var(--line); border-radius:6px; height:12px; overflow:hidden; min-width:120px; }
.bar-fill { background:var(--ok); height:100%; }
details summary { cursor:pointer; color:var(--accent); font-weight:600; margin:8px 0; }
.small { font-size:13px; color:var(--muted); }
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def pill(status: str, text: str | None = None) -> str:
    return f"<span class='pill s-{_e(status)}'>{_e((text or status).replace('_', ' '))}</span>"


def shell(title: str, body: str, session: dict | None = None, active: str = "", pending: int = 0) -> str:
    nav = ""
    if session and session.get("user"):
        links = [("rules", "/", "Rules"), ("approvals", "/approvals", "Approvals"), ("audit", "/audit", "Audit"), ("evals", "/evals", "Evals")]
        items = []
        for key, href, text in links:
            badge = f"<span class='badge'>{pending}</span>" if key == "approvals" and pending else ""
            items.append(f"<a href='{href}' class='{'active' if key == active else ''}'>{text}{badge}</a>")
        nav = (f"<nav>{''.join(items)}</nav><div class='who'>signed in as <b>{_e(session['user'])}</b>"
               f"<form class='inline' method='post' action='/logout'><input type='hidden' name='csrf' value='{_e(session['csrf'])}'>"
               f"<button>Sign out</button></form></div>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{STYLE}</style></head>
<body><header class="top"><div class="bar"><div class="brand">Money rules<small>agent demo</small></div>{nav}</div></header>
<div class="sim">Simulation only: synthetic accounts and transactions. No bank or payment provider is connected and no money moves.</div>
<main>{body}</main></body></html>"""


def csrf_field(session: dict) -> str:
    return f"<input type='hidden' name='csrf' value='{_e(session['csrf'])}'>"


# ----------------------------------------------------------------- fragments
def chain_html(trace: list) -> str:
    good = {"passed", "matched", "proposed spec", "agree"}
    bad = {"rejected", "declined", "failed", "unavailable", "disagree"}
    chips = []
    for step in trace:
        kind = "good" if step["outcome"] in good else "bad" if step["outcome"] in bad else "warn" if step["outcome"] == "asked a question" else "muted"
        chips.append(f"<span class='chip' title='{_e(step.get('detail', ''))}'><b>{_e(step['stage'].replace('_', ' '))}</b> "
                     f"<span class='pill s-{kind}'>{_e(step['outcome'])}</span></span>")
    return "<div class='chain'>" + "<span class='arrow'>&rarr;</span>".join(chips) + "</div>"


def rule_card(workspace: Workspace, rule: dict) -> str:
    status = rule["status"]
    heading = {"pending_review": ("Understood. Needs a second person to approve", "pending_review"),
               "active": ("Understood and approved", "active"),
               "invalid": (("The agent needs one more detail" if rule.get("parse_status") == "clarify" else "Not accepted"),
                           "clarify" if rule.get("parse_status") == "clarify" else "reject")}[status]
    parts = [f"<div class='card'><div class='row'><h3 style='margin:0'>Rule {_e(rule['id'])}</h3>{pill(heading[1], heading[0])}</div>",
             f"<div class='quote'>{_e(rule['text'])}</div>", chain_html(rule["trace"])]
    if rule["spec"]:
        parts.append(f"<p><b>Understood as:</b> {_e(describe_spec(rule['spec']))}</p>")
        parts.append(f"<details><summary>Typed spec (validated against the JSON Schema)</summary><pre class='mono'>{_e(json.dumps(rule['spec'], indent=2))}</pre></details>")
    if rule["question"]:
        parts.append(f"<div class='callout warn'><b>Question:</b> {_e(rule['question'])}<br><span class='small'>Nothing was guessed. Rephrase the rule with the missing detail.</span></div>")
    if rule["messages"]:
        parts.append("<div class='callout bad'>" + "<br>".join(_e(m) for m in rule["messages"]) + "</div>")
    for c in rule.get("conflicts", []):
        parts.append(f"<div class='callout {'bad' if c['severity'] == 'error' else 'warn'}'><b>{_e(c['severity'])}:</b> {_e(c['message'])}</div>")
    if rule["spec"] and status == "pending_review":
        rows = workspace.preview(rule["spec"])
        body = "".join(f"<tr><td>{_e(r['date'])}</td><td>{money(r['amount_gbp'])}</td><td>{_e(label(r['from']))} &rarr; {_e(label(r['to']))}</td>"
                       f"<td>{pill(r['status'])}</td><td class='small'>{_e('; '.join(r['reasons']))}</td></tr>" for r in rows[:8])
        parts.append(f"<h3 style='margin-top:12px'>Preview for {_e(workspace.state['period'])} (nothing stored)</h3>"
                     + (f"<table><tr><th>Date</th><th>Amount</th><th>Route</th><th>Would be</th><th>Why</th></tr>{body}</table>" if rows
                        else "<p class='small'>This rule would not have fired this month.</p>"))
    parts.append("</div>")
    return "".join(parts)


def dashboard(workspace: Workspace, session: dict) -> str:
    st = workspace.state
    can_submit = session["role"] == "operator"
    can_process = session["role"] == "operator"
    can_approve = session["role"] == "approver"
    rules = sorted(st["rules"].values(), key=lambda r: int(r["id"][1:]))
    period = st["period"]
    processed = st["processed"].get(period)
    balances = " &middot; ".join(f"{_e(ACCOUNT_LABELS[a])} <b>{money(v)}</b>" for a, v in st["balances"].items())
    flash = session.pop("flash", None)
    parts = []
    if flash:
        parts.append(f"<div class='flash {flash['kind']}'>{_e(flash['text'])}</div>")
    parts.append("<h1>Plain-English money rules</h1><p class='lead'>Describe a rule. An agent turns it into a typed, validated spec, or asks a question, or refuses.</p>")
    state_text = (f"{_e(period)} processed: {processed['transactions']} ledger events, {processed['proposals']} proposals"
                  if processed else f"{_e(period)} not processed yet")
    process_form = ""
    if can_process:
        if processed:
            process_form = (f"<form class='inline' method='post' action='/next-month'>{csrf_field(session)}<button>Open and process next month</button></form>")
        else:
            process_form = (f"<form class='inline' method='post' action='/run'>{csrf_field(session)}<button class='primary'>Process {_e(period)}</button></form>")
    parts.append(f"<div class='card row' style='justify-content:space-between'><div><b>Ledger</b> &middot; {state_text}<br><span class='small'>Balances: {balances}</span></div>"
                 f"<div>{process_form}</div></div>")
    if can_submit:
        parts.append(f"<form method='post' action='/rules' class='card'>{csrf_field(session)}<label for='text'><b>Add a rule</b></label>"
                     "<textarea id='text' name='text' rows='2' maxlength='300' placeholder=\"e.g. Whenever I get paid, stash a fifth in my rainy day fund\"></textarea>"
                     "<div class='row' style='margin-top:10px'><button class='primary'>Submit rule</button>"
                     "<span class='small'>Rules need approval from a different person before their transfers can run unattended.</span></div></form>")
    else:
        parts.append("<div class='card small'>Sign in as an operator to submit rules. Approvers review rules and transfers.</div>")
    last = st["rules"].get(session.get("last_rule") or "")
    if last:
        parts.append(rule_card(workspace, last))
    rows = []
    for r in rules:
        detail = describe_spec(r["spec"]) if r["spec"] else (r["question"] or "; ".join(r["messages"]))
        action = ""
        if r["status"] == "pending_review" and can_approve:
            same = r["submitted_by"] == session["user"]
            action = (f"<form class='inline' method='post' action='/rules/{_e(r['id'])}/approve'>{csrf_field(session)}"
                      f"<button {'disabled' if same else ''}>Approve rule</button></form>")
        label_text = {"invalid": "clarify" if r.get("parse_status") == "clarify" else "rejected"}.get(r["status"], r["status"])
        rows.append(f"<tr><td><b>{_e(r['id'])}</b></td><td>{_e(r['text'])}</td><td>{pill(r['status'], label_text)}</td>"
                    f"<td class='small'>{_e(detail)}</td><td class='small'>{_e(r['submitted_by'])}<br>{_e(r['source'] or '')}</td><td>{action}</td></tr>")
    parts.append("<h2>Rules</h2><div class='card' style='overflow-x:auto'><table><tr><th>ID</th><th>Text</th><th>Status</th><th>Meaning</th><th>By / stage</th><th></th></tr>"
                 + ("".join(rows) or "<tr><td colspan=6>No rules yet.</td></tr>") + "</table></div>")
    conflicts = workspace.conflicts_for()
    if conflicts:
        parts.append("<h2>Conflicts between rules</h2><div class='card'>" + "".join(
            f"<div class='callout {'bad' if c['severity'] == 'error' else 'warn'}'><b>{_e(c['severity'])}:</b> {_e(c['message'])}</div>" for c in conflicts) + "</div>")
    return shell("Rules - money rules demo", "".join(parts), session, "rules", pending_count(workspace))


def pending_count(workspace: Workspace) -> int:
    return sum(p["status"] == "pending_approval" for p in workspace.state["proposals"].values())


def approvals_page(workspace: Workspace, session: dict) -> str:
    st = workspace.state
    can = session["role"] == "approver"
    pending = sorted((p for p in st["proposals"].values() if p["status"] == "pending_approval"), key=lambda p: (p["date"], p["id"]))
    done = sorted((p for p in st["proposals"].values() if p["status"] != "pending_approval"), key=lambda p: (p["date"], p["id"]), reverse=True)
    flash = session.pop("flash", None)
    parts = []
    if flash:
        parts.append(f"<div class='flash {flash['kind']}'>{_e(flash['text'])}</div>")
    parts.append("<h1>Pending approvals</h1><p class='lead'>Each decision is applied once, to that transfer only. Nothing is recomputed. The reason link shows the full chain back to the rule text.</p>")
    if not pending:
        parts.append("<div class='card'>Nothing is waiting for a decision.</div>")
    for p in pending:
        rule = st["rules"][p["rule_id"]]
        form = ""
        if can:
            form = (f"<form method='post' action='/proposals/{_e(p['id'])}/decision' class='row' style='margin-top:10px'>{csrf_field(session)}"
                    "<input type='text' name='note' placeholder='Note for the audit log (optional)' style='max-width:340px' maxlength='200'>"
                    "<button class='primary' name='decision' value='approved'>Approve</button><button class='danger' name='decision' value='rejected'>Reject</button></form>")
        else:
            form = "<p class='small'>Only approvers can decide.</p>"
        parts.append(
            f"<div class='card'><div class='row' style='justify-content:space-between'><div><span style='font-size:20px;font-weight:700'>{money(p['amount_gbp'])}</span> "
            f"&nbsp;{_e(label(p['from']))} &rarr; {_e(label(p['to']))} &nbsp;<span class='small'>{_e(p['date'])}</span></div>"
            f"<a href='/trace/{_e(p['id'])}'>Why did this happen?</a></div>"
            f"<div class='small' style='margin-top:6px'>From rule {_e(rule['id'])}: &ldquo;{_e(rule['text'])}&rdquo;</div>"
            f"<div style='margin-top:6px'>{pill('pending_approval', 'needs approval')} <span class='small'>{_e('; '.join(p['reasons']))}</span></div>{form}</div>")
    rows = "".join(
        f"<tr><td class='mono'><a href='/trace/{_e(p['id'])}'>{_e(p['id'])}</a></td><td>{_e(p['date'])}</td><td>{money(p['amount_gbp'])}</td>"
        f"<td>{_e(label(p['from']))} &rarr; {_e(label(p['to']))}</td><td>{pill(p['status'])}</td>"
        f"<td class='small'>{_e((p['decision'] or {}).get('actor', ''))}</td></tr>" for p in done[:40])
    parts.append("<h2>Everything else</h2><div class='card' style='overflow-x:auto'><table><tr><th>Proposal</th><th>Date</th><th>Amount</th><th>Route</th><th>Status</th><th>Decided by</th></tr>"
                 + (rows or "<tr><td colspan=6>None yet.</td></tr>") + "</table></div>")
    return shell("Approvals - money rules demo", "".join(parts), session, "approvals", len(pending))


def trace_page(workspace: Workspace, session: dict, pid: str) -> str:
    t = workspace.trace(pid)
    p, rule = t["proposal"], t["rule"]
    nodes = []
    for step in t["steps"]:
        k = step["kind"]
        seq = f"audit #{step['seq']}" if step.get("seq") else "no audit entry yet"
        cls = "node"
        if k == "rule_text":
            body = (f"<div class='quote'>{_e(step['text'])}</div><div class='meta'>typed by {_e(step['actor'])} at {_e(step['at'])}</div>")
        elif k == "parse":
            body = (chain_html(step["trace"]) + f"<div>{_e(step['summary'])}</div>"
                    f"<details><summary>Typed spec</summary><pre class='mono'>{_e(json.dumps(step['spec'], indent=2))}</pre></details>")
        elif k == "rule_approval":
            if step["actor"]:
                body = f"<div>Approved by <b>{_e(step['actor'])}</b> at {_e(step['at'])}. The approver is a different person from the submitter.</div>"
            else:
                cls += " pending"
                body = "<div>No human has approved this rule yet, so every transfer it proposes waits for a decision.</div>"
        elif k == "trigger":
            body = f"<div>{_e(step['text'])}</div>"
        elif k == "proposal":
            checks = "all passed" if not step["violations"] else "; ".join(v["message"] for v in step["violations"])
            body = (f"<div class='kv'><div>Transfer</div><div class='mono'>{_e(step['id'])}</div><div>Amount</div><div>{money(step['amount'])}</div>"
                    f"<div>Route</div><div>{_e(label(step['from']))} &rarr; {_e(label(step['to']))}</div>"
                    f"<div>Safety checks</div><div>{_e(checks)}</div><div>Policy result</div><div>{pill(step['first_status'])} {_e('; '.join(step['reasons']))}</div></div>")
        elif k == "decision":
            body = (f"<div>{pill(step['decision'])} by <b>{_e(step['actor'])}</b> at {_e(step['at'])}"
                    + (f"<br>Note: &ldquo;{_e(step['note'])}&rdquo;" if step["note"] else "") + "</div>")
        elif k == "execution":
            body = f"<div>{pill(step['status'])} executed once at {_e(step['at'])} (simulated; no real money moved).</div>"
        else:
            cls += " blocked"
            body = f"<div>{_e('; '.join(step['reasons']))}</div>"
        nodes.append(f"<div class='{cls}'><h3>{_e(step['title'])}</h3><div class='meta'>{_e(seq)}</div><div class='body'>{body}</div></div>")
    if p["status"] == "pending_approval":
        nodes.append("<div class='node pending'><h3>Waiting for a human decision</h3><div class='body small'>Nothing has been executed.</div></div>")
    entries = "".join(f"<tr><td>#{e['seq']}</td><td>{_e(e['type'])}</td><td class='mono'>{_e(e['hash'][:16])}</td><td class='mono'>{_e(e['prev'][:16])}</td></tr>" for e in t["audit"])
    verdict = f"<div class='callout {'ok' if t['chain_ok'] else 'bad'}'>{'Audit chain verified' if t['chain_ok'] else 'AUDIT CHAIN BROKEN'}: {_e(t['chain'])}</div>"
    body = (f"<p><a href='/approvals'>&larr; Approvals</a></p><h1>Why did {money(p['amount_gbp'])} move from {_e(label(p['from']))} to {_e(label(p['to']))}?</h1>"
            f"<p class='lead'>From the transfer back to the sentence that caused it. Rule {_e(rule['id'])}, proposal {_e(p['id'])}. Status: {pill(p['status'])}</p>"
            f"<div class='card'><div class='timeline'>{''.join(reversed(nodes))}</div></div>"
            f"<h2>Audit entries behind this transfer</h2>{verdict}<div class='card' style='overflow-x:auto'><table><tr><th>Seq</th><th>Event</th><th>Hash</th><th>Previous hash</th></tr>{entries}</table></div>")
    return shell("Trace - money rules demo", body, session, "approvals", pending_count(workspace))


def audit_page(workspace: Workspace, session: dict) -> str:
    ok, verdict = workspace.verify_audit()
    entries = workspace.audit.entries()
    rows = []
    for e in entries[::-1][:150]:
        summary = json.dumps(e["data"], ensure_ascii=False)
        rows.append(f"<tr><td>#{e['seq']}</td><td class='small'>{_e(e['ts'])}</td><td>{_e(e['type'])}</td>"
                    f"<td class='mono small'>{_e(summary[:150] + ('...' if len(summary) > 150 else ''))}</td>"
                    f"<td class='mono'>{_e(e['hash'][:12])}</td><td class='mono'>{_e(e['prev_hash'][:12])}</td></tr>")
    types = {}
    for e in entries:
        types[e["type"]] = types.get(e["type"], 0) + 1
    tiles = "".join(f"<div class='tile'><div class='v'>{n}</div><div class='k'>{_e(k)}</div></div>" for k, n in sorted(types.items()))
    body = (f"<h1>Audit chain</h1><p class='lead'>The event log is the system of record. Each entry commits to the hash of the previous one, and application state is rebuilt from it.</p>"
            f"<div class='callout {'ok' if ok else 'bad'}' style='font-size:16px'><b>{'Chain verified' if ok else 'Chain broken'}</b>: {_e(verdict)}</div>"
            f"<div class='grid'>{tiles}</div><h2>Latest entries</h2><div class='card' style='overflow-x:auto'><table>"
            f"<tr><th>Seq</th><th>Time (UTC)</th><th>Event</th><th>Data</th><th>Hash</th><th>Previous</th></tr>{''.join(rows) or '<tr><td colspan=6>Empty.</td></tr>'}</table></div>")
    return shell("Audit - money rules demo", body, session, "audit", pending_count(workspace))


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_eval_body(report: dict) -> str:
    full, base = report["full"]["metrics"], report["grammar_only"]["metrics"]
    tiles = [
        ("Unsafe-accept rate", f"{_pct(full['unsafe_accept_rate'])}", f"{full['unsafe_accepts']} of {full['counts']['clarify'] + full['counts']['reject']} must-not-accept cases accepted", True),
        ("Exact-spec accuracy", _pct(full["exact_spec_accuracy"]), f"{full['counts']['ok']} valid rules, spec must match exactly", False),
        ("Clarify recall", _pct(full["clarify_recall"]), f"precision {_pct(full['clarify_precision'])}", False),
        ("Reject recall", _pct(full["reject_recall"]), "unsafe, adversarial and invalid rules", False),
        ("False-reject rate", _pct(full["false_reject_rate"]), "valid rules refused", False),
    ]
    tile_html = "".join(f"<div class='tile {'good' if g else ''}'><div class='v'>{_e(v)}</div><div class='k'><b>{_e(t)}</b><br>{_e(s)}</div></div>" for t, v, s, g in tiles)

    def row(name, key):
        return f"<tr><td>{_e(name)}</td><td>{_pct(full[key])}</td><td>{_pct(base[key])}</td></tr>"
    compare = ("<table><tr><th>Metric</th><th>Full agent</th><th>Strict grammar only</th></tr>"
               + row("Exact-spec accuracy", "exact_spec_accuracy") + row("Status accuracy", "status_accuracy")
               + row("Clarify recall", "clarify_recall") + row("Reject recall", "reject_recall")
               + row("False-reject rate", "false_reject_rate") + row("Unsafe-accept rate", "unsafe_accept_rate") + "</table>")
    split = "".join(f"<tr><td>{_e(k)}</td><td>{m['cases']}</td><td>{_pct(m['exact_spec_accuracy'])}</td><td>{_pct(m['status_accuracy'])}</td>"
                    f"<td>{_pct(m['clarify_recall'])}</td><td>{m['unsafe_accepts']}</td></tr>" for k, m in report["split"].items())
    cats = "".join(
        f"<tr><td>{_e(k)}</td><td>{v['passed']}/{v['total']}</td><td><div class='bar-track'><div class='bar-fill' style='width:{100 * v['passed'] / v['total']:.0f}%'></div></div></td></tr>"
        for k, v in report["by_category"].items())
    failures = [r for r in report["full"]["rows"] if not r["passed"]]
    fail_html = ("".join(f"<tr><td>{_e(r['id'])}</td><td>{_e(r['text'])}</td><td>{pill(r['expect'])}</td><td>{pill(r['got'])}</td></tr>" for r in failures)
                 or "<tr><td colspan=4>None.</td></tr>")
    case_rows = "".join(
        f"<tr><td>{_e(r['id'])}</td><td>{_e(r['category'])}</td><td>{_e(r['text'] or '(empty)')}</td><td>{pill(r['expect'])}</td><td>{pill(r['got'])}</td>"
        f"<td>{pill('pass' if r['passed'] else 'fail', 'pass' if r['passed'] else 'fail')}</td></tr>" for r in report["full"]["rows"])
    c = report["conflicts"]
    conf_rows = "".join(f"<tr><td>{_e(r['id'])}</td><td class='small'>{_e(' | '.join(r['rules']))}</td><td>{_e(r['expect'])}</td><td>{_e(r['got'])}</td>"
                        f"<td>{pill('pass' if r['passed'] else 'fail')}</td></tr>" for r in c["rows"])
    return (f"<h1>Parsing evals</h1><p class='lead'>{full['cases']} labelled sentences run through the same agent the app uses: guard, strict grammar, offline model, validation. "
            f"Unsafe-accept must be zero.</p><div class='grid'>{tile_html}</div>"
            f"<h2>Full agent against the strict grammar alone</h2><div class='card'>{compare}</div>"
            f"<h2>Development and held-out sentences</h2><div class='card'><table><tr><th>Split</th><th>Cases</th><th>Exact spec</th><th>Status</th><th>Clarify recall</th><th>Unsafe accepts</th></tr>{split}</table>"
            f"<p class='small'>The held-out sentences were written after the extractor was tuned and measured once before any change to it.</p></div>"
            f"<h2>By category (full agent)</h2><div class='card'><table><tr><th>Category</th><th>Passed</th><th></th></tr>{cats}</table></div>"
            f"<h2>Failures</h2><div class='card' style='overflow-x:auto'><table><tr><th>ID</th><th>Sentence</th><th>Expected</th><th>Got</th></tr>{fail_html}</table></div>"
            f"<h2>Conflict detection ({c['passed']}/{c['total']})</h2><div class='card' style='overflow-x:auto'><table><tr><th>ID</th><th>Rules</th><th>Expected</th><th>Got</th><th></th></tr>{conf_rows}</table></div>"
            f"<details><summary>All {full['cases']} cases</summary><div class='card' style='overflow-x:auto'><table><tr><th>ID</th><th>Category</th><th>Sentence</th><th>Expected</th><th>Got</th><th></th></tr>{case_rows}</table></div></details>")


def render_eval_page(report: dict, session: dict | None = None, pending: int = 0) -> str:
    return shell("Evals - money rules demo", render_eval_body(report), session or {"user": None}, "evals", pending)


def login_page(session: dict, error: str = "") -> str:
    err = f"<div class='flash error'>{_e(error)}</div>" if error else ""
    return shell("Sign in - money rules demo", f"""<div class='login card'><h1>Sign in</h1>
<p class='lead'>Local demo users only. The server printed generated logins when it first started.</p>{err}
<form method='post' action='/login'>{csrf_field(session)}
<label for='username'>Username</label><input type='text' id='username' name='username' autocomplete='username' required>
<label for='passphrase'>Passphrase</label><input type='{MASKED}' id='passphrase' name='passphrase' autocomplete='current-{MASKED}' required>
<div style='margin-top:16px'><button class='primary'>Sign in</button></div></form></div>""")


# ----------------------------------------------------------------- server
_EVAL_CACHE: dict = {}


def eval_report() -> dict:
    if "report" not in _EVAL_CACHE:
        _EVAL_CACHE["report"] = evals.run_all()
    return _EVAL_CACHE["report"]


def make_handler(workspace: Workspace, users: UserStore, sessions: Sessions, port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RulesDemo"
        sys_version = ""

        # -- plumbing
        def _session(self):
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            sid = jar["sid"].value if "sid" in jar else None
            session = sessions.get(sid)
            self._new_cookie = None
            if session is None:
                sid, session = sessions.create()
                self._new_cookie = sid
            self._sid = sid
            return session

        def _send(self, status: int, body: str = "", location: str | None = None, cookie: str | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            if cookie or self._new_cookie:
                self.send_header("Set-Cookie", f"sid={cookie or self._new_cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"localhost:{port}", "localhost")

        def _error(self, status: int, message: str, session=None) -> None:
            body = shell("Error", f"<div class='card'><h1>{_e(message)}</h1><p><a href='/'>Back</a></p></div>", session or {"user": None})
            self._send(status, body)

        # -- GET
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                self._new_cookie = None
                return self._send(400, "<h1>Bad host</h1>")
            session = self._session()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/login":
                if session.get("user"):
                    return self._send(303, location="/")
                return self._send(200, login_page(session))
            if not session.get("user"):
                return self._send(303, location="/login")
            with workspace.lock:
                workspace.refresh()
                try:
                    if path in ("/", "/index.html"):
                        return self._send(200, dashboard(workspace, session))
                    if path == "/approvals":
                        return self._send(200, approvals_page(workspace, session))
                    if path == "/audit":
                        return self._send(200, audit_page(workspace, session))
                    if path == "/evals":
                        return self._send(200, render_eval_page(eval_report(), session, pending_count(workspace)))
                    m = re.fullmatch(r"/trace/([A-Za-z0-9._-]+)", path)
                    if m:
                        return self._send(200, trace_page(workspace, session, m.group(1)))
                except KeyError:
                    return self._error(404, "Not found", session)
            self._error(404, "Not found", session)

        # -- POST
        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                self._new_cookie = None
                return self._send(400, "<h1>Bad host</h1>")
            session = self._session()
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{port}", f"http://localhost"):
                return self._error(403, "Cross-origin request refused", session)
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                return self._error(413, "Request too large", session)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

            def field(name: str) -> str:
                return form.get(name, [""])[0]

            if not csrf_ok(session, field("csrf")):
                return self._error(403, "Missing or invalid CSRF token. Reload the page and try again.", session)
            path = urllib.parse.urlsplit(self.path).path
            if path == "/login":
                return self._login(session, field("username").strip(), field("passphrase"))
            if not session.get("user"):
                return self._send(303, location="/login")
            if path == "/logout":
                sessions.destroy(self._sid)
                return self._send(303, location="/login", cookie="deleted; Max-Age=0")
            user, role = session["user"], session["role"]
            redirect = "/"
            try:
                with workspace.lock:
                    workspace.refresh()
                    if path == "/rules":
                        self._need(role, "submit_rule")
                        rule = workspace.submit_rule(field("text").strip(), actor=user)
                        session["last_rule"] = rule["id"]
                        session["flash"] = {"kind": "ok", "text": f"Rule {rule['id']} received."}
                    elif path in ("/run", "/next-month"):
                        self._need(role, "process")
                        result = workspace.run() if path == "/run" else workspace.next_month()
                        text = (f"Processed {result['period']}: {result['created']} proposals created." if result["processed"]
                                else f"{result['period']} was already processed; nothing changed.")
                        session["flash"] = {"kind": "ok", "text": text}
                        redirect = "/approvals" if pending_count(workspace) else "/"
                    elif m := re.fullmatch(r"/rules/(R\d+)/approve", path):
                        self._need(role, "approve_rule")
                        session["flash"] = {"kind": "ok", "text": workspace.approve_rule(m.group(1), actor=user)["message"] + "."}
                    elif m := re.fullmatch(r"/proposals/([A-Za-z0-9._-]+)/decision", path):
                        self._need(role, "decide")
                        decision = field("decision")
                        result = workspace.decide(m.group(1), decision, actor=user, note=field("note").strip()[:200])
                        session["flash"] = {"kind": "ok", "text": result["message"] + "."}
                        redirect = "/approvals"
                    else:
                        return self._error(404, "Not found", session)
            except PermissionError as exc:
                return self._error(403, str(exc), session)
            except (KeyError, ValueError) as exc:
                session["flash"] = {"kind": "error", "text": f"Refused: {exc.args[0] if exc.args else exc}"}
                return self._send(303, location="/approvals" if "proposal" in path else "/")
            self._send(303, location=redirect)

        @staticmethod
        def _need(role, permission):
            if not UserStore.can(role, permission):
                raise PermissionError(f"Your role ({role}) cannot do that.")

        def _login(self, session, username: str, passphrase: str) -> None:
            key = username.lower()[:40]
            if sessions.locked_out(key):
                return self._send(429, login_page(session, "Too many attempts. Wait a minute and try again."))
            role = users.verify(username, passphrase)
            if role is None:
                sessions.record_failure(key)
                return self._send(401, login_page(session, "Invalid username or passphrase."))
            sessions.destroy(self._sid)  # rotate the session id on login
            sid, _ = sessions.create(username, role)
            self._send(303, location="/", cookie=sid)

        def log_message(self, format, *args):  # keep the console quiet
            return

    return Handler


def build_server(workspace: Workspace, port: int = 8765, users: UserStore | None = None):
    users = users or UserStore(workspace.dir)
    created = users.ensure_demo_users()
    sessions = Sessions()
    server = ThreadingHTTPServer(("localhost", port), BaseHTTPRequestHandler)
    server.RequestHandlerClass = make_handler(workspace, users, sessions, server.server_address[1])
    return server, users, created


def serve(workspace: Workspace, host: str, port: int) -> None:
    if host != "localhost":
        raise ValueError("the demo page only binds to localhost")
    server, _, created = build_server(workspace, port)
    port = server.server_address[1]
    if created:
        print(f"Generated demo logins for {', '.join(created)}; they are in {workspace.dir / 'demo_logins.txt'} (git-ignored).")
    print(f"Serving http://localhost:{port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
