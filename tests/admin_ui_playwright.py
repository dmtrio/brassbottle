#!/usr/bin/env python3
"""Behaviour suite for the admin UI, runnable against either UI.

    python3 tests/admin_ui_playwright.py [--ui legacy|spa]      (default: legacy)

Needs `playwright` and its Chromium (`playwright install chromium`); the spa
run needs a built admin/ui/dist (`cd admin/ui && npm ci && npm run build`).
Set ADMIN_UI_SHOTS to keep the captures and REPORT.md (default: a temp dir).

The page is served by the REAL admin daemon (legacy Preact page, or the Vue app
from admin/ui/dist) in front of a stub broker (tests/admin_ui_stub_broker.py)
whose every reply is validated against admin/contract/ before it is served; a
stub that drifts from the contract refuses to start the suite. The browser's own
`POST /api/egress/decide` requests are captured and their JSON bodies asserted
literally. One PASS/FAIL line per behaviour check, per viewport. A check tagged
`design-change` asserts a deliberate difference from the legacy page (PLN Admin
UI rework); `--ui legacy` reports it as SKIP(design-change) instead of running it.

Exit status: 0 with no FAIL, 1 with any FAIL, 2 when the stub violates the contract.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from pathlib import Path
from typing import Callable
from unittest import mock

WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "tests"))

import admin_daemon as admin  # noqa: E402
import admin_ui_stub_broker as stub  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 820, "height": 1180},
    "phone": {"width": 390, "height": 844},
}
TIMEOUT_MS = 10_000
BANNER_CLEAR_MS = 15_000
BOTTLES_ALPHABETICAL = [("alpha", 5), ("mid", 3), ("zeta", 3)]
KNOWN_HOSTS = [row[2] for row in stub.OPEN_ROWS]


def log(message: str) -> None:
    print(f"[suite] {message}", file=sys.stderr, flush=True)


def lines(text: str) -> list[str]:
    return [part.strip() for part in text.splitlines() if part.strip()]


def squash(text: str) -> str:
    return " ".join(text.split())


# ---- captured browser traffic ------------------------------------------------


@dataclass
class Traffic:
    """What one page sent to /api/egress/decide, and what it logged to the console."""

    decides: list[dict] = field(default_factory=list)   # {"body": dict, "headers": dict}
    statuses: list[int] = field(default_factory=list)   # decide response statuses
    queue_reads: int = 0                                # GET /api/egress/queue responses seen
    console_errors: list[str] = field(default_factory=list)
    expected_failures: int = 0                          # scripted 4xx/5xx the browser logs

    def attach(self, page) -> None:
        def on_request(request):
            if request.method == "POST" and request.url.endswith("/api/egress/decide"):
                self.decides.append({"body": json.loads(request.post_data or "{}"), "headers": request.headers})

        def on_response(response):
            if response.request.method == "POST" and response.url.endswith("/api/egress/decide"):
                self.statuses.append(response.status)
            elif response.url.endswith("/api/egress/queue"):
                self.queue_reads += 1

        def on_console(message):
            if message.type != "error":
                return
            url = (message.location or {}).get("url", "")
            # The browser logs every scripted 4xx/5xx from the admin API; those
            # are the outcomes under test, not page errors.
            if message.text.startswith("Failed to load resource") and re.search(r"/api/egress/(decide|queue)$", url):
                self.expected_failures += 1
                return
            self.console_errors.append(message.text)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("console", on_console)
        page.on("pageerror", lambda error: self.console_errors.append(f"pageerror: {error}"))


# ---- per-UI drivers ------------------------------------------------------------


class Driver:
    """Everything the checks need from a page, hiding each UI's DOM."""

    ui = ""

    def __init__(self, page, traffic: Traffic):
        self.page = page
        self.traffic = traffic

    # -- reading
    def requests(self):
        raise NotImplementedError

    def row(self, host: str):
        return self.requests().filter(has_text=f"{host}:").first

    def hosts(self) -> list[str]:
        found = []
        for text in self.requests().all_inner_texts():
            found.append(next(host for host in KNOWN_HOSTS if f"{host}:" in text))
        return found

    def groups(self) -> list[tuple[str, int]]:
        raise NotImplementedError

    def title_count(self) -> int:
        match = re.match(r"\((\d+)\)", self.page.title())
        return int(match.group(1)) if match else 0

    def recent_text(self) -> str:
        raise NotImplementedError

    def stale_banner(self):
        raise NotImplementedError

    def note(self, host: str) -> str:
        """Inline outcome text on a row, apart from the row's own facts."""
        raise NotImplementedError

    # -- acting
    def act(self, host: str, action: str, *, reason: str | None = None, typed: str | None = None) -> dict:
        """Perform one action on a row and return the decide the browser sent."""
        before = len(self.traffic.decides)
        with self.page.expect_response(lambda r: r.url.endswith("/api/egress/decide"), timeout=TIMEOUT_MS):
            self._click_action(host, action, reason, typed)
        self._wait_until(lambda: len(self.traffic.decides) > before, 2)
        sent = self.traffic.decides[-1] if len(self.traffic.decides) > before else {}
        # Let the queue re-read that follows a decide land and render before the
        # next action: the legacy table has unkeyed rows, so a click aimed while
        # rows shift can hit the row that moved into place.
        reads = self.traffic.queue_reads
        self._wait_until(lambda: self.traffic.queue_reads > reads, 3)
        self.page.wait_for_timeout(150)
        return sent

    def _wait_until(self, condition: Callable[[], bool], seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)

    def _click_action(self, host, action, reason, typed):
        raise NotImplementedError

    def sent_nothing_after(self, action: Callable[[], None]) -> bool:
        before = len(self.traffic.decides)
        action()
        self.page.wait_for_timeout(400)  # a negative has to be waited out
        return len(self.traffic.decides) == before


