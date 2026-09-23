#!/usr/bin/env python3
"""Playwright test of the admin UI against a mock broker API.

Run by hand: `python3 tests/admin_ui_playwright.py` (needs `playwright` and its
Chromium: `playwright install chromium`, `playwright install-deps chromium`).
Set ADMIN_UI_SHOTS to a directory to keep the screenshots. Exits 0 when every
check passes and prints one PASS/FAIL line per check.

Serves the real shell and app.js from this checkout, with the two
API routes answered by an in-process fixture. Drives the page headless:
grouping by bottle, columns, chips, the five actions (payloads recorded),
the deny-global wrong-host refusal, a decision moving a row to the recent
list with the group header decrementing, and screenshots at desktop and
mobile in light and dark. Writes PNGs and REPORT.md to /artifacts/egress-ui/.
"""
from __future__ import annotations

import copy
import os
import tempfile
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("ADMIN_UI_SHOTS", tempfile.mkdtemp(prefix="admin-ui-")))
sys.path.insert(0, str(WORKTREE / "src"))
import admin_daemon as admin  # noqa: E402

FIXTURE = {
    "open": [
        {"request_id": "a1", "container": "bottle-a", "host": "x.example.com", "port": 443, "host_is_ip": False,
         "opened_at": "2026-09-22T21:00:00Z", "age_seconds": 300, "hit_count": 3, "uid": 1000, "comm": "curl",
         "reason": "npm install", "attempt": 0},
        {"request_id": "b1", "container": "bottle-b", "host": "x.example.com", "port": 443, "host_is_ip": False,
         "opened_at": "2026-09-22T21:00:30Z", "age_seconds": 270, "hit_count": 1, "uid": 1000, "comm": "pip",
         "reason": None, "attempt": 0},
        {"request_id": "a2", "container": "bottle-a", "host": "192.0.2.55", "port": 5432, "host_is_ip": True,
         "opened_at": "2026-09-22T21:01:00Z", "age_seconds": 240, "hit_count": 2, "uid": 1000, "comm": "psql",
         "reason": "db:migrate", "attempt": 1,
         "last_error": {"reason": "ip_requires_cidr", "attempt": 1, "at": "2026-09-22T21:02:00Z"}},
    ],
    "count": 3,
    "generated_at": "2026-09-22T21:05:00Z",
    "recent": [
        {"request_id": "r1", "container": "bottle-b", "host": "registry.npmjs.org", "port": 443, "status": "allowed",
         "scope": "live", "decided_at": "2026-09-22T20:50:00Z", "decided_by": "operator", "apply_status": "applied",
         "deny_reason": None},
    ],
}

