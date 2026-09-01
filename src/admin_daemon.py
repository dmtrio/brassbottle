#!/usr/bin/env python3
"""admin_daemon.py — loopback-only admin plane daemon for djinn.

Serves the browser admin UI and proxies operator actions to the egress broker
daemon with operator credentials held server-side only. The browser session
cookie gate defends against hostile web pages (CSRF and DNS rebinding), but
does not defend against local processes running as the same user; those can
already read the same token files on disk.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from egress_broker_host import (
    OPERATOR_TOKEN_FILENAME,
    address_family_for_host,
    daemon_base_url,
    ensure_operator_token,
)

LOG = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8817
UPSTREAM_TIMEOUT_SECONDS = 10.0
SESSION_COOKIE_NAME = "admin_session"
SESSION_HEADER_NAME = "X-Admin-UI"
QUEUE_POLL_SECONDS = 2
REASON_MAX_CHARS = 200
TOKEN_RACE_SLEEP_SECONDS = 0.2
TOKEN_REJECTED_ERROR = "operator token rejected by daemon; restart djinn admin"
UNREACHABLE_ERROR = "egress daemon unreachable"

_ICON_192 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAABMElEQVR4nO3SQQkAIADAQDW50a3gXiLcJRibYw8urdcB"
    "XzOgM6AzQGdAZ4DOgM6AzgCdAZ0BOgM6A3QGdAboDOgM0BnQGaAzoDNAN0BngM6AzgCdAToDOgN0BnQG6AzoDNAN0Bm"
    "gM6AzQGdAZ4DOgM4AnQGdAToDOgN0BmQG6AzQGdAZoDOgM0BnQGeAzoDOAJ0BnQE6AzoDdAZkBugM0BnQGaAzoDNAN0"
    "BngM6AzgCdAZ0BOgM6A3QGZAboDNAN0BmQGaAzoDNAZ0BngM6AzgCdAZ0BOgM6A3QGZAboDNAN0BmQGaAzoDNAZ0Bng"
    "M4AnQGdAToDOgN0BmQG6AzQGdAZkBmgM0BnQGeAzoDOAJ0BnQE6AzoDdAZkBugM0BnQGaAzoDNAN0BmgM4AnQGdAToD"
    "OgN0BmQG6AzoDNAN0BmgM0BnQGeAzoDOgM4A3QFxSCMmB+1Z8QAAAABJRU5ErkJggg=="
)
_ICON_512 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAB7ElEQVR4nO3RMQEAIAzAMMC/5+GiPEgU9Lpn5gBA6fY"
    "eAIBfAgCCAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAg"
    "ACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACA"
    "gACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgAC"
    "AgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgA"
    "CAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAg"
    "ACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACA"
    "gACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgAC"
    "AgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgA"
    "CAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAg"
    "ACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACA"
    "gACAgACAgACAgACAgACAgACAgACAgACAgACAgACAgACA4AsM5QIh1ZNQ0QAAAABJRU5ErkJggg=="
)

APP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#1b3a4b">
  <title>Egress queue - Djinn admin</title>
  <link rel="manifest" href="/manifest.webmanifest">
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --fg: #1c2630;
      --muted: #5f6b76;
      --accent: #0b6fa4;
      --warn: #8a5a00;
      --error: #9c1c1c;
      --line: #c8d2db;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #11161b;
        --panel: #182028;
        --fg: #e4ecf3;
        --muted: #9eb0c0;
        --accent: #67b9e3;
        --warn: #f2bc63;
        --error: #ff8f8f;
        --line: #2c3946;
      }
    }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; }
    header, footer, main { max-width: 1100px; margin: 0 auto; padding: 12px 16px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    nav button { border: 1px solid var(--line); background: var(--panel); color: var(--fg); border-radius: 8px; padding: 6px 10px; }
    nav button[aria-current="page"] { border-color: var(--accent); color: var(--accent); }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
    .meta { color: var(--muted); margin-bottom: 8px; }
    .banner { display:none; margin: 8px 0; padding: 8px; border-radius: 8px; border: 1px solid var(--warn); color: var(--warn); }
    .banner.error { border-color: var(--error); color: var(--error); }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 6px; text-align: left; vertical-align: top; }
    .host-group { background: color-mix(in srgb, var(--panel) 70%, var(--line)); font-weight: 600; }
    .badge { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; margin-left: 6px; font-size: 12px; color: var(--muted); }
    .chip { display: inline-block; margin-top: 4px; border-radius: 999px; padding: 2px 8px; border: 1px solid var(--line); font-size: 12px; }
    .chip.error { border-color: var(--error); color: var(--error); }
    .actions { display: flex; flex-wrap: wrap; gap: 6px; }
    .actions button { border: 1px solid var(--line); background: var(--panel); color: var(--fg); border-radius: 8px; padding: 4px 8px; }
    .actions button.warn { border-color: var(--warn); color: var(--warn); }
    .actions button.error { border-color: var(--error); color: var(--error); }
    .small { font-size: 12px; color: var(--muted); }
    input[type=text] { width: 100%; box-sizing: border-box; border: 1px solid var(--line); border-radius: 6px; padding: 4px 6px; background: var(--panel); color: var(--fg); }
    .empty { color: var(--muted); padding: 8px 0; }
  </style>
</head>
<body>
  <header>
    <h1>Djinn admin</h1>
    <nav aria-label="Panels">
      <button type="button" aria-current="page">Egress queue</button>
      <button type="button" disabled title="Coming soon">Denylist</button>
      <button type="button" disabled title="Coming soon">Bottles</button>
      <button type="button" disabled title="Coming soon">Backup</button>
    </nav>
  </header>
  <main>
    <section class="panel">
      <div class="meta" id="metaLine">Loading queue...</div>
      <button id="alertsBtn" type="button">Enable alerts</button>
      <div id="staleBanner" class="banner" role="status" aria-live="polite"></div>
      <div id="tableWrap"></div>
    </section>
  </main>
  <footer class="small">Shows open approval requests (decisions), not currently-permitted hosts — the ipset allowlist is the authority.</footer>
  <script>
  (function () {
    const pollMs = __POLL_MS__;
    const state = {
      seq: 0,
      timer: null,
      latestData: null,
      staleSince: null,
      inflightByKey: new Set(),
      rowMessage: new Map(),
      seenIds: new Set(),
      lastIds: new Set()
    };
    const tableWrap = document.getElementById("tableWrap");
    const staleBanner = document.getElementById("staleBanner");
    const metaLine = document.getElementById("metaLine");
    const alertsBtn = document.getElementById("alertsBtn");

    function setBanner(msg, isError) {
      if (!msg) {
        staleBanner.style.display = "none";
        staleBanner.textContent = "";
        staleBanner.className = "banner";
        return;
      }
      staleBanner.style.display = "block";
      staleBanner.textContent = msg;
      staleBanner.className = isError ? "banner error" : "banner";
    }

    function text(tag, value) {
      const el = document.createElement(tag);
      el.textContent = value;
      return el;
    }

    function button(label, className, onClick) {
      const el = document.createElement("button");
      el.type = "button";
      if (className) el.className = className;
      el.textContent = label;
      el.addEventListener("click", onClick);
      return el;
    }

    function ageString(seconds) {
      if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "-";
      if (seconds < 60) return Math.floor(seconds) + "s";
      if (seconds < 3600) return Math.floor(seconds / 60) + "m";
      return Math.floor(seconds / 3600) + "h";
    }

    function loopbackOrigin(origin) {
      try {
        const parsed = new URL(origin);
        if (parsed.protocol !== "http:") return false;
        const host = parsed.hostname;
        return host === "localhost" || host === "127.0.0.1" || host === "::1";
      } catch (_err) {
        return false;
      }
    }

    function updateTitle(count) {
      document.title = count > 0 ? "(" + count + ") Egress queue - Djinn admin" : "Egress queue - Djinn admin";
      if ("setAppBadge" in navigator) {
        Promise.resolve().then(function () {
          if (count > 0) return navigator.setAppBadge(count);
          return navigator.clearAppBadge();
        }).catch(function () {});
      }
    }

    function maybeNotify(openRows) {
      if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
      const nowIds = new Set();
      for (const row of openRows) {
        nowIds.add(row.request_id);
        if (state.seenIds.has(row.request_id)) continue;
        state.seenIds.add(row.request_id);
        const n = new Notification("Egress request", { body: row.container + " \u2192 " + row.host + ":" + row.port });
        n.onclick = function () { window.focus(); };
      }
      state.lastIds = nowIds;
    }

    function clearNode(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    }

    function requestKey(row) {
      return row.request_id;
    }

    function setInflight(row, value) {
      const key = requestKey(row);
      if (value) state.inflightByKey.add(key);
      else state.inflightByKey.delete(key);
    }

    function postDecision(row, action, reason, requireHostText) {
      if (requireHostText && requireHostText !== row.host) {
        state.rowMessage.set(row.request_id, { type: "error", text: "type exact host to arm global deny" });
        render(state.latestData);
        return;
      }
      const payload = { action: action, host: row.host };
      if (action !== "deny_global") payload.container = row.container;
      if (reason) payload.reason = reason;
      state.rowMessage.delete(row.request_id);
      setInflight(row, true);
      render(state.latestData);
      fetch("/api/egress/decide", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-UI": "1"
        },
        body: JSON.stringify(payload)
      }).then(async function (resp) {
        let body = {};
        try { body = await resp.json(); } catch (_err) {}
        if (resp.ok) {
          const failures = Array.isArray(body.apply_failures) ? body.apply_failures : null;
          if (!failures) {
            state.rowMessage.set(row.request_id, { type: "neutral", text: "decision recorded" });
          } else {
            const hit = failures.find(function (f) { return f && f.request_id === row.request_id; });
            if (!hit) {
              state.rowMessage.set(row.request_id, { type: "neutral", text: "decision recorded" });
            } else if (hit.reason === "ip_requires_cidr") {
              state.rowMessage.set(row.request_id, { type: "neutral", text: "recorded - add CIDR to manifest by hand" });
            } else if (hit.reason === "apply_failed") {
              state.rowMessage.set(row.request_id, { type: "error", text: "decision recorded but rule install FAILED - request stays queued" });
            } else {
              state.rowMessage.set(row.request_id, { type: "error", text: "decision recorded with apply failure" });
            }
          }
          setBanner("", false);
        } else if (resp.status === 400 && typeof body.error === "string") {
          state.rowMessage.set(row.request_id, { type: "error", text: body.error });
        } else if ((resp.status === 502 || resp.status === 503) && typeof body.error === "string") {
          setBanner(body.error, true);
        } else {
          setBanner("decide failed", true);
        }
      }).catch(function () {
        setBanner("egress daemon unreachable", true);
      }).finally(function () {
        setInflight(row, false);
        pollNow();
      });
    }

    function render(data) {
      state.latestData = data;
      clearNode(tableWrap);
      if (!data || !Array.isArray(data.open)) {
        tableWrap.appendChild(text("div", "No open requests"));
        return;
      }

      const openRows = data.open.slice();
      openRows.sort(function (a, b) {
        const aa = typeof a.age_seconds === "number" ? a.age_seconds : 0;
        const bb = typeof b.age_seconds === "number" ? b.age_seconds : 0;
        return bb - aa;
      });
      maybeNotify(openRows);
      updateTitle(typeof data.count === "number" ? data.count : openRows.length);
      const generated = typeof data.generated_at === "string" ? data.generated_at : "unknown";
      metaLine.textContent = "Open requests: " + openRows.length + " | generated at: " + generated;
      if (openRows.length === 0) {
        tableWrap.appendChild(text("div", "No open requests"));
        tableWrap.lastChild.className = "empty";
        return;
      }

      const groups = new Map();
      for (const row of openRows) {
        const key = row.host;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
      }

      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const hr = document.createElement("tr");
      ["Age", "Container", "Host:port", "UID", "Comm", "Hits", "Reason", "Actions"].forEach(function (name) {
        hr.appendChild(text("th", name));
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      for (const host of groups.keys()) {
        const rows = groups.get(host);
        if (rows.length > 1) {
          let totalHits = 0;
          for (const r of rows) totalHits += Number(r.hit_count || 0);
          const gr = document.createElement("tr");
          gr.className = "host-group";
          const td = document.createElement("td");
          td.colSpan = 8;
          td.textContent = host + " - " + rows.length + " request(s), " + totalHits + " hits";
          const arming = document.createElement("input");
          arming.type = "text";
          arming.maxLength = 200;
          arming.placeholder = "Type host to arm global deny";
          const denyBtn = button("Deny always (global)", "error", function () {
            const first = rows[0];
            postDecision({ request_id: "__group__:" + host, host: host, container: first.container }, "deny_global", "", arming.value);
          });
          denyBtn.style.marginLeft = "8px";
          td.appendChild(text("span", " "));
          td.appendChild(arming);
          td.appendChild(denyBtn);
          gr.appendChild(td);
          tbody.appendChild(gr);
        }
        for (const row of rows) {
          const tr = document.createElement("tr");
          const inflight = state.inflightByKey.has(requestKey(row));
          const denyReason = document.createElement("input");
          denyReason.type = "text";
          denyReason.maxLength = 200;
          denyReason.placeholder = "Optional deny reason";

          const globalArm = document.createElement("input");
          globalArm.type = "text";
          globalArm.maxLength = 200;
          globalArm.placeholder = "Type host for global deny";

          const hostCell = document.createElement("td");
          hostCell.textContent = row.host + ":" + row.port;
          if (row.host_is_ip) {
            const badge = text("span", "IP");
            badge.className = "badge";
            badge.title = "Approving records the decision; the CIDR must reach the manifest by hand.";
            hostCell.appendChild(badge);
          }

          tr.appendChild(text("td", ageString(row.age_seconds)));
          tr.appendChild(text("td", String(row.container || "")));
          tr.appendChild(hostCell);
          tr.appendChild(text("td", String(row.uid == null ? "" : row.uid)));
          tr.appendChild(text("td", String(row.comm || "")));
          tr.appendChild(text("td", String(row.hit_count == null ? "" : row.hit_count)));
          tr.appendChild(text("td", String(row.reason || "")));

          const actionsTd = document.createElement("td");
          const actions = document.createElement("div");
          actions.className = "actions";
          actions.appendChild(button("Allow", "", function () { postDecision(row, "allow_live", "", ""); }));
          actions.appendChild(button("Allow+manifest", "", function () { postDecision(row, "allow_manifest", "", ""); }));
          actions.appendChild(button("Deny", "warn", function () { postDecision(row, "deny", denyReason.value, ""); }));
          actions.appendChild(button("Deny always (bottle)", "warn", function () { postDecision(row, "deny_bottle", denyReason.value, ""); }));
          actions.appendChild(button("Deny always (global)", "error", function () { postDecision(row, "deny_global", denyReason.value, globalArm.value); }));
          for (const node of actions.querySelectorAll("button")) node.disabled = inflight;
          denyReason.disabled = inflight;
          globalArm.disabled = inflight;
          actionsTd.appendChild(actions);
          actionsTd.appendChild(denyReason);
          actionsTd.appendChild(globalArm);
          const msg = state.rowMessage.get(row.request_id);
          if (msg) {
            const chip = text("div", msg.text);
            chip.className = msg.type === "error" ? "chip error" : "chip";
            actionsTd.appendChild(chip);
          }
          tr.appendChild(actionsTd);
          tbody.appendChild(tr);
        }
      }
      table.appendChild(tbody);
      tableWrap.appendChild(table);
    }

    function pollNow() {
      const seq = ++state.seq;
      fetch("/api/egress/queue", { method: "GET" }).then(async function (resp) {
        let body = {};
        try { body = await resp.json(); } catch (_err) {}
        if (seq !== state.seq) return;
        if (resp.ok) {
          state.staleSince = null;
          setBanner("", false);
          render(body);
          return;
        }
        if (!state.staleSince) state.staleSince = new Date();
        const ts = state.staleSince.toLocaleTimeString();
        const msg = typeof body.error === "string" ? body.error : "daemon unreachable - data stale since " + ts;
        setBanner(msg, true);
      }).catch(function () {
        if (seq !== state.seq) return;
        if (!state.staleSince) state.staleSince = new Date();
        setBanner("daemon unreachable - data stale since " + state.staleSince.toLocaleTimeString(), true);
      }).finally(function () {
        if (seq !== state.seq) return;
        state.timer = setTimeout(pollNow, pollMs);
      });
    }

    alertsBtn.addEventListener("click", function () {
      if (!("Notification" in window)) return;
      Notification.requestPermission().catch(function () {});
    });

    if ("serviceWorker" in navigator) {
      try {
        navigator.serviceWorker.register("/sw.js").catch(function () {});
      } catch (_err) {}
    }
    pollNow();
    render({open: [], count: 0, generated_at: "-"});
  })();
  </script>
</body>
</html>
""".replace("__POLL_MS__", str(QUEUE_POLL_SECONDS * 1000))

