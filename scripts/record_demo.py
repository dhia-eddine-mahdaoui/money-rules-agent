"""Re-record docs/demo.gif (and docs/demo.mp4) by driving the real web UI in headless Chrome.

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python scripts/record_demo.py

Flow: type a rule -> parsed spec -> a vague rule gets a question -> second person approves the rule ->
ledger month is processed -> approver decides one transfer -> trace back to the rule text.
Sign-in happens in a separate, unrecorded context so no login details appear in the video.
The static ffmpeg binary comes from imageio-ffmpeg; no system ffmpeg is needed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rules_agent.service import Workspace  # noqa: E402
from rules_agent.web import build_server  # noqa: E402

W, H = 960, 640
PACE = 0.7  # shortens fixed pauses and typing delays
MAX_SECONDS = 22  # footage longer than this is sped up to fit
CURSOR_JS = """
(() => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:99999;width:20px;height:20px;border-radius:50%;background:rgba(20,98,74,.4);' +
    'border:2px solid #14624a;pointer-events:none;left:-40px;top:-40px;margin:-10px 0 0 -10px;transition:transform .08s';
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(d));
  document.addEventListener('mousemove', e => { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }, true);
  document.addEventListener('mousedown', () => { d.style.transform = 'scale(.55)'; }, true);
  document.addEventListener('mouseup', () => { d.style.transform = 'scale(1)'; }, true);
})();
"""


def main() -> None:
    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rules-demo-"))
    try:
        ws = Workspace(work / "data")
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

            cookies = {}
            for user in ("operator", "approver"):  # unrecorded sign-in
                ctx = browser.new_context()
                page = ctx.new_page()
                page.goto(f"{base}/login")
                page.fill("#username", user)
                page.fill("#passphrase", created[user])
                page.click("button.primary")
                page.wait_for_url(f"{base}/")
                cookies[user] = ctx.cookies()
                ctx.close()

            ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="light")
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            ctx.add_cookies(cookies["operator"])
            frames: list[tuple[float, bytes]] = []

            def snap() -> None:
                frames.append((time.monotonic(), page.screenshot(type="jpeg", quality=85)))

            def pause(ms: int) -> None:
                ms = int(ms * PACE)
                end = time.monotonic() + ms / 1000
                while time.monotonic() < end:
                    snap()
                    page.wait_for_timeout(60)

            def click(locator, settle: int = 250) -> None:
                locator.scroll_into_view_if_needed()
                box = locator.bounding_box()
                tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                sx, sy = getattr(click, "pos", (W / 2, H / 2))
                for i in range(1, 15):
                    page.mouse.move(sx + (tx - sx) * i / 14, sy + (ty - sy) * i / 14)
                    snap()
                    page.wait_for_timeout(20)
                click.pos = (tx, ty)
                pause(settle)
                page.mouse.down()
                pause(90)
                page.mouse.up()

            def type_text(text: str, delay: int) -> None:
                for ch in text:
                    page.keyboard.type(ch)
                    snap()
                    page.wait_for_timeout(int(delay * PACE))

            def scroll(total: int, step: int = 60, delay: int = 30) -> None:
                for _ in range(total // step):
                    page.mouse.wheel(0, step)
                    snap()
                    page.wait_for_timeout(delay)

            def switch(user: str, path: str = "/") -> None:
                ctx.clear_cookies()
                ctx.add_cookies(cookies[user])
                page.goto(f"{base}{path}")

            page.goto(base + "/")
            pause(1200)
            click(page.locator("#text"))
            type_text("Whenever I get paid, stash a fifth in my rainy day fund", 38)
            pause(500)
            click(page.locator("button.primary:has-text('Submit rule')"))
            page.wait_for_selector("text=Understood as")
            pause(600)
            scroll(420)
            pause(2400)

            switch("approver")
            pause(900)
            scroll(300)
            click(page.locator("tr:has-text('rainy day') button:has-text('Approve rule')"))
            pause(1300)

            switch("operator")
            pause(700)
            click(page.locator("button:has-text('Process 2026-11')"))
            page.wait_for_url(f"{base}/approvals")
            pause(1200)

            switch("approver", "/approvals")
            pause(900)
            card = page.locator("div.card:has(a[href='/trace/R5-TX-1125-21-a1'])")
            click(card.locator("input[name=note]"))
            type_text("matches my budget", 45)
            pause(300)
            click(card.locator("button:has-text('Approve')"))
            page.wait_for_url(f"{base}/approvals")
            pause(1000)
            click(page.locator("table a[href='/trace/R5-TX-1125-21-a1']"))
            page.wait_for_url(f"{base}/trace/R5-TX-1125-21-a1")
            pause(1300)
            scroll(1300, step=50, delay=45)
            pause(1800)

            ctx.close()
            browser.close()
        server.shutdown()
        server.server_close()

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        frame_dir = work / "frames"
        frame_dir.mkdir()
        lines = []
        speed = max(1.0, (frames[-1][0] - frames[0][0]) / MAX_SECONDS)
        for i, (ts, data) in enumerate(frames):
            (frame_dir / f"f{i:05d}.jpg").write_bytes(data)
            nxt = frames[i + 1][0] if i + 1 < len(frames) else ts + 1.5
            lines.append(f"file 'f{i:05d}.jpg'\nduration {(nxt - ts) / speed:.3f}")
        lines.append(f"file 'f{len(frames) - 1:05d}.jpg'")
        listing = frame_dir / "list.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        src = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        gif = out_dir / "demo.gif"
        palette = work / "palette.png"
        filters = "fps=9,scale=800:-1:flags=lanczos"
        subprocess.run([ffmpeg, "-y", *src, "-vf", f"{filters},palettegen=max_colors=64:stats_mode=diff", str(palette)],
                       check=True, capture_output=True)
        subprocess.run([ffmpeg, "-y", *src, "-i", str(palette), "-lavfi",
                        f"{filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle", str(gif)],
                       check=True, capture_output=True)
        subprocess.run([ffmpeg, "-y", *src, "-vf", "fps=25,scale=960:-2", "-c:v", "libx264", "-crf", "28",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_dir / "demo.mp4")], check=True, capture_output=True)
        seconds = (frames[-1][0] - frames[0][0]) / speed + 1.5
        print(f"wrote {gif.name} ({gif.stat().st_size / 1e6:.2f} MB, about {seconds:.0f} s) and demo.mp4")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