STATE = {"queue": copy.deepcopy(FIXTURE), "decides": []}
LOCK = threading.Lock()


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, admin.APP_HTML.encode(), "text/html; charset=utf-8")
        if self.path == "/app.js":
            return self._send(200, (WORKTREE / "src" / "admin_app.js").read_bytes(), "text/javascript")
        if self.path == "/vendor/htm-preact-standalone.module.js":
            return self._send(200, (WORKTREE / "src" / "admin_vendor" / "htm-preact-standalone-3.1.1.module.js").read_bytes(), "text/javascript")
        if self.path.startswith("/api/egress/queue"):
            with LOCK:
                return self._send(200, STATE["queue"])
        if self.path in ("/manifest.webmanifest", "/sw.js"):
            return self._send(404, {"error": "not in mock"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/egress/decide":
            with LOCK:
                STATE["decides"].append({"headers": {k: v for k, v in self.headers.items() if k in ("Content-Type", "X-Admin-UI")}, "body": body})
            failures = [{"request_id": "a2", "reason": "ip_requires_cidr"}] if body.get("host") == "192.0.2.55" else []
            return self._send(200, {"decided": ["x"], "apply_failures": failures})
        self._send(404, {"error": "not found"})


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    ok = True

    def check(cond, msg):
        nonlocal ok
        report.append(("PASS " if cond else "FAIL ") + msg)
        ok = ok and bool(cond)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        # screenshot matrix
        for theme in ("light", "dark"):
            for label, vp in (("desktop", {"width": 1280, "height": 900}), ("mobile", {"width": 390, "height": 844})):
                ctx = b.new_context(viewport=vp, color_scheme=theme, device_scale_factor=2 if label == "mobile" else 1)
                page = ctx.new_page()
                page.goto(base + "/")
                page.wait_for_selector("tr.group-row", timeout=10000)
                page.screenshot(path=str(OUT / f"admin-table-{label}-{theme}.png"), full_page=True)
                if label == "mobile":
                    sw = page.evaluate("() => document.documentElement.scrollWidth")
                    check(sw <= 390, f"{theme}/mobile: no horizontal overflow (scrollWidth {sw})")
                ctx.close()

        # behaviour, desktop light
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_selector("tr.group-row", timeout=10000)
        check(page.title() == "(3) Egress queue - Djinn admin", f"tab title carries the count ({page.title()!r})")
        headers = page.locator("tr.group-row").all_inner_texts()
        check(headers == ["bottle-a - 2 request(s)", "bottle-b - 1 request(s)"], f"group headers by bottle in first-seen order: {headers}")
        rows = page.locator("tbody tr").all_inner_texts()
        order = [r.split("\n")[0].strip() for r in rows]
        check(order[0].startswith("bottle-a") and "2026-09-22" in order[1] and "2026-09-22" in order[2] and order[3].startswith("bottle-b"),
              f"bottle-a's two rows stay together ahead of bottle-b (rows: {order})")
        first_cells = page.locator("tbody tr").nth(1).locator("td").all_inner_texts()
        check(len(first_cells) == 4, f"four columns per request row ({len(first_cells)})")
        check("x.example.com:443" in first_cells[1] and "3 hits" in first_cells[1], f"destination cell: {first_cells[1]!r}")
        check(first_cells[2].strip() == "npm install", f"reason cell: {first_cells[2]!r}")
        title_attr = page.locator("tbody tr").nth(1).locator("td").nth(0).get_attribute("title")
        check(title_attr == "2026-09-22T21:00:00Z", f"requested cell carries raw UTC in title ({title_attr})")
        b_cells = page.locator("tbody tr").nth(4).locator("td").all_inner_texts()
        check(b_cells[2].strip() == "—", f"missing reason renders an em-dash ({b_cells[2]!r})")
        ip_cells = page.locator("tbody tr").nth(2).locator("td").all_inner_texts()
        check("IP" in ip_cells[1] and "apply failed ×1" in ip_cells[3] and "ip_requires_cidr" in ip_cells[3],
              f"IP badge and apply-failed chip on the IP row ({ip_cells[1]!r} / {ip_cells[3][-60:]!r})")
        recent = page.locator("section.panel").nth(1)
        check("Recent decisions (24 h)" in recent.inner_text() and "registry.npmjs.org:443" in recent.inner_text() and "allowed / live" in recent.inner_text(),
              "recent list renders decided time, bottle, destination, outcome, by")

        # actions on the first row (bottle-a / x.example.com)
        row1 = page.locator("tbody tr").nth(1)
        row1.get_by_placeholder("Type host for global deny").fill("wrong.example.com")
        row1.get_by_role("button", name="Deny always (global)").click()
        page.wait_for_timeout(300)
        check("type exact host to arm global deny" in row1.inner_text(), "deny global with a wrong typed host is refused inline")
        check(len(STATE["decides"]) == 0, "no decide POST was sent on the refusal")
        row1.get_by_placeholder("Type host for global deny").fill("x.example.com")
        row1.get_by_placeholder("Optional deny reason").fill("telemetry")
        row1.get_by_role("button", name="Deny always (global)").click()
        page.wait_for_timeout(500)
        d = STATE["decides"][-1]
        check(d["body"] == {"action": "deny_global", "host": "x.example.com", "reason": "telemetry"},
              f"deny global body omits container and carries the reason: {d['body']}")
        check(d["headers"].get("X-Admin-UI") == "1" and d["headers"].get("Content-Type") == "application/json", "decide carries the UI header and JSON content type")
        for name, action in (("Allow", "allow_live"), ("Allow+manifest", "allow_manifest"), ("Deny", "deny"), ("Deny always (bottle)", "deny_bottle")):
            page.locator("tbody tr").nth(4).get_by_role("button", name=name, exact=True).click()
            page.wait_for_timeout(400)
            body = STATE["decides"][-1]["body"]
            check(body.get("action") == action and body.get("container") == "bottle-b" and body.get("host") == "x.example.com",
                  f"{name} posts {action} with container bottle-b: {body}")
        page.locator("tbody tr").nth(2).get_by_role("button", name="Allow", exact=True).click()
        page.wait_for_timeout(500)
        check("recorded - add CIDR to manifest by hand" in page.locator("tbody tr").nth(2).inner_text(), "IP-literal allow shows the CIDR-by-hand chip")

        # a decision lands: a1 moves to recent; header decrements 2 -> 1
        with LOCK:
            q = STATE["queue"]
            moved = [r for r in q["open"] if r["request_id"] == "a1"][0]
            q["open"] = [r for r in q["open"] if r["request_id"] != "a1"]
            q["count"] = len(q["open"])
            q["recent"].insert(0, {"request_id": "a1", "container": "bottle-a", "host": moved["host"], "port": 443, "status": "denied",
                                   "scope": "global", "decided_at": "2026-09-22T21:06:00Z", "decided_by": "operator",
                                   "apply_status": None, "deny_reason": "telemetry"})
        page.wait_for_function("() => document.querySelector('tr.group-row') && document.querySelector('tr.group-row').innerText.includes('1 request(s)')", timeout=8000)
        headers2 = page.locator("tr.group-row").all_inner_texts()
        check(sorted(headers2) == ["bottle-a - 1 request(s)", "bottle-b - 1 request(s)"], f"group header decremented after the decision (bottles reorder by their earliest open request): {headers2}")
        check("x.example.com:443" in page.locator("section.panel").nth(1).inner_text() and "denied / global - telemetry" in page.locator("section.panel").nth(1).inner_text(),
              "the decided row moved to the recent list with its outcome")
        check(page.title() == "(2) Egress queue - Djinn admin", f"tab title count follows ({page.title()!r})")
        page.screenshot(path=str(OUT / "admin-table-after-decision-desktop-light.png"), full_page=True)
        # last row of bottle-a decided: header disappears
        with LOCK:
            q = STATE["queue"]
            q["open"] = [r for r in q["open"] if r["request_id"] != "a2"]
            q["count"] = len(q["open"])
        page.wait_for_function("() => document.querySelectorAll('tr.group-row').length === 1", timeout=8000)
        check(page.locator("tr.group-row").all_inner_texts() == ["bottle-b - 1 request(s)"], "bottle-a's header disappears after its last request is decided")
        ctx.close()
        b.close()
    srv.shutdown()

    sha = subprocess.run(["git", "-C", str(WORKTREE), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    (OUT / "REPORT.md").write_text(
        "# Admin UI mock test (Playwright, headless Chromium, local mock broker API)\n\n"
        f"Source: integration branch at {sha} (src/admin_daemon.py APP_HTML, src/admin_app.js, vendored Preact).\n"
        "Broker API mocked in-process; no network, no docker.\n\n" + "\n".join(f"- {l}" for l in report) +
        "\n\nScreenshots: " + ", ".join(sorted(p.name for p in OUT.glob("*.png"))) + "\n"
    )
    print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
