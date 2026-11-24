"""Web UI against a real server on localhost: login, roles, CSRF, escaping, approval flow."""

import http.client
import re
import threading
import urllib.parse

import pytest

from rules_agent.service import Workspace
from rules_agent.web import build_server


class Client:
    def __init__(self, port):
        self.port = port
        self.sid = None

    def request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("localhost", self.port, timeout=10)
        hdrs = {"Host": f"localhost:{self.port}"}
        if self.sid:
            hdrs["Cookie"] = f"sid={self.sid}"
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie" and value.startswith("sid="):
                self.sid = value.split(";")[0][4:]
        conn.close()
        return resp, text

    def csrf(self, path="/"):
        _, text = self.request("GET", path)
        return re.search(r"name='csrf' value='([^']+)'", text).group(1)

    def login(self, user, passphrase):
        token = self.csrf("/login")
        return self.request("POST", "/login", {"csrf": token, "username": user, "passphrase": passphrase})


@pytest.fixture()
def app(tmp_path):
    ws = Workspace(tmp_path / "data")
    server, users, created = build_server(ws, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ws, port, created
    server.shutdown()
    server.server_close()


def _login(port, created, user):
    client = Client(port)
    resp, _ = client.login(user, created[user])
    assert resp.status == 303
    return client


def test_pages_require_login_and_login_is_checked(app):
    ws, port, created = app
    anon = Client(port)
    resp, _ = anon.request("GET", "/")
    assert resp.status == 303 and resp.getheader("Location") == "/login"
    resp, _ = anon.request("GET", "/audit")
    assert resp.status == 303
    resp, text = anon.login("operator", "wrong-passphrase")
    assert resp.status == 401 and "Invalid username or passphrase" in text
    resp, _ = anon.login("nobody", "x")
    assert resp.status == 401
    resp, _ = anon.login("operator", created["operator"])
    assert resp.status == 303
    resp, text = anon.request("GET", "/")
    assert resp.status == 200 and "Add a rule" in text


def test_login_locks_out_after_repeated_failures(app):
    _, port, created = app
    client = Client(port)
    statuses = [client.login("operator", "bad")[0].status for _ in range(6)]
    assert statuses[:5] == [401] * 5 and statuses[5] == 429
    assert client.login("operator", created["operator"])[0].status == 429


def test_session_cookie_is_rotated_on_login_and_flags_are_set(app):
    _, port, created = app
    client = Client(port)
    client.request("GET", "/login")
    before = client.sid
    conn = http.client.HTTPConnection("localhost", port)
    token = client.csrf("/login")
    client.login("operator", created["operator"])
    assert client.sid and client.sid != before
    conn.request("GET", "/login", headers={"Host": f"localhost:{port}"})
    header = conn.getresponse().getheader("Set-Cookie")
    assert "HttpOnly" in header and "SameSite=Strict" in header and token


def test_state_changing_posts_need_a_csrf_token(app):
    ws, port, created = app
    op = _login(port, created, "operator")
    resp, _ = op.request("POST", "/rules", {"text": "Pay £850 to landlord from checking on the 1st"})
    assert resp.status == 403
    resp, _ = op.request("POST", "/rules", {"text": "x", "csrf": "forged"})
    assert resp.status == 403
    resp, _ = op.request("POST", "/rules", {"text": "Pay £850 to landlord from checking on the 1st", "csrf": op.csrf()},
                         headers={"Origin": "http://elsewhere.invalid"})
    assert resp.status == 403
    assert not ws.state["rules"]
    resp, _ = op.request("POST", "/rules", {"text": "Pay £850 to landlord from checking on the 1st", "csrf": op.csrf()})
    assert resp.status == 303 and "R1" in ws.state["rules"]


def test_unexpected_host_header_is_refused(app):
    _, port, _ = app
    resp, _ = Client(port).request("GET", "/login", headers={"Host": "rebind.invalid"})
    assert resp.status == 400


def test_roles_separate_submitting_from_approving(app):
    ws, port, created = app
    op, ap = _login(port, created, "operator"), _login(port, created, "approver")
    resp, _ = ap.request("POST", "/rules", {"text": "Pay £850 to landlord from checking on the 1st", "csrf": ap.csrf()})
    assert resp.status == 403
    op.request("POST", "/rules", {"text": "Pay £850 to landlord from checking on the 1st", "csrf": op.csrf()})
    resp, _ = op.request("POST", "/rules/R1/approve", {"csrf": op.csrf()})
    assert resp.status == 403 and ws.state["rules"]["R1"]["status"] == "pending_review"
    resp, _ = ap.request("POST", "/rules/R1/approve", {"csrf": ap.csrf()})
    assert resp.status == 303 and ws.state["rules"]["R1"]["approved_by"] == "approver"


def test_full_flow_decision_idempotency_trace_and_audit(app):
    ws, port, created = app
    op, ap = _login(port, created, "operator"), _login(port, created, "approver")
    op.request("POST", "/rules", {"text": "whenever I get paid, put 20% in savings", "csrf": op.csrf()})
    _, page = op.request("GET", "/")
    assert "offline model" in page and "Preview for 2026-11" in page and "Understood as" in page
    ap.request("POST", "/rules/R1/approve", {"csrf": ap.csrf()})
    op.request("POST", "/run", {"csrf": op.csrf()})
    pid = "R1-TX-1125-21-a1"
    assert ws.state["proposals"][pid]["status"] == "pending_approval"
    _, text = ap.request("GET", "/approvals")
    assert pid in text and "Why did this happen?" in text
    for _ in range(2):  # a double submit must not execute twice
        ap.request("POST", f"/proposals/{pid}/decision", {"decision": "approved", "note": "ok", "csrf": ap.csrf("/approvals")})
    assert sum(e["type"] == "transfer.executed" for e in ws.audit.entries()) == 1
    assert ws.state["balances"]["savings"] == 3480.0
    _, trace = ap.request("GET", f"/trace/{pid}")
    assert "whenever I get paid, put 20% in savings" in trace and "Audit chain verified" in trace and "Human decision" in trace
    _, audit = ap.request("GET", "/audit")
    assert "Chain verified" in audit
    resp, text = ap.request("GET", "/evals")
    assert resp.status == 200 and "Unsafe-accept rate" in text
    resp, _ = ap.request("GET", "/trace/does-not-exist")
    assert resp.status == 404


def test_user_text_is_escaped(app):
    ws, port, created = app
    op = _login(port, created, "operator")
    op.request("POST", "/rules", {"text": "<b>bold</b> move <i>it</i>", "csrf": op.csrf()})
    _, page = op.request("GET", "/")
    assert "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page


def test_logout_destroys_the_session(app):
    _, port, created = app
    op = _login(port, created, "operator")
    resp, _ = op.request("POST", "/logout", {"csrf": op.csrf()})
    assert resp.status == 303
    op.sid = None
    resp, _ = op.request("GET", "/")
    assert resp.status == 303
