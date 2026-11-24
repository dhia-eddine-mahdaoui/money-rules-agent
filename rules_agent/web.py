"""Minimal local web page: rules, proposals awaiting approval, and the audit trail. Stdlib only."""

from __future__ import annotations

import html
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ledger import ACCOUNT_LABELS
from .service import Workspace, describe_spec, label, money

MAX_BODY = 10_000

STYLE = """
:root { --bg:#f7f7f5; --fg:#1d1d1b; --muted:#66665f; --card:#fff; --line:#dcdcd6; --ok:#1f7a4d; --warn:#9a6700; --bad:#b42318; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#161614; --fg:#ecece8; --muted:#a3a39a; --card:#1f1f1c; --line:#3a3a35; --ok:#5fd39a; --warn:#f0b64a; --bad:#ff8a80; } }
:root[data-theme="dark"] { --bg:#161614; --fg:#ecece8; --muted:#a3a39a; --card:#1f1f1c; --line:#3a3a35; --ok:#5fd39a; --warn:#f0b64a; --bad:#ff8a80; }
body { background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 20px 0 8px; }
.note { color: var(--muted); margin: 0 0 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.status { font-weight: 600; } .s-pending_approval, .s-pending_review { color: var(--warn); } .s-blocked, .s-rejected, .s-invalid { color: var(--bad); } .s-auto_executed, .s-approved, .s-active { color: var(--ok); }
form.inline { display: inline; } button { font: inherit; padding: 3px 9px; margin-right: 4px; border-radius: 5px; border: 1px solid var(--line); background: var(--card); color: var(--fg); cursor: pointer; }
input[type=text] { font: inherit; width: min(640px, 100%); padding: 6px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def render_page(workspace: Workspace, message: str = "") -> str:
    state = workspace.state
    rules = sorted(state["rules"].values(), key=lambda r: int(r["id"][1:]))
    proposals = sorted(state["proposals"].values(), key=lambda p: (p["status"] != "pending_approval", p["date"], p["id"]))
    entries = workspace.audit.entries()
    ok, verdict = workspace.verify_audit()

    rule_rows = []
    for r in rules:
        detail = _e(describe_spec(r["spec"])) if r["spec"] else _e("; ".join(r["messages"]) or r.get("question") or "")
        actions = ""
        if r["status"] == "pending_review":
            actions = (f'<form class="inline" method="post" action="/rules/{_e(r["id"])}/approve">'
                       f'<button>Approve rule</button></form>')
        rule_rows.append(f"<tr><td>{_e(r['id'])}</td><td>{_e(r['text'])}</td>"
                         f"<td class='status s-{_e(r['status'])}'>{_e(r['status'])}</td>"
                         f"<td>{detail}</td><td>{actions}</td></tr>")

    proposal_rows = []
    for p in proposals:
        actions = ""
        if p["status"] == "pending_approval":
            actions = (f'<form class="inline" method="post" action="/proposals/{_e(p["id"])}/decision">'
                       f'<button name="decision" value="approved">Approve</button>'
                       f'<button name="decision" value="rejected">Reject</button></form>')
        proposal_rows.append(
            f"<tr><td class='mono'>{_e(p['id'])}</td><td>{_e(p['date'])}</td>"
            f"<td>{money(p['amount_gbp'])}</td><td>{_e(label(p['from']))} &rarr; {_e(label(p['to']))}</td>"
            f"<td class='status s-{_e(p['status'])}'>{_e(p['status'])}</td>"
            f"<td>{_e('; '.join(p['reasons']))}</td><td>{actions}</td></tr>"
        )

    conflict_items = "".join(f"<li class='s-blocked'>[{_e(c['severity'])}] {_e(c['message'])}</li>"
                             for c in state["conflicts"]) or "<li>None detected.</li>"
    audit_rows = "".join(
        f"<tr><td>#{e['seq']}</td><td>{_e(e['ts'])}</td><td>{_e(e['type'])}</td>"
        f"<td class='mono'>{_e(e['data'])}</td><td class='mono'>{_e(e['hash'][:12])}</td></tr>"
        for e in entries[-100:][::-1]
    )
    balances = ""
    if state["last_run"]:
        balances = " &middot; ".join(
            f"{_e(ACCOUNT_LABELS[a])} {money(v)}" for a, v in state["last_run"]["final_balances"].items()
        )
    notice = f"<p class='note'>{_e(message)}</p>" if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Money rules: simulation</title><style>{STYLE}</style></head>
<body>
<h1>Plain-English money rules</h1>
<p class="note">Simulation only. Synthetic accounts and transactions; no real bank or payment is made.</p>
{notice}
<h2>Add a rule</h2>
<form method="post" action="/rules" class="card">
<input type="text" name="text" placeholder="When my salary arrives, move 20% to savings and £300 to the holiday pot">
<button>Submit rule</button>
</form>
<h2>Rules</h2>
<div class="card"><table><tr><th>ID</th><th>Text</th><th>Status</th><th>Parsed spec or rejection</th><th></th></tr>
{''.join(rule_rows) or '<tr><td colspan=5>No rules yet.</td></tr>'}</table></div>
<h2>Proposed transfers</h2>
<p class="note">Projected month: {balances or 'not run yet'}. Pending items wait for approval; approving re-runs the simulation.</p>
<div class="card"><table><tr><th>ID</th><th>Date</th><th>Amount</th><th>Route</th><th>Status</th><th>Reasons</th><th></th></tr>
{''.join(proposal_rows) or '<tr><td colspan=7>No proposals. Run the simulation from the CLI.</td></tr>'}</table></div>
<h2>Rule conflicts and loops</h2>
<div class="card"><ul>{conflict_items}</ul></div>
<h2>Audit trail <span class="note">({_e(verdict)})</span></h2>
<div class="card"><table><tr><th>Seq</th><th>Time (UTC)</th><th>Event</th><th>Data</th><th>Hash</th></tr>
{audit_rows or '<tr><td colspan=5>Empty.</td></tr>'}</table></div>
</body></html>"""


def make_handler(workspace: Workspace):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, location: str | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self._send(404, "<h1>Not found</h1>")
                return
            self._send(200, render_page(workspace))

        def do_POST(self):  # noqa: N802
            length = min(int(self.headers.get("Content-Length", 0) or 0), MAX_BODY)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            path = self.path.split("?")[0]
            try:
                if path == "/rules":
                    rule = workspace.submit_rule(form.get("text", [""])[0].strip())
                    message = f"{rule['id']} is {rule['status']}."
                elif m := re.fullmatch(r"/rules/(R\d+)/approve", path):
                    workspace.approve_rule(m.group(1), actor="web")
                    message = f"{m.group(1)} approved."
                elif m := re.fullmatch(r"/proposals/([A-Za-z0-9._-]+)/decision", path):
                    decision = form.get("decision", [""])[0]
                    workspace.decide(m.group(1), decision, actor="web")
                    message = f"{m.group(1)} {decision}."
                else:
                    self._send(404, "<h1>Not found</h1>")
                    return
            except (KeyError, ValueError) as exc:
                self._send(400, render_page(workspace, f"Rejected: {exc}"))
                return
            self._send(303, "", location="/?" + urllib.parse.urlencode({"msg": message}))

        def log_message(self, format, *args):  # keep the console quiet
            return

    return Handler


def serve(workspace: Workspace, host: str, port: int) -> None:
    if host != "localhost":
        raise ValueError("the demo page only binds to localhost")
    server = ThreadingHTTPServer((host, port), make_handler(workspace))
    print(f"Serving http://{host}:{port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
