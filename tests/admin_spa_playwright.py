#!/usr/bin/env python3
"""Playwright test of the Vue admin shell in spa mode.

Run by hand: `python3 tests/admin_spa_playwright.py` (needs `playwright` and
its Chromium: `playwright install chromium`). Set ADMIN_UI_SHOTS to a directory
to keep the screenshots. Exits 0 when every check passes and prints one
PASS/FAIL line per check.

Serves the real built admin/ui/dist through the real admin daemon in spa mode
(in-process, session key known), mints the session, then loads each app route
at desktop, tablet and phone in light and dark.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

WORKTREE = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("ADMIN_UI_SHOTS", tempfile.mkdtemp(prefix="admin-spa-ui-")))
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "tests"))

import admin_daemon as admin
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening


def _start_admin() -> tuple[admin.AdminHTTPServer, threading.Thread, str]:
    home = Path(tempfile.mkdtemp())
    egress_root = home / "run" / "egress"
    egress_root.mkdir(parents=True, exist_ok=True)
    (egress_root / admin.OPERATOR_TOKEN_FILENAME).write_text("token\n", encoding="utf-8")
    admin_key = "admin-test-key"
    env = {
        "DJINN_HOME": str(home),
        "DJINN_ADMIN_UI": "spa",
    }
    patcher = mock.patch.dict(os.environ, env, clear=False)
    patcher.start()
    server = admin.AdminHTTPServer(
        ("127.0.0.1", 0),
        egress_root=egress_root,
        session_secret="session-secret",
        operator_token="token",
        admin_key=admin_key,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_tcp_listening(server.server_address[0], server.server_address[1])
    return server, thread, admin_key


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    ok = True

    def check(cond, msg):
        nonlocal ok
        report.append(("PASS " if cond else "FAIL ") + msg)
        ok = ok and bool(cond)

    server, thread, admin_key = _start_admin()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    daemon_origin = f"{host}:{port}"

    # Cookie-less request gets the pointer page.
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/egress")
    resp = conn.getresponse()
    pointer_body = resp.read()
    conn.close()
    check(
        b"./djinn egress url" in pointer_body,
        "cookie-less /egress serves the pointer page",
    )

    # Mint the session cookie with Python so we can seed Playwright contexts.
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", f"/session?key={admin_key}")
    resp = conn.getresponse()
    set_cookie = resp.getheader("Set-Cookie", "")
    resp.read()
    conn.close()
    cookie_value = ""
    for part in set_cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if name == admin.SESSION_COOKIE_NAME and sep:
            cookie_value = value
            break

    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    external_requests: list[str] = []

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(msg.text)

    def on_request(req):
        parsed = urlparse(req.url)
        netloc = parsed.netloc
        if netloc and netloc != daemon_origin:
            external_requests.append(req.url)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        viewports = (
            ("desktop", {"width": 1440, "height": 900}),
            ("tablet", {"width": 820, "height": 1180}),
            ("phone", {"width": 390, "height": 844}),
        )
        routes = (
            ("/egress", "Egress"),
            ("/denylist", "Denylist"),
            ("/bottles", "Bottles"),
            ("/backup", "Backup"),
        )
        for theme in ("light", "dark"):
            for label, vp in viewports:
                ctx = browser.new_context(
                    viewport=vp,
                    color_scheme=theme,
                    device_scale_factor=2 if label == "phone" else 1,
                )
                ctx.add_cookies(
                    [
                        {
                            "name": admin.SESSION_COOKIE_NAME,
                            "value": cookie_value,
                            "domain": host,
                            "path": "/",
                            "httpOnly": True,
                            "sameSite": "Strict",
                        }
                    ]
                )
                page = ctx.new_page()
                page.on("console", on_console)
                page.on("request", on_request)
                for route, title in routes:
                    page.goto(base + route)
                    page.wait_for_selector("h1", timeout=10000)
                    shot_path = OUT / f"spa-{label}-{theme}-{route.strip('/').replace('/', '-') or 'root'}.png"
                    page.screenshot(path=str(shot_path), full_page=True)

                    h1 = page.locator("h1").inner_text().strip()
                    check(h1 == title, f"{label}/{theme}/{route}: h1 title is {title!r} ({h1!r})")

                    if label == "phone":
                        nav = page.locator("nav.tabbar")
                        nav_items = nav.locator("a.tabbar-item").all_inner_texts()
                    else:
                        nav = page.locator("aside nav")
                        nav_items = nav.locator("a.nav-item").all_inner_texts()
                    expected = ["Egress", "Denylist", "Bottles", "Backup"]
                    got = [item.splitlines()[0].strip() for item in nav_items]
                    check(
                        got == expected,
                        f"{label}/{theme}/{route}: nav shows four entries ({got!r})",
                    )

                    # Egress must not have a 'later' caption; the other three must.
                    if label != "phone":
                        egress_text = nav.locator("a.nav-item").first.inner_text()
                        deny_text = nav.locator("a.nav-item").nth(1).inner_text()
                        check(
                            "later" not in egress_text.lower(),
                            f"{label}/{theme}/{route}: Egress nav item has no 'later' caption",
                        )
                        check(
                            "later" in deny_text.lower(),
                            f"{label}/{theme}/{route}: Denylist nav item has 'later' caption",
                        )

                    sw = page.evaluate("() => document.documentElement.scrollWidth")
                    iw = page.evaluate("() => window.innerWidth")
                    check(sw <= iw, f"{label}/{theme}/{route}: no horizontal overflow ({sw} <= {iw})")

                    inter_loaded = page.evaluate(
                        "() => document.fonts.check('16px \"Inter Variable\"')"
                    )
                    check(inter_loaded, f"{label}/{theme}/{route}: Inter Variable font is loaded")

                    errors = list(console_errors)
                    console_errors.clear()
                    check(
                        not errors,
                        f"{label}/{theme}/{route}: no console errors ({errors!r})",
                    )

                    externals = list(external_requests)
                    external_requests.clear()
                    check(
                        not externals,
                        f"{label}/{theme}/{route}: no external requests ({externals!r})",
                    )
                ctx.close()
        browser.close()

    server.shutdown()
    server.server_close()
    join_thread_or_fail(thread, label="admin")

    print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