class LegacyDriver(Driver):
    ui = "legacy"

    def requests(self):
        # the first panel is the queue; the second is the recent list
        return self.page.locator("section.panel").first.locator("tbody tr:not(.group-row)")

    def groups(self):
        parsed = []
        for text in self.page.locator("tr.group-row").all_inner_texts():
            name, count = re.match(r"(\S+) - (\d+) request", squash(text)).groups()
            parsed.append((name, int(count)))
        return parsed

    def recent_text(self):
        return self.page.locator("section.panel").filter(has_text="Recent decisions").inner_text()

    def stale_banner(self):
        return self.page.locator(".banner.error")

    def note(self, host):
        return squash(" ".join(self.row(host).locator(".chip").all_inner_texts()))

    def _click_action(self, host, action, reason, typed):
        row = self.row(host)
        if reason:
            row.get_by_placeholder("Optional deny reason").fill(reason)
        if typed is not None:
            row.get_by_placeholder("Type host for global deny").fill(typed)
        label = {
            "allow_live": "Allow", "allow_manifest": "Allow+manifest", "deny": "Deny",
            "deny_bottle": "Deny always (bottle)", "deny_global": "Deny always (global)",
        }[action]
        row.get_by_role("button", name=label, exact=True).click()

    def refuse_global(self, host: str, typed: str) -> tuple[bool, str]:
        row = self.row(host)
        sent = self.sent_nothing_after(lambda: (
            row.get_by_placeholder("Type host for global deny").fill(typed),
            row.get_by_role("button", name="Deny always (global)", exact=True).click(),
        ))
        return sent, self.note(host)


