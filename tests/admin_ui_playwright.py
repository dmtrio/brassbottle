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
`design-change` asserts a deliberate difference from the legacy page and names
the PLN Architecture decision that makes it deliberate; `--ui legacy` reports it
as SKIP(design-change: <decision>) instead of running it. A check of behaviour
that only exists in the new app (`spa-only`) reports N/A(spa-only) on legacy.

Exit status: 0 with no FAIL, 1 with any FAIL, 2 when the stub violates the contract.
"""
from __future__ import annotations

import argparse
import calendar
import gc
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable
from unittest import mock
from urllib.parse import parse_qs, urlsplit

WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "tests"))
sys.path.insert(0, str(WORKTREE / "admin" / "ui" / "scripts"))

import admin_daemon as admin  # noqa: E402
import admin_ui_stub_broker as stub  # noqa: E402
import build_inputs  # noqa: E402
import png_pixels  # noqa: E402
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
FIXTURE_ROW = {row[2]: row for row in stub.OPEN_ROWS}   # host -> (id, bottle, host, port, ...)

# The PLN Architecture decisions a design-change check may cite, by exact name.
D_ORDERING = "Queue ordering and views"
D_ACTIONS = "Action labels map onto the existing decide API unchanged"
D_TITLE = "Tab title carries the open count"

# Records every stale-banner text the page renders, so a banner that flashes and
# clears is still seen. Works on both UIs (the SPA's test id, legacy's class).
BANNER_OBSERVER = """() => {
  window.__banners = [];
  const read = () => {
    const el = document.querySelector('[data-testid=stale-banner], .banner.error');
    const text = el ? (el.innerText || '').trim() : '';
    if (text) window.__banners.push(text);
  };
  new MutationObserver(read).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true});
  read();
}"""


def log(message: str) -> None:
    print(f"[suite] {message}", file=sys.stderr, flush=True)


TASK_WARNING = "Task was destroyed but it is pending"


class AsyncioWarnings(logging.Handler):
    """Collects what asyncio logs while the suite runs (it reports a task that was dropped while pending as an
    error-level log line on stderr, at garbage collection, where nothing else would catch it)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.pending: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # A handler on the asyncio logger turns off logging's last-resort stderr output for it,
        # so every record is still written to stderr here; the dropped-task ones are also kept.
        sys.stderr.write(self.format(record) + "\n")
        text = record.getMessage()
        if TASK_WARNING in text:
            self.pending.append(squash(text)[:300])


BUNDLE = "admin/ui/dist/index.html"


def bundle_refusal(root: Path) -> str | None:
    """Why the spa suite must not run against `root`'s dist/, or None when it is fresh.

    `npm run build` records every input it read in dist/.build-inputs.json. The
    bundle is stale when the inputs now differ from that record: a file added,
    deleted or changed. A build that FAILS is caught too: it never writes the
    manifest, so a failure that matters (an input changed since the last good
    build) shows as that input's difference.
    """
    if not (root / BUNDLE).is_file():
        return "admin/ui/dist is not built: run `cd admin/ui && npm ci && npm run build` first"
    started = time.monotonic()
    recorded = build_inputs.read_manifest(root)
    if recorded is None:
        log(f"stage=bundle manifest=missing duration={time.monotonic() - started:.2f}s")
        return ("REFUSING TO RUN: admin/ui/dist has no readable build-input manifest (dist/.build-inputs.json), "
                "so it cannot be told from a stale bundle. Run `cd admin/ui && npm run build` and check that it exits 0.")
    added, deleted, changed = build_inputs.drift(root, recorded)
    log(f"stage=bundle recorded={len(recorded)} added={len(added)} deleted={len(deleted)} changed={len(changed)} "
        f"duration={time.monotonic() - started:.2f}s")
    if not (added or deleted or changed):
        return None
    shown = [f"{kind} {path}" for kind, paths in (("added", added), ("deleted", deleted), ("changed", changed))
             for path in paths]
    total = len(shown)
    return (f"REFUSING TO RUN: admin/ui/dist was built from different inputs than the current ones "
            f"({total} differ: {', '.join(shown[:5])}{', …' if total > 5 else ''}): a failed or skipped build leaves the "
            "previous bundle in place. Run `cd admin/ui && npm run build` and check that it exits 0.")


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
    queue_statuses: list[int] = field(default_factory=list)
    recent: list[dict] = field(default_factory=list)    # query of each GET /api/egress/recent, as {name: value}
    recent_done: list[dict] = field(default_factory=list)  # the same queries once the page has the whole reply
    console_errors: list[str] = field(default_factory=list)
    expected_failures: int = 0                          # scripted 4xx/5xx the browser logs

    def attach(self, page) -> None:
        def on_request(request):
            if request.method == "GET" and urlsplit(request.url).path == "/api/egress/recent":
                self.recent.append({k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()})
            if request.method == "POST" and request.url.endswith("/api/egress/decide"):
                self.decides.append({"body": json.loads(request.post_data or "{}"), "headers": request.headers})

        def on_finished(request):
            if request.method == "GET" and urlsplit(request.url).path == "/api/egress/recent":
                self.recent_done.append({k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()})

        def on_response(response):
            if response.request.method == "POST" and response.url.endswith("/api/egress/decide"):
                self.statuses.append(response.status)
            elif response.url.endswith("/api/egress/queue"):
                self.queue_reads += 1
                self.queue_statuses.append(response.status)

        def on_console(message):
            if message.type != "error":
                return
            url = (message.location or {}).get("url", "")
            # The browser logs every scripted 4xx/5xx from the admin API; those
            # are the outcomes under test, not page errors.
            if message.text.startswith("Failed to load resource") and re.search(r"/api/egress/(decide|queue|recent|stream)(\?.*)?$", url):
                self.expected_failures += 1
                return
            self.console_errors.append(message.text)

        page.on("request", on_request)
        page.on("requestfinished", on_finished)
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

    def recent_rows(self) -> list[str]:
        raise NotImplementedError

    def stale_banner(self):
        raise NotImplementedError

    def note(self, host: str) -> str:
        """Inline outcome text on a row, apart from the row's own facts."""
        raise NotImplementedError

    def wait_note(self, host: str, pattern: str) -> str:
        """The row's outcome text once it matches `pattern` (case-insensitive)."""
        self._wait_until(lambda: re.search(pattern, self.note(host), re.I) is not None, TIMEOUT_MS / 1000)
        text = self.note(host)
        assert re.search(pattern, text, re.I), f"row note {text!r} does not match {pattern!r}"
        return text

    # -- acting
    def act(self, host: str, action: str, *, reason: str | None = None, typed: str | None = None,
            aborted: bool = False, reread: bool = True) -> dict:
        """Perform one action on a row and return the decide the browser sent.

        `aborted`: the request is expected to fail on the wire (no response).
        `reread`: wait for the queue re-read a decide normally triggers; a
        failed decide may legitimately not trigger one.
        """
        before = len(self.traffic.decides)
        if aborted:
            with self.page.expect_event("requestfailed", timeout=TIMEOUT_MS):
                self._click_action(host, action, reason, typed)
        else:
            with self.page.expect_response(lambda r: r.url.endswith("/api/egress/decide"), timeout=TIMEOUT_MS):
                self._click_action(host, action, reason, typed)
        self._wait_until(lambda: len(self.traffic.decides) > before, 2)
        sent = self.traffic.decides[-1] if len(self.traffic.decides) > before else {}
        # Let the queue re-read that follows a decide land and render before the
        # next action: the legacy table has unkeyed rows, so a click aimed while
        # rows shift can hit the row that moved into place.
        if reread:
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

    def recent_rows(self):
        panel = self.page.locator("section.panel").filter(has_text="Recent decisions")
        return [squash(text) for text in panel.locator("tbody tr").all_inner_texts()]

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

    def open_denylist_group(self) -> None:
        """Expand the recent list's denylist group (the SPA folds the denylist's hits into one collapsed group)."""
        toggle = self.page.locator("[data-testid=denylist-toggle]")
        if toggle.count() and toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
            self.page.locator("[data-testid=denylist-group] [data-testid=recent-row]").first.wait_for()

    def recent_text(self):
        self.open_denylist_group()
        return self.page.locator("[data-testid=recent]").inner_text()

    def recent_rows(self):
        self.open_denylist_group()
        return [squash(text) for text in self.page.locator("[data-testid=recent-row]").all_inner_texts()]

    def stale_banner(self):
        return self.page.locator("[data-testid=stale-banner]")

    def note(self, host):
        return squash(self.row(host).locator("[role=status]").inner_text())

    def set_view(self, label: str) -> None:
        self.page.locator("[data-testid=queue-view]").locator("button, [role=radio]").filter(
            has_text=re.compile(rf"^\s*{label}\s*$")).click()

    def button(self, label: str, host: str, port: int | None = None, bottle: str | None = None):
        """A row button by its unique accessible name, e.g. `Allow a1.example.com:443 in alpha`."""
        _id, fixture_bottle, _host, fixture_port, *_rest = FIXTURE_ROW[host]
        target = f"{host}:{port or fixture_port} in {bottle or fixture_bottle}"
        return self.page.get_by_role("button", name=f"{label} {target}", exact=True)

    def _menu(self, host, trigger, item):
        self.button(trigger, host).click()
        self.page.get_by_role("menuitem", name=item).click()

    def open_permanent_deny(self, host: str, action: str):
        item = "Deny permanently · bottle" if action == "deny_bottle" else "Deny permanently · global"
        self._menu(host, "More deny options for", item)
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for()
        return dialog

    def _click_action(self, host, action, reason, typed):
        if action == "allow_live":
            retry = self.button("Retry allow", host)
            (retry if retry.count() else self.button("Allow", host)).click()
        elif action == "allow_manifest":
            self._menu(host, "More allow options for", "Allow permanently · bottle")
        elif action == "deny":
            self.button("Deny", host).click()
        else:
            dialog = self.open_permanent_deny(host, action)
            if reason:
                dialog.locator("#deny-reason").fill(reason)
            if typed is not None:
                dialog.locator("#confirm-host").fill(typed)
            dialog.get_by_role("button", name="Deny permanently").click()

    def refuse_global(self, host: str, typed: str) -> tuple[bool, str]:
        dialog = self.open_permanent_deny(host, "deny_global")
        confirm = dialog.locator("#confirm-host")
        confirm.fill(typed)
        button = dialog.get_by_role("button", name="Deny permanently")
        # Try every way of submitting: Enter in the field and a forced click on
        # the button. The guard, not just the disabled state, must refuse them.
        sent = self.sent_nothing_after(lambda: (confirm.press("Enter"), button.click(force=True))) \
            and button.is_disabled()
        text = squash(dialog.inner_text())
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
        return sent, text


# ---- checks ----------------------------------------------------------------------


@dataclass
class Result:
    status: str          # PASS | FAIL | SKIP | N/A
    viewport: str
    number: str
    name: str
    detail: str = ""     # FAIL: what failed. SKIP: the PLN decision. N/A: why.

    def line(self) -> str:
        tag = ""
        if self.status == "SKIP":
            tag = f"(design-change: {self.detail})"
        elif self.status == "N/A":
            tag = f"({self.detail})"
        detail = f": {self.detail}" if self.detail and self.status == "FAIL" else ""
        return f"{self.status}{tag} [{self.viewport}] ({self.number}) {self.name}{detail}"


class Suite:
    def __init__(self, ui: str, viewport: str):
        self.ui, self.viewport = ui, viewport
        self.results: list[Result] = []

    def check(self, number: str, name: str, fn: Callable[[], None], *,
              decision: str | None = None, spa_only: bool = False) -> None:
        """Run `fn` (asserting inside).

        `decision`: the PLN Architecture decision that makes this check assert a
        deliberate difference from legacy; skipped (SKIP) on legacy.
        `spa_only`: behaviour that only exists in the new app; N/A on legacy.
        """
        assert not (decision and spa_only), "a check is design-change or spa-only, not both"
        if decision and self.ui == "legacy":
            self.results.append(Result("SKIP", self.viewport, number, name, decision))
            return
        if spa_only and self.ui == "legacy":
            self.results.append(Result("N/A", self.viewport, number, name, "spa-only"))
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
          lambda: _eq(drv.groups(), BOTTLES_ALPHABETICAL), decision=D_ORDERING)
    check("2", "By-bottle: rows newest first within each bottle",
          lambda: _eq(drv.hosts(), [h for bottle in ("alpha", "mid", "zeta") for h in by_bottle[bottle]]),
          decision=D_ORDERING)

    def all_view():
        drv.set_view("All")
        expect(page.locator("[data-testid=group]")).to_have_count(0)
        _eq(drv.hosts(), ["check18.example.com", "a2.example.com", "z3.example.com", "m2.example.com",
                          "bad-request.example.com", "a3.example.com", "m1.example.com", "z2.example.com",
                          "192.0.2.55", "a1.example.com", "z1.example.com"])
        assert "alpha" in drv.row("check18.example.com").inner_text(), "the bottle is not on line 2"
        drv.set_view("By bottle")
        expect(page.locator("[data-testid=group]")).to_have_count(3)
    check("3", "All view: newest first across bottles, bottle shown on line 2", all_view, decision=D_ORDERING)

    def facts():
        text = drv.row("z1.example.com").inner_text()
        for needle in ("z1.example.com:443", "3 hits", "npm install"):
            assert needle in text, f"{needle!r} missing from {squash(text)!r}"
    check("4", "Row shows host:port, hit count and reason", facts)
    no_reason = {"spa": "No reason given", "legacy": "—"}[ui]
    check("4", "Row without a reason says so",
          lambda: _in(no_reason, drv.row("a1.example.com").inner_text()))
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
          lambda: title_is(rf"^\({total}\) Egress · Djinn admin$"), decision=D_TITLE)

    def badge():
        expect(page.locator(".count-badge").first).to_have_text(str(total))
    check("5b", "The Egress nav entry carries the count badge", badge, spa_only=True)
    check("20", "No horizontal overflow (initial state)", overflow)

    def layout():
        rows = page.evaluate("""() => [...document.querySelectorAll('[data-testid=request]')].map((row) => {
            const box = row.closest('.panel, .item-card').getBoundingClientRect();
            const actions = row.querySelector('[data-testid=decide-actions]').getBoundingClientRect();
            const host = row.querySelector('[data-testid=request-host]');
            const size = parseFloat(getComputedStyle(host).fontSize);
            const line = parseFloat(getComputedStyle(host).lineHeight) || size * 1.5;
            return {host: host.textContent, actionsLeft: actions.left, actionsRight: actions.right,
                    panelLeft: box.left, panelRight: box.right, hostHeight: host.getBoundingClientRect().height, line};
        })""")
        assert len(rows) == total, f"{len(rows)} rows measured, expected {total}"
        for r in rows:
            assert r["actionsLeft"] >= r["panelLeft"] - 0.5 and r["actionsRight"] <= r["panelRight"] + 0.5, \
                f"{r['host']}: the action group [{r['actionsLeft']:.0f}, {r['actionsRight']:.0f}] leaves the panel " \
                f"[{r['panelLeft']:.0f}, {r['panelRight']:.0f}]"
            assert r["hostHeight"] <= r["line"] * 1.05, \
                f"{r['host']}: host text wraps ({r['hostHeight']:.0f}px tall for a {r['line']:.0f}px line)"
    check("26", "Every row's action group sits inside its panel and no host breaks across lines",
          layout, spa_only=True)

    def accessible_names():
        names = page.evaluate("""() => [...document.querySelectorAll('[data-testid=request]')].flatMap(
            (row) => [...row.querySelectorAll('button')].map((b) => b.getAttribute('aria-label') || b.innerText.trim()))""")
        assert len(names) == 4 * total, f"{len(names)} row buttons, expected {4 * total}"
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, f"row buttons share accessible names: {dupes[:3]}"
        for name in ("Allow a1.example.com:443 in alpha", "More deny options for a1.example.com:443 in alpha",
                     "Retry allow m2.example.com:443 in mid"):
            assert name in names, f"{name!r} not among the row buttons"
    check("24", "Every row button has its own accessible name (action, destination, bottle)",
          accessible_names, spa_only=True)

    def live_regions():
        regions = page.locator("[data-testid=request] [role=status]")
        assert regions.count() == total, f"{regions.count()} status regions for {total} rows"
        assert all(not text.strip() for text in regions.all_inner_texts()), "a status region is not empty before any decision"
    check("25", "Each row's status live region is rendered before any outcome and starts empty",
          live_regions, spa_only=True)

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
    check("11", "Plain Deny offers and sends no reason", plain_deny_offers_no_reason, decision=D_ACTIONS)

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
        # The row's own last_error already says "rule install failed"; the
        # outcome note is what this decide adds, so it must be absent before.
        assert "stays queued" not in drv.note("m2.example.com").lower(), "the outcome note exists before the decide"
        do("15", "m2.example.com", "allow_live")
        note = drv.wait_note("m2.example.com", r"rule install failed.*stays queued")
        assert re.search("stays queued", note, re.I), "row not marked as queued"
        assert drv.row("m2.example.com").is_visible(), "row left the queue"
    check("15", "apply_failed: the row stays queued and is marked", apply_failed_note)

    def bad_request_text():
        do("16", "bad-request.example.com", "deny")
        expect(drv.row("bad-request.example.com")).to_contain_text("bad request from broker")
        assert sent["16"], "no decide sent"
    check("16", "A 400 shows the server's error text on the row", bad_request_text)

    def decide_failure(number: str, name: str, *, broker_status: int | None, admin_status: int | None,
                       banner: str, clears: bool, abort: bool = False) -> None:
        """One decide fails while /queue polls keep succeeding: the banner can only come from the decide."""
        def run():
            row = "check18.example.com"
            assert not broker.outage, "the stub is in a full outage, not a decide-only one"
            page.evaluate(BANNER_OBSERVER)
            reads = len(traffic.queue_statuses)
            statuses = len(traffic.statuses)
            if abort:
                page.route("**/api/egress/decide", lambda route: route.abort())
            else:
                with broker.lock:
                    broker.decide_outage = broker_status
            try:
                sent = drv.act(row, "deny", aborted=abort, reread=False)
                assert sent, "the page sent no decide request"
                if admin_status is not None:
                    assert traffic.statuses[statuses:] == [admin_status], \
                        f"admin answered {traffic.statuses[statuses:]}, expected [{admin_status}]"
                page.wait_for_timeout(300)
            finally:
                if abort:
                    page.unroute("**/api/egress/decide")
                with broker.lock:
                    broker.decide_outage = None
            seen = page.evaluate("window.__banners")
            assert any(banner in text for text in seen), f"no banner containing {banner!r} was shown; saw {seen}"
            assert set(traffic.queue_statuses[reads:]) <= {200}, \
                f"a queue poll failed ({traffic.queue_statuses[reads:]}): the banner may not come from the decide"
            assert drv.row(row).is_visible(), "the row left despite the failed decide"
            if ui == "spa":
                assert all(text.startswith("Decision not sent: ") for text in seen), \
                    f"a decide-only failure must not read as stale data: {seen}"
                drv.wait_note(row, "^Not sent: " + re.escape(banner))
            if clears:
                expect(drv.stale_banner()).to_be_hidden(timeout=BANNER_CLEAR_MS)
        check(number, name, run)

    decide_failure("17", "Broker 500 on decide only → admin 502 → stale banner from the decide, cleared by the next good poll",
                   broker_status=500, admin_status=502, banner="decide failed on the daemon", clears=True)
    decide_failure("17b", "Broker 401 on decide only → admin 503 → stale banner from the decide",
                   broker_status=401, admin_status=503, banner="operator token rejected by daemon", clears=False)
    decide_failure("17c", "Decide request aborted on the wire → stale banner from the decide, cleared by the next good poll",
                   broker_status=None, admin_status=None, banner="egress daemon unreachable", clears=True, abort=True)

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

    def recent_shows_bottle_and_outcome():
        rows = drv.recent_rows()
        for host, bottle, outcome, reason in (
            ("registry.npmjs.org", "mid", "allowed", None), ("denied.example.com", "alpha", "denied", "telemetry"),
            ("ads.example.com", "zeta", "denied|denylist", "denylist: telemetry"),
            ("check18.example.com", "alpha", "denied", None), ("z1.example.com", "zeta", "denied", None),
        ):
            text = next((row for row in rows if f"{host}:" in row), None)
            assert text, f"{host} is not in recent: {rows}"
            assert bottle in text, f"bottle {bottle!r} missing from the recent row {text!r}"
            assert re.search(outcome, text, re.I), f"outcome {outcome!r} missing from the recent row {text!r}"
            if reason:
                assert reason in text, f"deny reason {reason!r} missing from the recent row {text!r}"
    check("22", "Recent rows show the destination, the bottle, the outcome and the deny reason of each decision",
          recent_shows_bottle_and_outcome)

    def recent_labels():
        text = drv.recent_text()
        for needle in ("registry.npmjs.org", "Allowed", "ads.example.com", "Denylist", "denied.example.com",
                       "Denied permanently · global", "z1.example.com", "Denied"):
            assert needle in text, f"{needle!r} missing from recent: {squash(text)[:300]}"
    check("22b", "Recent rows carry the decision labels", recent_labels, spa_only=True)

    def recent_separators():
        flows = page.evaluate("""() => [...document.querySelectorAll('[data-testid=recent-row] .meta-flow')].map((flow) => {
            const clip = flow.parentElement;
            const clipBox = clip.getBoundingClientRect();
            const boxes = [...flow.children].map((item) => item.getBoundingClientRect());
            const sep = parseFloat(getComputedStyle(flow.children[0], '::before').width);
            const bad = []; let wraps = 0;
            boxes.forEach((box, i) => {
                // items are centred, so a shorter one sits lower on its own line: compare with the previous bottom
                const first = i === 0 || box.top >= boxes[i - 1].bottom - 1;
                if (i > 0 && first) wraps += 1;
                // a separator sits in the item's left padding; on the first item
                // of a line it must lie outside the clip box, so it is not drawn
                if (first && box.left + sep > clipBox.left + 1) bad.push(i);
            });
            return {clips: getComputedStyle(clip).overflowX === 'hidden', bad, wraps};
        })""")
        assert flows, "no recent rows"
        assert all(f["clips"] for f in flows), "the recent meta line is not clipped, so a wrapped separator shows"
        assert not any(f["bad"] for f in flows), f"a separator starts a wrapped line: {flows}"
        if viewport == "phone":
            assert sum(f["wraps"] for f in flows) >= 1, "no recent row wraps at phone width, so this check proves nothing"
    check("27", "A recent row that wraps leaves no dangling separator", recent_separators, spa_only=True)

    def reason_limit():
        dialog = drv.open_permanent_deny("m2.example.com", "deny_bottle")
        dialog.locator("#deny-reason").fill("x" * 250)
        assert len(dialog.locator("#deny-reason").input_value()) == 200, "reason not capped at 200"
        assert "200/200" in dialog.inner_text(), "no character counter"
        assert "(optional, shown to the agent)" in dialog.inner_text(), "the reason label is not the design's"
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
    check("23", "Permanent-deny dialog caps the reason at 200 characters with a counter", reason_limit,
          decision=D_ACTIONS)

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


# ---- scenarios that need their own page ---------------------------------------------


def add_siblings(broker: stub.StubBroker) -> None:
    """Four more open requests around a1.example.com: alpha:8443 (same bottle, another port), zeta:443 (another
    bottle), api.a1.example.com in alpha (a subzone of it) and xa1.example.com in alpha (a lookalike, not a subzone)."""
    with broker.lock:
        base = next(row for row in broker.queue["open"] if row["request_id"] == "a1")
        opened = datetime.strptime(base["opened_at"], "%Y-%m-%dT%H:%M:%SZ")
        extra = (("a1p", "alpha", "a1.example.com", 8443), ("a1z", "zeta", "a1.example.com", 443),
                 ("a1s", "alpha", "api.a1.example.com", 443), ("a1x", "alpha", "xa1.example.com", 443))
        for offset, (rid, container, host, port) in enumerate(extra, start=1):
            broker.queue["open"].append({
                **base, "request_id": rid, "container": container, "host": host, "port": port,
                "opened_at": stub._iso(opened + timedelta(seconds=offset)), "age_seconds": base["age_seconds"] - offset,
            })
        broker.queue["open"].sort(key=lambda row: row["opened_at"])
        broker.queue["count"] = len(broker.queue["open"])


def _held_decides(page, traffic: Traffic, drv: "SpaDriver"):
    """Hold every decide POST at the page's network layer. Returns (held routes, release_next(), button(...))."""
    held: list = []
    page.route("**/api/egress/decide", lambda route: held.append(route))
    answered = [len(traffic.statuses)]

    def release_next() -> None:
        held.pop(0).continue_()
        answered[0] += 1
        drv._wait_until(lambda: len(traffic.statuses) >= answered[0], 5)

    def button(label: str, host: str = "a1.example.com", port: int = 443, bottle: str = "alpha"):
        return page.get_by_role("button", name=f"{label} {host}:{port} in {bottle}", exact=True)

    return held, release_next, button


def sibling_rows_lock(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """While a decide is in flight every open request it acts on is busy, and no other is."""
    from playwright.sync_api import expect

    broker.reset()
    add_siblings(broker)
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) + 4)
        held, release_next, button = _held_decides(page, traffic, drv)
        sub = dict(host="api.a1.example.com")
        lookalike = dict(host="xa1.example.com")

        # An allow acts on the zone in ITS bottle: the alpha:8443 sibling and the alpha subzone lock; zeta's a1,
        # the alpha lookalike and an unrelated host do not.
        button("Allow").click()
        drv._wait_until(lambda: len(held) == 1, 5)
        for label in ("Allow", "Deny", "More allow options for", "More deny options for"):
            expect(button(label, port=8443)).to_be_disabled()
            expect(button(label, **sub)).to_be_disabled()
        expect(button("Allow", bottle="zeta")).to_be_enabled()
        expect(button("Allow", **lookalike)).to_be_enabled()
        expect(drv.button("Allow", "a2.example.com")).to_be_enabled()
        release_next()
        expect(button("Allow", port=8443)).to_be_enabled()
        expect(button("Allow", **sub)).to_be_enabled()

        # A global deny acts on the zone in EVERY bottle: the alpha:8443 row, the alpha subzone and zeta's row lock.
        button("More deny options for", port=8443).click()
        page.get_by_role("menuitem", name="Deny permanently · global").click()
        dialog = page.get_by_role("dialog")
        dialog.locator("#confirm-host").fill("a1.example.com")
        dialog.get_by_role("button", name="Deny permanently").click()
        drv._wait_until(lambda: len(held) == 1, 5)
        expect(button("Allow", **sub)).to_be_disabled()
        expect(button("Allow", bottle="zeta")).to_be_disabled()
        expect(button("Allow", **lookalike)).to_be_enabled()
        expect(drv.button("Allow", "a2.example.com")).to_be_enabled()
        release_next()
    finally:
        context.close()


