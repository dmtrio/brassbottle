#!/usr/bin/env python3
"""Read-only local page for open egress approval decisions in flight.

This page shows open approval requests (operator decisions in flight), never
"currently permitted hosts". The firewall ipset allowlist remains the sole
authority for what egress is currently permitted.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from egress_broker_host import daemon_base_url, ensure_operator_token, read_daemon_endpoint

LOG = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8817
UPSTREAM_TIMEOUT_SECONDS = 5
UNREACHABLE_BODY = b'{"error":"egress daemon unreachable"}'
NOT_FOUND_BODY = b'{"error":"not found"}'

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Egress queue</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #ffffff;
      --fg: #111111;
      --muted: #666666;
      --line: #dddddd;
      --warn-bg: #ffefc2;
      --warn-fg: #4d3600;
      --badge-bg: #dce9ff;
      --badge-fg: #0f2b66;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1217;
        --fg: #f2f5f9;
        --muted: #9ca7b8;
        --line: #2a3240;
        --warn-bg: #4f3a00;
        --warn-fg: #ffe39a;
        --badge-bg: #243b66;
        --badge-fg: #d8e6ff;
      }
    }
    body {
      margin: 0;
      padding: 1rem;
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--fg);
    }
    h1 { margin: 0 0 0.5rem 0; font-size: 1.5rem; }
    .meta { color: var(--muted); margin-bottom: 0.75rem; }
    .banner {
      display: none;
      margin: 0 0 0.75rem 0;
      padding: 0.6rem 0.75rem;
      background: var(--warn-bg);
      color: var(--warn-fg);
      border-radius: 0.5rem;
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 0.75rem;
    }
    th, td {
      text-align: left;
      padding: 0.45rem 0.5rem;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      word-break: break-word;
    }
    th { font-size: 0.88rem; color: var(--muted); }
    .empty { color: var(--muted); margin: 0.75rem 0; }
    .badge {
      margin-left: 0.45rem;
      padding: 0.06rem 0.35rem;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      background: var(--badge-bg);
      color: var(--badge-fg);
      border: 1px solid var(--line);
      cursor: default;
    }
    footer {
      margin-top: 1rem;
      color: var(--muted);
      font-size: 0.9rem;
    }
  </style>
</head>
<body>
  <h1>Egress queue</h1>
  <div id="meta" class="meta">Loading...</div>
  <div id="staleBanner" class="banner" role="status" aria-live="polite"></div>
  <table aria-label="Open egress approval requests">
    <thead>
      <tr>
        <th>Age</th>
        <th>Container</th>
        <th>Host:port</th>
        <th>UID</th>
        <th>Comm</th>
        <th>Hits</th>
        <th>Reason</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="emptyState" class="empty" style="display:none;">No open requests</div>
  <footer>
    Shows open approval requests (decisions), not currently-permitted hosts —
    the ipset allowlist is the authority.
  </footer>
  <script>
  (function () {
    const metaEl = document.getElementById("meta");
    const rowsEl = document.getElementById("rows");
    const emptyEl = document.getElementById("emptyState");
    const staleEl = document.getElementById("staleBanner");
    let staleSince = null;

    function text(value) {
      if (value === null || value === undefined) {
        return "";
      }
      return String(value);
    }

    function pad2(value) {
      return String(value).padStart(2, "0");
    }

    function formatLocalTime(date) {
      return (
        date.getFullYear() + "-" +
        pad2(date.getMonth() + 1) + "-" +
        pad2(date.getDate()) + " " +
        pad2(date.getHours()) + ":" +
        pad2(date.getMinutes()) + ":" +
        pad2(date.getSeconds())
      );
    }

    function parseNumber(value, fallback) {
      if (typeof value === "number" && Number.isFinite(value)) {
        return value;
      }
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }

    function humanAge(seconds) {
      const total = Math.max(0, Math.floor(parseNumber(seconds, 0)));
      if (total < 60) {
        return total + "s";
      }
      if (total < 3600) {
        return Math.floor(total / 60) + "m";
      }
      if (total < 86400) {
        return Math.floor(total / 3600) + "h";
      }
      return Math.floor(total / 86400) + "d";
    }

    function showStaleBanner() {
      if (!staleSince) {
        staleSince = new Date();
      }
      staleEl.textContent = "daemon unreachable — data stale since " + formatLocalTime(staleSince);
      staleEl.style.display = "block";
    }

    function clearStaleBanner() {
      staleSince = null;
      staleEl.style.display = "none";
      staleEl.textContent = "";
    }

    function hostPortCell(entry) {
      const wrapper = document.createElement("div");
      const host = text(entry.host);
      const port = text(entry.port);
      wrapper.append(document.createTextNode(host + ":" + port));
      if (entry.host_is_ip === true) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "IP";
        badge.title = "Approving an IP literal records the decision, but the CIDR must be added to the manifest by hand.";
        wrapper.appendChild(badge);
      }
      return wrapper;
    }

    function rowCell(textValue) {
      const td = document.createElement("td");
      td.textContent = text(textValue);
      return td;
    }

    function render(payload) {
      const requests = Array.isArray(payload.open) ? payload.open : [];
      rowsEl.replaceChildren();
      requests.forEach(function (entry) {
        const tr = document.createElement("tr");
        tr.appendChild(rowCell(humanAge(entry.age_seconds)));
        tr.appendChild(rowCell(entry.container));
        const hostCell = document.createElement("td");
        hostCell.appendChild(hostPortCell(entry));
        tr.appendChild(hostCell);
        tr.appendChild(rowCell(entry.uid));
        tr.appendChild(rowCell(entry.comm));
        tr.appendChild(rowCell(entry.hit_count));
        tr.appendChild(rowCell(entry.reason));
        rowsEl.appendChild(tr);
      });
      emptyEl.style.display = requests.length === 0 ? "block" : "none";

      const generatedAt = text(payload.generated_at);
      const openCount = parseNumber(payload.count, requests.length);
      metaEl.textContent = "Open: " + openCount + " | generated_at: " + generatedAt;
    }

    async function poll() {
      try {
        const response = await fetch("/api/queue", { cache: "no-store" });
        if (!response.ok) {
          throw new Error("status " + response.status);
        }
        const payload = await response.json();
        render(payload);
        clearStaleBanner();
      } catch (_err) {
        showStaleBanner();
      }
    }

    poll();
    setInterval(poll, 2000);
  })();
  </script>
</body>
</html>
"""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class EgressQueueHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying runtime root and host-only operator token."""

    def __init__(self, server_address: tuple[str, int], egress_root: Path, operator_token: str) -> None:
        self.egress_root = egress_root
        self.operator_token = operator_token
        super().__init__(server_address, EgressQueueRequestHandler)


class EgressQueueRequestHandler(BaseHTTPRequestHandler):
    server: EgressQueueHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("egress queue http %s - %s", self.address_string(), format % args)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        LOG.info("egress queue response out status=%d bytes=%d", status, len(body))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(body, separators=(",", ":")).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        LOG.info("egress queue request enter path=%s", self.path)
        if self.path == "/":
            self._send_bytes(HTTPStatus.OK, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/api/queue":
            self._handle_api_queue()
            return
        self._send_bytes(HTTPStatus.NOT_FOUND, NOT_FOUND_BODY, "application/json")

    def _handle_api_queue(self) -> None:
        started = time.monotonic()
        endpoint = read_daemon_endpoint(self.server.egress_root)
        if endpoint is None:
            duration_ms = int((time.monotonic() - started) * 1000)
            LOG.info("egress queue upstream call status=unreachable duration_ms=%d bytes=0", duration_ms)
            self._send_bytes(HTTPStatus.SERVICE_UNAVAILABLE, UNREACHABLE_BODY, "application/json")
            return

        base_url = daemon_base_url(self.server.egress_root)
        request = urllib.request.Request(
            f"{base_url}/queue",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.server.operator_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                body = response.read()
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            duration_ms = int((time.monotonic() - started) * 1000)
            LOG.info(
                "egress queue upstream call status=%d duration_ms=%d bytes=%d",
                exc.code,
                duration_ms,
                len(body),
            )
            self._send_bytes(HTTPStatus.SERVICE_UNAVAILABLE, UNREACHABLE_BODY, "application/json")
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            duration_ms = int((time.monotonic() - started) * 1000)
            LOG.info("egress queue upstream call status=unreachable duration_ms=%d bytes=0", duration_ms)
            self._send_bytes(HTTPStatus.SERVICE_UNAVAILABLE, UNREACHABLE_BODY, "application/json")
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        LOG.info(
            "egress queue upstream call status=%d duration_ms=%d bytes=%d",
            status,
            duration_ms,
            len(body),
        )
        if status != HTTPStatus.OK:
            self._send_bytes(HTTPStatus.SERVICE_UNAVAILABLE, UNREACHABLE_BODY, "application/json")
            return
        self._send_bytes(HTTPStatus.OK, body, "application/json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="read-only egress queue page")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host (loopback only)")
    return parser


def _require_djinn_home() -> Path:
    raw = os.environ.get("DJINN_HOME", "").strip()
    if not raw:
        raise RuntimeError("DJINN_HOME is required")
    return Path(raw).expanduser()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    if not _is_loopback_host(args.host):
        parser.error(
            "refusing non-loopback --host: this page is unauthenticated and loopback-only is its security boundary"
        )
    try:
        djinn_home = _require_djinn_home()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    egress_root = djinn_home / "run" / "egress"
    operator_token = ensure_operator_token(egress_root)
    server = EgressQueueHTTPServer((args.host, args.port), egress_root, operator_token)
    LOG.info("egress queue listen host=%s port=%d", args.host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