class SpaDriver(Driver):
    ui = "spa"

    def requests(self):
        return self.page.locator("[data-testid=request]")

    def groups(self):
        parsed = []
        for text in self.page.locator("[data-testid=group]").all_inner_texts():
            name, count = re.match(r"(\S+) (\d+) open", squash(text)).groups()
            parsed.append((name, int(count)))
        return parsed

    def recent_text(self):
        return self.page.locator("[data-testid=recent]").inner_text()

    def stale_banner(self):
        return self.page.locator("[data-testid=stale-banner]")

    def note(self, host):
        return squash(self.row(host).locator("[role=status]").inner_text())

    def set_view(self, label: str) -> None:
        self.page.locator("button, [role=radio]").filter(has_text=re.compile(rf"^\s*{label}\s*$")).click()

    def _menu(self, host, trigger, item):
        self.row(host).get_by_role("button", name=trigger).click()
        self.page.get_by_role("menuitem", name=item).click()

    def open_permanent_deny(self, host: str, action: str):
        item = "Deny permanently · bottle" if action == "deny_bottle" else "Deny permanently · global"
        self._menu(host, "More deny options", item)
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for()
        return dialog

    def _click_action(self, host, action, reason, typed):
        row = self.row(host)
        if action == "allow_live":
            name = "Retry allow" if row.get_by_role("button", name="Retry allow", exact=True).count() else "Allow"
            row.get_by_role("button", name=name, exact=True).click()
        elif action == "allow_manifest":
            self._menu(host, "More allow options", "Allow permanently · bottle")
        elif action == "deny":
            row.get_by_role("button", name="Deny", exact=True).click()
        else:
            dialog = self.open_permanent_deny(host, action)
            if reason:
                dialog.locator("#deny-reason").fill(reason)
            if typed is not None:
                dialog.locator("#confirm-host").fill(typed)
            dialog.get_by_role("button", name="Deny permanently").click()

    def refuse_global(self, host: str, typed: str) -> tuple[bool, str]:
        dialog = self.open_permanent_deny(host, "deny_global")
        dialog.locator("#confirm-host").fill(typed)
        button = dialog.get_by_role("button", name="Deny permanently")
        sent = self.sent_nothing_after(lambda: None) and button.is_disabled()
        text = squash(dialog.inner_text())
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
        return sent, text


# ---- checks ----------------------------------------------------------------------


@dataclass
class Result:
    status: str          # PASS | FAIL | SKIP
    viewport: str
    number: str
    name: str
    detail: str = ""

    def line(self) -> str:
        tag = "(design-change)" if self.status == "SKIP" else ""
        detail = f": {self.detail}" if self.detail and self.status == "FAIL" else ""
        return f"{self.status}{tag} [{self.viewport}] ({self.number}) {self.name}{detail}"


class Suite:
    def __init__(self, ui: str, viewport: str):
        self.ui, self.viewport = ui, viewport
        self.results: list[Result] = []

    def check(self, number: str, name: str, fn: Callable[[], None], *, design_change: bool = False) -> None:
        """Run `fn` (asserting inside); a design-change check is skipped on legacy."""
        if design_change and self.ui == "legacy":
            self.results.append(Result("SKIP", self.viewport, number, name))
            return
        try:
            fn()
            self.results.append(Result("PASS", self.viewport, number, name))
        except Exception as exc:  # noqa: BLE001 - any failure is a FAIL line, never a crash
            self.results.append(Result("FAIL", self.viewport, number, name, squash(str(exc))[:300]))


def expect_body(traffic_entry: dict, expected: dict) -> None:
    assert traffic_entry, "the page sent no decide request"
    assert traffic_entry["body"] == expected, f"sent {traffic_entry['body']}, expected {expected}"