def overlapping_decides_lock(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A row stays locked until EVERY in-flight decide covering it has finished, not just the first to finish."""
    from playwright.sync_api import expect

    broker.reset()
    add_siblings(broker)
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) + 4)
        held, release_next, button = _held_decides(page, traffic, drv)
        sub = dict(host="api.a1.example.com")

        # 1st: Allow a1 in alpha locks a1, a1:8443 and the subzone. 2nd: a global deny from zeta's a1 also locks
        # zeta's row, and re-locks the alpha ones.
        button("Allow").click()
        drv._wait_until(lambda: len(held) == 1, 5)
        button("More deny options for", bottle="zeta").click()
        page.get_by_role("menuitem", name="Deny permanently · global").click()
        dialog = page.get_by_role("dialog")
        dialog.locator("#confirm-host").fill("a1.example.com")
        dialog.get_by_role("button", name="Deny permanently").click()
        drv._wait_until(lambda: len(held) == 2, 5)
        for locked in (button("Allow", port=8443), button("Allow", **sub), button("Allow", bottle="zeta")):
            expect(locked).to_be_disabled()

        # The first decide finishes: every row the second still covers stays locked.
        release_next()
        expect(drv.button("Allow", "a2.example.com")).to_be_enabled()
        for locked in (button("Allow", port=8443), button("Allow", **sub), button("Allow", bottle="zeta")):
            expect(locked).to_be_disabled()

        # The second finishes: now they unlock.
        release_next()
        expect(button("Allow", **sub)).to_be_enabled()
        expect(button("Allow", bottle="zeta")).to_be_enabled()
    finally:
        context.close()


def inflight_poll_keeps_banner(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A poll already in flight when a decide fails lands without clearing the banner; the next one does.

    Page time is frozen before the page loads (at a fixed instant, not one taken from the wall clock), so the
    5 s interval cannot start a poll of its own between the steps however long the load takes.
    """
    from playwright.sync_api import expect

    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        start = datetime(2026, 1, 1, 12, 0, 0)
        page.clock.install(time=start)
        page.clock.pause_at(start + timedelta(seconds=1))
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
        # a slow load must not matter: page time was frozen before the load, so more real time than the
        # 5 s poll interval passes without a poll
        polls = len(traffic.queue_statuses)
        page.wait_for_timeout(5500)
        assert len(traffic.queue_statuses) == polls, "the page polled while its clock was frozen"
        held: list = []
        page.route("**/api/egress/queue", lambda route: held.append(route))
        # a 400 re-reads the queue at once; hold that poll in flight
        drv.button("Deny", stub.BAD_REQUEST_HOST).click()
        drv._wait_until(lambda: len(held) >= 1, 5)
        assert held, "no queue poll went in flight"
        with broker.lock:
            broker.decide_outage = 500
        drv.button("Deny", "check18.example.com").click()
        expect(drv.stale_banner()).to_be_visible()
        # the poll in flight predates the failure: it lands and the banner stays
        reads = len(traffic.queue_statuses)
        for route in held:
            route.continue_()
        held.clear()
        drv._wait_until(lambda: len(traffic.queue_statuses) > reads, 5)
        page.wait_for_timeout(300)
        assert len(traffic.queue_statuses) > reads, "the held poll never landed"
        expect(drv.stale_banner()).to_be_visible()
        # the next poll starts after the failure and succeeds: the banner clears
        page.unroute("**/api/egress/queue")
        page.clock.run_for(5500)
        expect(drv.stale_banner()).to_be_hidden()
    finally:
        with broker.lock:
            broker.decide_outage = None
        context.close()


def banner_copy(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The banner claims stale data only after a failed poll; a decide failure reads as an unsent decision."""
    from playwright.sync_api import expect

    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        # This check is about which poll clears which banner, so the tab must be polling: no live stream
        # (with one open the tab does not poll; check 104 covers the banner when the stream ends).
        page.route(STREAM_ROUTE, lambda route: route.abort())
        # frozen before the load, so only the explicit run_for below starts a poll
        start = datetime(2026, 1, 1, 12, 0, 0)
        page.clock.install(time=start)
        page.clock.pause_at(start + timedelta(seconds=1))
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
        banner = drv.stale_banner()

        # A failed poll: the list on screen is the last good snapshot, and the banner says when it is from.
        with broker.lock:
            broker.outage = True
        page.clock.run_for(5500)
        expect(banner).to_be_visible()
        poll_text = banner.inner_text().strip()
        assert re.fullmatch(r"Showing data from \d{1,2}:\d{2}:\d{2}\s[AP]M: .+", poll_text), \
            f"poll-failure banner: {poll_text!r}"

        # A decide that also fails does not turn that banner into a decide message.
        drv.button("Deny", "check18.example.com").click()
        drv.wait_note("check18.example.com", "^Not sent: ")
        assert banner.inner_text().strip() == poll_text, f"banner changed to {banner.inner_text().strip()!r}"

        # The queue recovers: the next poll clears the banner.
        with broker.lock:
            broker.outage = False
        page.clock.run_for(5500)
        expect(banner).to_be_hidden()

        # A decide fails while polls are healthy: the list is current, so the banner must not say otherwise.
        with broker.lock:
            broker.decide_outage = 500
        drv.button("Deny", "check18.example.com").click()
        expect(banner).to_be_visible()
        assert banner.inner_text().strip() == "Decision not sent: decide failed on the daemon", \
            f"decide-failure banner: {banner.inner_text().strip()!r}"

        # The poll banner keeps its own clearing marker. A poll fails; the queue recovers, but the next poll is
        # held in flight; a decide fails; the held poll succeeds. That poll made the list current, so the poll
        # banner goes, and the decide's error, which the poll predates, takes its place.
        with broker.lock:
            broker.decide_outage = None
        page.clock.run_for(5500)
        expect(banner).to_be_hidden()
        with broker.lock:
            broker.outage = True
        page.clock.run_for(5500)
        expect(banner).to_be_visible()
        assert banner.inner_text().strip().startswith("Showing data from "), \
            f"poll-failure banner: {banner.inner_text().strip()!r}"
        with broker.lock:
            broker.outage = False
        held: list = []
        page.route("**/api/egress/queue", lambda route: held.append(route))
        page.clock.run_for(5500)
        drv._wait_until(lambda: len(held) >= 1, 5)
        assert held, "no queue poll went in flight"
        with broker.lock:
            broker.decide_outage = 500
        drv.button("Deny", "check18.example.com").click()
        drv.wait_note("check18.example.com", "^Not sent: ")
        assert banner.inner_text().strip().startswith("Showing data from "), \
            f"banner changed before the poll landed: {banner.inner_text().strip()!r}"
        reads = len(traffic.queue_statuses)
        for route in held:
            route.continue_()
        held.clear()
        drv._wait_until(lambda: len(traffic.queue_statuses) > reads, 5)
        page.unroute("**/api/egress/queue")
        expect(banner).to_have_text("Decision not sent: decide failed on the daemon")
        # the decide banner clears on the next poll, which starts after the decide failed
        page.clock.run_for(5500)
        expect(banner).to_be_hidden()
    finally:
        with broker.lock:
            broker.outage = False
            broker.decide_outage = None
        context.close()


def light_rows_have_no_red_pixels(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Each request row, photographed in the light theme, holds no pure-red (#ff0000) pixel.

    A `border-background/30` on the Allow chevron once coloured all four of its borders, and Chromium painted
    pure red on the antialiased right-hand corners wherever the compact (tablet, phone) card stretched the
    button to a sub-pixel position. Nothing in the light theme is meant to be exactly #ff0000.
    """
    from playwright.sync_api import expect

    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
        red = {}
        for index in range(len(stub.OPEN_ROWS)):
            row = drv.requests().nth(index)
            red[f"{index}: {row.inner_text().split(chr(10))[0]}"] = png_pixels.count_pixels(
                row.screenshot(), (255, 0, 0))
        assert len(red) == len(stub.OPEN_ROWS), f"photographed {len(red)} rows, expected {len(stub.OPEN_ROWS)}"
        assert sum(red.values()) == 0, f"pure-red pixels per row: { {k: v for k, v in red.items() if v} }"
    finally:
        context.close()


def add_long_comm_row(broker: stub.StubBroker) -> None:
    """One more open request whose `comm` is a single unbreakable string wider than any viewport's row.

    It has no uid, so the comm is the only text of its meta item: a wrap can only be the string breaking itself.
    """
    with broker.lock:
        base = next(row for row in broker.queue["open"] if row["request_id"] == "a1")
        broker.queue["open"].append({**base, "request_id": "lc1", "host": "lc.example.com", "comm": "x" * 120,
                                        "uid": None})
        broker.queue["open"].sort(key=lambda row: row["opened_at"])
        broker.queue["count"] = len(broker.queue["open"])


def long_meta_string_wraps(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """An unbreakable meta string breaks onto further lines inside its row instead of being clipped."""
    from playwright.sync_api import expect

    broker.reset()
    add_long_comm_row(broker)
    context, page, traffic = new_page(browser, served, viewport, "light")
    try:
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) + 1)
        found = drv.requests().filter(has_text="lc.example.com").first.evaluate("""(row) => {
            const flow = row.querySelector('.meta-flow');
            const clip = flow.parentElement.getBoundingClientRect();
            const item = [...flow.children].find((el) => el.textContent.includes('xxxx'));
            const box = item.getBoundingClientRect();
            const text = document.createTreeWalker(item, NodeFilter.SHOW_TEXT).nextNode();
            const range = document.createRange();
            range.selectNodeContents(text);
            return {right: box.right, clipRight: clip.right, overflow: item.scrollWidth - item.clientWidth,
                    lines: new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size,
                    text: item.textContent.trim()};
        }""")
        assert found["text"] == "x" * 120, f"the item holds more than the comm: {found}"
        assert found["right"] <= found["clipRight"] + 1, \
            f"the string runs {found['right'] - found['clipRight']:.0f}px past the row, where it is clipped: {found}"
        assert found["overflow"] <= 1, f"the string overflows its own box by {found['overflow']}px: {found}"
        assert found["lines"] >= 2, f"the string itself did not break onto a second line: {found}"
    finally:
        context.close()


def run_dedicated(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    suite = Suite(ui, viewport)
    suite.check("28", "A decide in flight locks every request it acts on (the host or a subzone of it in the same "
                      "bottle; all bottles on a global deny) and no other",
                lambda: sibling_rows_lock(browser, served, broker, viewport), spa_only=True)
    suite.check("28b", "A row stays locked until every in-flight decide that covers it has finished",
                lambda: overlapping_decides_lock(browser, served, broker, viewport), spa_only=True)
    suite.check("29", "A poll already in flight when a decide fails does not clear the banner; the next poll does",
                lambda: inflight_poll_keeps_banner(browser, served, broker, viewport), spa_only=True)
    suite.check("50", "The stale banner says `Showing data from <time>` only after a failed poll; a decide failure "
                      "with healthy polls reads `Decision not sent: <error>`",
                lambda: banner_copy(browser, served, broker, viewport), spa_only=True)
    suite.check("51", "The light theme paints no pure-red (#ff0000) pixel in any request row, on the compact "
                      "layout included",
                lambda: light_rows_have_no_red_pixels(browser, served, broker, viewport), spa_only=True)
    suite.check("52", "An unbreakable meta string (a 120-character comm) wraps inside its row instead of being clipped",
                lambda: long_meta_string_wraps(browser, served, broker, viewport), spa_only=True)
    return suite.results



# ---- Queue features: filters, per-bottle bulk decide, denylist group (80..96) --------------

FEATURE_HOSTS = {   # bottle -> its open hosts, newest first (stub.FEATURE_ROWS)
    "alpha": ["fa1.example.com", "fa2.example.com", "fa3.example.com", "192.0.2.77", "fa5.example.com"],
    "mid": ["fm1.example.com", "fm2.example.com", "fm3.example.com", "fm4.example.com"],
    "zeta": ["fz1.example.com", "fz2.example.com", "fz3.example.com"],
}
FEATURE_PORT = {"192.0.2.77": 5432, "fm4.example.com": 8443}   # every other feature row is on 443


def hp(host: str) -> str:
    return f"{host}:{FEATURE_PORT.get(host, 443)}"


def hps(*bottles: str) -> list[str]:
    return [hp(host) for bottle in bottles for host in FEATURE_HOSTS[bottle]]


ALL_FEATURE = hps("alpha", "mid", "zeta")


class QueuePage:
    """The queue on the queue-features fixture, driven by test ids and accessible names."""

    def __init__(self, page, traffic: Traffic, broker: stub.StubBroker):
        self.page, self.traffic, self.broker = page, traffic, broker
        self.drv = SpaDriver(page, traffic)

    @property
    def bar(self):
        return self.page.locator("[data-testid=queue-filters]")

    def hosts(self):
        return self.page.locator("[data-testid=request-host]")

    def expect_hosts(self, expected: list[str]) -> None:
        from playwright.sync_api import expect
        if expected:
            expect(self.hosts()).to_have_text(expected)
        else:
            expect(self.hosts()).to_have_count(0)

    def count_line(self) -> str:
        return squash(self.page.get_by_test_id("summary").inner_text())

    def pick_bottles(self, *names: str) -> None:
        self.page.get_by_test_id("queue-bottle-filter").click()
        for name in names:
            self.page.get_by_role("menuitemcheckbox", name=name, exact=True).click()
        self.page.keyboard.press("Escape")

    def state(self, label: str) -> None:
        self.state_item(label).click()

    def state_item(self, label: str):
        return self.page.get_by_test_id("queue-state").locator("button, [role=radio]").filter(
            has_text=re.compile(rf"^\s*{label}\s*$"))

    def age(self, label: str) -> None:
        self.page.get_by_test_id("queue-age").click()
        self.page.get_by_role("option", name=label, exact=True).click()

    def search(self, text: str) -> None:
        self.bar.get_by_placeholder("Search destination").fill(text)

    def clear_button(self):
        return self.page.get_by_test_id("queue-filter-clear")

    def reset(self) -> None:
        if self.clear_button().count():
            self.clear_button().click()
        self.expect_hosts(ALL_FEATURE)

    def bulk_button(self, verb: str, bottle: str):
        return self.page.get_by_role("button", name=f"{verb} all in {bottle}", exact=True)

    def dialog(self):
        return self.page.get_by_role("alertdialog")

    def ask(self, verb: str, bottle: str):
        self.bulk_button(verb, bottle).click()
        dialog = self.dialog()
        dialog.wait_for()
        return dialog

    def confirm(self, dialog, verb: str) -> None:
        dialog.get_by_role("button", name=re.compile(rf"^{verb} \d+$")).click()

    def status(self):
        return self.page.get_by_test_id("bulk-status")

    def result(self):
        """The finished run's result line alone (the status row also holds Dismiss)."""
        return self.status().locator(".pill")

    def broker_decides(self) -> list[dict]:
        with self.broker.lock:
            return [dict(body) for body in self.broker.decides]

    def sent_bodies(self) -> list[dict]:
        return [entry["body"] for entry in self.traffic.decides]

    def groups(self) -> list[tuple[str, int]]:
        return self.drv.groups()


def _f_bottle(q: QueuePage) -> None:
    q.pick_bottles("mid")
    q.expect_hosts(hps("mid"))
    _in("4 of 12 open", q.count_line())
    _eq(q.groups(), [("mid", 4)])
    q.pick_bottles("zeta")
    q.expect_hosts(hps("mid", "zeta"))
    _in("7 of 12 open", q.count_line())
    _in("2 bottles", q.page.get_by_test_id("queue-bottle-filter").inner_text())
    q.pick_bottles("mid", "zeta")
    q.expect_hosts(ALL_FEATURE)
    _in("All bottles", q.page.get_by_test_id("queue-bottle-filter").inner_text())


def _f_state(q: QueuePage) -> None:
    q.state("Failed apply")
    q.expect_hosts([hp("fa2.example.com"), hp("192.0.2.77"), hp("fm2.example.com"), hp("fz2.example.com")])
    _in("4 of 12 open", q.count_line())
    q.state("All")
    q.expect_hosts(ALL_FEATURE)


def _f_age(q: QueuePage) -> None:
    q.age("Under 5 minutes")
    q.expect_hosts([hp(h) for h in ("fa1.example.com", "fa2.example.com", "fm1.example.com",
                                    "fz1.example.com", "fz2.example.com")])
    q.age("Under 1 hour")
    q.expect_hosts([hp(h) for h in ("fa1.example.com", "fa2.example.com", "fa3.example.com", "fm1.example.com",
                                    "fm2.example.com", "fz1.example.com", "fz2.example.com")])
    q.age("Older than 1 hour")
    q.expect_hosts([hp(h) for h in ("192.0.2.77", "fa5.example.com", "fm3.example.com", "fm4.example.com",
                                    "fz3.example.com")])
    _in("5 of 12 open", q.count_line())
    q.age("Any age")
    q.expect_hosts(ALL_FEATURE)


def _f_search(q: QueuePage) -> None:
    q.search("fm4")
    q.expect_hosts([hp("fm4.example.com")])
    q.search("FZ")
    q.expect_hosts(hps("zeta"))
    q.search(":5432")
    q.expect_hosts([hp("192.0.2.77")])
    _in("1 of 12 open", q.count_line())
    q.search("")
    q.expect_hosts(ALL_FEATURE)


def _f_clear(q: QueuePage) -> None:
    from playwright.sync_api import expect
    assert q.clear_button().count() == 0, "Clear is shown with no filter active"
    q.pick_bottles("alpha", "mid")
    expect(q.clear_button()).to_be_visible()
    q.reset()
    assert q.clear_button().count() == 0, "Clear stays after it was used"
    q.state("Failed apply")
    q.age("Under 1 hour")
    q.search("fa")
    q.pick_bottles("alpha")
    q.expect_hosts([hp("fa2.example.com")])
    q.clear_button().click()
    q.expect_hosts(ALL_FEATURE)
    _in("12 of 12 open", q.count_line())
    _in("All bottles", q.page.get_by_test_id("queue-bottle-filter").inner_text())
    _in("Any age", q.page.get_by_test_id("queue-age").inner_text())
    _eq(q.bar.get_by_placeholder("Search destination").input_value(), "")
    _eq(q.state_item("All").get_attribute("data-state"), "on")
    _eq(q.state_item("Failed apply").get_attribute("data-state"), "off")
    assert q.clear_button().count() == 0, "Clear stays with every filter reset"
    q.search("no-such-host")
    expect(q.page.get_by_text("Nothing matches these filters.")).to_be_visible()
    q.expect_hosts([])
    _in("0 of 12 open", q.count_line())
    q.reset()


def _f_failed_detail(q: QueuePage) -> None:
    q.pick_bottles("alpha")
    q.state("Failed apply")
    q.expect_hosts([hp("fa2.example.com"), hp("192.0.2.77")])
    want = {
        "fa2.example.com": "Apply failed after 3 attempts: rule install failed",
        "192.0.2.77": "Apply failed after 1 attempt: an IP address needs a CIDR in the manifest",
    }
    for host, text in want.items():
        got = squash(q.drv.row(host).inner_text())
        assert _loose(text) in _loose(got), f"{host}: {text!r} missing from {got!r}"
    q.reset()
    q.state("Failed apply")
    for host, text in (("fm2.example.com", "Apply failed after 2 attempts: rule install failed"),
                       ("fz2.example.com", "Apply failed after 1 attempt: rule install failed")):
        got = squash(q.drv.row(host).inner_text())
        assert _loose(text) in _loose(got), f"{host}: {text!r} missing from {got!r}"
    q.state("All")
    q.expect_hosts(ALL_FEATURE)


def _deny_body(host: str, bottle: str) -> dict:
    return {"decision": "deny", "scope": "once", "host": host, "container": bottle}


def _f_deny_all(q: QueuePage) -> None:
    from playwright.sync_api import expect
    dialog = q.ask("Deny", "alpha")
    _in("Deny all 5 open requests for alpha?", squash(dialog.inner_text()))
    _eq([squash(t) for t in dialog.locator("[data-testid=bulk-hosts] li").all_inner_texts()], hps("alpha"))
    assert "hide" not in dialog.inner_text(), "the dialog claims hidden rows with no filter active"
    assert not q.broker_decides(), "a decide was sent before the confirmation"
    q.confirm(dialog, "Deny")
    expect(q.status()).to_contain_text("Denied 5 of 5 in alpha")
    _eq(q.broker_decides(), [_deny_body(h, "alpha") for h in FEATURE_HOSTS["alpha"]])
    _eq(q.sent_bodies(), [{"action": "deny", "host": h, "container": "alpha"} for h in FEATURE_HOSTS["alpha"]])
    q.expect_hosts(hps("mid", "zeta"))
    _eq(q.groups(), [("mid", 4), ("zeta", 3)])


def _f_deny_all_hidden(q: QueuePage) -> None:
    from playwright.sync_api import expect
    q.pick_bottles("alpha")
    q.state("Failed apply")
    q.expect_hosts([hp("fa2.example.com"), hp("192.0.2.77")])
    dialog = q.ask("Deny", "alpha")
    text = squash(dialog.inner_text())
    _in("Deny all 5 open requests for alpha?", text)
    _in("including 3 the current filters hide", text)
    _eq([squash(t) for t in dialog.locator("[data-testid=bulk-hosts] li").all_inner_texts()], hps("alpha"))
    q.confirm(dialog, "Deny")
    expect(q.status()).to_contain_text("Denied 5 of 5 in alpha")
    _eq(q.broker_decides(), [_deny_body(h, "alpha") for h in FEATURE_HOSTS["alpha"]])
    with q.broker.lock:
        left = sorted(r["request_id"] for r in q.broker.queue["open"])
    _eq(left, ["fm1", "fm2", "fm3", "fm4", "fz1", "fz2", "fz3"])


def _f_allow_all(q: QueuePage) -> None:
    from playwright.sync_api import expect
    dialog = q.ask("Allow", "zeta")
    _in("Allow all 3 open requests for zeta?", squash(dialog.inner_text()))
    q.confirm(dialog, "Allow")
    expect(q.status()).to_contain_text("Allowed 3 of 3 in zeta")
    _eq(q.broker_decides(), [{"decision": "allow", "scope": "live", "host": h, "container": "zeta"}
                             for h in FEATURE_HOSTS["zeta"]])
    _eq(q.sent_bodies(), [{"action": "allow_live", "host": h, "container": "zeta"} for h in FEATURE_HOSTS["zeta"]])
    q.expect_hosts(hps("alpha", "mid"))
    _eq(q.groups(), [("alpha", 5), ("mid", 4)])


def _f_cancel(q: QueuePage) -> None:
    dialog = q.ask("Deny", "mid")

    def cancel():
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
    assert q.drv.sent_nothing_after(cancel), "Cancel sent a decide"
    assert not q.broker_decides(), "the broker received a decide after Cancel"
    q.expect_hosts(ALL_FEATURE)


def _f_partial(q: QueuePage) -> None:
    from playwright.sync_api import expect
    held, release_next, _button = _held_decides(q.page, q.traffic, q.drv)
    q.broker.fail_nth_decide(2)
    dialog = q.ask("Deny", "mid")
    q.confirm(dialog, "Deny")
    for step in range(4):
        q.drv._wait_until(lambda: len(held) == 1, 5)
        assert len(held) == 1, f"decide {step + 1} was not in flight alone (held {len(held)})"
        assert len(q.broker_decides()) == step, f"the broker had {len(q.broker_decides())} decides before release {step + 1}"
        expect(q.page.get_by_test_id("bulk-count")).to_have_text(f"{step}/4")
        release_next()
        if step == 0:
            expect(q.page.get_by_test_id("bulk-progress")).to_have_attribute("aria-valuenow", "25")
    expect(q.status()).to_contain_text("Denied 3 of 4 in mid · 1 failed and stays open, marked in the list")
    _eq(q.broker_decides(), [_deny_body(h, "mid") for h in FEATURE_HOSTS["mid"]])
    q.expect_hosts(hps("alpha") + [hp("fm2.example.com")] + hps("zeta"))
    q.drv.wait_note("fm2.example.com", "^Not sent: ")
    _eq(q.groups(), [("alpha", 5), ("mid", 1), ("zeta", 3)])
    assert q.drv.stale_banner().count() == 0, "a failed bulk decide raised the banner"
    for host in ("fm1.example.com", "fm3.example.com", "fm4.example.com"):
        assert q.drv.row(host).count() == 0, f"{host} did not leave"


def _f_locks(q: QueuePage) -> None:
    from playwright.sync_api import expect
    held, release_next, button = _held_decides(q.page, q.traffic, q.drv)

    def single(label, host, bottle):
        return button(label, host, FEATURE_PORT.get(host, 443), bottle)

    # A row mid-decide keeps its bottle's bulk buttons off.
    single("Deny", "fm1.example.com", "mid").click()
    q.drv._wait_until(lambda: len(held) == 1, 5)
    for verb in ("Allow", "Deny"):
        expect(q.bulk_button(verb, "mid")).to_be_disabled()
        expect(q.bulk_button(verb, "zeta")).to_be_enabled()
    release_next()
    expect(q.bulk_button("Deny", "mid")).to_be_enabled()
    expect(q.drv.row("fm1.example.com")).to_have_count(0)

    # A bulk run locks every request it will decide, and no other bulk starts meanwhile.
    dialog = q.ask("Deny", "mid")
    _eq([squash(t) for t in dialog.locator("[data-testid=bulk-hosts] li").all_inner_texts()],
        [hp(h) for h in FEATURE_HOSTS["mid"][1:]])
    q.confirm(dialog, "Deny")
    q.drv._wait_until(lambda: len(held) == 1, 5)
    for label in ("Allow", "Deny", "More allow options for", "More deny options for"):
        expect(single(label, "fm3.example.com", "mid")).to_be_disabled()
    expect(single("Allow", "fz1.example.com", "zeta")).to_be_enabled()
    for verb in ("Allow", "Deny"):
        for bottle in ("alpha", "zeta"):
            expect(q.bulk_button(verb, bottle)).to_be_disabled()
    release_next()
    q.drv._wait_until(lambda: len(held) == 1, 5)
    before = len(q.broker_decides())
    # a poll lands while the next decide is held: the decided row must not come back
    q.page.wait_for_timeout(5500)
    assert q.drv.row("fm2.example.com").count() == 0, "a poll resurrected a decided row"
    assert len(q.broker_decides()) == before, "a decide was sent while the previous one was still in flight"
    expect(single("Allow", "fm3.example.com", "mid")).to_be_disabled()
    release_next()
    q.drv._wait_until(lambda: len(held) == 1, 5)
    release_next()
    expect(q.status()).to_contain_text("Denied 3 of 3 in mid")
    _eq(q.broker_decides(), [_deny_body(h, "mid") for h in FEATURE_HOSTS["mid"]])


def _f_denylist(q: QueuePage) -> None:
    from playwright.sync_api import expect
    toggle = q.page.get_by_test_id("denylist-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    _eq(q.page.get_by_test_id("denylist-group").count(), 1)
    _eq(squash(q.page.get_by_test_id("denylist-hits").inner_text()), "13 hits")
    group_rows = q.page.locator("[data-testid=denylist-group] [data-testid=recent-row]").locator("visible=true")
    _eq(group_rows.count(), 0)
    recent = q.page.get_by_test_id("recent")
    for host in ("ads.example.com", "track.example.net", "metrics.example.org"):
        assert host not in recent.inner_text(), f"{host} is listed while the denylist group is collapsed"
    ordinary = q.page.locator("[data-testid=recent] [data-testid=recent-row]").locator("visible=true")
    _eq(ordinary.count(), 1)
    _in("registry.npmjs.org", ordinary.first.inner_text())
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(group_rows).to_have_count(3)
    _eq([squash(t) for t in q.page.get_by_test_id("denylist-row-hits").all_inner_texts()], ["4×", "7×", "2×"])
    texts = [squash(t) for t in group_rows.all_inner_texts()]
    for text, (host, bottle) in zip(texts, (("ads.example.com", "zeta"), ("track.example.net", "alpha"),
                                            ("metrics.example.org", "mid"))):
        assert host in text and bottle in text, f"{host}/{bottle} missing from {text!r}"
    _eq(squash(q.page.get_by_test_id("denylist-hits").inner_text()), "13 hits")
    toggle.click()
    expect(group_rows).to_have_count(0)


def _f_denylist_card(q: QueuePage) -> None:
    """The collapsed denylist group is a card of its own; ordinary recent rows are not read as its members."""
    from playwright.sync_api import expect
    page = q.page
    expect(page.get_by_test_id("denylist-toggle")).to_have_attribute("aria-expanded", "false")
    _eq(page.locator("[data-testid=denylist-group] [data-testid=recent-row]").count(), 0)   # none in the DOM at all
    found = page.evaluate("""() => {
        const group = document.querySelector('[data-testid=denylist-group]');
        const rows = [...document.querySelectorAll('[data-testid=recent-row]')];
        const ordinary = rows.filter((row) => !group.contains(row));
        const groupCard = group.closest('.panel');
        const box = (el) => el.getBoundingClientRect();
        return {
            groupIsItsOwnCard: groupCard === group,
            ordinary: ordinary.length,
            sharedCard: ordinary.filter((row) => row.closest('.panel') === groupCard).length,
            gap: ordinary.length ? box(ordinary[0].closest('.panel')).top - box(group).bottom : null,
        };
    }""")
    assert found["ordinary"] == 1, f"expected the one ordinary recent row outside the group: {found}"
    assert found["groupIsItsOwnCard"], f"the denylist group is not its own card: {found}"
    assert found["sharedCard"] == 0, f"an ordinary recent row sits in the denylist group's card: {found}"
    assert found["gap"] >= 16, f"the ordinary rows' card is {found['gap']}px below the group, not clearly apart: {found}"


def _f_reach(q: QueuePage) -> None:
    from playwright.sync_api import expect
    page = q.page

    def inside(name, locator):
        box = locator.bounding_box()
        assert box, f"{name} has no box"
        width = page.evaluate("window.innerWidth")
        assert box["x"] >= -0.5 and box["x"] + box["width"] <= width + 0.5, \
            f"{name} [{box['x']:.0f}, {box['x'] + box['width']:.0f}] leaves the {width}px viewport"

    scroll, inner = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert scroll <= inner, f"scrollWidth {scroll} > innerWidth {inner}"
    inside("search", q.bar.get_by_placeholder("Search destination"))
    inside("bottle filter", page.get_by_test_id("queue-bottle-filter"))
    inside("state toggle", page.get_by_test_id("queue-state"))
    inside("age", page.get_by_test_id("queue-age"))
    for verb, bottle in (("Allow", "alpha"), ("Deny", "alpha"), ("Allow", "zeta"), ("Deny", "zeta")):
        button = q.bulk_button(verb, bottle)
        button.scroll_into_view_if_needed()
        inside(f"{verb} all in {bottle}", button)
    page.get_by_test_id("queue-bottle-filter").click()
    inside("bottle menu", page.get_by_role("menu"))
    page.keyboard.press("Escape")
    dialog = q.ask("Deny", "alpha")
    inside("confirm dialog", dialog)
    inside("dialog Cancel", dialog.get_by_role("button", name="Cancel"))
    inside("dialog Deny", dialog.get_by_role("button", name="Deny 5"))
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="hidden")
    scroll, inner = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert scroll <= inner, f"scrollWidth {scroll} > innerWidth {inner} after the dialog"
    expect(page.get_by_test_id("denylist-toggle")).to_be_visible()
    _eq(q.traffic.console_errors, [])


def _f_allow_all_ip(q: QueuePage) -> None:
    from playwright.sync_api import expect
    dialog = q.ask("Allow", "alpha")
    _in("Allow all 5 open requests for alpha?", squash(dialog.inner_text()))
    q.confirm(dialog, "Allow")
    # the IP-literal allow is recorded but leaves its row open: the result line must not count it as allowed
    expect(q.result()).to_have_text("Allowed 4 of 5 in alpha · 1 needs a CIDR in the manifest")
    allow = [{"decision": "allow", "scope": "live", "host": h, "container": "alpha"} for h in FEATURE_HOSTS["alpha"]]
    _eq(q.broker_decides(), allow)
    _eq(q.sent_bodies(), [{"action": "allow_live", "host": h, "container": "alpha"} for h in FEATURE_HOSTS["alpha"]])
    q.expect_hosts([hp("192.0.2.77")] + hps("mid", "zeta"))
    _eq(q.groups(), [("alpha", 1), ("mid", 4), ("zeta", 3)])
    q.drv.wait_note("192.0.2.77", "^Recorded — add the CIDR to the manifest by hand")
    assert q.drv.stale_banner().count() == 0, "an IP allow raised the banner"
    # the state toggle names itself for assistive tech
    _eq(q.page.get_by_test_id("queue-state").get_attribute("aria-label"), "Request state")


def _f_swept(q: QueuePage) -> None:
    from playwright.sync_api import expect
    held, release_next, button = _held_decides(q.page, q.traffic, q.drv)
    dialog = q.ask("Deny", "mid")
    q.confirm(dialog, "Deny")
    q.drv._wait_until(lambda: len(held) == 1, 5)
    # the running decide's spinner sits on the button that was pressed
    spin = {label: button(label, "fm1.example.com", 443, "mid").locator("svg.animate-spin").count()
            for label in ("Allow", "Deny")}
    # fm3 is decided elsewhere (as a zone decide would sweep it) while the first decide is in flight
    with q.broker.lock:
        q.broker.queue["open"] = [r for r in q.broker.queue["open"] if r["request_id"] != "fm3"]
        q.broker.queue["count"] = len(q.broker.queue["open"])
    release_next()
    for _ in range(2):
        q.drv._wait_until(lambda: len(held) == 1, 5)
        release_next()
    expect(q.result()).to_have_text("Denied 4 of 4 in mid")
    hosts = [h for h in FEATURE_HOSTS["mid"] if h != "fm3.example.com"]
    _eq(q.sent_bodies(), [{"action": "deny", "host": h, "container": "mid"} for h in hosts])
    _eq(q.broker_decides(), [_deny_body(h, "mid") for h in hosts])
    assert not held, f"{len(held)} decide(s) still held"
    q.expect_hosts(hps("alpha", "zeta"))
    _eq(spin, {"Allow": 0, "Deny": 1})


QUEUE_SHARED_CHECKS = [   # one page, in order; each leaves the filters cleared
    ("80", "The bottle multi-select narrows the queue to the chosen bottles and the count line follows", _f_bottle),
    ("81", "The Failed apply state shows only requests with a last_error", _f_state),
    ("82", "The age filter buckets requests: under 5 minutes, under 1 hour, older than 1 hour", _f_age),
    ("83", "The destination search narrows by host:port, ignoring case", _f_search),
    ("84", "Clear shows only while a filter is active and resets every filter; no match shows the empty state", _f_clear),
    ("85", "One bottle plus Failed apply shows exactly that bottle's failed requests with their last_error text",
     _f_failed_detail),
]
QUEUE_OWN_CHECKS = [   # each on a fresh page: they decide rows
    ("86", "Deny all for one bottle sends exactly one deny per open request of that bottle, and none for another",
     _f_deny_all),
    ("87", "Deny all under filters still decides every open request of the bottle, and the dialog says the filters "
           "hide some", _f_deny_all_hidden),
    ("88", "Allow all sends exactly the Allow body per open request of the bottle and those rows leave", _f_allow_all),
    ("89", "Cancel in the bulk dialog sends nothing", _f_cancel),
    ("90", "When the second decide of a bulk run fails that request stays, marked, the others leave, decides run "
           "one at a time and the run reports every request", _f_partial),
    ("91", "A request mid-decide keeps its bottle's bulk buttons off; a bulk run locks its requests and other "
           "bulk buttons, and a poll does not bring a decided row back", _f_locks),
    ("92", "Denylist hits are one group, collapsed by default, headed by the summed hits, with a row and N× per hit "
           "when expanded", _f_denylist),
    ("93", "Filters, bulk buttons, menus and the bulk dialog stay inside the viewport, with no console errors",
     _f_reach),
    ("94", "The collapsed denylist group is a card of its own with no row elements in it, and the ordinary recent rows "
           "sit in a separate card clearly below it", _f_denylist_card),
    ("95", "Allow all on a bottle with an IP-literal request reports it apart (Allowed 4 of 5 · 1 needs a CIDR in the "
           "manifest), sends the literal allow bodies and leaves that row open with its note", _f_allow_all_ip),
    ("96", "A request swept by an earlier decide of a bulk run is skipped, not sent, and the run still reports every "
           "request; the running decide's spinner sits on the pressed button", _f_swept),
]


def with_features_page(browser, served: Served, broker: stub.StubBroker, viewport: str, theme: str, fn):
    from playwright.sync_api import expect
    broker.reset()
    broker.use_fixture("queue-features")
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        expect.set_options(timeout=TIMEOUT_MS)
        page.goto(served.base + "/")
        expect(page.locator("[data-testid=request]")).to_have_count(len(stub.FEATURE_ROWS))
        return fn(QueuePage(page, traffic, broker))
    finally:
        broker.reset()
        context.close()


def run_queue_features(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    """Queue-feature checks (80..96); the SPA only."""
    suite = Suite(ui, viewport)
    if ui == "legacy":
        for number, name, _fn in QUEUE_SHARED_CHECKS + QUEUE_OWN_CHECKS:
            suite.check(number, name, lambda: None, spa_only=True)
        return suite.results

    def shared(q: QueuePage) -> None:
        for number, name, fn in QUEUE_SHARED_CHECKS:
            suite.check(number, name, lambda fn=fn: fn(q))

    try:
        with_features_page(browser, served, broker, viewport, "light", shared)
    except Exception as exc:  # noqa: BLE001 - the queue could not even open
        suite.results.append(Result("FAIL", viewport, "80", "queue-features page opens", squash(str(exc))[:300]))
    for number, name, fn in QUEUE_OWN_CHECKS:
        def own(fn=fn):
            with_features_page(browser, served, broker, viewport, "light", fn)
        suite.check(number, name, own)
    return suite.results


# ---- History tab ----------------------------------------------------------------------

HISTORY_PAGES = [50, 50, 50, 50, 47]


class HistoryPage:
    """The SPA's History tab, driven by test ids. `stub.recent_reply` over the
    stub's own rows is the oracle for what each page must show."""

    def __init__(self, page, served: Served, traffic: Traffic, broker: stub.StubBroker):
        self.page, self.served, self.traffic, self.broker = page, served, traffic, broker

    def open(self) -> None:
        from playwright.sync_api import expect
        self.page.goto(self.served.base + "/egress")
        self.page.get_by_test_id("tab-history").click()
        expect(self.page.get_by_test_id("history-row").first).to_be_visible()

    def rows(self):
        return self.page.get_by_test_id("history-row")

    def hosts(self) -> list[str]:
        return [text.split("\n")[0].strip().rsplit(":", 1)[0] for text in self.rows().all_inner_texts()]

    def label(self) -> str:
        return self.page.get_by_test_id("history-page").inner_text().strip()

    def older(self) -> None:
        self._turn("history-older")

    def newer(self) -> None:
        self._turn("history-newer")

    def _turn(self, test_id: str) -> None:
        from playwright.sync_api import expect
        before = self.label()
        button = self.page.get_by_test_id(test_id)
        expect(button).to_be_enabled()
        button.click()
        expect(self.page.get_by_test_id("history-page")).not_to_have_text(before)
        expect(self.page.get_by_test_id("history-older")).to_be_visible()
        self.settle()

    def settle(self) -> None:
        """Wait for the list to stop loading: both paging buttons are disabled while it does."""
        self.page.wait_for_timeout(120)

    def expected(self, query: str = "") -> dict:
        with self.broker.lock:
            status, page = stub.recent_reply(self.broker.history_rows(), query)
        assert status == 200
        return page

    def last_query(self) -> dict:
        assert self.traffic.recent, "the page sent no /api/egress/recent request"
        return self.traffic.recent[-1]

    def open_calendar(self):
        self.page.get_by_test_id("history-range").click()
        calendar = self.page.locator("[data-slot=range-calendar]")
        calendar.wait_for()
        return calendar

    def pick_day(self, days_ago: int) -> str:
        """Pick the local calendar day `days_ago` before the browser's today, going back through
        the calendar's months. Returns that day as YYYY-MM-DD (browser-local)."""
        year, month, day, months_back = self.page.evaluate(
            """(daysAgo) => {
              const now = new Date(), target = new Date(now.getFullYear(), now.getMonth(), now.getDate() - daysAgo);
              return [target.getFullYear(), target.getMonth() + 1, target.getDate(),
                      (now.getFullYear() * 12 + now.getMonth()) - (target.getFullYear() * 12 + target.getMonth())];
            }""", days_ago)
        calendar = self.open_calendar()
        for _ in range(months_back):
            calendar.locator("[data-slot=range-calendar-prev-button]").click()
        grid = calendar.locator("table").first
        grid.locator("[data-slot=range-calendar-trigger]:not([data-outside-view])").get_by_text(
            str(day), exact=True).first.click()
        return f"{year:04d}-{month:02d}-{day:02d}"

    def close_popover(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.locator("[data-slot=range-calendar]").wait_for(state="hidden")


def run_history(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    """History-tab checks (30..42, 60, 61, 62). One page per viewport, checked in order; the SPA only."""
    from playwright.sync_api import expect

    suite = Suite(ui, viewport)
    if ui == "legacy":
        for number, name, _fn in HISTORY_CHECKS + [RELTIME_CHECK, CONTRAST_CHECK]:
            suite.check(number, name, lambda: None, spa_only=True)
        return suite.results
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    hist = HistoryPage(page, served, traffic, broker)
    try:
        hist.open()
        for number, name, fn in HISTORY_CHECKS:
            suite.check(number, name, lambda fn=fn: fn(hist, page, traffic, broker))
    except Exception as exc:  # noqa: BLE001 - the History tab could not even open
        suite.results.append(Result("FAIL", viewport, "30", "History tab opens", squash(str(exc))[:300]))
    finally:
        close_page_context(context, page)
    for number, name, fn in (RELTIME_CHECK, CONTRAST_CHECK):
        suite.check(number, name, lambda fn=fn: fn(browser, served, broker, viewport))
    return suite.results


def _h_newest_first(hist, page, traffic, broker) -> None:
    want = [row["host"] for row in hist.expected()["rows"]]
    _eq(hist.hosts(), want)
    _eq(len(want), 50)
    _eq(hist.label(), "Page 1")
    assert hist.expected()["rows"][0]["host"] == "denied.example.com", "the newest decision is not first"
    expect_disabled(page, "history-newer", True)
    expect_disabled(page, "history-older", False)
    _eq(hist.traffic.recent[0], {"limit": "50"})
    # The queue is not left showing under the History tab.
    _eq(page.locator("[data-testid=request]:visible").count(), 0)
    _eq(page.locator("[data-testid=recent]:visible").count(), 0)


def _h_walk(hist, page, traffic, broker) -> None:
    every = [row["host"] for row in broker.history_rows()]
    seen, pages = [hist.hosts()], [hist.hosts()]
    for _ in range(len(HISTORY_PAGES) - 1):
        hist.older()
        seen.append(hist.hosts())
    _eq([len(p) for p in seen], HISTORY_PAGES)
    flat = [host for p in seen for host in p]
    _eq(len(flat), len(every))
    _eq(len(set(flat)), len(every))     # no repeats: the tie block straddles the first page boundary
    ordered = sorted(broker.history_rows(), key=lambda r: (r["decided_at"], r["request_id"]), reverse=True)
    _eq(flat, [r["host"] for r in ordered])
    expect_disabled(page, "history-older", True)
    _eq(hist.label(), f"Page {len(HISTORY_PAGES)}")
    # Each Older sent the previous page's last row as the cursor.
    for index, query in enumerate(traffic.recent[1:len(HISTORY_PAGES)]):
        last = hist.expected(f"limit=50{'&before=' + traffic.recent[index]['before'] if index else ''}")["rows"][-1]
        _eq(query["before"], f"{last['decided_at']},{last['request_id']}")
    # And back: Newer walks the same pages in reverse without a repeat.
    for index in range(len(HISTORY_PAGES) - 2, -1, -1):
        hist.newer()
        _eq(hist.hosts(), seen[index])
    _eq(hist.label(), "Page 1")
    expect_disabled(page, "history-newer", True)


def _h_thirty_days(hist, page, traffic, broker) -> None:
    for _ in range(len(HISTORY_PAGES) - 1):
        hist.older()
    row = hist.rows().filter(has_text=stub.ARCHIVE_HOST)
    _eq(row.count(), 1)
    archive = next(r for r in broker.history if r["host"] == stub.ARCHIVE_HOST)
    day = page.evaluate("""(iso) => new Date(iso).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})""",
                        archive["decided_at"])
    _in(day, row.inner_text())
    ago = page.evaluate("(iso) => Math.round((Date.now() - Date.parse(iso)) / 86400000)", archive["decided_at"])
    _eq(ago, 30)


def _h_date_range(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    day = hist.pick_day(30)
    expect(page.get_by_test_id("history-page")).to_have_text("Page 1")
    expect(hist.rows()).to_have_count(1)
    _in(stub.ARCHIVE_HOST, hist.rows().first.inner_text())
    query = hist.last_query()
    want = page.evaluate("""(day) => {
      const [y, m, d] = day.split('-').map(Number);
      const iso = (t) => t.toISOString().replace(/\\.\\d{3}Z$/, 'Z');
      const start = new Date(y, m - 1, d, 0, 0, 0);
      // The next local midnight, not start + 24 h: a DST day is 23 or 25 hours long.
      return [iso(start), iso(new Date(new Date(y, m - 1, d + 1, 0, 0, 0).getTime() - 1000))];
    }""", day)
    _eq((query["since"], query["until"]), tuple(want))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", query["since"]), query
    _eq(hist.expected(f"since={query['since']}&until={query['until']}")["rows"][0]["host"], stub.ARCHIVE_HOST)
    hist.close_popover()
    # Escape closes the popover and keeps the filter (the calendar itself clears on Escape).
    page.wait_for_timeout(300)
    _eq(page.get_by_test_id("history-range").inner_text().strip(), page.evaluate(
        "(day) => { const [y, m, d] = day.split('-').map(Number); "
        "return new Date(y, m - 1, d).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}); }", day))
    expect(hist.rows()).to_have_count(1)
    # Clearing the range asks for the newest page again, with neither bound.
    page.get_by_test_id("history-range").click()
    page.get_by_test_id("history-range-clear").click()
    expect(hist.rows()).to_have_count(50)
    query = hist.last_query()
    assert "since" not in query and "until" not in query, query
    hist.close_popover()


def _h_bottle(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    page.get_by_test_id("history-bottle").click()
    page.get_by_role("option", name="mid", exact=True).click()
    expect(page.get_by_test_id("history-page")).to_have_text("Page 1")
    query = hist.last_query()
    _eq(query["container"], "mid")
    want = [r["host"] for r in hist.expected("container=mid&limit=50")["rows"]]
    expect(hist.rows()).to_have_count(len(want))
    _eq(hist.hosts(), want)
    for text in hist.rows().all_inner_texts():
        assert re.search(r"\bmid\b", text), f"row from another bottle: {squash(text)!r}"
    # Paging keeps the filter, and picking All drops it.
    hist.older()
    _eq(hist.last_query()["container"], "mid")
    assert "before" in hist.last_query()
    page.get_by_test_id("history-bottle").click()
    page.get_by_role("option", name="All bottles", exact=True).click()
    expect(hist.rows()).to_have_count(50)
    assert "container" not in hist.last_query() and "before" not in hist.last_query(), hist.last_query()


def _h_client_side(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    sent = len(traffic.recent)
    history_search(page).fill("history-01")
    expect(hist.rows()).to_have_count(10)
    for host in hist.hosts():
        assert "history-01" in host, host
    history_search(page).fill("")
    status_button = lambda label: page.get_by_test_id("history-status").get_by_text(label, exact=True)
    status_button("Denied").click()
    denied = hist.page.locator("[data-testid=history-row]")
    expect(denied.first).to_be_visible()
    for text in denied.all_inner_texts():
        assert re.search(r"Denied|Denylist", text), squash(text)
        assert "Allowed" not in text, squash(text)
    status_button("All").click()
    expect(hist.rows()).to_have_count(50)
    _eq(len(traffic.recent), sent)      # search and the toggle never asked the broker again


def _h_failure(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    before = hist.hosts()
    with broker.lock:
        broker.outage = True
    try:
        page.get_by_test_id("history-older").click()
        expect(page.get_by_test_id("history-error")).to_be_visible()
        _eq(hist.hosts(), before)                  # the list and the page number stay as they were
        _eq(hist.label(), "Page 1")
    finally:
        with broker.lock:
            broker.outage = False
    page.get_by_test_id("history-error").get_by_role("button", name="Retry").click()
    expect(page.get_by_test_id("history-error")).to_have_count(0)
    _eq(hist.label(), "Page 2")


def _h_overflow_and_console(hist, page, traffic, broker) -> None:
    hist.open()
    scroll, inner = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert scroll <= inner, f"scrollWidth {scroll} > innerWidth {inner}"
    for row in hist.rows().all():
        box = row.bounding_box()
        assert box and box["x"] + box["width"] <= inner + 0.5, f"a row overflows: {box}"
    _eq(traffic.console_errors, [])


def _pick_bottle(page, name: str) -> None:
    page.get_by_test_id("history-bottle").click()
    page.get_by_role("option", name=name, exact=True).click()


def _h_filter_failure(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    hist.older()                                     # page 2: a cursor is on the stack
    _eq(hist.label(), "Page 2")
    with broker.lock:
        broker.outage = True
    try:
        _pick_bottle(page, "mid")
        expect(page.get_by_test_id("history-error")).to_be_visible()
        # The previous filter's rows, Older cursor and page number are gone, not left under "mid".
        _eq(hist.rows().count(), 0)
        _eq(hist.label(), "Page 1")
        expect_disabled(page, "history-older", True)
        expect_disabled(page, "history-newer", True)
        _in("0 of 0", page.get_by_test_id("history-summary").inner_text())
    finally:
        with broker.lock:
            broker.outage = False
    page.get_by_test_id("history-error").get_by_role("button", name="Retry").click()
    expect(page.get_by_test_id("history-error")).to_have_count(0)
    want = [r["host"] for r in hist.expected("container=mid&limit=50")["rows"]]
    expect(hist.rows()).to_have_count(len(want))
    _eq(hist.hosts(), want)
    _eq(hist.label(), "Page 1")


def _h_stale_reply(hist, page, traffic, broker) -> None:
    from playwright.sync_api import expect
    hist.open()
    sent, done = len(traffic.recent), len(traffic.recent_done)
    held = broker.hold_next_recent()
    try:
        _pick_bottle(page, "mid")                    # its reply is held
        expect(page.get_by_test_id("history-summary")).to_be_visible()
        _pick_bottle(page, "zeta")                   # answered at once
        want = [r["host"] for r in hist.expected("container=zeta&limit=50")["rows"]]
        expect(hist.rows()).to_have_count(len(want))
    finally:
        held.release()
    assert held.sent.wait(5), "the held reply was never sent"
    # The late reply's arrival is the signal, not a sleep: the stub has only handed it to the socket.
    deadline = time.monotonic() + 15
    while "mid" not in [q.get("container") for q in traffic.recent_done[done:]]:
        assert time.monotonic() < deadline, "the held reply never reached the page"
        page.wait_for_timeout(20)
    # Two frames after arrival: room for a reply handler deferred by a task or two. This is a bound, not
    # proof: a stale reply applied later than that would still pass, as it would have with the old sleep.
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    _eq([q.get("container") for q in traffic.recent[sent:]], ["mid", "zeta"])
    _eq(hist.hosts(), want)                          # the superseded reply did not replace them
    _in("zeta", page.get_by_test_id("history-bottle").inner_text())
    expect_disabled(page, "history-older", hist.expected("container=zeta&limit=50")["next"] is None)


# Every row is two lines (host and date, then pill and bottle) at every viewport, a denied row with a
# reason and a row with a long bottle name included.
TWO_LINE_ROW_MAX_PX = 80
REASON_MIN_PX = 64                       # a shrunken deny reason keeps a few characters and an ellipsis
SM_WIDTHS = (640, 700, 768, 820)         # the widths where the reason's line is tightest


def _h_row_layout(hist, page, traffic, broker) -> None:
    hist.open()
    phone = page.viewport_size["width"] < 640
    rows = hist.rows().all()
    assert len(rows) == 50, len(rows)
    tall = []
    for row in rows:
        text = squash(row.inner_text())
        box = row.bounding_box()
        if box["height"] > TWO_LINE_ROW_MAX_PX:
            tall.append((round(box["height"]), text[:70]))
        if phone:
            assert "by operator" not in text and "by denylist" not in text, f"'by' shown on a phone: {text}"
            assert not re.search(r"\d+[smhd] ago", text), f"relative time shown on a phone: {text}"
        else:
            assert re.search(r"\bby (operator|denylist|sweep)\b", text), f"no 'by' on: {text}"
            assert re.search(r"\b\d+[smhd] ago\b", text), f"no relative time on: {text}"
    assert not tall, f"rows taller than two lines ({TWO_LINE_ROW_MAX_PX}px): {tall[:5]}"
    # The fixture page really holds the worst cases this budget is for.
    texts = [squash(row.inner_text()) for row in rows]
    assert any(stub.LONG_BOTTLE in t and "Denied permanently" in t for t in texts), "no long-bottle denied row on the page"
    long_hosts = [row for row in rows if stub.LONG_HOST_SUFFIX in squash(row.inner_text())]
    assert long_hosts, "no row with a long host on the page"
    for row in long_hosts:
        host = row.get_by_test_id("history-row-host")
        full = squash(host.inner_text())
        assert len(full.split(":")[0]) >= 63, f"the fixture host is not a long one: {full!r}"
        _eq(host.get_attribute("title"), full)          # the whole host stays reachable, and in the row's name
        _eq(host.evaluate("el => getComputedStyle(el).textOverflow"), "ellipsis")
        assert full in squash(row.inner_text()), full
    # The date sits top-right, on the host's line.
    row = rows[0]
    head, when = row.locator(".row-title").bounding_box(), row.get_by_test_id("history-row-when").bounding_box()
    box = row.bounding_box()
    assert when["y"] < head["y"] + head["height"], f"date is below the host: {when} {head}"
    assert when["x"] + when["width"] > box["x"] + box["width"] - 40, f"date is not at the right edge: {when} {box}"
    # Pill then bottle on the line below.
    pill, bottle = row.locator(".pill").first.bounding_box(), row.locator(".meta-item").nth(1).bounding_box()
    assert pill["y"] > head["y"] + head["height"] - 1 and pill["x"] < bottle["x"], f"meta line out of order: {pill} {bottle}"


def _h_reason_and_long_bottle(hist, page, traffic, broker) -> None:
    """The deny reason is a meta-item on the pill and bottle line from sm up and is not shown
    below it; a long bottle name ends in an ellipsis on a phone rather than wrapping."""
    hist.open()
    phone = page.viewport_size["width"] < 640
    denied = [r for r in hist.expected("limit=50")["rows"] if r["deny_reason"]]
    assert any(r["deny_reason"] == stub.LONG_REASON for r in denied), "the fixture page has no long reason"
    reasons = page.get_by_test_id("history-row-reason")
    _eq(reasons.count(), len(denied))
    for row in hist.rows().all():
        reason = row.get_by_test_id("history-row-reason")
        if reason.count() == 0:
            continue
        if phone:
            assert not reason.is_visible(), f"a deny reason is shown on a phone: {reason.inner_text()!r}"
            continue
        assert reason.is_visible(), "the deny reason is hidden from sm up"
        meta, bottle = reason.bounding_box(), row.get_by_test_id("history-row-bottle").bounding_box()
        assert abs((meta["y"] + meta["height"] / 2) - (bottle["y"] + bottle["height"] / 2)) < 4, \
            f"the reason is not on the bottle's line: {meta} {bottle}"
    if not phone:
        # A bottle name far wider than the line gives way to an ellipsis; "by ..." and the reason are not clipped.
        xl_rows = [row for row in hist.rows().all() if stub.XL_BOTTLE in squash(row.inner_text())]
        assert xl_rows, "no row with the 47 character bottle name"
        # The bottle fills the line: the reason still shows a few characters, ending in an ellipsis.
        own = page.viewport_size
        for width in dict.fromkeys((*SM_WIDTHS, own["width"])):
            page.set_viewport_size({"width": width, "height": own["height"]})
            for row in xl_rows:
                reason = row.get_by_test_id("history-row-reason")
                if reason.count() == 0:
                    continue
                content = reason.evaluate(
                    "el => { const s = getComputedStyle(el); "
                    "return el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight); }")
                assert content >= REASON_MIN_PX, f"at {width}px the deny reason has a {content}px content box: {squash(row.inner_text())}"
                _eq(reason.evaluate("el => getComputedStyle(el).textOverflow"), "ellipsis")
                assert reason.evaluate("el => el.scrollWidth > el.clientWidth"), f"at {width}px the deny reason is not cut off with an ellipsis"
        page.set_viewport_size(own)
        for row in xl_rows:
            line = row.locator(".meta-line").bounding_box()
            by = row.locator(".meta-item", has_text=re.compile(r"^by ")).bounding_box()
            assert by["x"] + by["width"] <= line["x"] + line["width"] + 0.5, f"'by ...' is clipped by the bottle name: {by} {line}"
            name = row.get_by_test_id("history-row-bottle").locator(".truncate")
            _eq(name.evaluate("el => getComputedStyle(el).textOverflow"), "ellipsis")
            if name.evaluate("el => el.scrollWidth > el.clientWidth"):
                assert page.viewport_size["width"] < 1024, "the 47 character bottle name is cut off on a desktop"
    long_rows = [row for row in hist.rows().all() if stub.LONG_BOTTLE in squash(row.inner_text())]
    assert long_rows, "no row with the long bottle name"
    for row in long_rows:
        name = row.get_by_test_id("history-row-bottle").locator(".truncate")
        clipped = name.evaluate("el => el.scrollWidth > el.clientWidth")
        overflow = name.evaluate("el => getComputedStyle(el).textOverflow")
        _eq(overflow, "ellipsis")
        wide = "Denied permanently" in squash(row.inner_text())   # the pill that leaves the bottle no room
        if phone and wide:
            assert clipped, f"the long bottle name is not cut off on a phone: {squash(row.inner_text())}"
        elif not phone:
            assert not clipped, f"the long bottle name is cut off from sm up: {squash(row.inner_text())}"


def _h_toolbar_layout(hist, page, traffic, broker) -> None:
    hist.open()
    box = lambda locator: locator.bounding_box()
    search = box(history_search(page))
    date = box(page.get_by_test_id("history-range"))
    bottle = box(page.get_by_test_id("history-bottle"))
    status = box(page.get_by_test_id("history-status"))
    same_row = lambda *boxes: max(b["y"] for b in boxes) < min(b["y"] + b["height"] for b in boxes)
    width = page.viewport_size["width"]
    if width >= 1024:
        assert same_row(search, date, bottle, status), f"desktop toolbar wraps: {search} {date} {bottle} {status}"
    elif width >= 640:
        assert same_row(search, date, bottle), f"tablet: search, date and bottle are not on one row: {search} {date} {bottle}"
        assert status["y"] >= search["y"] + search["height"], f"tablet: the status toggle is not below: {status} {search}"
    else:
        assert status["y"] >= search["y"] + search["height"], f"phone: status is not below search: {status}"
    for name, b in (("search", search), ("date", date), ("bottle", bottle), ("status", status)):
        assert b["x"] >= 0 and b["x"] + b["width"] <= width + 0.5, f"{name} leaves the viewport: {b}"
    # The bottle label sits next to its icon, not centred in the trigger.
    trigger = page.get_by_test_id("history-bottle")
    icon = trigger.locator("svg").first.bounding_box()
    label = trigger.locator("[data-slot=select-value]").bounding_box()
    assert label["x"] - (icon["x"] + icon["width"]) < 16, f"gap between the bottle icon and its label: {icon} {label}"


def _h_date_trigger_name(hist, page, traffic, broker) -> None:
    hist.open()
    button = page.get_by_test_id("history-range")
    _eq(button.get_attribute("aria-label"), "Date range: Any date")
    _eq(page.get_by_role("button", name="Date range: Any date").count(), 1)
    hist.pick_day(30)
    label = button.inner_text().strip()
    assert label != "Any date", label
    _eq(button.get_attribute("aria-label"), f"Date range: {label}")
    _eq(page.get_by_role("button", name=f"Date range: {label}", exact=True).count(), 1)
    hist.close_popover()


def history_search(page):
    """History's search field: the queue's own (hidden while History is open) shares its placeholder."""
    return page.get_by_placeholder("Search destination").locator("visible=true")


def expect_disabled(page, test_id: str, disabled: bool) -> None:
    from playwright.sync_api import expect
    button = page.get_by_test_id(test_id)
    expect(button).to_be_disabled() if disabled else expect(button).to_be_enabled()


RELTIME_DRIFT_SECONDS = 6   # the fake clock also runs in real time, so allow a few seconds past the boundary


def reltime_labels_after(seconds_ago: int, advance: int) -> set:
    """The relative-time tails a row `seconds_ago` old may read `advance` seconds later: the
    seconds or minutes below an hour (relTime switches to hours at 3600 s), then whole hours."""
    labels = set()
    for drift in range(RELTIME_DRIFT_SECONDS):
        total = seconds_ago + advance + drift
        labels.add(f"· {total // 60}m ago" if total < 3600 else f"· {total // 3600}h ago")
    return labels


def _h_relative_time_ticks(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Its own page, on a fake clock installed before load: the relative times are not
    frozen at render. Two minutes pass and no /recent request is sent, yet every young
    row's text has moved on. The queue poll is held: it re-renders the panel every 5 s
    and would refresh the text by accident, so only the panel's own clock can pass."""
    from playwright.sync_api import expect
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    held: list = []
    try:
        page.clock.install(time=datetime.now(timezone.utc))
        hist = HistoryPage(page, served, traffic, broker)
        hist.open()
        page.route("**/api/egress/queue", lambda route: held.append(route))
        when = page.get_by_test_id("history-row-when")
        expect(when).to_have_count(50)
        # The relative time is inside a span hidden below sm, so read text_content, not inner_text.
        before = [squash(text or "") for text in when.evaluate_all("els => els.map(e => e.textContent)")]
        assert all(re.search(r"\d+[smhd] ago", text) for text in before), before[:3]
        sent = len(traffic.recent)
        page.clock.run_for(2 * 60 * 1000)
        page.wait_for_timeout(100)
        after = [squash(text or "") for text in when.evaluate_all("els => els.map(e => e.textContent)")]
        _eq(len(traffic.recent), sent)
        # Rows already in hours or days rightly read the same two minutes on; the ones in
        # seconds or minutes must have moved on to the age two minutes later.
        fresh = [(b, a) for b, a in zip(before, after) if re.search(r"\b\d+[sm] ago", b)]
        assert fresh, f"no row was young enough to show the change: {before[:5]}"
        for b, a in fresh:
            seconds = int(re.search(r"\b(\d+)([sm]) ago", b).group(1)) * (60 if b.endswith("m ago") else 1)
            wanted = reltime_labels_after(seconds, 120)
            assert any(a.endswith(w) for w in wanted), f"a row said {b!r}, then {a!r} after 2 minutes (wanted one of {sorted(wanted)})"
    finally:
        # A held poll left unanswered is a task still pending when the context closes (check 164).
        for route in held:
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass
        close_page_context(context, page)


RELTIME_CHECK = ("60", "History's relative times update on an open page (a fake clock advanced 2 minutes changes every row still in seconds or minutes)", _h_relative_time_ticks)


# The selected day's text over its effective background: each ancestor's colour composited from the
# page canvas up (colours resolved through a canvas, so any CSS colour syntax works), then WCAG 2.x
# relative luminance. Returns {ratio, fg, bg}.
SELECTED_DAY_CONTRAST_JS = """(cell) => {
  const ctx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  const rgba = (css) => {
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data; return [r, g, b, a / 255];
  };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3]));
  const layers = [];
  for (let el = cell; el; el = el.parentElement) layers.push(rgba(getComputedStyle(el).backgroundColor));
  let bg = [255, 255, 255];
  for (const layer of layers.reverse()) bg = over(layer, bg);
  const text = rgba(getComputedStyle(cell).color);
  const fg = over(text, bg);
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return {ratio: (hi + 0.05) / (lo + 0.05), fg: fg.map(Math.round), bg: bg.map(Math.round)};
}"""
MIN_TEXT_CONTRAST = 4.5


def _measure_cell(cell, prefix: str) -> dict:
    """The cell's contrast in the four states a person meets it in: the mouse over it or away, the
    keyboard focus on it or not (the focus: classes would otherwise mask the base and hover colours)."""
    settle = "el => Promise.all(el.getAnimations().map((a) => a.finished))"   # past the colour transition
    page = cell.page
    out = {}
    cell.focus()
    cell.hover()
    cell.evaluate(settle)
    out[f"{prefix}/hovered+focused"] = cell.evaluate(SELECTED_DAY_CONTRAST_JS)
    page.mouse.move(0, 0)
    cell.evaluate(settle)
    out[f"{prefix}/away+focused"] = cell.evaluate(SELECTED_DAY_CONTRAST_JS)
    cell.evaluate("el => el.blur()")
    cell.evaluate(settle)
    assert cell.evaluate("el => document.activeElement !== el"), "the cell kept its focus"
    out[f"{prefix}/away+unfocused"] = cell.evaluate(SELECTED_DAY_CONTRAST_JS)
    cell.hover()
    cell.evaluate(settle)
    assert cell.evaluate("el => document.activeElement !== el"), "hovering the cell focused it"
    out[f"{prefix}/hovered+unfocused"] = cell.evaluate(SELECTED_DAY_CONTRAST_JS)
    return out


def selected_day_contrast(browser, served: Served, broker: stub.StubBroker, viewport: str, theme: str) -> dict:
    """Pick a day in the History date picker and measure its selected cell, then extend the pick to
    a two-day range and measure the range's end cell (selection-end without selection-start) and
    its start cell. A single-day pick carries both attributes, so only the range reaches each half."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        hist = HistoryPage(page, served, traffic, broker)
        hist.open()
        picked = hist.pick_day(3)                # the popover stays open on the selected day
        cell = page.locator("[data-slot=range-calendar-trigger][data-selected]").first
        cell.wait_for()
        found = _measure_cell(cell, "single day")
        year, month, day = (int(part) for part in picked.split("-"))
        other = day + 1 if day < calendar.monthrange(year, month)[1] else day - 1
        page.locator("[data-slot=range-calendar] table").first.locator(
            "[data-slot=range-calendar-trigger]:not([data-outside-view])").get_by_text(str(other), exact=True).first.click()
        end = page.locator("[data-slot=range-calendar-trigger][data-selection-end]:not([data-selection-start])")
        start = page.locator("[data-slot=range-calendar-trigger][data-selection-start]:not([data-selection-end])")
        _eq(end.count(), 1)
        _eq(start.count(), 1)
        found.update(_measure_cell(end, "range end"))
        found.update(_measure_cell(start, "range start"))
        return found
    finally:
        context.close()


def _h_calendar_contrast(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    low = []
    for theme in ("light", "dark"):
        measured = selected_day_contrast(browser, served, broker, viewport, theme)
        _eq(len(measured), 12)                   # three cells (single day, range end, range start), four states each
        for state, got in measured.items():
            if got["ratio"] < MIN_TEXT_CONTRAST:
                low.append(f"{theme}/{state}: {got['ratio']:.2f}:1 (text {got['fg']} on {got['bg']})")
    assert not low, f"the selected day is under {MIN_TEXT_CONTRAST}:1: " + "; ".join(low)


CONTRAST_CHECK = ("62", "The selected day, a range's end and its start in the History date picker have at least 4.5:1 text contrast, hovered or not, focused or not, light and dark", _h_calendar_contrast)

HISTORY_CHECKS = [
    ("30", "History lists the whole store newest first, 50 to a page, in keyset order", _h_newest_first),
    ("31", "Older then Newer walk every page with no row repeated (across a tie in decided_at) and back", _h_walk),
    ("32", "The row decided 30 days ago is reachable by paging", _h_thirty_days),
    ("33", "Picking a day sends the local day as since and until; Clear drops both", _h_date_range),
    ("34", "The bottle filter sends container, keeps it while paging and drops it for All bottles", _h_bottle),
    ("35", "Search and the status toggle filter the loaded page without asking the broker", _h_client_side),
    ("36", "A failed page keeps the list and the page number, shows a banner, and Retry recovers", _h_failure),
    ("37", "History has no horizontal overflow and no console errors", _h_overflow_and_console),
    ("38", "A failed fetch for a new filter shows no rows, no Older cursor and Page 1, not the old filter's", _h_filter_failure),
    ("39", "A slow reply for a superseded filter never replaces the current filter's rows", _h_stale_reply),
    ("40", "Every row is two lines (host and date, pill and bottle) at every viewport, a 63+ character host ending in an ellipsis with the whole host in its title, with by and relative time from sm up", _h_row_layout),
    ("41", "Search, date and bottle share a row on tablet and desktop, the bottle label sits by its icon", _h_toolbar_layout),
    ("42", "The date trigger is named \"Date range: <label>\"", _h_date_trigger_name),
    ("61", "A deny reason sits inline from sm up and is hidden below; a long bottle name ends in an ellipsis on a phone, and from sm up gives way without clipping by or the reason", _h_reason_and_long_bottle),
]

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
    # A fixed locale and zone: check 50 reads the banner's 12-hour clock, and the date picker checks
    # count local days (UTC has no DST day to shift a row across midnight), so the runner's own
    # LANG/TZ must not decide whether a check passes.
    context = browser.new_context(
        viewport=VIEWPORTS[viewport], color_scheme=theme, locale="en-US", timezone_id="UTC",
        device_scale_factor=2 if viewport == "phone" else 1)
    context.add_cookies([{
        "name": admin.SESSION_COOKIE_NAME, "value": served.cookie, "domain": served.host,
        "path": "/", "httpOnly": True, "sameSite": "Strict"}])
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    traffic = Traffic()
    traffic.attach(page)
    return context, page, traffic


def close_page_context(context, page) -> None:
    """Close a context after unrouting its page: a route still installed holds a task that is pending when the
    context goes, and asyncio reports it as "Task was destroyed but it is pending" (check 164)."""
    try:
        page.unroute_all(behavior="ignoreErrors")
    except Exception as error:  # noqa: BLE001 - closing must go on
        log(f"stage=close unroute_all failed: {error}")
    context.close()


def open_queue(page, served: Served, driver_cls, traffic: Traffic) -> Driver:
    from playwright.sync_api import expect

    page.goto(served.base + "/")
    drv = driver_cls(page, traffic)
    expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
    return drv


def capture(page, out: Path, ui: str, viewport: str, theme: str, state: str, *, scroll_to=None) -> None:
    """Screenshot at scroll 0 (the whole page), or with `scroll_to` in view (the viewport only).

    A full-page capture of a scrolled page shows the sticky header over ghosted
    content, so every capture starts from the top.
    """
    path = out / f"{ui}-{viewport}-{theme}-{state}.png"
    if scroll_to is not None:
        scroll_to.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        page.screenshot(path=str(path))
    else:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
        page.screenshot(path=str(path), full_page=True)
    log(f"capture path={path.name} bytes={path.stat().st_size}")


def capture_states(browser, served, broker, driver_cls, out: Path, ui: str, viewport: str, theme: str) -> None:
    """Initial queue, then (spa) the dialog, All view, stale banner, empty queue; the outcome notes."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        drv = open_queue(page, served, driver_cls, traffic)
        capture(page, out, ui, viewport, theme, "initial")
        if ui == "spa":
            drv.set_view("All")
            page.wait_for_timeout(200)
            capture(page, out, ui, viewport, theme, "all-view")
            drv.set_view("By bottle")
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
        if ui == "spa":
            capture(page, out, ui, viewport, theme, "failed-apply", scroll_to=drv.row("m2.example.com"))
    finally:
        context.close()
    if ui == "spa":
        if theme == "light" and viewport != "desktop":
            capture_long_comm(browser, served, broker, out, viewport, theme)
        capture_stale_banner(browser, served, broker, driver_cls, out, viewport, theme)
        capture_empty(browser, served, broker, out, viewport, theme)
        capture_history(browser, served, broker, out, viewport, theme)
        capture_queue_features(browser, served, broker, out, viewport, theme)
        capture_stream_states(browser, served, broker, out, viewport, theme)


def capture_long_comm(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """A 120-character comm on one request: the string wraps inside its card."""
    from playwright.sync_api import expect

    broker.reset()
    add_long_comm_row(broker)
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) + 1)
        capture(page, out, "spa", viewport, theme, "long-comm", scroll_to=drv.row("lc.example.com"))
    finally:
        broker.reset()
        context.close()


def capture_stale_banner(browser, served, broker, driver_cls, out: Path, viewport: str, theme: str) -> None:
    """A decide fails on the broker while polls succeed; later polls are held so the banner stays for the capture."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, theme)
    held: list = []
    try:
        drv = open_queue(page, served, driver_cls, traffic)
        page.route("**/api/egress/queue", lambda route: held.append(route))
        with broker.lock:
            broker.decide_outage = 500
        drv.act("a1.example.com", "deny", reread=False)
        from playwright.sync_api import expect
        expect(drv.stale_banner()).to_be_visible()
        capture(page, out, "spa", viewport, theme, "stale-banner")
    finally:
        with broker.lock:
            broker.decide_outage = None
        for route in held:   # polls held for the capture: pending tasks when the context closes (check 164)
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass
        close_page_context(context, page)


def capture_empty(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """No open requests; the recent list still shows."""
    from playwright.sync_api import expect

    broker.reset()
    with broker.lock:
        broker.queue["open"] = []
        broker.queue["count"] = 0
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        page.goto(served.base + "/")
        expect(page.get_by_text("No open requests.")).to_be_visible()
        capture(page, out, "spa", viewport, theme, "empty")
    finally:
        broker.reset()
        context.close()



def capture_queue_features(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """Queue features: filtered, bulk dialog, bulk in progress, partial failure, expanded denylist group, Allow all with an IP row left open."""
    from playwright.sync_api import expect

    def filtered(q: QueuePage) -> None:
        q.pick_bottles("alpha")
        q.state("Failed apply")
        q.expect_hosts([hp("fa2.example.com"), hp("192.0.2.77")])
        capture(q.page, out, "spa", viewport, theme, "queue-filtered")

    def in_progress(q: QueuePage) -> None:
        held, release_next, _button = _held_decides(q.page, q.traffic, q.drv)
        dialog = q.ask("Deny", "mid")
        capture(q.page, out, "spa", viewport, theme, "queue-bulk-dialog")
        q.confirm(dialog, "Deny")
        q.drv._wait_until(lambda: len(held) == 1, 5)
        release_next()
        q.drv._wait_until(lambda: len(held) == 1, 5)
        expect(q.page.get_by_test_id("bulk-count")).to_have_text("1/4")
        capture(q.page, out, "spa", viewport, theme, "queue-bulk-in-progress")
        for _ in range(3):
            release_next()
            q.drv._wait_until(lambda: len(held) == 1, 2)
        expect(q.status()).to_contain_text("of 4 in mid")

    def partial(q: QueuePage) -> None:
        q.broker.fail_nth_decide(2)
        q.confirm(q.ask("Deny", "mid"), "Deny")
        expect(q.status()).to_contain_text("1 failed and stays open")
        q.drv.wait_note("fm2.example.com", "^Not sent: ")
        capture(q.page, out, "spa", viewport, theme, "queue-partial-failure")

    def denylist(q: QueuePage) -> None:
        q.page.get_by_test_id("denylist-toggle").click()
        expect(q.page.get_by_test_id("denylist-row-hits")).to_have_count(3)
        capture(q.page, out, "spa", viewport, theme, "queue-denylist-expanded",
                scroll_to=q.page.get_by_test_id("denylist-group"))

    def allow_all_ip(q: QueuePage) -> None:
        q.confirm(q.ask("Allow", "alpha"), "Allow")
        expect(q.result()).to_have_text("Allowed 4 of 5 in alpha · 1 needs a CIDR in the manifest")
        q.drv.wait_note("192.0.2.77", "^Recorded")
        capture(q.page, out, "spa", viewport, theme, "queue-allow-all-ip")

    for fn in (filtered, in_progress, partial, denylist, allow_all_ip):
        with_features_page(browser, served, broker, viewport, theme, fn)


def capture_history(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """History: the first page, an older page (page 3), and the view filtered to the day 30 days back."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        hist = HistoryPage(page, served, traffic, broker)
        hist.open()
        capture(page, out, "spa", viewport, theme, "history-first")
        hist.older()
        hist.older()
        capture(page, out, "spa", viewport, theme, "history-older")
        hist.open()
        row = hist.rows().filter(has_text=stub.LONG_HOST_SUFFIX).first
        row.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        row.screenshot(path=str(out / f"spa-{viewport}-{theme}-history-long-host.png"))
        hist.open()
        hist.pick_day(30)
        from playwright.sync_api import expect
        expect(hist.rows()).to_have_count(1)
        page.locator("[data-slot=range-calendar-trigger][data-selected]").first.evaluate(
            "el => Promise.all(el.getAnimations().map((a) => a.finished))")   # a settled selected day, mouse over it
        capture(page, out, "spa", viewport, theme, "history-filtered-open")
        hist.close_popover()
        capture(page, out, "spa", viewport, theme, "history-filtered")
    finally:
        context.close()

# ---- Service worker (120..127) --------------------------------------------------------------

LEGACY_SHELL_CACHE = "djinn-admin-shell-v3"     # the legacy page's cache; the new worker deletes it
OTHER_FOREIGN_CACHE = "some-other-app-cache"    # any cache that is not the worker's own goes too
OWN_CACHE_PREFIX = "djinn-admin-precache-"      # pwa.ts CACHE_ID + workbox's precache name
POISON = "POISONED-BY-THE-TEST"

# Plants pages in caches the worker must never consult, before any worker exists.
SEED_CACHES_JS = """async ([legacy, other, poison]) => {
  for (const name of [legacy, other]) {
    const cache = await caches.open(name);
    for (const path of ['/', '/egress', '/app.js', '/api/egress/queue']) await cache.put(path, new Response(poison));
  }
  return await caches.keys();
}"""

# Waits for the worker to be active, controlling this page, and to have pruned every foreign cache.
SETTLED_JS = """async ([legacy, other]) => {
  const reg = await navigator.serviceWorker.ready;
  // `ready` resolves while the worker may still be `activating`; wait for `activated`.
  if (!reg.active || reg.active.state !== 'activated' || !navigator.serviceWorker.controller) return false;
  const keys = await caches.keys();
  return !keys.includes(legacy) && !keys.includes(other);
}"""

# Plants the same pages in the worker's OWN cache: were `/` or `/api/*` ever answered from a
# cache the worker owns, this is what it would answer with.
PLANTED_PATHS = ["/", "/egress", "/api/egress/queue"]
PLANT_OWN_CACHE_JS = """async ([prefix, poison, paths]) => {
  const own = (await caches.keys()).find((name) => name.startsWith(prefix));
  const cache = await caches.open(own);
  for (const path of paths) await cache.put(path, new Response(poison));
  return own;
}"""

CACHE_STORAGE_JS = """async () => {
  const out = {};
  for (const name of await caches.keys()) {
    out[name] = (await (await caches.open(name)).keys()).map((req) => new URL(req.url).pathname);
  }
  return out;
}"""


# Every entry of every cache: its url (path and query) and whether its body is still the planted poison.
# An entry the worker wrote over a planted url has a body that is no longer the poison.
CACHE_ENTRIES_JS = """async (poison) => {
  const out = {};
  for (const name of await caches.keys()) {
    const cache = await caches.open(name);
    out[name] = [];
    for (const req of await cache.keys()) {
      const u = new URL(req.url);
      out[name].push({url: u.pathname + u.search, poisoned: (await (await cache.match(req)).text()) === poison});
    }
  }
  return out;
}"""

# The daemon is unreachable: a fetch and a navigation must fail, not be answered from a cache.
OFFLINE_FETCH_JS = """async (url) => {
  try {
    const r = await fetch(url, {cache: 'no-store'});
    return {failed: false, status: r.status, text: (await r.text()).slice(0, 80)};
  } catch (err) {
    return {failed: true, error: String(err)};
  }
}"""


class DaemonRequestLog:
    """Every request the admin daemon answers, as (method, path with query, status, has cookie).

    A request the service worker answered from a cache never reaches the daemon, so
    a request in this log went to the network and one missing from it did not.
    """

    def __init__(self):
        self.entries: list[dict] = []
        self._patch = None

    def __enter__(self):
        original = admin.AdminRequestHandler.log_request
        entries = self.entries

        def record(handler, code="-", size="-"):
            entries.append({"method": handler.command, "path": handler.path,
                            "status": int(code) if str(code).isdigit() else 0,
                            "cookie": bool(handler.headers.get("Cookie"))})
            original(handler, code, size)

        self._patch = mock.patch.object(admin.AdminRequestHandler, "log_request", record)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def count(self, path: str, since: int = 0, *, method: str = "GET") -> int:
        return sum(1 for e in self.entries[since:] if e["path"] == path and e["method"] == method)


@dataclass
class SwObservations:
    seeded: list = field(default_factory=list)          # cache names before the worker existed
    registration: dict = field(default_factory=dict)    # scope, script URL, state
    caches_before: dict = field(default_factory=dict)   # Cache Storage once the worker is active
    own_cache: str = ""
    reload: dict = field(default_factory=dict)          # what a reload, and a fresh open of `/`, got
    api: list = field(default_factory=list)             # each /api/egress/queue probe: url, status, body, daemon count
    no_cookie: list = field(default_factory=list)       # `/`, `/egress`: the page the browser was shown
    manifest: dict = field(default_factory=dict)
    sw_headers: dict = field(default_factory=dict)
    caches_after: dict = field(default_factory=dict)    # Cache Storage at the very end
    entries_after: dict = field(default_factory=dict)   # the same, with each entry's url and query, and whether it is still planted
    offline: dict = field(default_factory=dict)         # the api fetch and the navigation of `/` with the daemon unreachable


def service_worker_scenario(browser, served: Served, broker: stub.StubBroker, viewport: str) -> SwObservations:
    """Install the worker in a fresh context, then probe what it does and does not serve."""
    from playwright.sync_api import expect

    started = time.monotonic()
    broker.reset()
    obs = SwObservations()
    context = browser.new_context(
        viewport=VIEWPORTS[viewport], color_scheme="light", locale="en-US", timezone_id="UTC",
        service_workers="allow", device_scale_factor=2 if viewport == "phone" else 1)
    cookie = {"name": admin.SESSION_COOKIE_NAME, "value": served.cookie, "domain": served.host,
              "path": "/", "httpOnly": True, "sameSite": "Strict"}
    context.add_cookies([cookie])
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    try:
        with DaemonRequestLog() as daemon:
            # Before any worker exists: a page that is not the app registers nothing, so the legacy
            # and foreign caches can be planted first.
            page.goto(served.base + "/favicon.svg")
            obs.seeded = page.evaluate(SEED_CACHES_JS, [LEGACY_SHELL_CACHE, OTHER_FOREIGN_CACHE, POISON])
            log(f"stage=sw seed viewport={viewport} caches={sorted(obs.seeded)}")

            page.goto(served.base + "/")
            expect(page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS))
            page.wait_for_function(SETTLED_JS, arg=[LEGACY_SHELL_CACHE, OTHER_FOREIGN_CACHE], timeout=TIMEOUT_MS * 2)
            obs.registration = page.evaluate("""async () => {
                const reg = await navigator.serviceWorker.ready;
                return {scope: reg.scope, script: reg.active.scriptURL, state: reg.active.state,
                        controlled: navigator.serviceWorker.controller !== null,
                        controllerScript: navigator.serviceWorker.controller && navigator.serviceWorker.controller.scriptURL};
            }""")
            obs.caches_before = page.evaluate(CACHE_STORAGE_JS)
            log(f"stage=sw active viewport={viewport} scope={obs.registration['scope']} "
                f"script={obs.registration['script']} caches={ {k: len(v) for k, v in obs.caches_before.items()} }")

            obs.own_cache = page.evaluate(PLANT_OWN_CACHE_JS, [OWN_CACHE_PREFIX, POISON, PLANTED_PATHS])

            # (121) reloading the page, and opening `/` afresh, go to the network, not the planted copy.
            # The app's router lands `/` on /egress, so a reload asks for /egress: both are probed.
            here = urlsplit(page.url).path
            for label, load in (("reload", lambda: page.reload()), ("root", lambda: page.goto(served.base + "/"))):
                path = here if label == "reload" else "/"
                mark = len(daemon.entries)
                response = load()
                expect(page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS))
                obs.reload[label] = {
                    "path": path, "status": response.status, "from_worker": response.from_service_worker,
                    "cache_control": response.headers.get("cache-control"), "poisoned": POISON in page.content(),
                    "daemon_gets": daemon.count(path, mark),
                    "controlled": page.evaluate("navigator.serviceWorker.controller !== null")}

            # (122) API reads from the controlled page: each unique URL must reach the daemon
            # The same URL three times: a cache that answers the second read shows as a missing daemon hit.
            for _ in range(3):
                url = f"/api/egress/queue?probe={viewport}"
                mark = len(daemon.entries)
                got = page.evaluate("""async (url) => {
                    const r = await fetch(url, {cache: 'no-store'});
                    return {status: r.status, text: await r.text()};
                }""", url)
                obs.api.append({"url": url, "status": got["status"], "poisoned": POISON in got["text"],
                                "daemon": daemon.count(url, mark)})

            # (127) the daemon unreachable: an /api read and a navigation of `/` must fail. Were either
            # answered from a cache the worker owns (network-first, offline fallback) this is where it shows.
            nonce = f"offline-{viewport}-{int(time.time() * 1000)}"
            context.set_offline(True)
            try:
                obs.offline["api"] = page.evaluate(OFFLINE_FETCH_JS, f"/api/egress/queue?probe={nonce}")
                obs.offline["api"]["url"] = f"/api/egress/queue?probe={nonce}"
                # The app's own URL, already read online: a fallback keyed on it shows here, not on a fresh probe.
                obs.offline["app_api"] = page.evaluate(OFFLINE_FETCH_JS, "/api/egress/queue")
                obs.offline["app_api"]["url"] = "/api/egress/queue"
                try:
                    response = page.goto(served.base + "/")
                    obs.offline["root"] = {"failed": False, "status": response.status if response else None,
                                           "from_worker": response.from_service_worker if response else None}
                except Exception as exc:  # noqa: BLE001 - the navigation failing is the expected outcome
                    obs.offline["root"] = {"failed": True, "error": squash(str(exc))[:120]}
            finally:
                context.set_offline(False)
            log(f"stage=sw offline viewport={viewport} api_failed={obs.offline['api']['failed']} "
                f"root_failed={obs.offline['root']['failed']}")
            page.goto(served.base + "/egress")
            expect(page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS))

            # (126) the worker script itself and the manifest, as the browser fetches them
            obs.sw_headers = page.evaluate("""async () => {
                const r = await fetch('/sw.js', {cache: 'no-store'});
                return {status: r.status, type: r.headers.get('content-type'), cache: r.headers.get('cache-control'),
                        precaches: (await r.text()).includes('precacheAndRoute')};
            }""")
            # (125) the manifest the page links
            obs.manifest = page.evaluate("""async () => {
                const link = document.querySelector('link[rel=manifest]');
                if (!link) return {linked: false};
                const r = await fetch(link.href);
                const json = await r.json();
                const icons = [];
                for (const icon of json.icons) {
                    const ir = await fetch(new URL(icon.src, link.href));
                    icons.push({src: icon.src, sizes: icon.sizes, status: ir.status, type: ir.headers.get('content-type')});
                }
                return {linked: true, href: new URL(link.href).pathname, status: r.status,
                        type: r.headers.get('content-type'), json, icons};
            }""")

            # (123) no session cookie: the pointer page, on `/` and on an app route, worker active
            context.clear_cookies()
            for route in ("/", "/egress"):
                mark = len(daemon.entries)
                response = page.goto(served.base + route)
                obs.no_cookie.append({
                    "route": route, "status": response.status, "from_worker": response.from_service_worker,
                    "text": squash(page.locator("body").inner_text()), "poisoned": POISON in page.content(),
                    "app_mounted": page.locator("[data-testid=request], #app *").count() > 0,
                    "daemon": daemon.count(route, mark), "cookie_sent": any(
                        e["cookie"] for e in daemon.entries[mark:] if e["path"] == route),
                    "controlled": page.evaluate("navigator.serviceWorker.controller !== null")})
            obs.caches_after = page.evaluate(CACHE_STORAGE_JS)
            obs.entries_after = page.evaluate(CACHE_ENTRIES_JS, POISON)
            log(f"stage=sw probes viewport={viewport} daemon_requests={len(daemon.entries)} "
                f"caches_after={ {k: len(v) for k, v in obs.caches_after.items()} } "
                f"ms={int((time.monotonic() - started) * 1000)}")
    finally:
        context.close()
    return obs


def _own_cache_only(caches: dict, own: str) -> None:
    assert list(caches) == [own], f"Cache Storage holds {sorted(caches)}, expected only {own}"
    assert own.startswith(OWN_CACHE_PREFIX), f"{own} is not the worker's own precache"


def run_service_worker(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    suite = Suite(ui, viewport)
    obs, failure = None, None
    if ui == "spa":
        try:
            obs = service_worker_scenario(browser, served, broker, viewport)
        except Exception as exc:  # noqa: BLE001 - every check below then reports it
            failure = squash(str(exc))[:200]

    def check(number: str, name: str, assertion: Callable[[SwObservations], None]) -> None:
        def run() -> None:
            if failure is not None:
                raise AssertionError(f"the service-worker scenario failed: {failure}")
            assertion(obs)
        suite.check(number, name, run, spa_only=True)

    def registered(o: SwObservations) -> None:
        reg = o.registration
        assert reg["scope"] == served.base + "/", f"scope {reg['scope']}"
        assert reg["script"] == served.base + "/sw.js", f"script {reg['script']}"
        assert reg["state"] == "activated", f"state {reg['state']}"
        assert reg["controlled"] and reg["controllerScript"] == reg["script"], f"the page is not controlled: {reg}"
        assert o.sw_headers["precaches"], "the worker at /sw.js is not the generated precache worker"

    def reload_hits_network(o: SwObservations) -> None:
        assert set(o.reload) == {"reload", "root"}
        for label, r in o.reload.items():
            assert r["controlled"], f"{label}: the page was not controlled by the worker"
            assert r["status"] == 200 and not r["from_worker"], f"{label}: answered by the worker: {r}"
            assert not r["poisoned"], f"{label}: showed the page planted in the worker's own cache"
            assert r["cache_control"] == "no-store", f"{label}: cache-control {r['cache_control']!r}"
            assert r["daemon_gets"] == 1, f"{label}: the daemon saw {r['daemon_gets']} GET {r['path']}, expected 1"

    def api_hits_network(o: SwObservations) -> None:
        assert len(o.api) == 3
        for probe in o.api:
            assert probe["status"] == 200 and not probe["poisoned"], f"{probe['url']} answered from a cache: {probe}"
            assert probe["daemon"] == 1, f"the daemon saw {probe['daemon']} request(s) for {probe['url']}, expected 1"

    def pointer_without_cookie(o: SwObservations) -> None:
        assert [p["route"] for p in o.no_cookie] == ["/", "/egress"]
        for page_seen in o.no_cookie:
            where = page_seen["route"]
            assert page_seen["controlled"], f"{where}: the page was not controlled by the worker"
            assert page_seen["status"] == 200 and not page_seen["from_worker"], f"{where}: {page_seen}"
            assert "./djinn egress url" in page_seen["text"], f"{where} is not the pointer page: {page_seen['text'][:80]!r}"
            assert not page_seen["poisoned"] and not page_seen["app_mounted"], f"{where} showed the app or a cached page"
            assert page_seen["daemon"] == 1 and not page_seen["cookie_sent"], \
                f"{where}: the daemon saw {page_seen['daemon']} cookie-less request(s), cookie_sent={page_seen['cookie_sent']}"

    def caches_pruned(o: SwObservations) -> None:
        assert LEGACY_SHELL_CACHE in o.seeded and OTHER_FOREIGN_CACHE in o.seeded, f"not seeded: {o.seeded}"
        assert LEGACY_SHELL_CACHE not in o.caches_before and OTHER_FOREIGN_CACHE not in o.caches_before
        own = [name for name in o.caches_before if name.startswith(OWN_CACHE_PREFIX)]
        assert len(own) == 1, f"expected one precache, found {sorted(o.caches_before)}"
        _own_cache_only(o.caches_before, own[0])
        _own_cache_only(o.caches_after, own[0])
        assert o.own_cache == own[0]
        urls = o.caches_before[own[0]]
        assert urls and all(path.startswith("/assets/") for path in urls), f"the precache holds {urls}"
        # At the very end the cache also holds the pages this test planted in it (PLANTED_PATHS, still
        # the poison). Everything else, a url a probe fetched included, must be a hashed /assets/ file.
        for entry in o.entries_after[own[0]]:
            planted = entry["url"] in PLANTED_PATHS and entry["poisoned"]
            assert planted or entry["url"].startswith("/assets/"), \
                f"the worker's own cache holds {entry['url']}, which is neither under /assets/ nor a page this test planted"
        # ...and the /assets/ entries are exactly the precache: nothing was stored under an /assets/ key since.
        assets_after = sorted(e["url"] for e in o.entries_after[own[0]] if e["url"].startswith("/assets/"))
        assert assets_after == sorted(urls), f"the worker's /assets/ entries changed: {sorted(set(assets_after) ^ set(urls))}"

    def offline_fails(o: SwObservations) -> None:
        api, app_api, root = o.offline["api"], o.offline["app_api"], o.offline["root"]
        for probe in (api, app_api):
            assert probe["failed"], f"{probe['url']} with the daemon unreachable was answered: {probe}"
            assert "Failed to fetch" in probe["error"], f"{probe['url']} failed, but not with a network error: {probe['error']}"
        assert root["failed"], f"navigating to / with the daemon unreachable was answered: {root}"
        assert "ERR_INTERNET_DISCONNECTED" in root["error"], f"navigating to / failed, but not with a network error: {root['error']}"

    def manifest_installable(o: SwObservations) -> None:
        m = o.manifest
        assert m["linked"] and m["href"] == "/manifest.webmanifest", f"manifest link: {m}"
        assert m["status"] == 200 and m["type"] == "application/manifest+json", f"manifest served as {m['type']}"
        assert m["json"]["display"] == "standalone" and m["json"]["start_url"] == "/" and m["json"]["scope"] == "/"
        assert m["json"]["name"] == "Djinn admin" and m["json"]["theme_color"] == "#fafafb"
        assert sorted(i["sizes"] for i in m["icons"]) == ["192x192", "512x512"], m["icons"]
        assert all(i["status"] == 200 and i["type"] == "image/png" for i in m["icons"]), m["icons"]

    def worker_script_never_cached(o: SwObservations) -> None:
        h = o.sw_headers
        assert h["status"] == 200 and h["type"].startswith("text/javascript"), h
        assert h["cache"] == "no-cache", f"sw.js is served with cache-control {h['cache']!r}"

    check("120", "The service worker registers from the app at scope `/`, activates and controls the page; "
                 "/sw.js is the generated precache worker", registered)
    check("121", "With the worker active, reloading the page and opening `/` afresh go to the network (the daemon sees "
                 "each GET, cache-control no-store), not a copy planted in the worker's own cache", reload_hits_network)
    check("122", "With the worker active, every /api/egress/queue read from the page, the same URL again and again, "
                 "reaches the daemon", api_hits_network)
    check("123", "With the worker active and the session cookie cleared, `/` and an app route show the pointer page "
                 "from the network, not the app", pointer_without_cookie)
    check("124", "Cache Storage holds only the worker's own precache, and only /assets/ urls in it (at the end, besides "
                 "the pages the test planted itself); a pre-seeded `djinn-admin-shell-v3` and another foreign cache are "
                 "deleted after activation", caches_pruned)
    check("125", "The page links a web manifest: standalone, start `/`, tokens' canvas colour, 192 and 512 png icons "
                 "that load", manifest_installable)
    check("126", "/sw.js is served as JavaScript with cache-control no-cache", worker_script_never_cached)
    check("127", "With the daemon unreachable (context offline) an /api/egress/queue fetch and a navigation to `/` "
                 "fail with a network error instead of being answered from a cache", offline_fails)
    return suite.results


# ---- notification bell ----------------------------------------------------------------

BELL_LABELS = {   # the tooltip: what the bell is doing (and, for a toggle, what a click does)
    "unsupported": "Desktop notifications are not available on this device or browser",
    "default": "Enable desktop notifications",
    "on": "Desktop notifications on. Click to mute",
    "muted": "Desktop notifications muted. Click to turn on",
    "denied": "Desktop notifications are blocked in browser settings",
}
INSECURE_LABEL = "Desktop notifications need a secure connection (https or localhost)"
TOGGLE_NAME = "Desktop notifications"   # a toggle keeps one accessible name; aria-pressed carries on/muted
BELL_STATES = list(BELL_LABELS)
BROWSER_NEXT_STEP = "Open the admin in a desktop browser such as Chrome or Firefox."


def bell_name(state: str) -> str:
    return TOGGLE_NAME if state in ("on", "muted") else BELL_LABELS[state]


def assert_bell(bell, state: str, *, label: str | None = None) -> None:
    """The bell's state, accessible name, tooltip and (for a toggle) pressed state, all at once."""
    label = label or BELL_LABELS[state]
    name = TOGGLE_NAME if state in ("on", "muted") else label
    got = {"state": bell.get_attribute("data-state"), "name": bell.get_attribute("aria-label"),
           "title": bell.get_attribute("title"), "pressed": bell.get_attribute("aria-pressed")}
    want = {"state": state, "name": name, "title": label,
            "pressed": {"on": "true", "muted": "false"}.get(state)}
    assert got == want, f"bell {got} != {want}"
MUTE_STORAGE_KEY = "djinn-admin-notifications-muted"


def notify_spy_script(*, permission: str = "default", answer: str = "granted", wrap: bool = False,
                      remove: bool = False, throws: bool = False, insecure: bool = False) -> str:
    """An init script that replaces `window.Notification` with a recorder.

    `window.__notifySpy` holds `permission` (what the page reads), `answer` (what `requestPermission`
    resolves with and then reports), `requests` (how many times the page asked) and `made` (every
    construction: title, options, and the instance, whose `onclick` the test calls). `wrap` builds the
    real API underneath (a subclass, so the browser constructs and validates each notification) but still
    reports `permission` itself: headless Chromium reports `denied` whatever a context grants. `remove`
    deletes the API. `throws` makes the constructor throw `TypeError: Illegal constructor`, as Chrome for
    Android does, while `requestPermission` still grants; `spy.attempts` counts the constructions tried.
    `insecure` makes `window.isSecureContext` false.
    """
    return f"""(() => {{
  const spy = window.__notifySpy = {{permission: {json.dumps(permission)}, answer: {json.dumps(answer)},
                                    requests: 0, attempts: 0, made: []}};
  if ({json.dumps(insecure)}) Object.defineProperty(window, 'isSecureContext', {{value: false}});
  if ({json.dumps(remove)}) {{ delete window.Notification; return; }}
  const Base = {json.dumps(wrap)} ? window.Notification : class {{ close() {{ this.closed = true; }} }};
  class SpyNotification extends Base {{
    constructor(title, options) {{
      spy.attempts += 1;
      if ({json.dumps(throws)}) throw new TypeError("Failed to construct 'Notification': Illegal constructor");
      super(title, options);
      this.onclick = null;
      spy.made.push({{title, options: {{...(options || {{}})}}, instance: this}});
    }}
    static requestPermission(cb) {{
      spy.requests += 1;
      spy.permission = spy.answer;
      if (cb) cb(spy.answer);
      return Promise.resolve(spy.answer);
    }}
  }}
  Object.defineProperty(SpyNotification, 'permission', {{get: () => spy.permission}});
  window.Notification = SpyNotification;
}})();"""


class NotifyPage:
    """A queue page with the Notification spy installed.

    By default the tab is held on the polling path and the poll is driven by a fake clock: the stream is
    refused before load (the tab reads Polling and polls every 5 s), so a snapshot is one `poll()` call, not
    a 5 s wait, and the checks do not depend on which source delivers it. `live` leaves the real path: no
    fake clock, the stream open, snapshots arriving by it in real time.
    """

    POLL_MS = 5000

    def __init__(self, browser, served: Served, broker: stub.StubBroker, viewport: str, theme: str = "light", *,
                 permission: str = "default", answer: str = "granted", wrap: bool = False, remove: bool = False,
                 throws: bool = False, insecure: bool = False, grant: bool = False, live: bool = False):
        from playwright.sync_api import expect
        broker.reset()
        self.broker = broker
        self.live = live
        self.queue_reads_seen: list[float] = []
        if live:
            settle_hub(served)   # a stream of an earlier check would hand this tab a snapshot from before the reset
        self.context, self.page, self.traffic = new_page(browser, served, viewport, theme)
        try:
            if grant:
                self.context.grant_permissions(["notifications"], origin=served.base)
            self.page.add_init_script(script=notify_spy_script(
                permission=permission, answer=answer, wrap=wrap, remove=remove, throws=throws, insecure=insecure))
            if live:
                self.queue_reads_seen = queue_requests(self.page)
            else:
                self.page.route(STREAM_ROUTE, lambda route: route.abort())
                # A fake clock from before the first script runs: the poll only fires on `poll()`.
                self.page.clock.install(time=datetime.now(timezone.utc))
            self.page.goto(served.base + "/")
            expect(self.page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS))
            if live:
                wait_link(self.page, "live", TIMEOUT_MS)
                # The window of "no /api/egress/queue read with the stream open" starts at the first frame;
                # the one read a connecting tab makes is set aside (as open_live_page does).
                assert len(self.queue_reads_seen) == 1, f"{len(self.queue_reads_seen)} queue reads while the stream was opening"
                deadline = time.monotonic() + 5
                while self.traffic.queue_reads < 1 and time.monotonic() < deadline:
                    self.page.wait_for_timeout(20)
                assert self.traffic.queue_reads == 1, f"{self.traffic.queue_reads} queue responses while the stream was opening"
                self.queue_reads_seen.clear()
                self.traffic.queue_reads = 0
            else:
                wait_link(self.page, "polling", TIMEOUT_MS)
        except Exception:
            self.context.close()
            raise

    def close(self) -> None:
        close_page_context(self.context, self.page)

    @property
    def bell(self):
        return self.page.get_by_test_id("notify-bell")

    def poll(self, *, rows: int | None = None) -> None:
        """One queue poll, waited out until the page has rendered its reply."""
        from playwright.sync_api import expect
        reads = self.traffic.queue_reads
        self.page.clock.run_for(self.POLL_MS)
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while self.traffic.queue_reads <= reads and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        assert self.traffic.queue_reads > reads, "the poll never reached the daemon"
        if rows is not None:
            expect(self.page.locator("[data-testid=request]")).to_have_count(rows)
        self.page.wait_for_timeout(100)   # the notification is fired in the same tick as the render

    def made(self) -> list[dict]:
        return self.page.evaluate(
            "() => window.__notifySpy.made.map(n => ({title: n.title, body: n.options.body, tag: n.options.tag}))")

    def permission_requests(self) -> int:
        return self.page.evaluate("() => window.__notifySpy.requests")

    def click_notification(self, index: int = 0) -> None:
        """What the OS does when the user clicks a notification: call the recorded handler."""
        self.page.evaluate("(i) => window.__notifySpy.made[i].instance.onclick(new Event('click'))", index)

    def focused_request(self) -> str | None:
        return self.page.evaluate(
            "() => document.activeElement && document.activeElement.getAttribute('data-request-id')")


def _filed(title_bottle: str, host: str, port: int, rid: str) -> dict:
    return {"title": f"New egress request from {title_bottle}", "body": f"{host}:{port}", "tag": rid}


def _n_defaults_ask_nothing(n: NotifyPage) -> None:
    for _ in range(2):
        n.poll()
    assert n.permission_requests() == 0, f"the page asked for permission {n.permission_requests()} time(s) without a click"
    assert n.made() == [], n.made()
    assert_bell(n.bell, "default")


def _n_click_asks_once(n: NotifyPage) -> None:
    n.bell.click()
    n.page.wait_for_function("() => window.__notifySpy.requests > 0")
    n.page.wait_for_selector("[data-testid=notify-bell][data-state=on]")
    assert n.permission_requests() == 1, n.permission_requests()
    assert_bell(n.bell, "on")
    # The rows already open when permission was granted are not news, then or on the next snapshot.
    assert n.made() == [], n.made()
    n.poll()
    assert n.made() == [], n.made()


def _n_one_per_new_request(n: NotifyPage) -> None:
    from playwright.sync_api import expect
    assert n.bell.get_attribute("data-state") == "on"
    n.poll()
    assert n.made() == [], f"the initial snapshot's rows notified: {n.made()}"
    n.broker.file_request("new.example.com", container="mid", request_id="n1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.made() == [_filed("mid", "new.example.com", 443, "n1")], n.made()
    n.poll()
    n.poll()
    assert n.made() == [_filed("mid", "new.example.com", 443, "n1")], f"the same row notified again: {n.made()}"
    # Two at once: one each, and the earlier one still counted once.
    n.broker.file_request("two.example.com", container="alpha", port=8443, request_id="n2")
    n.broker.file_request("192.0.2.9", container="zeta", port=5432, request_id="n3")
    n.poll(rows=len(stub.OPEN_ROWS) + 3)
    assert n.made() == [
        _filed("mid", "new.example.com", 443, "n1"),
        _filed("alpha", "two.example.com", 8443, "n2"),
        _filed("zeta", "192.0.2.9", 5432, "n3"),
    ], n.made()
    expect(n.page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS) + 3)


def _n_mute(n: NotifyPage) -> None:
    served_page = n.page
    n.bell.click()
    served_page.wait_for_selector("[data-testid=notify-bell][data-state=muted]")
    assert_bell(n.bell, "muted")
    assert n.permission_requests() == 0, "muting asked for permission"
    n.broker.file_request("quiet.example.com", request_id="q1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.made() == [], f"a muted tab notified: {n.made()}"
    # Unmuting shows nothing that arrived while muted, only what comes next.
    n.bell.click()
    served_page.wait_for_selector("[data-testid=notify-bell][data-state=on]")
    assert_bell(n.bell, "on")
    n.poll()
    assert n.made() == [], f"unmuting replayed the requests that arrived muted: {n.made()}"
    n.bell.click()
    served_page.wait_for_selector("[data-testid=notify-bell][data-state=muted]")
    # The mute is this viewer's, kept in localStorage: it survives a reload.
    assert served_page.evaluate("(k) => localStorage.getItem(k)", MUTE_STORAGE_KEY) == "1"
    served_page.reload()
    served_page.wait_for_selector("[data-testid=notify-bell][data-state=muted]")
    n.broker.file_request("quiet2.example.com", request_id="q2")
    n.poll(rows=len(stub.OPEN_ROWS) + 2)
    assert n.made() == [], f"a reloaded muted tab notified: {n.made()}"
    # Unmuting shows nothing that arrived while muted, only what comes next.
    n.bell.click()
    served_page.wait_for_selector("[data-testid=notify-bell][data-state=on]")
    n.poll()
    assert n.made() == [], f"unmuting replayed the requests that arrived muted: {n.made()}"
    n.broker.file_request("loud.example.com", request_id="l1")
    n.poll(rows=len(stub.OPEN_ROWS) + 3)
    assert n.made() == [_filed("alpha", "loud.example.com", 443, "l1")], n.made()
    assert served_page.evaluate("(k) => localStorage.getItem(k)", MUTE_STORAGE_KEY) is None


def _n_denied(n: NotifyPage) -> None:
    from playwright.sync_api import expect
    assert_bell(n.bell, "denied")
    n.bell.click()
    blocked = n.page.get_by_test_id("notify-blocked")
    expect(blocked).to_be_visible()
    text = squash(blocked.inner_text())
    assert "blocked" in text and "browser" in text and "settings" in text, text
    assert n.permission_requests() == 0, "a blocked bell prompted anyway"
    n.broker.file_request("nope.example.com", request_id="d1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.made() == [], f"a blocked tab notified: {n.made()}"


def _n_click_focuses_row(n: NotifyPage) -> None:
    page = n.page
    n.poll()
    n.broker.file_request("focus.example.com", container="zeta", request_id="f1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert [m["tag"] for m in n.made()] == ["f1"], n.made()
    # From the History tab: the queue tab comes back and the row has focus.
    page.get_by_test_id("tab-history").click()
    page.wait_for_selector("[data-testid=request]", state="hidden")
    n.click_notification(0)
    page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('data-request-id') === 'f1'")
    box = page.evaluate("() => { const r = document.activeElement.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, h: innerHeight}; }")
    assert 0 <= box["top"] and box["bottom"] <= box["h"], f"the focused row is not in view: {box}"
    assert n.focused_request() == "f1"
    # From another route.
    page.locator("a[href='/denylist']").first.click()
    page.wait_for_url("**/denylist")
    assert page.locator("[data-testid=request]").count() == 0
    n.click_notification(0)
    page.wait_for_url("**/egress")
    page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('data-request-id') === 'f1'")
    # With a filter that does NOT hide it: the filter stays, and nothing is announced.
    from playwright.sync_api import expect
    search = page.get_by_test_id("queue-filters").get_by_role("textbox")
    status = page.get_by_test_id("filters-cleared-status")
    search.fill("focus.example")
    expect(page.locator("[data-testid=request]")).to_have_count(1)
    page.get_by_test_id("tab-history").click()
    n.click_notification(0)
    page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('data-request-id') === 'f1'")
    assert search.input_value() == "focus.example", f"a filter that did not hide the row was cleared: {search.input_value()!r}"
    assert (status.text_content() or "").strip() == "", status.text_content()
    # With a filter hiding it: the filters give way, and the polite live region says so.
    search.fill("check18")
    expect(page.locator("[data-testid=request]")).to_have_count(1)
    n.click_notification(0)
    page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('data-request-id') === 'f1'")
    assert search.input_value() == "", "the filter that hid the row was left on"
    assert (status.text_content() or "").strip() == "Filters cleared to show focus.example.com", status.text_content()
    assert status.get_attribute("role") == "status", status.get_attribute("role")
    # A request decided since: the click still lands on the queue, with no error.
    n.broker.withdraw_request("f1")
    n.poll(rows=len(stub.OPEN_ROWS))
    page.get_by_test_id("tab-history").click()
    n.click_notification(0)
    expect(page.get_by_test_id("tab-queue")).to_have_attribute("data-state", "active")
    assert not n.traffic.console_errors, n.traffic.console_errors


def _n_real_api(n: NotifyPage) -> None:
    assert n.bell.get_attribute("data-state") == "on", n.bell.get_attribute("data-state")
    granted = n.page.evaluate("() => navigator.permissions.query({name: 'notifications'}).then((s) => s.state)")
    assert granted == "granted", f"the context's own grant did not take: {granted}"
    n.broker.file_request("real.example.com", container="mid", port=8443, request_id="real1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.made() == [_filed("mid", "real.example.com", 8443, "real1")], n.made()
    assert n.permission_requests() == 0
    tag = n.page.evaluate("() => window.__notifySpy.made[0].instance.tag")
    assert tag == "real1", f"the real Notification's own tag: {tag!r}"


def _n_unsupported(n: NotifyPage) -> None:
    from playwright.sync_api import expect
    assert_bell(n.bell, "unsupported")
    # Not disabled: a touch screen shows no tooltip, so a click has to say why.
    expect(n.bell).to_be_enabled()
    n.bell.focus()
    n.bell.click()
    explained = n.page.get_by_test_id("notify-unsupported")
    expect(explained).to_be_visible()
    assert BROWSER_NEXT_STEP in squash(explained.inner_text()), explained.inner_text()
    n.page.keyboard.press("Escape")
    n.broker.file_request("still.example.com", request_id="u1")
    n.page.clock.run_for(NotifyPage.POLL_MS)
    expect(n.page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS) + 1)
    assert not n.traffic.console_errors, n.traffic.console_errors


def _n_constructor_throws(n: NotifyPage) -> None:
    """Chrome for Android: the API, a permission, and a constructor that throws `Illegal constructor`."""
    from playwright.sync_api import expect
    warned: list[str] = []
    n.page.on("console", lambda m: warned.append(m.text) if "stage=fire failed" in m.text else None)
    n.bell.click()
    n.page.wait_for_selector("[data-testid=notify-bell][data-state=on]")
    assert_bell(n.bell, "on")
    n.poll()
    n.broker.file_request("droid.example.com", container="mid", request_id="t1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.page.evaluate("() => window.__notifySpy.attempts") == 1
    # Nothing was shown, so the bell must not claim it will be: it says why instead.
    n.page.wait_for_selector("[data-testid=notify-bell][data-state=unsupported]")
    assert_bell(n.bell, "unsupported")
    expect(n.bell).to_be_enabled()
    n.bell.click()
    assert BROWSER_NEXT_STEP in squash(n.page.get_by_test_id("notify-unsupported").inner_text())
    n.page.keyboard.press("Escape")
    # Never "on" again this session, and the constructor is not tried again: two more requests, one attempt.
    n.broker.file_request("droid2.example.com", request_id="t2")
    n.broker.file_request("droid3.example.com", request_id="t3")
    n.poll(rows=len(stub.OPEN_ROWS) + 3)
    assert n.page.evaluate("() => window.__notifySpy.attempts") == 1, "the constructor was tried again after it threw"
    assert n.made() == [], n.made()
    assert n.bell.get_attribute("data-state") == "unsupported"
    assert len(warned) == 1, f"the failure was logged {len(warned)} times: {warned}"
    assert not n.traffic.console_errors, n.traffic.console_errors


def _n_permission_reread(n: NotifyPage) -> None:
    """The permission changes under the open page (browser site settings): the bell follows on focus."""
    from playwright.sync_api import expect
    n.poll()
    assert_bell(n.bell, "on")
    n.page.evaluate("() => { window.__notifySpy.permission = 'denied'; window.dispatchEvent(new Event('focus')); }")
    n.page.wait_for_selector("[data-testid=notify-bell][data-state=denied]")
    assert_bell(n.bell, "denied")
    n.broker.file_request("revoked.example.com", request_id="p1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert n.made() == [], f"a tab whose permission was revoked notified: {n.made()}"
    # And back: granted again (the tab was hidden while the setting changed), on the visibility change.
    n.page.evaluate("() => { window.__notifySpy.permission = 'granted'; document.dispatchEvent(new Event('visibilitychange')); }")
    n.page.wait_for_selector("[data-testid=notify-bell][data-state=on]")
    assert_bell(n.bell, "on")
    n.poll()
    assert n.made() == [], f"regranting replayed what arrived while blocked: {n.made()}"
    n.broker.file_request("granted.example.com", request_id="p2")
    n.poll(rows=len(stub.OPEN_ROWS) + 2)
    assert [m["tag"] for m in n.made()] == ["p2"], n.made()
    expect(n.bell).to_have_attribute("data-state", "on")


def _n_arrives_by_stream(n: NotifyPage) -> None:
    """The real path: the tab is Live, so a request filed at the broker arrives as a stream frame, not a poll."""
    from playwright.sync_api import expect
    expect(link_state(n.page)).to_have_attribute("data-state", "live")
    assert_bell(n.bell, "on")
    assert n.made() == [], f"the initial snapshot's rows notified: {n.made()}"
    started = time.monotonic()
    n.broker.file_request("stream.example.com", container="mid", port=8443, request_id="s1")
    n.page.wait_for_function("() => window.__notifySpy.made.length > 0", timeout=LIVE_WITHIN_MS)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert elapsed_ms < LIVE_WITHIN_MS, f"the notification took {elapsed_ms} ms"
    log(f"check 143 filed-to-notified_ms={elapsed_ms}")
    assert n.made() == [_filed("mid", "stream.example.com", 8443, "s1")], n.made()
    expect(n.page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS) + 1)
    # Held past one 5 s poll interval: a tab that polled beside its stream would show, and a second frame of
    # the same rows must not notify again.
    n.page.wait_for_timeout(5500)
    assert not n.queue_reads_seen, f"the tab read /api/egress/queue {len(n.queue_reads_seen)} time(s) with its stream open"
    assert n.traffic.queue_reads == 0, f"{n.traffic.queue_reads} queue responses seen with the stream open"
    assert n.made() == [_filed("mid", "stream.example.com", 8443, "s1")], f"a later frame notified again: {n.made()}"
    wait_link(n.page, "live", 500)
    assert not n.traffic.console_errors, n.traffic.console_errors


FILTERS_CLEARED = "Filters cleared to show focus.example.com"


def _cleared_note_raised(n: NotifyPage):
    """Files a request a filter hides, clicks its notification, and leaves the queue showing the note.

    Returns (search box, polite status, visible pill, the filters bar)."""
    from playwright.sync_api import expect
    page = n.page
    n.poll()
    n.broker.file_request("focus.example.com", container="zeta", request_id="f1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert [m["tag"] for m in n.made()] == ["f1"], n.made()
    search = page.get_by_test_id("queue-filters").get_by_role("textbox")
    search.fill("check18")   # hides f1; the click below has to give way
    page.get_by_test_id("tab-history").click()
    page.wait_for_selector("[data-testid=request]", state="hidden")
    n.click_notification(0)
    status = page.get_by_test_id("filters-cleared-status")
    pill = page.locator(".pill", has_text="Filters cleared")
    expect(pill).to_have_text(FILTERS_CLEARED)
    assert (status.text_content() or "").strip() == FILTERS_CLEARED, status.text_content()
    assert search.input_value() == "", "the filter that hid the row was left on"
    return search, status, pill


def _n_note_goes_on_filter_change(n: NotifyPage) -> None:
    """The note is about the reset it announces: a filter changed afterwards ends it, in the pill and the live region."""
    from playwright.sync_api import expect
    search, status, pill = _cleared_note_raised(n)
    search.fill("zz")
    expect(pill).to_have_count(0)
    assert (status.text_content() or "").strip() == "", f"the live region kept {status.text_content()!r} after a filter changed"
    # The other controls end it the same way, not only the search box: "zz" hides f1 again, so the click resets.
    n.click_notification(0)
    expect(pill).to_have_text(FILTERS_CLEARED)
    n.page.get_by_test_id("queue-state").get_by_text("Failed apply").click()
    expect(pill).to_have_count(0)
    assert (status.text_content() or "").strip() == "", status.text_content()
    assert not n.traffic.console_errors, n.traffic.console_errors


def _n_note_goes_when_decided(n: NotifyPage) -> None:
    """The note names a request: once that request is decided (or gone from the queue) it names nothing."""
    from playwright.sync_api import expect
    _search, status, pill = _cleared_note_raised(n)
    n.page.get_by_role("button", name="Allow focus.example.com:443 in zeta", exact=True).click()
    expect(n.page.locator("[data-testid=request]")).to_have_count(len(stub.OPEN_ROWS))
    expect(pill).to_have_count(0)
    assert (status.text_content() or "").strip() == "", f"the live region kept {status.text_content()!r} after its request was decided"
    # Left the queue without a click of ours (another admin decided it, or the broker swept it).
    n.broker.file_request("focus.example.com", container="zeta", request_id="f2")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    n.page.get_by_test_id("queue-filters").get_by_role("textbox").fill("check18")
    n.page.get_by_test_id("tab-history").click()
    assert [m["tag"] for m in n.made()] == ["f1", "f2"], n.made()
    n.click_notification(1)
    expect(pill).to_have_text(FILTERS_CLEARED)
    n.broker.withdraw_request("f2")
    n.poll(rows=len(stub.OPEN_ROWS))
    expect(pill).to_have_count(0)
    assert (status.text_content() or "").strip() == "", status.text_content()
    assert not n.traffic.console_errors, n.traffic.console_errors


BELL_GLYPHS = {"default": "bell", "on": "bell-ring", "muted": "bell-off", "denied": "shield-ban", "unsupported": "bell-minus"}


def _n_glyph_per_state(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Every state has a glyph of its own, so muted and denied differ by shape and not by colour alone."""
    seen: dict[str, str] = {}
    for state in BELL_STATES:
        n = open_bell_state(browser, served, broker, viewport, "light", state)
        try:
            icons = n.page.evaluate("""() => [...document.querySelectorAll('[data-testid=notify-bell] svg')].map(
              (svg) => ({icon: svg.getAttribute('data-icon'), lucide: [...svg.classList].filter((c) => c.startsWith('lucide-') && c !== 'lucide')}))""")
            assert len(icons) == 1, f"{state}: {len(icons)} glyphs in the bell {icons}"
            seen[state] = icons[0]["icon"]
            # The attribute is the app's word for it; the class is what lucide drew.
            assert f"lucide-{seen[state]}" in icons[0]["lucide"], f"{state}: data-icon {seen[state]!r} but drawn as {icons[0]['lucide']}"
        finally:
            n.close()
    assert seen == BELL_GLYPHS, f"glyphs per state {seen} != {BELL_GLYPHS}"
    assert len(set(seen.values())) == len(seen), f"two states share a glyph: {seen}"


def _n_popover_copy(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The popovers give a reason and a next step in their body, never the heading again, and the blocked one
    does not ask for a reload (the page re-reads the permission when the tab is focused)."""
    want = {
        "denied": ("notify-blocked", "Notifications are blocked",
                   "Your browser is blocking notifications for this site. Allow them in the browser's site settings, then come back to this page."),
        "unsupported": ("notify-unsupported", "Notifications are not available",
                        "This browser or device cannot show them. Open the admin in a desktop browser such as Chrome or Firefox."),
    }
    for state, insecure in (("denied", False), ("unsupported", False), ("unsupported", True)):
        how = dict(BELL_PAGE[state], **({"insecure": True} if insecure else {}))
        n = NotifyPage(browser, served, broker, viewport, "light", **how)
        try:
            n.page.wait_for_selector(f"[data-testid=notify-bell][data-state={state}]")
            n.bell.click()
            testid, heading, body = want[state]
            if insecure:
                body = "Desktop notifications need a secure connection (https or localhost). Open the admin that way."
            popover = n.page.get_by_test_id(testid)
            popover.wait_for()
            paragraphs = [squash(t) for t in popover.locator("p").all_inner_texts()]
            assert paragraphs == [heading, body], f"{state}{' (insecure)' if insecure else ''}: {paragraphs}"
        finally:
            n.close()

def _n_insecure_wording(n: NotifyPage) -> None:
    assert_bell(n.bell, "unsupported", label=INSECURE_LABEL)
    n.bell.click()
    text = squash(n.page.get_by_test_id("notify-unsupported").inner_text())
    assert INSECURE_LABEL in text and "not available on this device" not in text, text


def _n_leaves_and_returns(n: NotifyPage) -> None:
    n.poll()
    n.broker.file_request("again.example.com", request_id="r1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert [m["tag"] for m in n.made()] == ["r1"], n.made()
    n.broker.withdraw_request("r1")
    n.poll(rows=len(stub.OPEN_ROWS))
    n.broker.file_request("again.example.com", request_id="r1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert [m["tag"] for m in n.made()] == ["r1"], f"a request that left and came back notified again: {n.made()}"
    # One that was already open when the tab loaded, and went and came back.
    n.broker.withdraw_request("a1")
    n.poll(rows=len(stub.OPEN_ROWS))
    n.broker.file_request("a1.example.com", request_id="a1")
    n.poll(rows=len(stub.OPEN_ROWS) + 1)
    assert [m["tag"] for m in n.made()] == ["r1"], f"an initial row that left and came back notified: {n.made()}"


BELL_PAGE = {   # how each bell state is reached, as NotifyPage arguments (a click follows for `muted`)
    "default": {},
    "on": {"permission": "granted"},
    "muted": {"permission": "granted"},
    "denied": {"permission": "denied"},
    "unsupported": {"remove": True},
}


def open_bell_state(browser, served: Served, broker: stub.StubBroker, viewport: str, theme: str, state: str) -> NotifyPage:
    n = NotifyPage(browser, served, broker, viewport, theme, **BELL_PAGE[state])
    try:
        if state == "muted":
            n.bell.click()
        n.page.wait_for_selector(f"[data-testid=notify-bell][data-state={state}]")
    except Exception:
        n.close()
        raise
    return n


LINK_LABELS = ("Connecting", "Live", "Reconnecting", "Polling", "Paused")   # every state of the top-bar indicator

BAR_BOXES_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom}; };
  const bell = document.querySelector('[data-testid=notify-bell]');
  const link = document.querySelector('[data-testid=link-state]');
  const bar = document.querySelector('header');
  const theme = [...bar.querySelectorAll('button')].find((b) => b !== bell);
  return {bell: box(bell), link: box(link), title: box(bar.querySelector('h1')), bar: box(bar), theme: box(theme),
          width: innerWidth, scroll: document.documentElement.scrollWidth, icon: !!bell.querySelector('svg'),
          linkName: link.getAttribute('aria-label')};
}"""


def _apart(a: dict, b: dict) -> bool:
    return a["r"] <= b["l"] or b["r"] <= a["l"]


def _inside(inner: dict, outer: dict) -> bool:
    return outer["l"] <= inner["l"] and inner["r"] <= outer["r"] and outer["t"] <= inner["t"] and inner["b"] <= outer["b"]


def _n_bell_layout(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The bell and the link indicator share the top bar with the title and the theme button: all inside the bar
    and the viewport, none over another, no horizontal overflow, whatever the bell's state and the indicator's word."""
    for state in BELL_STATES:
        n = open_bell_state(browser, served, broker, viewport, "light", state)
        try:
            found = n.page.evaluate(BAR_BOXES_JS)
            b, link, bar, theme, title = found["bell"], found["link"], found["bar"], found["theme"], found["title"]
            assert found["icon"], f"{state}: the bell has no icon"
            assert _inside(b, bar), f"{state}: bell outside the top bar {found}"
            assert b["r"] <= found["width"] and b["l"] >= 0, f"{state}: bell outside the viewport {found}"
            assert _apart(b, theme), f"{state}: bell overlaps the theme button {found}"
            assert b["r"] - b["l"] >= 24 and b["b"] - b["t"] >= 24, f"{state}: bell smaller than a 24 px target {found}"
            assert found["scroll"] <= found["width"], f"{state}: horizontal overflow {found}"
            assert _inside(link, bar) and link["r"] <= found["width"] and link["l"] >= 0, f"{state}: indicator outside the top bar or viewport {found}"
            assert _apart(link, b) and _apart(link, theme) and _apart(link, title), f"{state}: indicator overlaps its neighbours {found}"
            assert found["linkName"] == "Queue updates: Polling", f"{state}: indicator name {found['linkName']!r}"
            assert_bell(n.bell, state)
            assert not n.traffic.console_errors, n.traffic.console_errors
        finally:
            n.close()
    # The indicator's widest word is not the one this tab happens to show (Polling): measure the bar with each
    # of the five, set on the element itself (the text node only, its accessible name and icon stay the app's).
    n = open_bell_state(browser, served, broker, viewport, "light", "on")
    try:
        for label in LINK_LABELS:
            found = n.page.evaluate(f"""() => {{
              const link = document.querySelector('[data-testid=link-state]');
              const text = [...link.childNodes].reverse().find((c) => c.nodeType === Node.TEXT_NODE);
              text.textContent = ' {label} ';
              return ({BAR_BOXES_JS})();
            }}""")
            b, link, bar, theme, title = found["bell"], found["link"], found["bar"], found["theme"], found["title"]
            assert _inside(link, bar) and link["r"] <= found["width"], f"indicator '{label}' outside the top bar {found}"
            assert _apart(link, b) and _apart(link, theme) and _apart(link, title), f"indicator '{label}' overlaps a neighbour {found}"
            assert _inside(b, bar) and _apart(b, theme), f"with '{label}' the bell left its place {found}"
            assert found["scroll"] <= found["width"], f"indicator '{label}': horizontal overflow {found}"
    finally:
        n.close()


NOTIFY_CHECKS = [
    # (number, name, how the page is opened, assertion)
    ("130", "The page never asks for notification permission on load: with rows open and two polls, `requestPermission` is called zero times and the bell reads `Enable desktop notifications`",
     {}, _n_defaults_ask_nothing),
    ("131", "Clicking the bell in the default state calls `requestPermission` exactly once and, granted, the bell reads on; the rows open at that moment raise no notification, then or on the next snapshot",
     {}, _n_click_asks_once),
    ("132", "A request filed after load raises exactly one notification, with the literal title `New egress request from <bottle>`, body `<host>:<port>` and tag = request id; the initial snapshot's rows raise none, and later snapshots of the same rows raise no second",
     {"permission": "granted"}, _n_one_per_new_request),
    ("133", "Muted (the bell's second click, kept in localStorage across a reload) raises zero notifications, and unmuting does not replay what arrived while muted",
     {"permission": "granted"}, _n_mute),
    ("134", "With permission denied the bell explains the browser blocks notifications (no prompt) and a new request raises zero notifications",
     {"permission": "denied"}, _n_denied),
    ("135", "Calling the notification's `onclick` focuses that request's row, in view: from History, from another route and with a filter hiding it; for a request decided since it still lands on the queue without an error",
     {"permission": "granted"}, _n_click_focuses_row),
    ("136", "With the real Notification API underneath (Playwright grant on the context) a filed request constructs one real notification whose own `tag` is the request id",
     {"permission": "granted", "wrap": True, "grant": True}, _n_real_api),
    ("137", "Without the Notification API the bell is a focusable, enabled button that says notifications are not available on this device or browser and explains it when clicked, and the queue still works with no console error",
     {"remove": True}, _n_unsupported),
    ("138", "A request that leaves and comes back with the same id raises no second notification, whether it was filed after load or open when the tab loaded",
     {"permission": "granted"}, _n_leaves_and_returns),
    ("140", "When the notification constructor throws (`Illegal constructor`, as on Chrome for Android) though permission was granted, a filed request moves the bell from on to unsupported with a label that says notifications are not available on this device or browser, it never reads on again that session, the constructor is tried once, and the failure is logged once",
     {"answer": "granted", "throws": True}, _n_constructor_throws),
    ("141", "The bell follows a permission changed under the open page: granted to denied on `focus` (a request filed then raises none), and back to granted on `visibilitychange` (what arrived while blocked is not replayed)",
     {"permission": "granted"}, _n_permission_reread),
    ("142", "On a page that is not a secure context the unsupported bell says it needs a secure connection (https or localhost), not that the browser lacks the API",
     {"remove": True, "insecure": True}, _n_insecure_wording),
    ("143", "With the stream open (no polling, the top bar says Live) a request filed at the broker arrives by the stream and raises exactly one notification, with the literal title `New egress request from <bottle>`, body `<host>:<port>` and tag = request id, within 3 s, and the tab makes no /api/egress/queue read after the first frame",
     {"permission": "granted", "live": True}, _n_arrives_by_stream),
]
BELL_DETAIL_CHECKS = [   # 160..164: the round-2 polish; those that open their own pages take (browser, served, broker, viewport)
    ("160", "In spa mode the `Filters cleared to show <host>` note, in the pill and in the polite live region, is gone as soon as a filter changes (the search box, the state toggle)",
     {"permission": "granted"}, _n_note_goes_on_filter_change),
    ("161", "The `Filters cleared to show <host>` note is gone, pill and live region, once that request is decided from its row, or leaves the queue without our click",
     {"permission": "granted"}, _n_note_goes_when_decided),
]
BELL_OWN_PAGE_CHECKS = [
    ("162", "Each bell state has its own glyph: default `bell`, on `bell-ring`, muted `bell-off`, denied `shield-ban`, unsupported `bell-minus`, and no two share one; the drawn lucide class matches",
     _n_glyph_per_state),
    ("163", "The blocked popover body reads `Your browser is blocking notifications for this site. Allow them in the browser's site settings, then come back to this page.`; the unsupported popover's body gives the reason and next step (`Open the admin in a desktop browser such as Chrome or Firefox.`, or the secure-connection wording) and does not repeat its heading",
     _n_popover_copy),
]
BELL_LAYOUT_CHECK = ("139", "The bell in every state (default, on, muted, denied, unsupported) sits inside the top bar and the viewport, clear of the theme button and of the link indicator (whichever of its five words it shows), at least 24 px, with its state's accessible name and no horizontal overflow",
                     _n_bell_layout)


def run_notifications(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    """Notification bell checks (130..143); the SPA only."""
    suite = Suite(ui, viewport)
    if ui == "legacy":
        for number, name, _how, _fn in NOTIFY_CHECKS:
            suite.check(number, name, lambda: None, spa_only=True)
        suite.check(BELL_LAYOUT_CHECK[0], BELL_LAYOUT_CHECK[1], lambda: None, spa_only=True)
        for number, name, *_rest in BELL_DETAIL_CHECKS + BELL_OWN_PAGE_CHECKS:
            suite.check(number, name, lambda: None, spa_only=True)
        return suite.results
    for number, name, how, fn in NOTIFY_CHECKS:
        def run(how=how, fn=fn):
            n = NotifyPage(browser, served, broker, viewport, "light", **how)
            try:
                fn(n)
            finally:
                n.close()
        suite.check(number, name, run)
    number, name, fn = BELL_LAYOUT_CHECK
    suite.check(number, name, lambda: fn(browser, served, broker, viewport))
    for number, name, how, fn in BELL_DETAIL_CHECKS:
        def run(how=how, fn=fn):
            n = NotifyPage(browser, served, broker, viewport, "light", **how)
            try:
                fn(n)
            finally:
                n.close()
        suite.check(number, name, run)
    for number, name, fn in BELL_OWN_PAGE_CHECKS:
        suite.check(number, name, lambda fn=fn: fn(browser, served, broker, viewport))
    broker.reset()
    return suite.results


def capture_bell_states(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """The bell in each state at rest (pointer away, focus blurred), and the explanations open."""
    for state in BELL_STATES:
        n = open_bell_state(browser, served, broker, viewport, theme, state)
        try:
            # A click leaves the bell hovered and focused; a state is captured as it rests.
            n.page.mouse.move(2, viewport_height(viewport) - 2)
            n.page.evaluate("() => document.activeElement && document.activeElement.blur()")
            n.page.wait_for_timeout(300)   # past the hover transition
            capture(n.page, out, "spa", viewport, theme, f"bell-{state}")
            n.page.locator("header").screenshot(path=str(out / f"spa-{viewport}-{theme}-bell-{state}-bar.png"))
            if state in ("denied", "unsupported"):
                n.bell.click()
                n.page.get_by_test_id("notify-blocked" if state == "denied" else "notify-unsupported").wait_for()
                capture(n.page, out, "spa", viewport, theme, f"bell-{state}-open")
        finally:
            n.close()
    broker.reset()


def capture_bell_live(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """The bell on, beside the Live indicator: the stream path, no route aborted."""
    n = NotifyPage(browser, served, broker, viewport, theme, permission="granted", live=True)
    try:
        n.page.mouse.move(2, viewport_height(viewport) - 2)
        n.page.evaluate("() => document.activeElement && document.activeElement.blur()")
        n.page.wait_for_timeout(300)
        assert_bell(n.bell, "on")
        capture(n.page, out, "spa", viewport, theme, "bell-on-live")
        n.page.locator("header").screenshot(path=str(out / f"spa-{viewport}-{theme}-bell-on-live-bar.png"))
    finally:
        n.close()
    broker.reset()


def capture_filters_cleared(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """The `Filters cleared to show <host>` pill, as a notification click leaves it."""
    n = NotifyPage(browser, served, broker, viewport, theme, permission="granted")
    try:
        _cleared_note_raised(n)
        n.page.wait_for_timeout(300)
        capture(n.page, out, "spa", viewport, theme, "filters-cleared-note")
    finally:
        n.close()
    broker.reset()


def viewport_height(viewport: str) -> int:
    return VIEWPORTS[viewport]["height"]


# ---- legacy → behaviour mapping ------------------------------------------------------

# ---- Live stream: GET /api/egress/stream in the tab (100..104) ----------------------------

STREAM_ROUTE = "**/api/egress/stream"
QUEUE_ROUTE_PATH = "/api/egress/queue"
LIVE_WITHIN_MS = 3000          # the Claim: a filed request shows in an open tab within 3 s
POLL_GAP_S = (4.0, 6.5)        # the fallback polls every 5 s
STREAM_FULL = {"error": "too many live streams"}
# A browser without the Web Locks API (an insecure origin has none) or without BroadcastChannel: every tab
# keeps a stream of its own, closed while it is hidden. Injected before the app runs.
NO_LOCKS_JS = "Object.defineProperty(Navigator.prototype, 'locks', {configurable: true, get: () => undefined});"
NO_CHANNEL_JS = "delete window.BroadcastChannel;"


def queue_requests(page) -> list[float]:
    """The time (monotonic seconds) of every GET /api/egress/queue the page sends, by its request event."""
    seen: list[float] = []
    page.on("request", lambda request: seen.append(time.monotonic())
            if request.method == "GET" and urlsplit(request.url).path == QUEUE_ROUTE_PATH else None)
    return seen


def link_state(page):
    return page.locator("[data-testid=link-state]")


def wait_link(page, state: str, timeout_ms: int) -> None:
    from playwright.sync_api import expect

    expect(link_state(page)).to_have_attribute("data-state", state, timeout=timeout_ms)


def settle_hub(served: Served) -> None:
    """Let the stream hub empty and its cached snapshot age out, so a tab opened next starts from the queue as it is now.

    A stream a moment ago (an earlier check's) leaves the hub a snapshot under one poll interval old, which a
    new stream is handed as it is; that snapshot is of the queue before a reset and would arrive as a change.
    """
    hub = served.server.stream_hub
    deadline = time.monotonic() + 5
    while hub.stream_count() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert hub.stream_count() == 0, f"{hub.stream_count()} streams left open by an earlier check"
    time.sleep(hub.poll_seconds + 0.1)


def open_live_page(browser, served: Served, broker: stub.StubBroker, viewport: str, theme: str = "light",
                   init_script: str | None = None):
    """A fresh tab with its stream open: (context, page, traffic, driver, queue request times from the first frame on).

    `init_script` runs in every page of its context before the app does (NO_LOCKS_JS: the fallback path).
    """
    from playwright.sync_api import expect

    broker.reset()
    hub = served.server.stream_hub
    settle_hub(served)
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        if init_script:
            context.add_init_script(script=init_script)
        seen = queue_requests(page)
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))
        wait_link(page, "live", TIMEOUT_MS)
        assert hub.stream_count() == 1, f"{hub.stream_count()} streams open for one tab"
        # The window of the Claim's "no /api/egress/queue traffic once the stream is open" starts here, at the
        # first frame. Before it, a tab that is still connecting reads the queue once so its list is never
        # empty; that one read is asserted and set aside, so `seen` and `traffic.queue_reads` count only reads
        # made with the stream open.
        assert len(seen) == 1, f"{len(seen)} queue reads while the stream was opening, expected the one"
        drv._wait_until(lambda: traffic.queue_reads == 1, 5)
        assert traffic.queue_reads == 1, f"{traffic.queue_reads} queue responses while the stream was opening"
        seen.clear()
        traffic.queue_reads = 0
    except BaseException:
        context.close()   # a tab left open would keep its stream and fail every later check
        raise
    return context, page, traffic, drv, seen


def _live_request_arrives(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The Claim. Its window opens at the stream's first frame (open_live_page), so the single read a tab
    makes while connecting is not in it: from there on the tab makes no /api/egress/queue read at all."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    live_at = time.monotonic()
    try:
        indicator = link_state(page)
        expect(indicator).to_have_text("Live")
        assert indicator.get_attribute("aria-label") == "Queue updates: Live", indicator.get_attribute("aria-label")
        host = "live-arrival.example.com"
        started = time.monotonic()
        broker.file_request(host, container="alpha")
        drv.row(host).wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        assert elapsed_ms < LIVE_WITHIN_MS, f"the request took {elapsed_ms} ms to appear"
        log(f"check 100 viewport={viewport} filed-to-visible_ms={elapsed_ms}")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) + 1)
        assert drv.title_count() == len(stub.OPEN_ROWS) + 1, f"tab title {page.title()!r}"
        # hold the window open past one 5 s poll interval, so a tab that polled beside its stream would show
        page.wait_for_timeout(max(0, int((live_at + 5.5 - time.monotonic()) * 1000)))
        assert not seen, f"the tab read /api/egress/queue {len(seen)} time(s) while its stream was open"
        assert traffic.queue_reads == 0, f"{traffic.queue_reads} queue responses seen"
        wait_link(page, "live", 500)
    finally:
        context.close()


def _stream_lost_then_back(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Stream killed and its reopen refused: Reconnecting, then Polling with a 5 s poll; stream back: Live, no polling."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    try:
        assert not seen
        page.route(STREAM_ROUTE, lambda route: route.abort())
        served.server.stream_hub.end_streams("check 101")
        wait_link(page, "reconnecting", 2500)
        expect(link_state(page)).to_have_text("Reconnecting")
        # the drop starts polling at once: one read, made while the indicator still says Reconnecting
        # (the reopen is 3 s away), not only once it has turned to Polling
        drv._wait_until(lambda: len(seen) == 1, 2)
        assert len(seen) == 1, f"{len(seen)} queue reads within 2 s of the drop"
        assert link_state(page).get_attribute("data-state") == "reconnecting", "the read came after Reconnecting ended"
        wait_link(page, "polling", 8000)
        expect(link_state(page)).to_have_text("Polling")
        assert link_state(page).get_attribute("aria-label") == "Queue updates: Polling"
        drv._wait_until(lambda: len(seen) >= 3, 14)
        assert len(seen) >= 3, f"only {len(seen)} queue reads while polling"
        gaps = [round(b - a, 2) for a, b in zip(seen, seen[1:])]
        assert all(POLL_GAP_S[0] <= gap <= POLL_GAP_S[1] for gap in gaps[-2:]), f"poll gaps {gaps}, expected about 5 s"
        host = "polled-in.example.com"
        broker.file_request(host, container="mid")
        drv.row(host).wait_for(state="visible", timeout=7000)   # by the next poll
        page.unroute(STREAM_ROUTE)
        wait_link(page, "live", 13000)
        expect(link_state(page)).to_have_text("Live")
        settled = len(seen)
        page.wait_for_timeout(6000)   # longer than a poll interval
        assert len(seen) == settled, f"{len(seen) - settled} queue read(s) after the stream came back"
        host = "streamed-again.example.com"
        started = time.monotonic()
        broker.file_request(host, container="zeta")
        drv.row(host).wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        assert len(seen) == settled, "a request arrived by polling, not by the stream"
        log(f"check 101 viewport={viewport} back-on-stream filed-to-visible_ms={int((time.monotonic() - started) * 1000)}")
    finally:
        context.close()


class RawStreams:
    """Extra streams held open by raw sockets carrying the session cookie.

    Not by the page: Chromium allows six connections per origin, so a page cannot hold eight streams of
    its own (the seventh would wait for a free connection and never reach the daemon).
    """

    def __init__(self, served: Served, count: int):
        self.socks = []
        for _ in range(count):
            sock = socket.create_connection((served.host, served.port), timeout=5)
            sock.sendall((f"GET /api/egress/stream HTTP/1.1\r\nHost: {served.host}:{served.port}\r\n"
                          f"Cookie: {admin.SESSION_COOKIE_NAME}={served.cookie}\r\n\r\n").encode("ascii"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                assert chunk, "the daemon closed a stream while it was being opened"
                head += chunk
            assert head.startswith(b"HTTP/1.0 200"), head[:60]
            self.socks.append(sock)

    def close(self) -> None:
        for sock in self.socks:
            sock.close()
        self.socks = []


FETCH_STREAM_JS = """async () => {
  const controller = new AbortController();
  const response = await fetch('/api/egress/stream', { signal: controller.signal });
  const out = { status: response.status, type: response.headers.get('content-type') };
  if (response.status !== 200) out.body = await response.json();
  controller.abort();
  return out;
}"""


def _ninth_stream_refused(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    extra = None
    try:
        # the tab holds one stream; seven more make eight, and the ninth is refused
        extra = RawStreams(served, 7)
        assert served.server.stream_hub.stream_count() == 8
        refused = page.evaluate(FETCH_STREAM_JS)
        assert refused == {"status": 503, "type": "application/json", "body": STREAM_FULL}, refused
        # without the session cookie the stream is refused like the other API routes
        anonymous = context.browser.new_context()
        try:
            reply = anonymous.request.get(served.base + "/api/egress/stream")
            assert (reply.status, reply.json()) == (403, {"error": "forbidden"}), (reply.status, reply.text())
        finally:
            anonymous.close()
        wait_link(page, "live", 500)   # the tab's own stream is untouched
        extra.close()
        drv._wait_until(lambda: served.server.stream_hub.stream_count() == 1, 5)
        assert served.server.stream_hub.stream_count() == 1
        again = page.evaluate(FETCH_STREAM_JS)
        assert again["status"] == 200 and again["type"].startswith("text/event-stream"), again
        assert not seen
    finally:
        if extra is not None:
            extra.close()
        context.close()


def _ninth_tab_polls_then_goes_live(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A tab that gets no stream slot degrades to polling, and takes a stream when one frees."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    other = extra = other_context = None
    try:
        extra = RawStreams(served, 7)
        # Its own browser context, so its own Web Locks: a second tab of the same context would follow the
        # first one's stream (checks 150..) rather than ask the daemon for one and be refused.
        other_context, other, other_traffic = new_page(browser, served, viewport, "light")
        other_seen = queue_requests(other)
        other.goto(served.base + "/")
        other_drv = SpaDriver(other, other_traffic)
        expect(other_drv.requests()).to_have_count(len(stub.OPEN_ROWS))   # the rows came from polling
        wait_link(other, "polling", TIMEOUT_MS)
        assert other_seen, "the refused tab never polled"
        assert not seen, "the tab that holds a stream polled"
        extra.close()
        wait_link(other, "live", 13000)
        settled = len(other_seen)
        other.wait_for_timeout(6000)
        assert len(other_seen) == settled, "the tab kept polling after its stream opened"
        assert not other_traffic.console_errors, other_traffic.console_errors
    finally:
        if extra is not None:
            extra.close()
        if other_context is not None:
            other_context.close()
        context.close()


def _outage_ends_streams_and_the_banner_holds(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A lasting broker outage ends the streams; the tab polls, its banner says the data is old, and it recovers on its own."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    try:
        banner = drv.stale_banner()
        expect(banner).to_be_hidden()
        with broker.lock:
            broker.outage = True
        expect(banner).to_be_visible(timeout=10_000)
        text = banner.inner_text().strip()
        assert re.fullmatch(r"Showing data from \d{1,2}:\d{2}:\d{2}\s[AP]M: .+", text), f"banner {text!r}"
        assert link_state(page).get_attribute("data-state") in ("reconnecting", "polling")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))   # the last good list stays on screen
        with broker.lock:
            broker.outage = False
        expect(banner).to_be_hidden(timeout=BANNER_CLEAR_MS)
        wait_link(page, "live", 13000)
    finally:
        with broker.lock:
            broker.outage = False
        context.close()


def _decide_failure_banner_while_live(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """With the stream open nothing polls, so a decide banner clears by one read a poll interval after the failure."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    try:
        banner = drv.stale_banner()
        with broker.lock:
            broker.decide_outage = 500
        drv.button("Deny", "check18.example.com").click()
        expect(banner).to_have_text("Decision not sent: decide failed on the daemon")
        failed_at = time.monotonic()
        assert not seen, "the failed decide re-read the queue at once"
        with broker.lock:
            broker.decide_outage = None
        expect(banner).to_be_hidden(timeout=8000)
        waited = time.monotonic() - failed_at
        assert 4.0 <= waited <= 7.5, f"the banner cleared after {waited:.1f} s, expected about 5 s"
        assert len(seen) == 1, f"{len(seen)} queue reads to clear it, expected 1"
        wait_link(page, "live", 500)
    finally:
        with broker.lock:
            broker.decide_outage = None
        context.close()


# The connection pool: Chromium lets a page hold about six HTTP/1.1 connections to one origin, across every
# tab of the browser. A tab that keeps its stream open takes one for as long as it lives, so six admin tabs
# left every other request (a decide, a queue read, a page load) waiting for a connection that never frees.
POOL_TABS = 6
HIDDEN_EXTRA_TABS = 3
DECIDE_WITHIN_MS = 3000

# Headless Chromium reports every page visible whatever is in front (checked: bringing another page to the
# front, minimizing the window, and turning focus emulation off all leave `document.visibilityState` at
# "visible", and so does a headed Chromium under Playwright). So a hidden tab is made by overriding what the
# app reads, `document.visibilityState`, and dispatching the `visibilitychange` event the browser would.
SET_TAB_HIDDEN_JS = """(hidden) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event('visibilitychange'));
  return [document.visibilityState, document.hidden];
}"""


def set_tab_hidden(page, hidden: bool) -> None:
    state = page.evaluate(SET_TAB_HIDDEN_JS, hidden)
    assert state == ["hidden" if hidden else "visible", hidden], f"the page reads visibility {state}"


def open_background_tab(context, served: Served, rows: int = len(stub.OPEN_ROWS)):
    """Another tab of the same browser context with its stream open and `rows` open requests: (page, queue request times)."""
    from playwright.sync_api import expect

    tab = context.new_page()
    tab.set_default_timeout(TIMEOUT_MS)
    traffic = Traffic()
    traffic.attach(tab)
    tab_seen = queue_requests(tab)
    tab.goto(served.base + "/")
    expect(SpaDriver(tab, traffic).requests()).to_have_count(rows)
    wait_link(tab, "live", TIMEOUT_MS)
    return tab, tab_seen


def _hidden_tabs_free_the_connection_pool(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The fallback (no Web Locks): six real tabs, five hidden, the visible one decides at once. Hidden tabs hold no
    stream and read slowly. With Web Locks a browser holds one stream in all (checks 150..), so this is what a
    browser without them, or a page on an insecure origin, does; and, with no BroadcastChannel either, two tabs
    still hold a stream each."""
    from playwright.sync_api import expect

    hub = served.server.stream_hub
    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport, init_script=NO_LOCKS_JS)
    background: list[tuple] = []
    try:
        for _ in range(POOL_TABS - 1):
            background.append(open_background_tab(context, served))
        assert hub.stream_count() == POOL_TABS, f"{hub.stream_count()} streams for {POOL_TABS} tabs"
        reads_before = [len(tab_seen) for _tab, tab_seen in background]
        for tab, _tab_seen in background:
            set_tab_hidden(tab, True)
        # the symptom: with every connection held by a stream, the visible tab's decide waits for one
        started = time.monotonic()
        drv.button("Deny", "check18.example.com").click()
        try:
            expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS) - 1, timeout=DECIDE_WITHIN_MS)
        except AssertionError:
            raise AssertionError(
                f"the decide had not completed after {DECIDE_WITHIN_MS} ms with {POOL_TABS} tabs open and 5 hidden "
                f"({hub.stream_count()} streams held, {len(traffic.decides)} decide sent, statuses {traffic.statuses})") from None
        decide_ms = int((time.monotonic() - started) * 1000)
        log(f"check 106 viewport={viewport} tabs={POOL_TABS} decide_ms={decide_ms}")
        assert traffic.statuses == [200], f"decide statuses {traffic.statuses}"
        # the mechanism: hidden tabs closed their streams, read the queue once at once (a slow poll of 30 s
        # follows) and say Paused; the visible tab still holds its own stream and is Live
        for tab, _tab_seen in background:
            wait_link(tab, "paused", 2500)
            assert link_state(tab).get_attribute("aria-label") == "Queue updates: Paused"
        drv._wait_until(lambda: hub.stream_count() == 1, 5)
        assert hub.stream_count() == 1, f"{hub.stream_count()} streams held by {POOL_TABS - 1} hidden tabs and one visible"
        wait_link(page, "live", 500)
        drv._wait_until(lambda: all(len(t_seen) == before + 1 for (_t, t_seen), before in zip(background, reads_before)), 3)
        page.wait_for_timeout(1500)
        reads = [len(t_seen) - before for (_t, t_seen), before in zip(background, reads_before)]
        assert reads == [1] * (POOL_TABS - 1), f"queue reads by the hidden tabs since they were hidden: {reads}, expected one each"
        assert len(seen) <= 1, f"the visible tab read the queue {len(seen)} times: its decide's read is the one"
        # hidden tabs are not limited by the pool: more open, load and hide, and the count of streams stays at one
        for _ in range(HIDDEN_EXTRA_TABS):
            tab, tab_seen = open_background_tab(context, served, len(stub.OPEN_ROWS) - 1)   # one was decided
            background.append((tab, tab_seen))
            set_tab_hidden(tab, True)
            wait_link(tab, "paused", 2500)
        assert len(context.pages) == POOL_TABS + HIDDEN_EXTRA_TABS, len(context.pages)
        drv._wait_until(lambda: hub.stream_count() == 1, 5)
        assert hub.stream_count() == 1, f"{hub.stream_count()} streams with {len(context.pages)} tabs open"
        # showing a tab again reopens its stream, reading the queue once at once
        back, back_seen = background[0]
        reads = len(back_seen)
        set_tab_hidden(back, False)
        wait_link(back, "live", 5000)
        assert len(back_seen) == reads + 1, f"{len(back_seen) - reads} queue reads on return, expected one"
        assert hub.stream_count() == 2, f"{hub.stream_count()} streams with two visible tabs"
        assert not traffic.console_errors, traffic.console_errors
    finally:
        context.close()
    # Locks but no BroadcastChannel: no way to hear a leader, so every tab is its own (a stream each)
    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport, init_script=NO_CHANNEL_JS)
    try:
        assert page.evaluate("() => typeof BroadcastChannel") == "undefined"
        open_background_tab(context, served)
        assert hub.stream_count() == 2, f"{hub.stream_count()} streams for two tabs without BroadcastChannel"
    finally:
        context.close()


def _connecting_reads_once_and_keeps_reading_until_the_first_frame(browser, served: Served, broker: stub.StubBroker,
                                                                    viewport: str) -> None:
    """A stream slow to open: the tab reads Connecting, shows the queue from one read at once, and polls
    until the first frame; then Live, polling stopped."""
    from playwright.sync_api import expect

    broker.reset()
    hub = served.server.stream_hub
    deadline = time.monotonic() + 5
    while hub.stream_count() and time.monotonic() < deadline:
        time.sleep(0.05)
    context, page, traffic = new_page(browser, served, viewport, "light")
    held: list = []
    try:
        page.route(STREAM_ROUTE, lambda route: held.append(route))   # the stream request waits until released
        seen = queue_requests(page)
        drv = SpaDriver(page, traffic)
        page.goto(served.base + "/")
        expect(drv.requests()).to_have_count(len(stub.OPEN_ROWS))   # the list is the read's: no stream yet
        wait_link(page, "connecting", 500)
        expect(link_state(page)).to_have_text("Connecting")
        assert "Opening the live connection" in link_state(page).get_attribute("title")
        assert len(seen) == 1, f"{len(seen)} queue reads on first load, expected one"
        assert len(held) == 1, f"{len(held)} stream requests"
        host = "connecting-poll.example.com"
        broker.file_request(host, container="mid")
        drv.row(host).wait_for(state="visible", timeout=7000)   # the next poll, 5 s after the first read
        assert len(seen) == 2, f"{len(seen)} queue reads after 5 s of connecting, expected the second"
        wait_link(page, "connecting", 500)
        held[0].continue_()
        wait_link(page, "live", 5000)
        settled = len(seen)
        page.wait_for_timeout(5500)   # longer than a poll interval
        assert len(seen) == settled, f"{len(seen) - settled} queue read(s) after the stream was open"
        assert not traffic.console_errors, traffic.console_errors
    finally:
        context.close()


def _failed_read_while_live_polls_until_one_succeeds(browser, served: Served, broker: stub.StubBroker,
                                                      viewport: str) -> None:
    """The stream up, one queue read fails (the read after a decide): the banner says the list is old, the tab
    polls every 5 s until a read succeeds, then stops; the stream stays Live throughout."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    failing = {"on": True}
    try:
        def queue_route(route):
            if failing["on"]:
                route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "queue read failed"}))
            else:
                route.continue_()
        page.route("**" + QUEUE_ROUTE_PATH, queue_route)
        banner = drv.stale_banner()
        # A decide is followed by a read of the queue. This one (an allow whose rule install fails: the request
        # stays queued) changes nothing at the broker, so no stream frame follows to end the recovery polling
        # this check is about; check 158 is the frame that does.
        drv.button("Retry allow", stub.APPLY_FAILED_HOST).click()
        expect(banner).to_be_visible(timeout=5000)
        assert re.match(r"Showing data from ", banner.inner_text().strip()), banner.inner_text()
        drv._wait_until(lambda: len(seen) >= 3, 13)   # the read after the decide, then a poll every 5 s
        assert len(seen) >= 3, f"{len(seen)} queue reads while the banner was up, expected the decide's and two polls"
        assert served.server.stream_hub.stream_count() == 1
        wait_link(page, "live", 500)
        failing["on"] = False
        expect(banner).to_be_hidden(timeout=7000)   # the next poll succeeds
        wait_link(page, "live", 500)
        settled = len(seen)
        page.wait_for_timeout(6000)   # longer than a poll interval
        assert len(seen) == settled, f"{len(seen) - settled} queue read(s) after one succeeded: the polling did not stop"
        wait_link(page, "live", 500)
    finally:
        context.close()


def _heartbeat_reaches_page_script(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The heartbeat is an `hb` event a real EventSource dispatches (a `: hb` comment would not be), and an
    app tab that is receiving them stays Live and reads nothing."""
    server = served.server
    kept = server.stream_heartbeat_seconds
    server.stream_heartbeat_seconds = 0.3
    context = None
    try:
        context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
        result = page.evaluate("""() => new Promise((resolve) => {
          const source = new EventSource('/api/egress/stream');
          const beats = [];
          source.addEventListener('hb', (event) => {
            beats.push(event.data);
            if (beats.length === 3) { source.close(); resolve(beats); }
          });
          setTimeout(() => { source.close(); resolve(beats); }, 4000);
        })""")
        assert result == ["{}", "{}", "{}"], f"heartbeat events seen by the page: {result}"
        page.wait_for_timeout(1500)   # the app's own stream has had a dozen more
        wait_link(page, "live", 500)
        assert not seen, "the tab read the queue while its stream was up"
        assert not traffic.console_errors, traffic.console_errors
    finally:
        server.stream_heartbeat_seconds = kept
        if context is not None:
            context.close()


def run_live_stream(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    suite = Suite(ui, viewport)
    suite.check("100", "A request filed at the broker appears in an open tab within 3 s with no /api/egress/queue "
                       "request from the tab once its stream is open (from the first frame); the top bar says Live",
                lambda: _live_request_arrives(browser, served, broker, viewport), spa_only=True)
    suite.check("101", "A lost stream reads Reconnecting and reads the queue at once, then Polling with a 5 s poll of "
                       "/api/egress/queue; when the stream returns it reads Live and the polling stops",
                lambda: _stream_lost_then_back(browser, served, broker, viewport), spa_only=True)
    suite.check("102", "A ninth concurrent stream gets 503 with the error body, a request without the session is "
                       "refused 403, and the open streams are untouched",
                lambda: _ninth_stream_refused(browser, served, broker, viewport), spa_only=True)
    suite.check("103", "A tab that gets no stream slot polls (indicator Polling) and goes Live, polling stopped, "
                       "when a slot frees",
                lambda: _ninth_tab_polls_then_goes_live(browser, served, broker, viewport), spa_only=True)
    suite.check("104", "A lasting broker outage ends the streams: the tab polls, the banner says `Showing data from "
                       "<time>`, and both clear when the broker returns",
                lambda: _outage_ends_streams_and_the_banner_holds(browser, served, broker, viewport), spa_only=True)
    suite.check("105", "A decide that fails while the stream is open raises `Decision not sent: <error>` and it clears "
                       "by one queue read about 5 s later, the stream still open",
                lambda: _decide_failure_banner_while_live(browser, served, broker, viewport), spa_only=True)
    suite.check("106", "Fallback without Web Locks: with six tabs of the app open and five of them hidden, the visible tab's "
                       "decide completes within 3 s; hidden tabs hold no stream (Paused) and read the queue once, a slow "
                       "poll after; a tab shown again reopens its stream; without BroadcastChannel two tabs hold two streams",
                lambda: _hidden_tabs_free_the_connection_pool(browser, served, broker, viewport), spa_only=True)
    suite.check("107", "A tab whose stream is slow to open reads Connecting, shows the queue from one read at once and "
                       "polls until the first frame; then Live, polling stopped",
                lambda: _connecting_reads_once_and_keeps_reading_until_the_first_frame(browser, served, broker, viewport),
                spa_only=True)
    suite.check("108", "A queue read that fails while the stream is Live raises the banner and polls every 5 s until a "
                       "read succeeds, then stops; the stream stays Live",
                lambda: _failed_read_while_live_polls_until_one_succeeds(browser, served, broker, viewport), spa_only=True)
    suite.check("109", "The stream's heartbeat is an `hb` event with `{}` as its data that a real EventSource dispatches "
                       "to page script; the app's tab receiving them stays Live",
                lambda: _heartbeat_reaches_page_script(browser, served, broker, viewport), spa_only=True)
    return suite.results


def capture_stream_states(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """The top-bar indicator in Connecting, Live, Paused, Reconnecting and Polling, and a request arriving in an open tab."""
    from playwright.sync_api import expect

    broker.reset()
    slow_context, slow_page, slow_traffic = new_page(browser, served, viewport, theme)
    held: list = []
    try:
        slow_page.route(STREAM_ROUTE, lambda route: held.append(route))   # a stream slow to open
        slow_page.goto(served.base + "/")
        expect(SpaDriver(slow_page, slow_traffic).requests()).to_have_count(len(stub.OPEN_ROWS))
        wait_link(slow_page, "connecting", 1000)
        capture(slow_page, out, "spa", viewport, theme, "stream-connecting")
    finally:
        for route in held:   # a stream request still held is a task pending when the context closes (check 164)
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass
        close_page_context(slow_context, slow_page)
    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport, theme)
    try:
        capture(page, out, "spa", viewport, theme, "stream-live")
        # Paused is the fallback's state: a tab that leads keeps its stream when hidden, so this one is a
        # tab of a browser without Web Locks (its own context, as its own browser).
        solo_context, solo_page, solo_traffic = new_page(browser, served, viewport, theme)
        try:
            solo_context.add_init_script(script=NO_LOCKS_JS)
            solo_page.goto(served.base + "/")
            expect(SpaDriver(solo_page, solo_traffic).requests()).to_have_count(len(stub.OPEN_ROWS))
            wait_link(solo_page, "live", TIMEOUT_MS)
            set_tab_hidden(solo_page, True)
            wait_link(solo_page, "paused", 2500)
            capture(solo_page, out, "spa", viewport, theme, "stream-paused")
        finally:
            solo_context.close()
        capture(page, out, "spa", viewport, theme, "stream-arrival-before")
        host = "live-arrival.example.com"
        broker.file_request(host, container="alpha")
        drv.row(host).wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        capture(page, out, "spa", viewport, theme, "stream-arrival-after")
        capture(page, out, "spa", viewport, theme, "stream-arrival-row", scroll_to=drv.row(host))
        page.route(STREAM_ROUTE, lambda route: route.abort())
        served.server.stream_hub.end_streams("capture")
        wait_link(page, "reconnecting", 2500)
        capture(page, out, "spa", viewport, theme, "stream-reconnecting")
        wait_link(page, "polling", 8000)
        capture(page, out, "spa", viewport, theme, "stream-polling")
    finally:
        close_page_context(context, page)


# ---- One live stream per browser: leader election over Web Locks (150..159, 170..171) ----------------------
#
# Every check here uses REAL pages of ONE browser context (same origin, so the pages share Web Locks and
# BroadcastChannel as the tabs of one browser do). The first page to load holds the lock and the daemon's one
# stream; the daemon's own stream count (`stream_hub.stream_count()`) is what says how many a browser holds.

# The app's tab title of a request arriving is not enough to tell WHO delivered it, so a page records every
# change of its link indicator (Live -> Connecting -> Live is how a takeover reads).
RECORD_LINK_STATES_JS = """(() => {
  const seen = window.__linkStates = [];
  const note = () => {
    const el = document.querySelector('[data-testid=link-state]');
    const state = el && el.getAttribute('data-state');
    if (state && seen[seen.length - 1] !== state) seen.push(state);
  };
  new MutationObserver(note).observe(document, {subtree: true, childList: true, attributes: true, attributeFilter: ['data-state']});
})();"""
FOLLOWER_SILENCE_S = 40        # shared-link.ts: the stream's 35 s silence limit and a 5 s margin
TAKEOVER_WITHIN_S = 5          # a closed leader's lock passes and the new leader's stream is open within this
ONE_STREAM_TABS = 6            # visible tabs of the Claim's "6 visible + 3 more"
DECIDE_QUICK_MS = 1000


@dataclass
class Tab:
    """One page of the context: its traffic, the time of every queue read it makes, its driver."""

    page: object
    traffic: Traffic
    seen: list
    drv: SpaDriver

    def state(self) -> str | None:
        return link_state(self.page).get_attribute("data-state")

    def notified(self) -> list[dict]:
        return self.page.evaluate(
            "() => window.__notifySpy.made.map(n => ({title: n.title, body: n.options.body, tag: n.options.tag}))")


def open_one_stream_context(browser, served: Served, broker: stub.StubBroker, viewport: str, *scripts: str,
                            theme: str = "light"):
    """A fresh context with `scripts` injected into every page: (context, first page, its traffic)."""
    broker.reset()
    settle_hub(served)
    context, page, traffic = new_page(browser, served, viewport, theme)
    try:
        for script in scripts:
            context.add_init_script(script=script)
    except BaseException:
        context.close()
        raise
    return context, page, traffic


def load_tab(page, traffic: Traffic, served: Served, rows: int = len(stub.OPEN_ROWS)) -> Tab:
    """Load the queue in `page` and wait for its indicator to say Live; its reads are counted from the load."""
    from playwright.sync_api import expect

    seen = queue_requests(page)
    drv = SpaDriver(page, traffic)
    page.goto(served.base + "/")
    expect(drv.requests()).to_have_count(rows)
    wait_link(page, "live", TIMEOUT_MS)
    return Tab(page, traffic, seen, drv)


def next_tab(context, served: Served, rows: int = len(stub.OPEN_ROWS)) -> Tab:
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    traffic = Traffic()
    traffic.attach(page)
    return load_tab(page, traffic, served, rows)


def open_tabs(browser, served: Served, broker: stub.StubBroker, viewport: str, count: int, *scripts: str,
              theme: str = "light"):
    """`count` tabs opened one after another (the first one leads): (context, tabs)."""
    context, page, traffic = open_one_stream_context(browser, served, broker, viewport, *scripts, theme=theme)
    tabs: list[Tab] = []
    try:
        tabs.append(load_tab(page, traffic, served))
        while len(tabs) < count:
            tabs.append(next_tab(context, served))
    except BaseException:
        context.close()   # a page left open would keep the lock and fail every later check
        raise
    return context, tabs


def assert_one_stream(served: Served, tabs: list[Tab], what: str = "") -> None:
    hub = served.server.stream_hub
    tabs[0].drv._wait_until(lambda: hub.stream_count() == 1, 3)
    assert hub.stream_count() == 1, f"{hub.stream_count()} streams held by {len(tabs)} tabs{what}"


def arrives_in_every_tab(tabs: list[Tab], broker: stub.StubBroker, host: str, label: str, **filed) -> int:
    """File a request at the broker; every tab shows it within 3 s. Returns the slowest tab's milliseconds."""
    started = time.monotonic()
    broker.file_request(host, **filed)
    slowest = 0
    for index, tab in enumerate(tabs):
        left = max(1, LIVE_WITHIN_MS - int((time.monotonic() - started) * 1000))
        try:
            tab.drv.row(host).wait_for(state="visible", timeout=left)
        except Exception:
            raise AssertionError(f"{label}: tab {index} (link {tab.state()}) had not shown {host} "
                                 f"{int((time.monotonic() - started) * 1000)} ms after it was filed") from None
        slowest = max(slowest, int((time.monotonic() - started) * 1000))
    return slowest


def no_reads(tabs: list[Tab], label: str) -> None:
    reads = [len(tab.seen) for tab in tabs]
    assert reads == [0] * len(tabs), f"{label}: /api/egress/queue reads per tab {reads}, expected none"


def _nine_tabs_hold_one_stream(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """6 visible tabs and 3 more (hidden): the daemon has one stream; a decide from a follower and from the leader
    completes in under a second; the other tabs follow it."""
    from playwright.sync_api import expect

    context, tabs = open_tabs(browser, served, broker, viewport, ONE_STREAM_TABS)
    try:
        assert_one_stream(served, tabs, " (all visible)")
        for _ in range(HIDDEN_EXTRA_TABS):
            tab = next_tab(context, served)
            set_tab_hidden(tab.page, True)
            tabs.append(tab)
        assert len(context.pages) == ONE_STREAM_TABS + HIDDEN_EXTRA_TABS, len(context.pages)
        assert_one_stream(served, tabs, f" ({ONE_STREAM_TABS} visible, {HIDDEN_EXTRA_TABS} hidden)")
        states = [tab.state() for tab in tabs]
        assert states == ["live"] * len(tabs), f"link states {states}: a follower shows the leader's, hidden or not"
        # the symptom of a stream per tab: every connection of the pool held, so a decide waits for one
        for who, tab, host, rows in (("follower", tabs[ONE_STREAM_TABS - 1], "check18.example.com", len(stub.OPEN_ROWS) - 1),
                                     ("leader", tabs[0], "a1.example.com", len(stub.OPEN_ROWS) - 2)):
            started = time.monotonic()
            tab.drv.button("Deny", host).click()
            try:
                expect(tab.drv.requests()).to_have_count(rows, timeout=DECIDE_QUICK_MS)
            except AssertionError:
                raise AssertionError(f"the {who}'s decide had not completed after {DECIDE_QUICK_MS} ms with "
                                     f"{len(tabs)} tabs open ({served.server.stream_hub.stream_count()} streams held, "
                                     f"{len(tab.traffic.decides)} decide sent, statuses {tab.traffic.statuses})") from None
            log(f"check 150 viewport={viewport} tabs={len(tabs)} {who}-decide_ms={int((time.monotonic() - started) * 1000)}")
            assert tab.traffic.statuses == [200], f"{who} decide statuses {tab.traffic.statuses}"
        # the leader's next frame reaches everyone, the acting tabs included
        for index, tab in enumerate(tabs):
            expect(tab.drv.requests()).to_have_count(len(stub.OPEN_ROWS) - 2, timeout=LIVE_WITHIN_MS)
        assert_one_stream(served, tabs, " after the decides")
        assert all(not tab.traffic.console_errors for tab in tabs), [tab.traffic.console_errors for tab in tabs]
    finally:
        context.close()


def _request_reaches_every_tab(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Four visible tabs and two hidden: a request filed at the broker shows in every one within 3 s and no tab
    reads /api/egress/queue: the followers never did, the leader only while it was connecting."""
    context, tabs = open_tabs(browser, served, broker, viewport, 4)
    try:
        for _ in range(2):
            tab = next_tab(context, served)
            set_tab_hidden(tab.page, True)
            tabs.append(tab)
        assert_one_stream(served, tabs)
        no_reads(tabs[1:], "followers, from their load")
        assert len(tabs[0].seen) == 1, f"the leader read the queue {len(tabs[0].seen)} times while it connected, expected the one"
        for tab in tabs:
            tab.seen.clear()
            tab.traffic.queue_reads = 0
        host = "one-stream-arrival.example.com"
        started = time.monotonic()
        slowest = arrives_in_every_tab(tabs, broker, host, "check 151", container="alpha")
        log(f"check 151 viewport={viewport} tabs={len(tabs)} filed-to-every-tab_ms={slowest}")
        assert slowest < LIVE_WITHIN_MS
        for index, tab in enumerate(tabs):
            assert tab.drv.title_count() == len(stub.OPEN_ROWS) + 1, f"tab {index} title {tab.page.title()!r}"
        # held past one 5 s poll interval, so a tab that polled beside the leader's stream would show
        tabs[0].page.wait_for_timeout(max(0, int((started + 5.5 - time.monotonic()) * 1000)))
        no_reads(tabs, "after the first frame")
        assert [tab.traffic.queue_reads for tab in tabs] == [0] * len(tabs)
        assert [tab.state() for tab in tabs] == ["live"] * len(tabs)
        assert_one_stream(served, tabs)
    finally:
        context.close()


def _one_notification_per_tab(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The bell on in two tabs, one of them hidden: a filed request raises exactly one notification in each, within
    3 s; the one stream is the leader's, the hidden tab hears it over the channel."""
    context, tabs = open_tabs(browser, served, broker, viewport, 2, notify_spy_script(permission="granted"))
    try:
        set_tab_hidden(tabs[1].page, True)
        assert_one_stream(served, tabs)
        for index, tab in enumerate(tabs):
            assert_bell(tab.page.get_by_test_id("notify-bell"), "on")
            assert tab.notified() == [], f"tab {index}: the initial snapshot's rows notified: {tab.notified()}"
        started = time.monotonic()
        broker.file_request("bell-two.example.com", container="mid", port=8443, request_id="os1")
        for index, tab in enumerate(tabs):
            left = max(1, LIVE_WITHIN_MS - int((time.monotonic() - started) * 1000))
            try:
                tab.page.wait_for_function("() => window.__notifySpy.made.length > 0", timeout=left)
            except Exception:
                raise AssertionError(f"tab {index} ({'hidden' if index else 'leader'}) raised no notification "
                                     f"{int((time.monotonic() - started) * 1000)} ms after the request was filed") from None
        log(f"check 152 viewport={viewport} filed-to-both-notified_ms={int((time.monotonic() - started) * 1000)}")
        tabs[0].page.wait_for_timeout(1500)   # a second one, from a second delivery, would show by now
        for index, tab in enumerate(tabs):
            assert tab.notified() == [_filed("mid", "bell-two.example.com", 8443, "os1")], \
                f"tab {index} raised {tab.notified()}"
        no_reads(tabs[1:], "the follower")
        assert_one_stream(served, tabs)
    finally:
        context.close()


def _leader_closes(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The leader's tab closes: the lock passes to another tab, which opens the stream (Connecting), the rest
    follow it (Connecting, then Live), the daemon again has one stream, and a new request reaches every tab."""
    context, tabs = open_tabs(browser, served, broker, viewport, 3, RECORD_LINK_STATES_JS)
    try:
        assert_one_stream(served, tabs)
        left = tabs[1:]
        for tab in left:
            tab.page.evaluate("() => { window.__linkStates.length = 0; window.__linkStates.push('live'); }")
            tab.seen.clear()
        closed_at = time.monotonic()
        tabs[0].page.close()
        hub = served.server.stream_hub

        def took_over() -> bool:
            return all(tab.page.evaluate("() => window.__linkStates").count("live") >= 2 for tab in left)

        try:
            left[0].drv._wait_until(took_over, TAKEOVER_WITHIN_S)
        finally:
            recorded = [tab.page.evaluate("() => window.__linkStates") for tab in left]
        took_ms = int((time.monotonic() - closed_at) * 1000)
        log(f"check 153 viewport={viewport} takeover_ms={took_ms} link states {recorded}")
        assert recorded == [["live", "connecting", "live"]] * 2, \
            f"link states of the tabs left after {took_ms} ms: {recorded}, expected Live, Connecting, Live in both: " \
            "the closing leader says Connecting on its way out, and the next one is Connecting until its first frame"
        assert took_ms < TAKEOVER_WITHIN_S * 1000
        assert hub.stream_count() == 1, f"{hub.stream_count()} streams after the leader closed"
        slowest = arrives_in_every_tab(left, broker, "after-takeover.example.com", "check 153", container="zeta")
        log(f"check 153 viewport={viewport} filed-to-both_ms={slowest}")
        assert len(left[1].seen) == 0, f"the follower read the queue {len(left[1].seen)} times"
        assert len(left[0].seen) <= 1, f"the new leader read the queue {len(left[0].seen)} times, expected at most the one"
        assert hub.stream_count() == 1
    finally:
        context.close()


def _hidden_leader_keeps_the_stream(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The leader's tab goes to the background (visibilityState hidden, as check 106 fakes it): it keeps the one
    stream, stays Live, and a filed request reaches it and the other tabs within 3 s, no tab reading the queue."""
    context, tabs = open_tabs(browser, served, broker, viewport, 3)
    try:
        assert_one_stream(served, tabs)
        set_tab_hidden(tabs[0].page, True)
        tabs[0].page.wait_for_timeout(1500)   # a tab that closed its stream on hiding has done it by now
        assert tabs[0].state() == "live", f"the hidden leader reads {tabs[0].state()}"
        assert [tab.state() for tab in tabs] == ["live"] * 3
        assert_one_stream(served, tabs, " with the leader hidden")
        for tab in tabs:
            tab.seen.clear()
        started = time.monotonic()
        slowest = arrives_in_every_tab(tabs, broker, "hidden-leader.example.com", "check 154", container="mid")
        log(f"check 154 viewport={viewport} filed-to-every-tab_ms={slowest}")
        tabs[0].page.wait_for_timeout(max(0, int((started + 5.5 - time.monotonic()) * 1000)))
        no_reads(tabs, "with the leader hidden")
        assert_one_stream(served, tabs)
    finally:
        context.close()


def _silent_leader_makes_a_follower_poll(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A follower that hears nothing from the leader for 40 s (the stream's 35 s silence limit and a margin) reads the
    queue itself and says Polling, rather than show old data as Live; the leader's next message ends that."""
    from playwright.sync_api import expect

    context, page, traffic = open_one_stream_context(browser, served, broker, viewport)
    try:
        leader = load_tab(page, traffic, served)
        follower_page = context.new_page()
        follower_page.set_default_timeout(TIMEOUT_MS)
        follower_traffic = Traffic()
        follower_traffic.attach(follower_page)
        # this tab's clock is ours from before its first script: 40 s pass in a call, the leader's do not
        follower_page.clock.install(time=datetime.now(timezone.utc))
        follower = load_tab(follower_page, follower_traffic, served)
        no_reads([follower], "a follower, from its load")
        follower_page.clock.fast_forward((FOLLOWER_SILENCE_S - 1) * 1000)
        follower_page.wait_for_timeout(300)
        assert follower.state() == "live" and not follower.seen, \
            f"after {FOLLOWER_SILENCE_S - 1} s of silence: {follower.state()}, {len(follower.seen)} reads"
        follower_page.clock.fast_forward(2000)
        wait_link(follower_page, "polling", 2000)
        expect(link_state(follower_page)).to_have_text("Polling")
        follower.drv._wait_until(lambda: len(follower.seen) == 1, 3)
        assert len(follower.seen) == 1, f"{len(follower.seen)} reads when the leader went silent, expected the one at once"
        # the leader speaks again (a request is filed: its frame is relayed): the follower shows it, is Live and stops
        started = time.monotonic()
        broker.file_request("silent-leader.example.com", container="alpha")
        follower.drv.row("silent-leader.example.com").wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        leader.drv.row("silent-leader.example.com").wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        wait_link(follower_page, "live", 1000)
        log(f"check 155 viewport={viewport} leader-back_ms={int((time.monotonic() - started) * 1000)}")
        settled = len(follower.seen)
        follower_page.clock.run_for(12_000)   # two polls' worth, if it still polled
        follower_page.wait_for_timeout(300)
        assert len(follower.seen) == settled, f"{len(follower.seen) - settled} read(s) after the leader was heard again"
        assert follower.state() == "live"
        assert not follower_traffic.console_errors, follower_traffic.console_errors
    finally:
        context.close()


def _decide_from_a_follower(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A decide is the acting tab's: one POST from it and none from the others; its own read after the decide works
    and the leader's next frame takes the row out of every tab; still one stream."""
    from playwright.sync_api import expect

    context, tabs = open_tabs(browser, served, broker, viewport, 3)
    try:
        assert_one_stream(served, tabs)
        for tab in tabs:
            tab.seen.clear()
        actor = tabs[2]
        actor.drv.button("Deny", "check18.example.com").click()
        expect(actor.drv.requests()).to_have_count(len(stub.OPEN_ROWS) - 1, timeout=DECIDE_QUICK_MS)
        for index, tab in enumerate(tabs):
            expect(tab.drv.requests()).to_have_count(len(stub.OPEN_ROWS) - 1, timeout=LIVE_WITHIN_MS)
            assert len(tab.traffic.decides) == (1 if tab is actor else 0), f"tab {index} sent {len(tab.traffic.decides)} decides"
        assert actor.traffic.decides[0]["body"]["host"] == "check18.example.com", actor.traffic.decides[0]["body"]
        assert actor.traffic.statuses == [200]
        actor.page.wait_for_timeout(1500)
        assert [len(tab.seen) for tab in tabs] == [0, 0, 1], \
            f"queue reads per tab {[len(tab.seen) for tab in tabs]}: the acting tab's one read after its decide, no other"
        assert_one_stream(served, tabs)
    finally:
        context.close()


def _leader_loss_is_shown_in_every_tab(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The leader's stream is lost (and its reopen refused): every tab reads Reconnecting, then Polling; only the leader
    reads the queue, and what it reads reaches the followers; when the stream returns every tab is Live again."""
    context, tabs = open_tabs(browser, served, broker, viewport, 3)
    try:
        assert_one_stream(served, tabs)
        for tab in tabs:
            tab.seen.clear()
        tabs[0].page.route(STREAM_ROUTE, lambda route: route.abort())
        served.server.stream_hub.end_streams("check 157")
        for tab in tabs:
            wait_link(tab.page, "reconnecting", 2500)
        tabs[0].drv._wait_until(lambda: len(tabs[0].seen) == 1, 2)
        assert len(tabs[0].seen) == 1, f"the leader made {len(tabs[0].seen)} reads within 2 s of the drop, expected the one at once"
        for tab in tabs:
            wait_link(tab.page, "polling", 8000)
            assert link_state(tab.page).inner_text().strip() == "Polling", link_state(tab.page).inner_text()
        host = "leader-polled.example.com"
        broker.file_request(host, container="mid")
        for index, tab in enumerate(tabs):
            tab.drv.row(host).wait_for(state="visible", timeout=7000)   # the leader's next poll, relayed
        no_reads(tabs[1:], "followers of a polling leader")
        tabs[0].page.unroute(STREAM_ROUTE)
        for tab in tabs:
            wait_link(tab.page, "live", 13000)
        no_reads(tabs[1:], "followers when the stream came back")
        assert_one_stream(served, tabs)
        slowest = arrives_in_every_tab(tabs, broker, "leader-live-again.example.com", "check 157", container="zeta")
        log(f"check 157 viewport={viewport} back-on-stream filed-to-every-tab_ms={slowest}")
    finally:
        context.close()


def _frame_ends_the_recovery_polling(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A read fails while Live and the tab polls until one succeeds (check 108); a stream frame is a good read, so
    it clears the banner and ends that polling although every read of the queue still fails."""
    from playwright.sync_api import expect

    context, page, traffic, drv, seen = open_live_page(browser, served, broker, viewport)
    try:
        page.route("**" + QUEUE_ROUTE_PATH, lambda route: route.fulfill(
            status=500, content_type="application/json", body=json.dumps({"error": "queue read failed"})))
        banner = drv.stale_banner()
        # the decide of check 108: it changes nothing at the broker, so the recovery polling it starts is not
        # ended by a frame before this check files the one that should
        drv.button("Retry allow", stub.APPLY_FAILED_HOST).click()
        expect(banner).to_be_visible(timeout=5000)
        drv._wait_until(lambda: len(seen) >= 2, 8)   # the read after the decide, then a poll of the recovery
        assert len(seen) >= 2, f"{len(seen)} reads while the banner was up"
        host = "frame-ends-recovery.example.com"
        broker.file_request(host, container="mid")
        drv.row(host).wait_for(state="visible", timeout=LIVE_WITHIN_MS)   # by the stream: every read fails
        expect(banner).to_be_hidden(timeout=1000)
        wait_link(page, "live", 500)
        page.wait_for_timeout(300)   # a read in flight when the frame landed has settled
        settled = len(seen)
        page.wait_for_timeout(6500)   # longer than a poll interval
        assert len(seen) == settled, f"{len(seen) - settled} read(s) after a frame arrived: the recovery polling went on"
        expect(banner).to_be_hidden()
    finally:
        context.close()


def _connecting_spinner_respects_reduced_motion(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """Connecting's spinner turns, and holds still where the viewer asks for reduced motion."""
    broker.reset()
    context, page, traffic = new_page(browser, served, viewport, "light")
    held: list = []
    try:
        page.route(STREAM_ROUTE, lambda route: held.append(route))   # a stream slow to open: the tab stays Connecting
        page.goto(served.base + "/")
        wait_link(page, "connecting", TIMEOUT_MS)
        icon = link_state(page).locator("svg")
        assert icon.count() == 1, icon.count()

        def animation() -> str:
            return icon.evaluate("(el) => getComputedStyle(el).animationName")

        assert animation() == "spin", f"the Connecting icon's animation is {animation()!r}"
        page.emulate_media(reduced_motion="reduce")
        assert animation() == "none", f"with reduced motion the Connecting icon's animation is {animation()!r}"
        page.emulate_media(reduced_motion="no-preference")
        assert animation() == "spin"
        assert not traffic.console_errors, traffic.console_errors
    finally:
        context.close()


# What every page of the context hears on the channel, beside the app: (kind, from, to) of each message.
BUS_SPY_JS = """(() => {
  const seen = window.__busSeen = [];
  const channel = new BroadcastChannel('djinn-admin-queue');
  channel.onmessage = (e) => seen.push({kind: e.data.kind, from: e.data.from, to: e.data.to ?? null});
})();"""


def _frozen_leader_hands_over(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """The leader's page gets Page Lifecycle `freeze` (as the browser's memory or energy saver sends it before it stops the
    page's script, the lock staying held unless the page lets go): another tab leads within 5 s, one stream, and a filed
    request reaches the tabs still running within 3 s; on `resume` the tab follows without a stream of its own.

    The event is dispatched by hand: Chromium's `Page.setWebLifecycleState` accepts `frozen` in headless and does
    nothing (no `freeze` event, the page's timers keep running), so the page is never really frozen here. This pins the
    app's reaction to the event, not the browser's freezing."""
    context, tabs = open_tabs(browser, served, broker, viewport, 3, RECORD_LINK_STATES_JS)
    try:
        assert_one_stream(served, tabs)
        frozen, rest = tabs[0], tabs[1:]
        for tab in rest:
            tab.page.evaluate("() => { window.__linkStates.length = 0; window.__linkStates.push('live'); }")
            tab.seen.clear()
        started = time.monotonic()
        frozen.page.evaluate("() => document.dispatchEvent(new Event('freeze'))")

        def took_over() -> bool:
            return all(tab.page.evaluate("() => window.__linkStates").count("live") >= 2 for tab in rest)

        try:
            rest[0].drv._wait_until(took_over, TAKEOVER_WITHIN_S)
        finally:
            recorded = [tab.page.evaluate("() => window.__linkStates") for tab in rest]
        took_ms = int((time.monotonic() - started) * 1000)
        log(f"check 170 viewport={viewport} freeze-to-takeover_ms={took_ms} link states {recorded}")
        assert recorded == [["live", "connecting", "live"]] * 2, \
            f"link states of the running tabs {took_ms} ms after the leader froze: {recorded}, expected Live, " \
            "Connecting, Live in both: the freezing tab says Connecting as it lets go, and the next leader is " \
            "Connecting until its first frame"
        assert took_ms < TAKEOVER_WITHIN_S * 1000, f"{took_ms} ms"
        assert_one_stream(served, rest, " after the leader froze")
        slowest = arrives_in_every_tab(rest, broker, "frozen-leader.example.com", "check 170", container="zeta")
        log(f"check 170 viewport={viewport} filed-to-running-tabs_ms={slowest}")
        assert slowest < LIVE_WITHIN_MS
        reads = [len(tab.seen) for tab in rest]
        assert sum(reads) <= 1, f"the tabs read the queue {reads} times: only the new leader's one while it connects"
        frozen.page.evaluate("() => document.dispatchEvent(new Event('resume'))")
        wait_link(frozen.page, "live", TIMEOUT_MS)
        frozen.drv.row("frozen-leader.example.com").wait_for(state="visible", timeout=LIVE_WITHIN_MS)
        assert_one_stream(served, tabs, " after the frozen tab thawed")
        slowest = arrives_in_every_tab(tabs, broker, "thawed.example.com", "check 170 after the thaw", container="zeta")
        log(f"check 170 viewport={viewport} filed-to-all-three_ms={slowest}")
        assert all(not tab.traffic.console_errors for tab in tabs), [tab.traffic.console_errors for tab in tabs]
    finally:
        context.close()


def _hello_is_answered_to_the_asking_tab(browser, served: Served, broker: stub.StubBroker, viewport: str) -> None:
    """A tab that opens asks the leader; the answer is addressed to it, and the tabs already open hear no answer meant
    for the newcomer (an older snapshot in it could replace a fresher read of their own)."""
    context, tabs = open_tabs(browser, served, broker, viewport, 2, BUS_SPY_JS)
    try:
        follower = tabs[1]
        follower.page.evaluate("() => { window.__busSeen.length = 0 }")
        tabs.append(next_tab(context, served))
        follower.page.wait_for_timeout(500)
        heard = follower.page.evaluate("() => window.__busSeen")
        hellos = [m for m in heard if m["kind"] == "hello"]
        answers = [m for m in heard if m["kind"] in ("link", "snapshot")]
        log(f"check 171 viewport={viewport} heard {[(m['kind'], m['to'] is not None) for m in heard]}")
        assert len(hellos) == 1, f"the follower heard {len(hellos)} hellos: {heard}"
        assert answers, f"the follower heard no answer to the newcomer's hello: {heard}"
        assert all(m["to"] == hellos[0]["from"] for m in answers), \
            f"an answer to the newcomer's hello was not addressed to it: {answers} (hello from {hellos[0]['from']})"
        assert tabs[2].state() == "live"
        assert_one_stream(served, tabs)
    finally:
        context.close()


ONE_STREAM_CHECKS = [
    ("150", "In one browser context, 6 visible tabs and 3 hidden ones hold one stream at the daemon (all Live, hidden "
            "included); a decide from a follower and one from the leader each complete in under 1 s, and the leader's "
            "next frame takes the rows out of every tab", _nine_tabs_hold_one_stream),
    ("151", "A request filed at the broker shows in each of 6 tabs (2 hidden) within 3 s, and no tab reads "
            "/api/egress/queue after the first frame (a follower never does)", _request_reaches_every_tab),
    ("152", "With the bell on in two tabs, one hidden, a filed request raises exactly one notification in each within "
            "3 s (the literal title, body and tag), over one stream", _one_notification_per_tab),
    ("153", "When the leader's tab closes another tab takes over within 5 s: the tabs left read Live, Connecting, Live, "
            "the daemon has one stream again, and a request filed then reaches both", _leader_closes),
    ("154", "A leader hidden keeps the one stream and stays Live; a filed request reaches it and the other tabs within "
            "3 s, and no tab reads the queue", _hidden_leader_keeps_the_stream),
    ("155", "A follower that hears nothing from the leader for 40 s reads the queue itself and says Polling; the leader's "
            "next message returns it to Live and stops the reads", _silent_leader_makes_a_follower_poll),
    ("156", "A decide is the acting tab's alone (one POST from it, none from the others; its own read after it works), "
            "and the row leaves every tab by the leader's next frame; still one stream", _decide_from_a_follower),
    ("157", "A stream lost by the leader reads Reconnecting then Polling in every tab, only the leader reads the "
            "queue (what it reads reaches the followers), and every tab is Live again when the stream returns",
     _leader_loss_is_shown_in_every_tab),
    ("158", "A stream frame ends the polling a failed read started while Live: the banner clears and no read follows "
            "though every read of the queue still fails", _frame_ends_the_recovery_polling),
    ("159", "Connecting's spinner turns (`animation-name: spin`) and holds still under `prefers-reduced-motion: reduce`",
     _connecting_spinner_respects_reduced_motion),
    ("170", "A leader that gets Page Lifecycle `freeze` (dispatched by hand: headless Chromium does not freeze) lets go of "
            "the lock: another tab leads within 5 s with one stream at the daemon, a filed request reaches the running "
            "tabs within 3 s, and on `resume` the tab follows without a stream of its own", _frozen_leader_hands_over),
    ("171", "A new tab's hello is answered to that tab alone: every link and snapshot message a tab already open hears "
            "meanwhile is addressed to the newcomer", _hello_is_answered_to_the_asking_tab),
]


def run_one_stream(ui: str, viewport: str, browser, served: Served, broker: stub.StubBroker) -> list[Result]:
    """One live stream per browser (150..159, 170..171); the SPA only."""
    suite = Suite(ui, viewport)
    for number, name, fn in ONE_STREAM_CHECKS:
        suite.check(number, name, lambda fn=fn: fn(browser, served, broker, viewport), spa_only=True)
    broker.reset()
    return suite.results


def capture_one_stream(browser, served, broker, out: Path, viewport: str, theme: str) -> None:
    """Leader and follower tabs of one browser, both Live; then the leader closes, the follower's indicator reads
    Connecting (the new leader's stream held back) and Live again."""
    held: list = []
    context, tabs = open_tabs(browser, served, broker, viewport, 3, theme=theme)
    try:
        leader, next_leader, follower = tabs
        capture(leader.page, out, "spa", viewport, theme, "one-stream-leader-live")
        capture(follower.page, out, "spa", viewport, theme, "one-stream-follower-live")
        next_leader.page.route(STREAM_ROUTE, lambda route: held.append(route))   # its stream is slow to open, once it leads
        leader.page.close()
        wait_link(follower.page, "connecting", 5000)
        capture(follower.page, out, "spa", viewport, theme, "one-stream-follower-connecting")
        for route in held:
            route.continue_()
        wait_link(follower.page, "live", 5000)
        capture(follower.page, out, "spa", viewport, theme, "one-stream-follower-live-again")
    finally:
        context.close()


LEGACY_MAP = [
    # (original check in the legacy suite, behaviour check(s), note)
    ("`{light,dark}/mobile: no horizontal overflow` (1 site, 2 lines)", "(20)", "now at desktop, tablet and phone, before and after decisions"),
    ("`tab title carries the count`", "(5) count; (5) format SKIP(design-change: Tab title carries the open count)", "the count assertion runs on both UIs; the format `(N) Egress · Djinn admin` is the PLN decision \"Tab title carries the open count\""),
    ("`group headers by bottle in first-seen order`", "(1) SKIP(design-change: Queue ordering and views)", "PLN decision \"Queue ordering and views\" (2026-09-23): bottles alphabetical, replacing legacy's first-seen order; (1) any-order grouping and counts still run on both UIs"),
    ("`bottle-a's two rows stay together ahead of bottle-b`", "(1) any-order grouping; (2) SKIP(design-change: Queue ordering and views)", "contiguity is asserted by the grouped host order; the order itself is the PLN decision \"Queue ordering and views\" (newest first within a bottle)"),
    ("`four columns per request row`", "retired: asserts legacy DOM", "the SPA row is two lines in three columns; the facts it carried are covered by (4)"),
    ("`destination cell: host:port and hits`", "(4)", ""),
    ("`reason cell`", "(4)", ""),
    ("`requested cell carries raw UTC in title`", "retired: asserts legacy DOM", "a tooltip attribute on a legacy cell; the SPA shows relative time and the clock time instead"),
    ("`missing reason renders an em-dash`", "(4) runs on both UIs", "each UI asserts its own no-reason text: legacy `—`, SPA `No reason given`; no PLN decision is involved"),
    ("`IP badge and apply-failed chip on the IP row`", "(4)", "text differs per UI (legacy `apply failed ×1: ip_requires_cidr`, SPA `Apply failed after 1 attempt: an IP address needs a CIDR in the manifest`); each UI asserts its own text from the `last_error` object, on both `ip_requires_cidr` and `apply_failed` rows"),
    ("`recent list renders decided time, bottle, destination, outcome, by`", "(18), (22)", "(18) asserts a decided row appears in recent on both UIs; (22) asserts on both UIs that each recent row shows its destination, bottle, outcome and deny reason (legacy `denied / global`, SPA `Denied permanently · global`), which legacy's original check asserted only for the host; the legacy decided-time and `by` cells are retired with the legacy table, and (22b) checks the SPA's labels (N/A(spa-only) on legacy)"),
    ("`deny global with a wrong typed host is refused inline`", "(12)", "legacy shows an inline chip; the SPA disables the dialog button and shows a mismatch message, and also refuses Enter and a forced click"),
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

# New behaviour checks with no legacy original: (number, name, decision or N/A).
NEW_CHECKS = [
    ("1", "By-bottle grouping as a full-order assertion (bottles alphabetical)", "SKIP(design-change: Queue ordering and views) on legacy"),
    ("3", "All view: newest first across bottles, bottle on line 2", "SKIP(design-change: Queue ordering and views) on legacy"),
    ("5b", "The Egress nav entry carries the count badge", "N/A(spa-only) on legacy: the badge is new SPA behaviour, no PLN decision"),
    ("11", "Plain Deny offers and sends no reason", "SKIP(design-change: Action labels map onto the existing decide API unchanged) on legacy"),
    ("15", "`apply_failed` outcome: the note is added by the decide and the row stays queued", "runs on both UIs"),
    ("16", "A 400 shows the server's error text on the row", "runs on both UIs"),
    ("17, 17b, 17c", "A decide fails alone (broker 500 → admin 502, broker 401 → admin 503, request aborted) while polls succeed: a banner from the decide, and on the SPA the row note `Not sent: …`", "runs on both UIs"),
    ("21", "No console errors", "runs on both UIs"),
    ("22b", "Recent rows carry the SPA's decision labels", "N/A(spa-only) on legacy: SPA labels, no PLN decision"),
    ("23", "Permanent-deny dialog caps the reason at 200 characters, with a counter and the design's label", "SKIP(design-change: Action labels map onto the existing decide API unchanged) on legacy"),
    ("24", "Every row button has its own accessible name", "N/A(spa-only) on legacy"),
    ("25", "Each row's status live region is rendered before any outcome", "N/A(spa-only) on legacy"),
    ("26", "Every row's action group sits inside its panel and no host breaks across lines", "N/A(spa-only) on legacy"),
    ("27", "A recent row that wraps leaves no dangling separator", "N/A(spa-only) on legacy"),
    ("28", "A decide in flight locks every request it acts on and no other", "N/A(spa-only) on legacy"),
    ("28b", "A row stays locked until every in-flight decide that covers it has finished", "N/A(spa-only) on legacy"),
    ("29", "A poll already in flight when a decide fails does not clear the banner", "N/A(spa-only) on legacy"),
    ("30", "History lists the whole store newest first, 50 to a page, in keyset order", "N/A(spa-only) on legacy: the History tab is new (PLN step 4)"),
    ("31", "Older then Newer walk every page with no row repeated (across a tie in decided_at) and back", "N/A(spa-only) on legacy"),
    ("32", "The row decided 30 days ago is reachable by paging", "N/A(spa-only) on legacy"),
    ("33", "Picking a day sends the local day as since and until; Clear drops both", "N/A(spa-only) on legacy"),
    ("34", "The bottle filter sends container, keeps it while paging and drops it for All bottles", "N/A(spa-only) on legacy"),
    ("35", "Search and the status toggle filter the loaded page without asking the broker", "N/A(spa-only) on legacy"),
    ("36", "A failed page keeps the list and the page number, shows a banner, and Retry recovers", "N/A(spa-only) on legacy"),
    ("37", "History has no horizontal overflow and no console errors", "N/A(spa-only) on legacy"),
    ("38", "A failed fetch for a new filter shows no rows, no Older cursor and Page 1, not the old filter's", "N/A(spa-only) on legacy"),
    ("39", "A slow reply for a superseded filter never replaces the current filter's rows", "N/A(spa-only) on legacy"),
    ("40", "Every row is two lines (host and date, pill and bottle) at every viewport, a 63+ character host ending in an ellipsis with the whole host in its title, with by and relative time from sm up", "N/A(spa-only) on legacy"),
    ("41", "Search, date and bottle share a row on tablet and desktop, the bottle label sits by its icon", "N/A(spa-only) on legacy"),
    ("42", "The date trigger is named \"Date range: <label>\"", "N/A(spa-only) on legacy"),
    ("50", "The stale banner says `Showing data from <time>` only after a failed poll; a decide failure reads `Decision not sent: <error>`", "N/A(spa-only) on legacy"),
    ("51", "The light theme paints no pure-red (#ff0000) pixel in any request row", "N/A(spa-only) on legacy"),
    ("52", "An unbreakable meta string wraps inside its row instead of being clipped", "N/A(spa-only) on legacy"),
    ("80", "The bottle multi-select narrows the queue to the chosen bottles; the count line reads `N of M open`", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("81", "The Failed apply state shows only requests with a `last_error`", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("82", "The age filter buckets requests: under 5 minutes, under 1 hour, older than 1 hour", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("83", "The destination search narrows by host:port, ignoring case", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("84", "Clear shows only while a filter is active and resets every filter; a filter matching nothing shows `Nothing matches these filters.`", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("85", "One bottle plus Failed apply shows exactly that bottle's failed requests with their `last_error` reason and attempt", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("86", "Deny all for one bottle sends exactly one `deny` decide per open request of that bottle (literal bodies) and none for another bottle; those rows leave", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("87", "Deny all decides every open request of the bottle even when filters hide some, and the dialog says so", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("88", "Allow all sends exactly the Allow body per open request of the bottle (literal bodies)", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("89", "Cancel in the bulk dialog sends nothing", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("90", "When the mock fails the second decide of a bulk run that request stays open and marked while the others leave; decides run one at a time; the run reports every request", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("91", "A request mid-decide keeps its bottle's bulk buttons off; a bulk run locks its requests and other bulk buttons; a poll does not bring a decided row back", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("92", "Denylist hits are one group, collapsed by default, headed by the summed hits, expanding to a row with `N×` each", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("93", "Filters, bulk buttons, menus and the bulk dialog stay inside the viewport at every viewport, with no console errors", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("94", "The collapsed denylist group is a card of its own with no row elements in it; the ordinary recent rows sit in a separate card clearly below", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("95", "Allow all on a bottle with an IP-literal request reports it apart (`Allowed 4 of 5 in alpha · 1 needs a CIDR in the manifest`), sends the literal Allow bodies and leaves that row open with its note; the state toggle is named `Request state`", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("96", "A request swept by an earlier decide of a bulk run is skipped, not sent, and the run still reports every request; the running decide's spinner sits on the pressed button", "N/A(spa-only) on legacy: queue features are new SPA behaviour (PLN step 5)"),
    ("60", "History's relative times update on an open page (a fake clock advanced 2 minutes changes every row still in seconds or minutes)", "N/A(spa-only) on legacy"),
    ("61", "A deny reason sits inline from sm up and is hidden below; a long bottle name ends in an ellipsis on a phone, and from sm up gives way without clipping by or the reason", "N/A(spa-only) on legacy"),
    ("62", "The selected day, a range's end and its start in the History date picker have at least 4.5:1 text contrast, hovered or not, focused or not, light and dark", "N/A(spa-only) on legacy"),
    ("100", "A request filed at the broker appears in an open tab within 3 s with no `/api/egress/queue` request from the tab once its stream is open (from the first frame); the top bar says Live", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("101", "A lost stream reads Reconnecting and reads the queue at once, then Polling with a 5 s poll of `/api/egress/queue`; when the stream returns it reads Live and the polling stops", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("102", "A ninth concurrent stream gets 503 with the error body, a request without the session is refused 403, and the open streams are untouched", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("103", "A tab that gets no stream slot polls (indicator Polling) and goes Live, polling stopped, when a slot frees", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("104", "A lasting broker outage ends the streams: the tab polls, the banner says `Showing data from <time>`, and both clear when the broker returns", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("105", "A decide that fails while the stream is open raises `Decision not sent: <error>` and it clears by one queue read about 5 s later, the stream still open", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("106", "Fallback without Web Locks: with six tabs of the app open and five of them hidden, the visible tab's decide completes within 3 s; hidden tabs hold no stream (Paused) and read the queue once, a slow poll after; a tab shown again reopens its stream; without BroadcastChannel two tabs hold two streams", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("107", "A tab whose stream is slow to open reads Connecting, shows the queue from one read at once and polls until the first frame; then Live, polling stopped", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("108", "A queue read that fails while the stream is Live raises the banner and polls every 5 s until a read succeeds, then stops; the stream stays Live", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("109", "The stream's heartbeat is an `hb` event with `{}` as its data that a real EventSource dispatches to page script; the app's tab receiving them stays Live", "N/A(spa-only) on legacy: the live stream is new SPA behaviour (PLN step 6)"),
    ("120", "The service worker registers from the app at scope `/`, activates and controls the page; /sw.js is the generated precache worker", "N/A(spa-only) on legacy: the legacy page keeps its inline worker (PLN step 6)"),
    ("121", "With the worker active, reloading the page and opening `/` afresh go to the network (the daemon sees each GET, cache-control no-store), not a copy planted in the worker's own cache", "N/A(spa-only) on legacy"),
    ("122", "With the worker active, every /api/egress/queue read from the page, the same URL again and again, reaches the daemon", "N/A(spa-only) on legacy"),
    ("123", "With the worker active and the session cookie cleared, `/` and an app route show the pointer page from the network, not the app", "N/A(spa-only) on legacy"),
    ("124", "Cache Storage holds only the worker's own precache, and only /assets/ urls in it (at the end, besides the pages the test planted itself); a pre-seeded `djinn-admin-shell-v3` and another foreign cache are deleted after activation", "N/A(spa-only) on legacy"),
    ("125", "The page links a web manifest: standalone, start `/`, the tokens' canvas colour, 192 and 512 png icons that load", "N/A(spa-only) on legacy"),
    ("126", "/sw.js is served as JavaScript with cache-control no-cache", "N/A(spa-only) on legacy"),
    ("127", "With the daemon unreachable (context offline) an /api/egress/queue fetch and a navigation to `/` fail with a network error instead of being answered from a cache", "N/A(spa-only) on legacy"),
    ("130", "The page never asks for notification permission on load; the bell reads `Enable desktop notifications`", "N/A(spa-only) on legacy: notifications are new SPA behaviour (PLN step 6)"),
    ("131", "Clicking the bell in the default state calls `requestPermission` exactly once and, granted, the bell reads on; rows open at that moment raise no notification", "N/A(spa-only) on legacy"),
    ("132", "A request filed after load raises exactly one notification with the literal title, body and tag; the initial snapshot's rows and repeat snapshots raise none", "N/A(spa-only) on legacy"),
    ("133", "Muted (kept in localStorage across a reload) raises zero notifications; unmuting does not replay what arrived while muted", "N/A(spa-only) on legacy"),
    ("134", "With permission denied the bell explains the browser blocks notifications, sends no prompt, and nothing is raised", "N/A(spa-only) on legacy"),
    ("135", "The notification's `onclick` focuses that request's row, in view, from History, from another route and past a hiding filter; a decided request still lands on the queue", "N/A(spa-only) on legacy"),
    ("136", "With the real Notification API underneath a filed request constructs one real notification whose own `tag` is the request id", "N/A(spa-only) on legacy"),
    ("137", "Without the Notification API the bell is disabled and says so; the queue still works with no console error", "N/A(spa-only) on legacy"),
    ("138", "A request that leaves and comes back with the same id raises no second notification", "N/A(spa-only) on legacy"),
    ("139", "The bell in every state sits inside the top bar and viewport, clear of the theme button and the link indicator, with its state's accessible name", "N/A(spa-only) on legacy"),
    ("140", "A notification constructor that throws moves the bell to unsupported, never on again, tried once, logged once", "N/A(spa-only) on legacy"),
    ("141", "The bell follows a permission changed under the open page, on focus and on visibility change", "N/A(spa-only) on legacy"),
    ("142", "The unsupported bell on an insecure page says it needs a secure connection", "N/A(spa-only) on legacy"),
    ("143", "With the stream open a request filed at the broker arrives by the stream and raises exactly one notification with the literal title, body and tag within 3 s, and the tab reads no /api/egress/queue after the first frame", "N/A(spa-only) on legacy"),
    ("150", "In one browser context, 6 visible tabs and 3 hidden ones hold one stream at the daemon (all Live, hidden included); a decide from a follower and one from the leader each complete in under 1 s, and the leader's next frame takes the rows out of every tab", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("151", "A request filed at the broker shows in each of 6 tabs (2 hidden) within 3 s, and no tab reads /api/egress/queue after the first frame (a follower never does)", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("152", "With the bell on in two tabs, one hidden, a filed request raises exactly one notification in each within 3 s (the literal title, body and tag), over one stream", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("153", "When the leader's tab closes another tab takes over within 5 s: the tabs left read Live, Connecting, Live, the daemon has one stream again, and a request filed then reaches both", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("154", "A leader hidden keeps the one stream and stays Live; a filed request reaches it and the other tabs within 3 s, and no tab reads the queue", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("155", "A follower that hears nothing from the leader for 40 s reads the queue itself and says Polling; the leader's next message returns it to Live and stops the reads", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("156", "A decide is the acting tab's alone (one POST from it, none from the others; its own read after it works), and the row leaves every tab by the leader's next frame; still one stream", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("157", "A stream lost by the leader reads Reconnecting then Polling in every tab, only the leader reads the queue (what it reads reaches the followers), and every tab is Live again when the stream returns", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("158", "A stream frame ends the polling a failed read started while Live: the banner clears and no read follows though every read of the queue still fails", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("159", "Connecting's spinner turns (`animation-name: spin`) and holds still under `prefers-reduced-motion: reduce`", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("160", "The `Filters cleared to show <host>` note is gone as soon as a filter changes", "N/A(spa-only) on legacy"),
    ("161", "The `Filters cleared to show <host>` note is gone once that request is decided or leaves the queue", "N/A(spa-only) on legacy"),
    ("162", "Each bell state has its own glyph, so muted and denied differ by shape and not by colour alone", "N/A(spa-only) on legacy"),
    ("163", "The blocked and unsupported popovers give a reason and a next step in their body, without repeating the heading or asking for a reload", "N/A(spa-only) on legacy"),
    ("164", "The spa suite's log has no `Task was destroyed but it is pending` line", "N/A(spa-only) on legacy"),
    ("170", "A leader that gets Page Lifecycle `freeze` (dispatched by hand: headless Chromium does not freeze) lets go of the lock: another tab leads within 5 s with one stream at the daemon, a filed request reaches the running tabs within 3 s, and on `resume` the tab follows without a stream of its own", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
    ("171", "A new tab's hello is answered to that tab alone: every link and snapshot message a tab already open hears meanwhile is addressed to the newcomer", "N/A(spa-only) on legacy: one stream per browser (PLN step 6.4)"),
]


def mapping_markdown() -> str:
    rows = ["| Original legacy check | Behaviour check | Note |", "|---|---|---|"]
    rows += [f"| {a} | {b} | {c} |" for a, b, c in LEGACY_MAP]
    new = ["| Check | What it asserts | On legacy |", "|---|---|---|"]
    new += [f"| ({a}) | {b} | {c} |" for a, b, c in NEW_CHECKS]
    return (
        "# Legacy check → behaviour check mapping\n\n"
        "Every check in the pre-refactor `tests/admin_ui_playwright.py` (21 call sites, 24 report lines when the "
        "overflow and action loops expand) and where it went. A check that asserts a deliberate difference from the "
        "legacy page reports `SKIP(design-change: <PLN Architecture decision>)` under `--ui legacy`; a check of "
        "behaviour that only exists in the new app reports `N/A(spa-only)`. Nothing else is skipped.\n\n"
        + "\n".join(rows) + "\n\n"
        "## New behaviour checks with no legacy original\n\n"
        + "\n".join(new) + "\n"
    )


# ---- main -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ui", choices=["legacy", "spa"], default="legacy")
    ui = parser.parse_args().ui
    out = Path(os.environ.get("ADMIN_UI_SHOTS") or tempfile.mkdtemp(prefix="admin-ui-"))
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    if ui == "spa":
        refusal = bundle_refusal(WORKTREE)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2

    asyncio_warnings = AsyncioWarnings()
    logging.getLogger("asyncio").addHandler(asyncio_warnings)
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
                results += run_dedicated(ui, viewport, browser, served, broker)
                results += run_history(ui, viewport, browser, served, broker)
                results += run_queue_features(ui, viewport, browser, served, broker)
                results += run_live_stream(ui, viewport, browser, served, broker)
                results += run_service_worker(ui, viewport, browser, served, broker)
                results += run_notifications(ui, viewport, browser, served, broker)
                results += run_one_stream(ui, viewport, browser, served, broker)
                log(f"stage=scenario viewport={viewport} ms={int((time.monotonic() - t0) * 1000)} "
                    f"decides={len(broker.decides)}")
                for theme in ("light", "dark"):
                    try:
                        capture_states(browser, served, broker, driver_cls, out, ui, viewport, theme)
                    except Exception as exc:  # noqa: BLE001 - a capture that cannot be taken is a FAIL line
                        results.append(Result("FAIL", viewport, "0", f"captures ({theme})", squash(str(exc))[:300]))
                    if ui == "spa":
                        try:
                            capture_bell_states(browser, served, broker, out, viewport, theme)
                        except Exception as exc:  # noqa: BLE001 - a capture that cannot be taken is a FAIL line
                            results.append(Result("FAIL", viewport, "0", f"bell captures ({theme})", squash(str(exc))[:300]))
                        for extra in (capture_bell_live, capture_filters_cleared):
                            try:
                                extra(browser, served, broker, out, viewport, theme)
                            except Exception as exc:  # noqa: BLE001 - a capture that cannot be taken is a FAIL line
                                results.append(Result("FAIL", viewport, "0", f"{extra.__name__} ({theme})", squash(str(exc))[:300]))
                        try:
                            capture_one_stream(browser, served, broker, out, viewport, theme)
                        except Exception as exc:  # noqa: BLE001 - a capture that cannot be taken is a FAIL line
                            results.append(Result("FAIL", viewport, "0", f"one-stream captures ({theme})", squash(str(exc))[:300]))
            browser.close()
    finally:
        served.stop()
        broker.stop()

    if broker.violations:
        for violation in broker.violations:
            print(f"CONTRACT VIOLATION: {violation}", file=sys.stderr)
        return 2

    if ui == "legacy":
        results.append(Result("N/A", "all", "164", dict((a, b) for a, b, _c in NEW_CHECKS)["164"], "spa-only"))
    else:
        gc.collect()   # a dropped task is reported when it is collected
        pending = asyncio_warnings.pending
        log(f"stage=asyncio-warnings pending_task_lines={len(pending)}")
        results.append(Result("FAIL" if pending else "PASS", "all", "164",
                              dict((a, b) for a, b, _c in NEW_CHECKS)["164"],
                              f"{len(pending)} line(s), first: {pending[0]}" if pending else ""))

    printed = [result.line() for result in results]
    counts = {status: sum(1 for r in results if r.status == status) for status in ("PASS", "SKIP", "N/A", "FAIL")}
    summary = (f"{ui.upper()}: {counts['PASS']} PASS, {counts['SKIP']} SKIP(design-change), "
               f"{counts['N/A']} N/A(spa-only), {counts['FAIL']} FAIL")
    mapping = mapping_markdown()
    (out / "legacy-mapping.md").write_text(mapping, encoding="utf-8")
    (out / f"REPORT-{ui}.md").write_text("\n".join(f"- {line}" for line in printed) + f"\n\n{summary}\n", encoding="utf-8")
    print("\n".join(printed))
    print(f"\n{summary}  ({int(time.monotonic() - started)}s, captures in {out})")
    print("\n" + mapping)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
