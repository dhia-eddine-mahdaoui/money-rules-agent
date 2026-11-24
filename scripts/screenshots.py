"""Regenerate docs/screenshots/*.png with a headless browser.

Needs the dev requirement (pip install -r requirements-dev.txt) and a Chromium or Chrome build.
It starts the web UI on localhost with a throw-away data directory, signs in through the real
login form, and drives the same pages a user sees. Only page content is captured.

    .venv/bin/python scripts/screenshots.py [output-dir]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rules_agent.service import Workspace  # noqa: E402
from rules_agent.web import build_server  # noqa: E402


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="rules-shots-"))
    try:
        ws = Workspace(data)
        for line in (ROOT / "examples" / "rules.txt").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                ws.submit_rule(line.strip(), actor="operator")
        for rule_id in list(ws.state["rules"]):
            ws.approve_rule(rule_id, actor="approver")
        server, _, created = build_server(ws, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://localhost:{port}"

        with sync_playwright() as p:
            chrome = shutil.which("google-chrome") or shutil.which("chromium")
            browser = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"]) if chrome else p.chromium.launch()

            def session(user: str):
                ctx = browser.new_context(viewport={"width": 1180, "height": 900}, device_scale_factor=1.5, color_scheme="light")
                page = ctx.new_page()
                page.goto(f"{base}/login")
                page.fill("#username", user)
                page.fill("#passphrase", created[user])
                page.click("button.primary")
                page.wait_for_url(f"{base}/")
                return ctx, page

            def shot(page, name: str, max_height: int | None = None) -> None:
                kwargs = {"path": str(out_dir / name), "full_page": True}
                if max_height:
                    kwargs["full_page"] = False
                    kwargs["clip"] = {"x": 0, "y": 0, "width": 1180, "height": max_height}
                page.screenshot(**kwargs)
                print("wrote", name)

            # 1 + 2: rule entry as the operator
            ctx, page = session("operator")
            page.fill("#text", "Whenever I get paid, stash a fifth in my rainy day fund")
            page.click("button.primary:has-text('Submit rule')")
            page.wait_for_selector("text=Understood as")
            shot(page, "01-rule-parsed.png", 1300)
            page.fill("#text", "Move some money to savings when I get paid")
            page.click("button.primary:has-text('Submit rule')")
            page.wait_for_selector("text=Nothing was guessed")
            shot(page, "02-rule-clarify.png", 760)
            ctx.close()

            # approver approves the new rule, operator processes the month
            ctx, page = session("approver")
            page.locator("tr:has-text('rainy day') button:has-text('Approve rule')").click()
            ctx.close()
            ctx, page = session("operator")
            page.click("button:has-text('Process 2026-11')")
            page.wait_for_url(f"{base}/approvals")
            ctx.close()

            # 3: pending approvals, then decisions and the trace
            ctx, page = session("approver")
            page.goto(f"{base}/approvals")
            shot(page, "03-pending-approvals.png", 1500)
            for pid, note in (("R3-sched-2026-11-01-a1", "rent confirmed"), ("R5-TX-1125-21-a1", "matches my budget")):
                page.goto(f"{base}/approvals")
                card = page.locator(f"div.card:has(a[href='/trace/{pid}'])")
                card.locator("input[name=note]").fill(note)
                card.locator("button:has-text('Approve')").click()
                page.wait_for_url(f"{base}/approvals")
            page.goto(f"{base}/trace/R5-TX-1125-21-a1")
            shot(page, "04-trace.png")
            page.goto(f"{base}/audit")
            shot(page, "05-audit-chain.png", 1000)
            page.goto(f"{base}/evals")
            shot(page, "06-eval-results.png", 1900)
            ctx.close()
            browser.close()
        server.shutdown()
        server.server_close()
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots")