def run_scenario(ui: str, viewport: str, page, drv: Driver, traffic: Traffic, broker: stub.StubBroker) -> list[Result]:
    from playwright.sync_api import expect

    suite = Suite(ui, viewport)
    check = suite.check
    expect.set_options(timeout=TIMEOUT_MS)
    total = len(stub.OPEN_ROWS)

    def overflow() -> None:
        scroll, inner = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert scroll <= inner, f"scrollWidth {scroll} > innerWidth {inner}"

    # ---- initial render -----------------------------------------------------
    expect(drv.requests()).to_have_count(total)
    by_bottle = {
        "alpha": ["check18.example.com", "a2.example.com", "a3.example.com", "192.0.2.55", "a1.example.com"],
        "mid": ["m2.example.com", "bad-request.example.com", "m1.example.com"],
        "zeta": ["z3.example.com", "z2.example.com", "z1.example.com"],
    }
    check("1", "Rows are grouped by bottle with the right open counts (any bottle order)",
          lambda: _eq(sorted(drv.groups()), BOTTLES_ALPHABETICAL))
    check("1", "By-bottle grouping: bottles alphabetical",
          lambda: _eq(drv.groups(), BOTTLES_ALPHABETICAL), design_change=True)
    check("2", "By-bottle: rows newest first within each bottle",
          lambda: _eq(drv.hosts(), [h for bottle in ("alpha", "mid", "zeta") for h in by_bottle[bottle]]),
          design_change=True)
    if ui == "spa":
        def all_view():
            drv.set_view("All")
            expect(page.locator("[data-testid=group]")).to_have_count(0)
            _eq(drv.hosts(), ["check18.example.com", "a2.example.com", "z3.example.com", "m2.example.com",
                              "bad-request.example.com", "a3.example.com", "m1.example.com", "z2.example.com",
                              "192.0.2.55", "a1.example.com", "z1.example.com"])
            assert "alpha" in drv.row("check18.example.com").inner_text(), "the bottle is not on line 2"
            drv.set_view("By bottle")
            expect(page.locator("[data-testid=group]")).to_have_count(3)
        check("3", "All view: newest first across bottles, bottle shown on line 2", all_view, design_change=True)
    else:
        check("3", "All view: newest first across bottles, bottle shown on line 2", lambda: None, design_change=True)

    def facts():
        text = drv.row("z1.example.com").inner_text()
        for needle in ("z1.example.com:443", "3 hits", "npm install"):
            assert needle in text, f"{needle!r} missing from {squash(text)!r}"
    check("4", "Row shows host:port, hit count and reason", facts)
    no_reason = {"spa": "No reason given", "legacy": "—"}[ui]
    check("4", "Row without a reason says so",
          lambda: _in(no_reason, drv.row("a1.example.com").inner_text()), design_change=True)
    apply_text = {
        "spa": ("IP address", "Apply failed after 1 attempt: an IP address needs a CIDR in the manifest",
                "Apply failed after 2 attempts: rule install failed"),
        "legacy": ("IP", "apply failed ×1: ip_requires_cidr", "apply failed ×2: apply_failed"),
    }[ui]

    def apply_facts():
        ip_text = squash(drv.row("192.0.2.55").inner_text())
        assert apply_text[0] in ip_text, f"IP marker {apply_text[0]!r} missing from {ip_text!r}"
        assert _loose(apply_text[1]) in _loose(ip_text), f"{apply_text[1]!r} missing from {ip_text!r}"
        m2_text = squash(drv.row("m2.example.com").inner_text())
        assert _loose(apply_text[2]) in _loose(m2_text), f"{apply_text[2]!r} missing from {m2_text!r}"
        assert "IP address" not in m2_text
    check("4", "Row shows the IP marker and the failed-apply text from the last_error object", apply_facts)

    def title_is(pattern: str) -> None:
        expect(page).to_have_title(re.compile(pattern))

    check("5", "Title carries the open count", lambda: title_is(rf"^\({total}\) "))
    check("5", "Title format is '(N) Egress · Djinn admin'",
          lambda: title_is(rf"^\({total}\) Egress · Djinn admin$"), design_change=True)
    if ui == "spa":
        def badge():
            expect(page.locator(".count-badge").first).to_have_text(str(total))
        check("5", "The Egress nav entry carries the count badge", badge, design_change=True)
    else:
        check("5", "The Egress nav entry carries the count badge", lambda: None, design_change=True)
    check("20", "No horizontal overflow (initial state)", overflow)

    # ---- the five actions and their exact bodies ------------------------------
    sent: dict[str, dict] = {}

    def do(key: str, host: str, action: str, **kwargs) -> None:
        sent[key] = drv.act(host, action, **kwargs)

    check("6", "Allow sends exactly {action, host, container}",
          lambda: (do("6", "a1.example.com", "allow_live"),
                   expect_body(sent["6"], {"action": "allow_live", "host": "a1.example.com", "container": "alpha"})))
    check("7", "Allow permanently · bottle sends exactly {action, host, container}",
          lambda: (do("7", "a2.example.com", "allow_manifest"),
                   expect_body(sent["7"], {"action": "allow_manifest", "host": "a2.example.com", "container": "alpha"})))
    check("8", "Deny sends exactly {action, host, container}",
          lambda: (do("8", "m1.example.com", "deny"),
                   expect_body(sent["8"], {"action": "deny", "host": "m1.example.com", "container": "mid"})))
    check("9", "Deny permanently · bottle sends {action, host, container, reason}",
          lambda: (do("9", "z2.example.com", "deny_bottle", reason="not needed"),
                   expect_body(sent["9"], {"action": "deny_bottle", "host": "z2.example.com",
                                           "container": "zeta", "reason": "not needed"})))
    check("10", "Deny permanently · global sends {action, host} with no container",
          lambda: (do("10", "a3.example.com", "deny_global", typed="a3.example.com"),
                   expect_body(sent["10"], {"action": "deny_global", "host": "a3.example.com"})))

    def plain_deny_offers_no_reason():
        assert "reason" not in sent["8"]["body"], "plain Deny sent a reason"
        # a1 was decided; check a fresh row still open
        assert drv.row("z1.example.com").get_by_role("textbox").count() == 0, "a reason field is offered"
    check("11", "Plain Deny offers and sends no reason", plain_deny_offers_no_reason, design_change=True)

    def wrong_host_refused():
        refused, text = drv.refuse_global("z1.example.com", "wrong.example.com")
        assert refused, "a decide was sent, or the confirm button was enabled, for a wrong host"
        assert re.search(r"exact host|does not match", text), f"no refusal shown: {text!r}"
    check("12", "Global deny with a wrong typed host sends nothing and shows the refusal", wrong_host_refused)
    check("13", "Global deny with a reason sends {action, host, reason}",
          lambda: (do("13", "z3.example.com", "deny_global", reason="telemetry", typed="z3.example.com"),
                   expect_body(sent["13"], {"action": "deny_global", "host": "z3.example.com", "reason": "telemetry"})))

    # ---- outcomes --------------------------------------------------------------
    def ip_note():
        do("14", "192.0.2.55", "allow_live")
        expect(drv.row("192.0.2.55")).to_contain_text(re.compile(r"add (the )?CIDR to (the )?manifest by hand", re.I))
    check("14", "ip_requires_cidr: the row says to add the CIDR by hand", ip_note)

    def apply_failed_note():
        do("15", "m2.example.com", "allow_live")
        expect(drv.row("m2.example.com")).to_contain_text(re.compile("rule install failed", re.I))
        assert re.search("stays queued", drv.row("m2.example.com").inner_text(), re.I), "row not marked as queued"
        assert drv.row("m2.example.com").is_visible(), "row left the queue"
    check("15", "apply_failed: the row stays queued and is marked", apply_failed_note)

    def bad_request_text():
        do("16", "bad-request.example.com", "deny")
        expect(drv.row("bad-request.example.com")).to_contain_text("bad request from broker")
        assert sent["16"], "no decide sent"
    check("16", "A 400 shows the server's error text on the row", bad_request_text)

    def outage_banner():
        with broker.lock:
            broker.outage = True
        try:
            before = len(traffic.statuses)
            do("17", "check18.example.com", "deny")
            deadline = time.monotonic() + 3
            while len(traffic.statuses) == before and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert traffic.statuses[-1] == 502, f"admin answered {traffic.statuses[-1:]} to a broker 500"
            expect(drv.stale_banner()).to_be_visible()
            assert drv.stale_banner().inner_text().strip(), "the banner is empty"
            assert drv.row("check18.example.com").is_visible(), "the row left despite the failed decide"
        finally:
            with broker.lock:
                broker.outage = False
        expect(drv.stale_banner()).to_be_hidden(timeout=BANNER_CLEAR_MS)
    check("17", "Broker 500 → admin 502 → stale banner, cleared by the next good poll", outage_banner)

    # ---- decided rows leave the queue and land in recent ------------------------
    def leaves_and_lands():
        alpha_before = dict(drv.groups()).get("alpha")
        do("18", "check18.example.com", "deny")
        expect(drv.requests().filter(has_text="check18.example.com:")).to_have_count(0)
        recent = drv.recent_text()
        assert "check18.example.com" in recent, f"not in recent: {squash(recent)[:200]}"
        assert dict(drv.groups()).get("alpha") == alpha_before - 1, f"alpha count: {drv.groups()}"
        # z1 is zeta's last open row: deciding it removes the group.
        do("18b", "z1.example.com", "deny")
        expect(drv.requests().filter(has_text="z1.example.com:")).to_have_count(0)
        assert "zeta" not in [name for name, _ in drv.groups()], f"zeta group remained: {drv.groups()}"
        assert "z1.example.com" in drv.recent_text()
    check("18", "A decided row leaves the queue, the group count decrements, a bottle's last row removes "
                "its group, and the row appears in recent", leaves_and_lands)
    check("5", "Title count follows the decided rows",
          lambda: title_is(rf"^\({len(stub.OPEN_ROWS) - 8}\) "))

    def recent_labels():
        text = drv.recent_text()
        for needle in ("registry.npmjs.org", "Allowed", "ads.example.com", "Denylist", "denied.example.com",
                       "Denied permanently · global", "telemetry", "z1.example.com", "Denied"):
            assert needle in text, f"{needle!r} missing from recent: {squash(text)[:300]}"
    check("22", "Recent rows carry destination, bottle, decision label and deny reason", recent_labels,
          design_change=True)

    def reason_limit():
        dialog = drv.open_permanent_deny("m2.example.com", "deny_bottle")
        dialog.locator("#deny-reason").fill("x" * 250)
        assert len(dialog.locator("#deny-reason").input_value()) == 200, "reason not capped at 200"
        assert "200/200" in dialog.inner_text(), "no character counter"
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
    check("23", "Permanent-deny dialog caps the reason at 200 characters with a counter", reason_limit,
          design_change=True)

    # ---- request headers, overflow, console ---------------------------------------
    def headers_ok():
        assert traffic.decides, "no decide requests were captured"
        for entry in traffic.decides:
            headers = entry["headers"]
            assert headers.get("x-admin-ui") == "1", f"X-Admin-UI: {headers.get('x-admin-ui')!r}"
            assert headers.get("content-type", "").split(";")[0] == "application/json", headers.get("content-type")
    check("19", "Every decide carries X-Admin-UI: 1 and a JSON content type", headers_ok)
    check("20", "No horizontal overflow (after decisions)", overflow)
    check("21", "No console errors or uncaught page errors",
          lambda: _eq(traffic.console_errors, []))
    return suite.results