MANIFEST = {
    "name": "Djinn admin",
    "short_name": "Djinn",
    "display": "standalone",
    "start_url": "/",
    "theme_color": "#1b3a4b",
    "background_color": "#11161b",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}

SW_JS = """const CACHE_VERSION = "djinn-admin-shell-v1";
const SHELL_PATHS = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL_PATHS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return;
  const isShellPath = SHELL_PATHS.includes(url.pathname);

  if (url.pathname === "/") {
    event.respondWith(
      fetch(req).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy)).catch(() => {});
        return resp;
      }).catch(() => caches.match(req))
    );
    return;
  }

  if (!isShellPath) return;

  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy)).catch(() => {});
      return resp;
    }))
  );
});
"""


def _json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _is_loopback_value(raw_host: str) -> bool:
    host = raw_host.strip().lower()
    if not host:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.startswith("[") and "]:" in host:
        host = host[1:].split("]:", 1)[0]
    elif ":" in host and host.count(":") == 1:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses: list[str] = [info[4][0] for info in infos]
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_loopback:
                return False
        except ValueError:
            return False
    return True


def _ensure_admin_operator_token(egress_root: Path) -> str:
    try:
        token = ensure_operator_token(egress_root)
        if token:
            return token
    except FileExistsError:
        time.sleep(TOKEN_RACE_SLEEP_SECONDS)
        token_path = egress_root / OPERATOR_TOKEN_FILENAME
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
        raise RuntimeError(
            f"operator token unavailable ({token_path})"
        ) from None
    raise RuntimeError("operator token unavailable (empty token)")


