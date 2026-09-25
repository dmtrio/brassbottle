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
from datetime import datetime, timedelta
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
    console_errors: list[str] = field(default_factory=list)
    expected_failures: int = 0                          # scripted 4xx/5xx the browser logs

    def attach(self, page) -> None:
        def on_request(request):
            if request.method == "GET" and urlsplit(request.url).path == "/api/egress/recent":
                self.recent.append({k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()})
            if request.method == "POST" and request.url.endswith("/api/egress/decide"):
                self.decides.append({"body": json.loads(request.post_data or "{}"), "headers": request.headers})

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
            if message.text.startswith("Failed to load resource") and re.search(r"/api/egress/(decide|queue|recent)(\?.*)?$", url):
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

    def recent_text(self):
        return self.page.locator("[data-testid=recent]").inner_text()

    def recent_rows(self):
        return [squash(text) for text in self.page.locator("[data-testid=recent-row]").all_inner_texts()]

    def stale_banner(self):
        return self.page.locator("[data-testid=stale-banner]")

    def note(self, host):
        return squash(self.row(host).locator("[role=status]").inner_text())

    def set_view(self, label: str) -> None:
        self.page.locator("button, [role=radio]").filter(has_text=re.compile(rf"^\s*{label}\s*$")).click()

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
                    text: text.textContent};
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
    """History-tab checks (30..42). One page per viewport, checked in order; the SPA only."""
    from playwright.sync_api import expect

    suite = Suite(ui, viewport)
    if ui == "legacy":
        for number, name, _fn in HISTORY_CHECKS:
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
        context.close()
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
    page.get_by_placeholder("Search destination").fill("history-01")
    expect(hist.rows()).to_have_count(10)
    for host in hist.hosts():
        assert "history-01" in host, host
    page.get_by_placeholder("Search destination").fill("")
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
    sent = len(traffic.recent)
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
    page.wait_for_timeout(400)                       # let the late reply reach the page
    _eq([q.get("container") for q in traffic.recent[sent:]], ["mid", "zeta"])
    _eq(hist.hosts(), want)                          # the superseded reply did not replace them
    _in("zeta", page.get_by_test_id("history-bottle").inner_text())
    expect_disabled(page, "history-older", hist.expected("container=zeta&limit=50")["next"] is None)


# One row is two lines (host and date, then pill and bottle) with an optional third for a deny reason.
TWO_LINE_ROW_MAX_PX = 80
THREE_LINE_ROW_MAX_PX = 104


def _h_row_layout(hist, page, traffic, broker) -> None:
    hist.open()
    phone = page.viewport_size["width"] < 640
    rows = hist.rows().all()
    assert len(rows) == 50, len(rows)
    tall = []
    for row in rows:
        text = squash(row.inner_text())
        box = row.bounding_box()
        # A deny reason is the optional third line; a bottle and pill too wide for a phone may wrap.
        lines = 3 if any(r["host"] in text and r["deny_reason"] for r in hist.expected("limit=50")["rows"]) else 2
        limit = THREE_LINE_ROW_MAX_PX if lines == 3 or (phone and stub.LONG_BOTTLE in text) else TWO_LINE_ROW_MAX_PX
        if box["height"] > limit:
            tall.append((round(box["height"]), limit, text[:70]))
        if phone:
            assert "by operator" not in text and "by denylist" not in text, f"'by' shown on a phone: {text}"
            assert not re.search(r"\d+[smhd] ago", text), f"relative time shown on a phone: {text}"
        else:
            assert re.search(r"\bby (operator|denylist|sweep)\b", text), f"no 'by' on: {text}"
            assert re.search(r"\b\d+[smhd] ago\b", text), f"no relative time on: {text}"
    assert not tall, f"rows taller than their line budget: {tall[:5]}"
    # The date sits top-right, on the host's line.
    row = rows[0]
    head, when = row.locator(".row-title").bounding_box(), row.get_by_test_id("history-row-when").bounding_box()
    box = row.bounding_box()
    assert when["y"] < head["y"] + head["height"], f"date is below the host: {when} {head}"
    assert when["x"] + when["width"] > box["x"] + box["width"] - 40, f"date is not at the right edge: {when} {box}"
    # Pill then bottle on the line below.
    pill, bottle = row.locator(".pill").first.bounding_box(), row.locator(".meta-item").nth(1).bounding_box()
    assert pill["y"] > head["y"] + head["height"] - 1 and pill["x"] < bottle["x"], f"meta line out of order: {pill} {bottle}"


def _h_toolbar_layout(hist, page, traffic, broker) -> None:
    hist.open()
    box = lambda locator: locator.bounding_box()
    search = box(page.get_by_placeholder("Search destination"))
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


def expect_disabled(page, test_id: str, disabled: bool) -> None:
    from playwright.sync_api import expect
    button = page.get_by_test_id(test_id)
    expect(button).to_be_disabled() if disabled else expect(button).to_be_enabled()


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
    ("40", "Rows are two lines (host and date, pill and bottle), with by and relative time from sm up", _h_row_layout),
    ("41", "Search, date and bottle share a row on tablet and desktop, the bottle label sits by its icon", _h_toolbar_layout),
    ("42", "The date trigger is named \"Date range: <label>\"", _h_date_trigger_name),
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
    try:
        drv = open_queue(page, served, driver_cls, traffic)
        held: list = []
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
        context.close()


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
        hist.pick_day(30)
        from playwright.sync_api import expect
        expect(hist.rows()).to_have_count(1)
        capture(page, out, "spa", viewport, theme, "history-filtered-open")
        hist.close_popover()
        capture(page, out, "spa", viewport, theme, "history-filtered")
    finally:
        context.close()

# ---- legacy → behaviour mapping ------------------------------------------------------

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
    ("40", "Rows are two lines (host and date, pill and bottle), with by and relative time from sm up", "N/A(spa-only) on legacy"),
    ("41", "Search, date and bottle share a row on tablet and desktop, the bottle label sits by its icon", "N/A(spa-only) on legacy"),
    ("42", "The date trigger is named \"Date range: <label>\"", "N/A(spa-only) on legacy"),
    ("50", "The stale banner says `Showing data from <time>` only after a failed poll; a decide failure reads `Decision not sent: <error>`", "N/A(spa-only) on legacy"),
    ("51", "The light theme paints no pure-red (#ff0000) pixel in any request row", "N/A(spa-only) on legacy"),
    ("52", "An unbreakable meta string wraps inside its row instead of being clipped", "N/A(spa-only) on legacy"),
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
                log(f"stage=scenario viewport={viewport} ms={int((time.monotonic() - t0) * 1000)} "
                    f"decides={len(broker.decides)}")
                for theme in ("light", "dark"):
                    try:
                        capture_states(browser, served, broker, driver_cls, out, ui, viewport, theme)
                    except Exception as exc:  # noqa: BLE001 - a capture that cannot be taken is a FAIL line
                        results.append(Result("FAIL", viewport, "0", f"captures ({theme})", squash(str(exc))[:300]))
            browser.close()
    finally:
        served.stop()
        broker.stop()

    if broker.violations:
        for violation in broker.violations:
            print(f"CONTRACT VIOLATION: {violation}", file=sys.stderr)
        return 2

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