def _eq(actual, expected) -> None:
    assert actual == expected, f"got {actual!r}, expected {expected!r}"


def _loose(text: str) -> str:
    """Text without whitespace: legacy chips render `x:y` where the SPA renders `x: y`."""
    return "".join(text.split())


def _in(needle: str, text: str) -> None:
    assert needle in text, f"{needle!r} not in {squash(text)!r}"


# ---- serving --------------------------------------------------------------------


class Served:
    """The real admin daemon in front of the stub broker."""

    def __init__(self, ui: str, broker_url: str):
        self.home = Path(tempfile.mkdtemp(prefix="admin-ui-home-"))
        root = self.home / "run" / "egress"
        root.mkdir(parents=True)
        (root / admin.OPERATOR_TOKEN_FILENAME).write_text("operator-test-token\n", encoding="utf-8")
        env = {"DJINN_HOME": str(self.home), "EGRESS_BROKER_URL": broker_url}
        if ui == "spa":
            env["DJINN_ADMIN_UI"] = "spa"
            env["DJINN_ADMIN_UI_DIST"] = str(WORKTREE / "admin" / "ui" / "dist")
        self.patcher = mock.patch.dict(os.environ, env, clear=False)
        self.patcher.start()
        if ui == "legacy":
            os.environ.pop("DJINN_ADMIN_UI", None)  # the flag unset is the legacy page
        self.server = admin.AdminHTTPServer(
            ("127.0.0.1", 0), egress_root=root, session_secret="session-secret",
            operator_token="operator-test-token", admin_key="admin-test-key")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        wait_for_tcp_listening(self.host, self.port)
        self.base = f"http://{self.host}:{self.port}"
        self.cookie = self._mint_cookie()

    def _mint_cookie(self) -> str:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", "/session?key=admin-test-key")
        response = conn.getresponse()
        set_cookie = response.getheader("Set-Cookie", "")
        response.read()
        conn.close()
        for part in set_cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if name == admin.SESSION_COOKIE_NAME and sep:
                return value
        raise RuntimeError("no session cookie minted")

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        join_thread_or_fail(self.thread, label="admin daemon")
        self.patcher.stop()