def _upstream_json(
    *,
    base_url: str,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any], int]:
    url = base_url.rstrip("/") + path
    payload: bytes | None = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    LOG.info("admin upstream start method=%s url=%s", method, url)
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    duration_ms = int((time.monotonic() - started) * 1000)
    LOG.info(
        "admin upstream status=%d duration_ms=%d bytes=%d",
        status,
        duration_ms,
        len(raw),
    )
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed, len(raw)


class AdminHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying admin-plane state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        egress_root: Path,
        session_secret: str,
        operator_token: str,
    ):
        self.address_family = address_family_for_host(server_address[0])
        self.egress_root = egress_root
        self.session_secret = session_secret
        self.operator_token = operator_token
        super().__init__(server_address, AdminRequestHandler)

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            LOG.info("admin client disconnect host=%s port=%s", client_address[0], client_address[1])
            return
        super().handle_error(request, client_address)


class AdminRequestHandler(BaseHTTPRequestHandler):
    """Routes app shell/static and egress panel API calls."""

    server: AdminHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("admin http %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        routes: dict[tuple[str, str], Callable[[], None]] = {
            ("GET", "/"): self._handle_root,
            ("GET", "/manifest.webmanifest"): self._handle_manifest,
            ("GET", "/sw.js"): self._handle_sw,
            ("GET", "/icon-192.png"): self._handle_icon_192,
            ("GET", "/icon-512.png"): self._handle_icon_512,
            ("GET", "/api/egress/queue"): self._handle_egress_queue_get,
            ("POST", "/api/egress/decide"): self._handle_egress_decide_post,
        }
        handler = routes.get((method, path))
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        handler()

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        LOG.info("admin response out status=%d bytes=%d", status, len(body))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        self._send_bytes(status, _json_bytes(body), content_type="application/json")

    def _request_json_body(self) -> tuple[dict[str, Any] | None, int]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}, length
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, length
        if not isinstance(body, dict):
            return None, length
        return body, length

    def _handle_root(self) -> None:
        headers = {
            "Set-Cookie": (
                f"{SESSION_COOKIE_NAME}={self.server.session_secret}; "
                "SameSite=Strict; Path=/; HttpOnly"
            )
        }
        self._send_bytes(HTTPStatus.OK, APP_HTML.encode("utf-8"), content_type="text/html; charset=utf-8", headers=headers)

    def _handle_manifest(self) -> None:
        body = json.dumps(MANIFEST, separators=(",", ":")).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, body, content_type="application/manifest+json")

    def _handle_sw(self) -> None:
        self._send_bytes(HTTPStatus.OK, SW_JS.encode("utf-8"), content_type="application/javascript")

    def _handle_icon_192(self) -> None:
        self._send_bytes(HTTPStatus.OK, _ICON_192, content_type="image/png")

    def _handle_icon_512(self) -> None:
        self._send_bytes(HTTPStatus.OK, _ICON_512, content_type="image/png")

    # ---- Egress panel handlers -------------------------------------------------

    def _daemon_base_url(self) -> str:
        return daemon_base_url(self.server.egress_root)

    def _handle_egress_queue_get(self) -> None:
        LOG.info("admin request enter method=GET path=/api/egress/queue bytes=0")
        try:
            status, upstream, _bytes = _upstream_json(
                base_url=self._daemon_base_url(),
                method="GET",
                path="/queue",
                token=self.server.operator_token,
                body=None,
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": UNREACHABLE_ERROR})
            return

        if status == HTTPStatus.OK:
            self._send_json(HTTPStatus.OK, upstream)
            return
        if status == HTTPStatus.UNAUTHORIZED:
            LOG.warning("admin upstream auth rejected path=/queue")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": TOKEN_REJECTED_ERROR})
            return
        if status >= 500:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": UNREACHABLE_ERROR})
            return
        self._send_json(HTTPStatus.BAD_GATEWAY, {"error": UNREACHABLE_ERROR})

    def _read_session_cookie(self) -> str:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return ""
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        morsel = parsed.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel is not None else ""

    def _json_content_type_ok(self) -> bool:
        raw = self.headers.get("Content-Type", "")
        if not raw:
            return False
        media_type = raw.split(";", 1)[0].strip().lower()
        return media_type == "application/json"

    def _origin_ok(self) -> bool:
        raw = self.headers.get("Origin")
        if raw is None:
            return True
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            return False
        if parsed.scheme != "http":
            return False
        if not parsed.hostname:
            return False
        return _is_loopback_value(parsed.hostname)

    def _host_header_ok(self) -> bool:
        value = self.headers.get("Host", "")
        if not value:
            return False
        return _is_loopback_value(value)

    def _session_gate_ok(self) -> bool:
        cookie = self._read_session_cookie()
        if not cookie:
            LOG.info("admin session gate failed check=cookie_missing")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if not hmac.compare_digest(cookie, self.server.session_secret):
            LOG.info("admin session gate failed check=cookie_mismatch")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if not self._json_content_type_ok():
            LOG.info("admin session gate failed check=content_type")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if self.headers.get(SESSION_HEADER_NAME) != "1":
            LOG.info("admin session gate failed check=x_admin_ui")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if not self._origin_ok():
            LOG.info("admin session gate failed check=origin")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if not self._host_header_ok():
            LOG.info("admin session gate failed check=host")
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        return True

    def _validate_decide_payload(self, body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        action = body.get("action")
        if action not in ("allow_live", "allow_manifest", "deny", "deny_bottle", "deny_global"):
            return None, "invalid action"
        host = body.get("host")
        if not isinstance(host, str) or not host.strip():
            return None, "host is required"
        container = body.get("container")
        if action != "deny_global":
            if not isinstance(container, str) or not container.strip():
                return None, "container is required"
        else:
            if container is not None and not isinstance(container, str):
                return None, "container must be a string"
        reason = body.get("reason")
        deny_action = action in ("deny", "deny_bottle", "deny_global")
        if deny_action:
            if reason is not None:
                if not isinstance(reason, str):
                    return None, "reason must be a string"
                if len(reason) > REASON_MAX_CHARS:
                    return None, "reason must be <= 200 characters"
        else:
            if reason is not None:
                return None, "reason only allowed for deny actions"

        upstream: dict[str, Any] = {"host": host}
        if action == "allow_live":
            upstream["decision"] = "allow"
            upstream["scope"] = "live"
            upstream["container"] = container
        elif action == "allow_manifest":
            upstream["decision"] = "allow"
            upstream["scope"] = "manifest"
            upstream["container"] = container
        elif action == "deny":
            upstream["decision"] = "deny"
            upstream["scope"] = "once"
            upstream["container"] = container
        elif action == "deny_bottle":
            upstream["decision"] = "deny"
            upstream["scope"] = "bottle"
            upstream["container"] = container
        else:
            upstream["decision"] = "deny"
            upstream["scope"] = "global"
        if deny_action and isinstance(reason, str) and reason:
            upstream["reason"] = reason
        return upstream, None

    def _handle_egress_decide_post(self) -> None:
        if not self._session_gate_ok():
            return
        payload, request_bytes = self._request_json_body()
        LOG.info(
            "admin request enter method=POST path=/api/egress/decide bytes=%d",
            request_bytes,
        )
        if payload is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        upstream_body, error = self._validate_decide_payload(payload)
        if error is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return
        reason_len = len(upstream_body.get("reason", "")) if isinstance(upstream_body.get("reason"), str) else 0
        LOG.info(
            "admin decide mapped decision=%s scope=%s reason_len=%d",
            upstream_body["decision"],
            upstream_body["scope"],
            reason_len,
        )
        try:
            status, upstream, _bytes = _upstream_json(
                base_url=self._daemon_base_url(),
                method="POST",
                path="/decide",
                token=self.server.operator_token,
                body=upstream_body,
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": UNREACHABLE_ERROR})
            return

        if status == HTTPStatus.OK:
            decided = upstream.get("decided")
            out: dict[str, Any] = {"ok": True, "decided": len(decided) if isinstance(decided, list) else 0}
            apply_failures = upstream.get("apply_failures")
            if isinstance(apply_failures, list):
                out["apply_failures"] = [
                    {"request_id": item["request_id"], "reason": item["reason"]}
                    for item in apply_failures
                    if isinstance(item, dict)
                    and isinstance(item.get("request_id"), str)
                    and isinstance(item.get("reason"), str)
                ]
            persisted = upstream.get("persisted")
            if isinstance(persisted, dict):
                zone = persisted.get("zone")
                scope = persisted.get("scope")
                if isinstance(zone, str) and isinstance(scope, str):
                    out["persisted"] = {"zone": zone, "scope": scope}
            self._send_json(HTTPStatus.OK, out)
            return
        if status == HTTPStatus.BAD_REQUEST:
            text = "bad request"
            if isinstance(upstream.get("error"), str):
                text = upstream["error"][:REASON_MAX_CHARS]
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": text})
            return
        if status == HTTPStatus.UNAUTHORIZED:
            LOG.warning("admin upstream auth rejected path=/decide")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": TOKEN_REJECTED_ERROR})
            return
        if status >= 500:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "decide failed on the daemon"})
            return
        self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "decide failed on the daemon"})


def run_daemon(*, host: str, port: int, egress_root: Path) -> None:
    operator_token = _ensure_admin_operator_token(egress_root)
    session_secret = secrets.token_urlsafe(32)
    server = AdminHTTPServer(
        (host, port),
        egress_root=egress_root,
        session_secret=session_secret,
        operator_token=operator_token,
    )
    LOG.info(
        "admin daemon listen host=%s port=%d family=%s",
        host,
        server.server_address[1],
        "AF_INET6" if server.address_family == socket.AF_INET6 else "AF_INET",
    )
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="djinn admin daemon")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    return parser


def _egress_root_from_env() -> Path:
    home = os.environ.get("DJINN_HOME", "").strip()
    if not home:
        raise RuntimeError("DJINN_HOME is required")
    return Path(home).expanduser() / "run" / "egress"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _is_loopback_value(args.host):
        print(f"Error: --host must be loopback (got {args.host!r})", file=sys.stderr)
        return 1
    try:
        egress_root = _egress_root_from_env()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        run_daemon(host=args.host, port=args.port, egress_root=egress_root)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"Error: cannot bind {args.host}:{args.port} (is djinn admin already running?)",
                file=sys.stderr,
            )
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