def new_page(browser, served: Served, viewport: str, theme: str):
    context = browser.new_context(
        viewport=VIEWPORTS[viewport], color_scheme=theme,
        device_scale_factor=2 if viewport == "phone" else 1)
    context.add_cookies([{
        "name": admin.SESSION_COOKIE_NAME, "value": served.cookie, "domain": served.host,
        "path": "/", "httpOnly": True, "sameSite": "Strict"}])
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    traffic = Traffic()
    traffic.attach(page)
    return context, page, traffic


def open_queue(page, served: Served, driver_cls, traffic: Traffic) -> Driver:
    from playwright.sync_api import expect

    page.goto(served.base + "/")
    drv = driver_cls(page, traffic)
    expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
    return drv


def capture(page, out: Path, ui: str, viewport: str, theme: str, state: str) -> None:
    path = out / f"{ui}-{viewport}-{theme}-{state}.png"
    page.screenshot(path=str(path), full_page=True)
    log(f"capture path={path.name} bytes={path.stat().st_size}")


def capture_states(browser, served, broker, driver_cls, out: Path, ui: str, viewport: str, theme: str) -> None:
    """Initial queue, a dialog (spa), and a queue carrying the three outcome notes."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        drv = open_queue(page, served, driver_cls, traffic)
        capture(page, out, ui, viewport, theme, "initial")
        if ui == "spa":
            drv.open_permanent_deny("z1.example.com", "deny_global")
            page.get_by_role("dialog").locator("#confirm-host").fill("z1.example")
            capture(page, out, ui, viewport, theme, "dialog")
            page.get_by_role("button", name="Cancel").click()
            page.get_by_role("dialog").wait_for(state="hidden")
        drv.act("192.0.2.55", "allow_live")
        drv.act("m2.example.com", "allow_live")
        drv.act("bad-request.example.com", "deny")
        page.wait_for_timeout(500)
        capture(page, out, ui, viewport, theme, "outcomes")
    finally:
        context.close()


# ---- legacy → behaviour mapping ------------------------------------------------------

LEGACY_MAP = [
    # (original check in the legacy suite, behaviour check(s), note)
    ("`{light,dark}/mobile: no horizontal overflow` (1 site, 2 lines)", "(20)", "now at desktop, tablet and phone, before and after decisions"),
    ("`tab title carries the count`", "(5) count; (5) format is design-change", "the title text changed on purpose: `(N) Egress · Djinn admin` (PLN, Architecture); the count assertion runs on both UIs"),
    ("`group headers by bottle in first-seen order`", "(1) design-change", "PLN decision 2026-09-23: bottles alphabetical, replacing legacy's first-seen order"),
    ("`bottle-a's two rows stay together ahead of bottle-b`", "(1), (2) design-change", "contiguity is asserted by the grouped host order; the order itself is a PLN decision (newest first within a bottle)"),
    ("`four columns per request row`", "retired: asserts legacy DOM", "the SPA row is two lines in three columns; the facts it carried are covered by (4)"),
    ("`destination cell: host:port and hits`", "(4)", ""),
    ("`reason cell`", "(4)", ""),
    ("`requested cell carries raw UTC in title`", "retired: asserts legacy DOM", "a tooltip attribute on a legacy cell; the SPA shows relative time and the clock time instead"),
    ("`missing reason renders an em-dash`", "(4) design-change", "the SPA says `No reason given`, per the accepted mockup"),
    ("`IP badge and apply-failed chip on the IP row`", "(4)", "text differs per UI (legacy `apply failed ×1: ip_requires_cidr`, SPA `Apply failed after 1 attempt: an IP address needs a CIDR in the manifest`); each UI asserts its own text from the `last_error` object, on both `ip_requires_cidr` and `apply_failed` rows"),
    ("`recent list renders decided time, bottle, destination, outcome, by`", "(18), (22) design-change", "(18) asserts a decided row appears in recent on both UIs; (22) asserts the SPA's decision labels (the legacy `denied / global` text is retired with the legacy table)"),
    ("`deny global with a wrong typed host is refused inline`", "(12)", "legacy shows an inline chip; the SPA disables the dialog button and shows a mismatch message"),
    ("`no decide POST was sent on the refusal`", "(12)", ""),
    ("`deny global body omits container and carries the reason`", "(13), (10)", "(10) adds the reason-less body"),
    ("`decide carries the UI header and JSON content type`", "(19)", "asserted for every decide the page sent"),
    ("`Allow` / `Allow+manifest` / `Deny` / `Deny always (bottle)` post exactly `{action, host, container}` (1 site, 4 lines)", "(6), (7), (8), (9)", "(9) also carries the reason legacy offered"),
    ("`IP-literal allow shows the CIDR-by-hand chip`", "(14)", ""),
    ("`group header decremented after the decision`", "(18)", "both UIs assert the alpha count 5 → 4 (legacy header `alpha - 4 request(s)`)"),
    ("`the decided row moved to the recent list with its outcome`", "(18)", ""),
    ("`tab title count follows`", "(5)", ""),
    ("`bottle-a's header disappears after its last request is decided`", "(18)", "asserted on zeta"),
]


def mapping_markdown() -> str:
    rows = ["| Original legacy check | Behaviour check | Note |", "|---|---|---|"]
    rows += [f"| {a} | {b} | {c} |" for a, b, c in LEGACY_MAP]
    return (
        "# Legacy check → behaviour check mapping\n\n"
        "Every check in the pre-refactor `tests/admin_ui_playwright.py` (21 call sites, 24 report lines when the "
        "overflow and action loops expand) and where it went. `design-change` checks assert a deliberate "
        "difference from the legacy page and report SKIP under `--ui legacy`.\n\n"
        + "\n".join(rows) + "\n\n"
        "New behaviour checks with no legacy original: (1) as a full-order assertion, (3) the All view, (11) plain "
        "Deny without a reason, (15) `apply_failed` outcome, (16) a 400 on the row, (17) the stale banner and its "
        "recovery, (21) no console errors, (22) recent labels, (23) the dialog's reason limit.\n"
    )


# ---- main -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ui", choices=["legacy", "spa"], default="legacy")
    ui = parser.parse_args().ui
    out = Path(os.environ.get("ADMIN_UI_SHOTS") or tempfile.mkdtemp(prefix="admin-ui-"))
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    if ui == "spa" and not (WORKTREE / "admin" / "ui" / "dist" / "index.html").is_file():
        print("admin/ui/dist is not built: run `cd admin/ui && npm ci && npm run build` first", file=sys.stderr)
        return 2

    broker = stub.StubBroker(log=lambda message: None)
    try:
        broker.preflight()
    except stub.ContractViolation as exc:
        print(f"REFUSING TO START: the stub broker drifted from the contract: {exc}", file=sys.stderr)
        return 2
    log(f"stage=preflight ok schemas={stub.QUEUE_SCHEMA},{stub.DECIDE_SCHEMA},{stub.ERROR_SCHEMA}")
    broker_url = broker.start()
    served = Served(ui, broker_url)
    log(f"stage=serve ui={ui} admin={served.base} broker={broker_url}")

    driver_cls = SpaDriver if ui == "spa" else LegacyDriver
    results: list[Result] = []
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for viewport in VIEWPORTS:
                broker.reset()
                context, page, traffic = new_page(browser, served, viewport, "light")
                t0 = time.monotonic()
                try:
                    drv = open_queue(page, served, driver_cls, traffic)
                    results += run_scenario(ui, viewport, page, drv, traffic, broker)
                except Exception as exc:  # noqa: BLE001 - the scenario could not even start
                    results.append(Result("FAIL", viewport, "0", "queue renders", squash(str(exc))[:300]))
                finally:
                    context.close()
                log(f"stage=scenario viewport={viewport} ms={int((time.monotonic() - t0) * 1000)} "
                    f"decides={len(broker.decides)}")
                for theme in ("light", "dark"):
                    capture_states(browser, served, broker, driver_cls, out, ui, viewport, theme)
            browser.close()
    finally:
        served.stop()
        broker.stop()

    if broker.violations:
        for violation in broker.violations:
            print(f"CONTRACT VIOLATION: {violation}", file=sys.stderr)
        return 2

    printed = [result.line() for result in results]
    counts = {status: sum(1 for r in results if r.status == status) for status in ("PASS", "SKIP", "FAIL")}
    summary = f"{ui.upper()}: {counts['PASS']} PASS, {counts['SKIP']} SKIP(design-change), {counts['FAIL']} FAIL"
    mapping = mapping_markdown()
    (out / "legacy-mapping.md").write_text(mapping, encoding="utf-8")
    (out / f"REPORT-{ui}.md").write_text("\n".join(f"- {line}" for line in printed) + f"\n\n{summary}\n", encoding="utf-8")
    print("\n".join(printed))
    print(f"\n{summary}  ({int(time.monotonic() - started)}s, captures in {out})")
    print("\n" + mapping)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
