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
import queue
import secrets
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
REASON_MAX_CHARS = 200
TOKEN_RACE_SLEEP_SECONDS = 0.2
TOKEN_REJECTED_ERROR = "operator token rejected by daemon; restart djinn admin"
UNREACHABLE_ERROR = "egress daemon unreachable"
RECENT_QUERY_PARAMS = frozenset({"before", "limit", "container", "since", "until"})
CONTAINER_MARKER_ENV = "DJINN_CONTAINER"
ADMIN_UI_ENV = "DJINN_ADMIN_UI"
ADMIN_UI_SPA_VALUE = "spa"
ADMIN_UI_DIST_ENV = "DJINN_ADMIN_UI_DIST"
SPA_BUILD_MANIFEST = ".build-inputs.json"   # admin/ui/scripts/build_inputs.py writes it into dist/; never served

# Live queue stream (spa mode only): GET /api/egress/stream.
STREAM_PATH = "/api/egress/stream"
STREAM_MAX = 8                        # concurrent streams; the ninth gets 503
STREAM_POLL_SECONDS = 2.0             # upstream /queue poll, only while a stream is open
STREAM_HEARTBEAT_SECONDS = 15.0       # `: hb` comment on an idle stream
STREAM_FAILURE_LIMIT = 2              # consecutive failed polls before the streams are ended
STREAM_WRITE_TIMEOUT_SECONDS = 10.0   # a client that stops reading is treated as gone
STREAM_FULL_ERROR = "too many live streams"

# Per-run session secret, created host-side by `djinn egress start` (never in
# secrets.env, never mounted into a bottle). GET /session?key=<secret> is the
# only thing that mints the session cookie, so a page that never learned the
# key — a bottle reaching the published port over the host gateway, or a
# hostile web page — gets the pointer page and no cookie.
ADMIN_KEY_FILENAME = "admin.key"
ADMIN_KEY_ENV = "EGRESS_ADMIN_KEY"

POINTER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#1b3a4b">
  <title>Egress queue - Djinn admin</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem; }
    main { max-width: 40rem; margin: 0 auto; }
    code { word-break: break-all; }
  </style>
</head>
<body>
  <main>
    <h1>Egress admin</h1>
    <p>This page must be opened from the session URL the egress service
    prints. On the djinn host run:</p>
    <p><code>./djinn egress url</code></p>
    <p>and open the URL it prints (it carries the one-time session key this
    page is guarded by). Opening the bare page address sets nothing.</p>
  </main>
</body>
</html>
"""

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

_SRC_DIR = Path(__file__).resolve().parent
_APP_JS_BYTES = (_SRC_DIR / "admin_app.js").read_bytes()
_VENDOR_JS_PATH = _SRC_DIR / "admin_vendor" / "htm-preact-standalone-3.1.1.module.js"
_VENDOR_JS_BYTES = _VENDOR_JS_PATH.read_bytes()

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
    .group-row { background: color-mix(in srgb, var(--panel) 70%, var(--line)); font-weight: 600; }
    .recent-heading { font-size: 14px; margin: 0 0 8px; }
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
    /* Phone width: a request row stacks its four cells so the actions never
       push the table past the viewport; the column name becomes a label. */
    @media (max-width: 640px) {
      table thead { display: none; }
      table tr { display: block; border-bottom: 1px solid var(--line); padding: 6px 0; }
      table tr.group-row { padding: 6px; }
      table td { display: block; border-bottom: none; padding: 3px 6px; }
      table td[data-label]::before { content: attr(data-label) ": "; color: var(--muted); font-size: 12px; }
    }
  </style>
</head>
<body>
  <div id="appMount"></div>
  <script type="module" src="/app.js"></script>
</body>
</html>
"""

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

SW_JS = """const CACHE_VERSION = "djinn-admin-shell-v3";
const SHELL_PATHS = [
  "/app.js",
  "/vendor/htm-preact-standalone.module.js",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png"
];

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

  // "/" is NEVER cached: without a session cookie it is the pointer page, and
  // a cached pointer page would masquerade as the app shell after a session
  // expires. Only the asset routes below are cached.
  if (url.pathname === "/") {
    event.respondWith(fetch(req));
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


_SPA_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _content_type_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return _SPA_CONTENT_TYPES.get(ext, "application/octet-stream")


def _default_spa_dist() -> Path:
    return _SRC_DIR.parent / "admin" / "ui" / "dist"


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


def ensure_admin_key(egress_root: Path) -> str:
    """Create or return the per-run session key (run/egress/admin.key).

    Same create-race tolerance as the operator token: O_EXCL so a racing
    admin daemon cannot clobber the file, and a loser of the race re-reads
    the winner's value. Created host-side by `djinn egress start` BEFORE the
    containers come up, so the file stays operator-owned on the host and the
    host-side `djinn egress url` can read it without root-in-container
    ownership getting in the way.
    """
    key_path = egress_root / ADMIN_KEY_FILENAME
    if key_path.is_file():
        try:
            key = key_path.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
    key = secrets.token_urlsafe(32)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        time.sleep(TOKEN_RACE_SLEEP_SECONDS)
        try:
            key = key_path.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if key:
            return key
        raise RuntimeError(f"admin session key unavailable ({key_path})") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    LOG.info("admin session key created path=%s", key_path.name)
    return key


def read_admin_key(egress_root: Path) -> str:
    """Read the existing session key; RuntimeError (naming `start`) when the
    file is missing or empty. Never creates one — `djinn egress url` must not
    mint a key the running admin daemon has not loaded."""
    key_path = egress_root / ADMIN_KEY_FILENAME
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"no admin session key at {key_path} — run: ./djinn egress start"
        ) from None
    except OSError as exc:
        raise RuntimeError(f"cannot read {key_path}: {exc}") from exc
    if not key:
        raise RuntimeError(
            f"admin session key file is empty at {key_path} — restart: ./djinn egress start"
        )
    return key


def session_url(key: str, port: int = DEFAULT_PORT) -> str:
    """The URL `djinn egress start`/`url` print: one page-load that mints the
    session cookie and lands on the app. The key is token_urlsafe, so it is
    already URL-safe without quoting."""
    return f"http://127.0.0.1:{port}/session?key={key}"


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


def _queue_change_key(snapshot: dict[str, Any]) -> str:
    """What makes one queue snapshot differ from the last, for the live stream.

    `generated_at` is the poll's own clock and each open row's `age_seconds`
    grows with it: both change on every poll, and the app derives a row's age
    from `opened_at`, so neither is news. Everything else is: rows opening or
    closing, hit counts, apply attempts and errors, the recent list.
    """
    stripped = {name: value for name, value in snapshot.items() if name != "generated_at"}
    rows = snapshot.get("open")
    if isinstance(rows, list):
        stripped["open"] = [
            {name: value for name, value in row.items() if name != "age_seconds"}
            if isinstance(row, dict) else row
            for row in rows
        ]
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"))


def _queue_frame(snapshot: dict[str, Any]) -> bytes:
    """One `queue` event: the whole snapshot as a single data line (the JSON is compact, so it has no newline)."""
    return b"event: queue\ndata: " + _json_bytes(snapshot) + b"\n\n"


class StreamUnavailable(Exception):
    """A stream could not be opened: `status` and the `error_response` message to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_CLOSE = object()   # queued to a stream to tell its handler to end


class _Stream:
    """One open browser stream: a mailbox its handler thread drains.

    The handler sleeps in select() on its client socket and on `wake_r`, so a
    frame reaches it at once and a client that goes away is noticed at once (a
    tab that reloads must not keep its old stream's slot for a poll interval).
    """

    def __init__(self) -> None:
        self.mailbox: queue.Queue[Any] = queue.Queue()
        self.ready = False   # set once its first frame is queued; only ready streams receive fan-out
        self.opened = time.monotonic()
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.wake_w.setblocking(False)

    def push(self, item: Any) -> None:
        self.mailbox.put(item)
        try:
            self.wake_w.send(b"x")
        except BlockingIOError:
            pass   # a wake-up is already pending
        except OSError:
            pass   # the handler has finished and closed its end

    def drain_wake(self) -> None:
        try:
            self.wake_r.recv(4096)
        except (BlockingIOError, OSError):
            pass

    def release(self) -> None:
        self.wake_r.close()
        self.wake_w.close()


class QueueStreamHub:
    """Fans the broker's queue out to every open browser stream.

    ONE poller thread asks the broker for /queue every `poll_seconds`, only while
    a stream is open, and hands each stream the whole snapshot when it changed
    (see `_queue_change_key`). A stream that connects gets the current snapshot
    at once: the cached one when it is under a poll interval old, else a fresh
    fetch. `max_streams` caps the open streams; `open()` raises
    StreamUnavailable(503) beyond it. A poll that fails leaves the streams open
    and emits nothing; after `failure_limit` in a row the streams are ended, so
    each tab falls back to polling `/api/egress/queue` and its stale banner
    tells the operator the list is out of date. A connect while the broker is
    unreachable is refused with 503 for the same reason.
    """

    def __init__(
        self,
        fetch: Callable[[], tuple[int, dict[str, Any], int]],
        *,
        max_streams: int = STREAM_MAX,
        poll_seconds: float = STREAM_POLL_SECONDS,
        failure_limit: int = STREAM_FAILURE_LIMIT,
    ) -> None:
        self._fetch = fetch
        self.max_streams = max_streams
        self.poll_seconds = poll_seconds
        self.failure_limit = failure_limit
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._fetch_lock = threading.Lock()   # one upstream fetch at a time, poller or connect
        self._streams: list[_Stream] = []
        self._poller: threading.Thread | None = None
        self._closed = False
        self._key: str | None = None
        self._frame: bytes | None = None
        self._fetched_at = 0.0
        self._failures = 0

    def stream_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def open(self) -> _Stream:
        """Reserve a slot, get the current snapshot, queue it as the stream's first frame."""
        with self._lock:
            if self._closed:
                raise StreamUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, "admin shutting down")
            if len(self._streams) >= self.max_streams:
                LOG.info("admin stream refused streams=%d max=%d", len(self._streams), self.max_streams)
                raise StreamUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, STREAM_FULL_ERROR)
            stream = _Stream()
            self._streams.append(stream)
        try:
            self._ensure_fresh()
            with self._lock:
                if self._closed:
                    raise StreamUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, "admin shutting down")
                assert self._frame is not None
                stream.push(self._frame)
                stream.ready = True
                if self._poller is None:
                    self._poller = threading.Thread(
                        target=self._poll_loop, name="admin-sse-poller", daemon=True
                    )
                    self._poller.start()
        except BaseException:
            self.close_stream(stream)
            stream.release()
            raise
        return stream

    def close_stream(self, stream: _Stream) -> None:
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)
            self._cond.notify_all()

    def end_streams(self, reason: str) -> None:
        """End every open stream (its client sees the connection close and reopens); the hub stays usable."""
        with self._lock:
            self._end_all_locked(reason)

    def close(self) -> None:
        """Daemon shutdown: end every stream and stop the poller."""
        with self._lock:
            self._closed = True
            self._end_all_locked("shutdown")

    def _end_all_locked(self, reason: str) -> None:
        ended = self._streams
        self._streams = []
        for stream in ended:
            stream.push(_CLOSE)
        self._cond.notify_all()
        if ended:
            LOG.info("admin stream end streams=%d reason=%s", len(ended), reason)

    def _ensure_fresh(self) -> None:
        with self._lock:
            fresh = self._frame is not None and time.monotonic() - self._fetched_at < self.poll_seconds
        if fresh:
            return
        if not self._refresh():
            raise StreamUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, UNREACHABLE_ERROR)

    def _refresh(self) -> bool:
        """One upstream poll; fan the snapshot out to the ready streams when it changed."""
        with self._fetch_lock:
            started = time.monotonic()
            status, snapshot, size = 0, {}, 0
            try:
                status, snapshot, size = self._fetch()
            except Exception as exc:  # noqa: BLE001 - any failure of the poll is a failed poll, never a dead poller
                LOG.info("admin stream poll failed error=%s", type(exc).__name__)
            duration_ms = int((time.monotonic() - started) * 1000)
            if status != HTTPStatus.OK or not isinstance(snapshot.get("open"), list):
                with self._lock:
                    self._failures += 1
                    failures = self._failures
                LOG.info(
                    "admin stream poll status=%d duration_ms=%d bytes=%d ok=false consecutive_failures=%d",
                    status, duration_ms, size, failures,
                )
                return False
            key, frame = _queue_change_key(snapshot), _queue_frame(snapshot)
            with self._lock:
                self._failures = 0
                changed = key != self._key
                self._key, self._frame, self._fetched_at = key, frame, time.monotonic()
                targets = [stream for stream in self._streams if stream.ready] if changed else []
            for stream in targets:
                stream.push(frame)
            LOG.info(
                "admin stream poll status=%d duration_ms=%d bytes=%d ok=true changed=%s fanout=%d",
                status, duration_ms, size, str(changed).lower(), len(targets),
            )
            return True

    def _poll_loop(self) -> None:
        me = threading.current_thread()
        LOG.info("admin stream poller start interval_s=%s", self.poll_seconds)
        while True:
            deadline = time.monotonic() + self.poll_seconds
            with self._lock:
                while not self._closed and self._streams and time.monotonic() < deadline:
                    self._cond.wait(timeout=max(0.0, deadline - time.monotonic()))
                if self._closed or not self._streams:
                    if self._poller is me:
                        self._poller = None
                    LOG.info("admin stream poller stop")
                    return
            if not self._refresh():
                with self._lock:
                    if self._failures >= self.failure_limit:
                        self._end_all_locked("upstream unreachable")


class AdminHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying admin-plane state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        egress_root: Path,
        session_secret: str,
        operator_token: str,
        admin_key: str,
        stream_max: int = STREAM_MAX,
        stream_poll_seconds: float = STREAM_POLL_SECONDS,
        stream_heartbeat_seconds: float = STREAM_HEARTBEAT_SECONDS,
        stream_failure_limit: int = STREAM_FAILURE_LIMIT,
    ):
        self.address_family = address_family_for_host(server_address[0])
        self.egress_root = egress_root
        self.session_secret = session_secret
        self.operator_token = operator_token
        self.admin_key = admin_key
        self.app_js = _APP_JS_BYTES
        self.vendor_js = _VENDOR_JS_BYTES
        self.spa_mode = os.environ.get(ADMIN_UI_ENV) == ADMIN_UI_SPA_VALUE
        self.spa_dist = Path(os.environ.get(ADMIN_UI_DIST_ENV) or _default_spa_dist())
        self.spa_index: bytes | None = None
        self.spa_allowlist: dict[str, tuple[Path, str]] = {}
        if self.spa_mode:
            self._load_spa()
        self.stream_heartbeat_seconds = stream_heartbeat_seconds
        # The live queue stream exists in spa mode only; legacy answers its path 404 like any unknown one.
        self.stream_hub: QueueStreamHub | None = (
            QueueStreamHub(
                self._fetch_queue,
                max_streams=stream_max,
                poll_seconds=stream_poll_seconds,
                failure_limit=stream_failure_limit,
            )
            if self.spa_mode
            else None
        )
        super().__init__(server_address, AdminRequestHandler)

    def _fetch_queue(self) -> tuple[int, dict[str, Any], int]:
        return _upstream_json(
            base_url=daemon_base_url(self.egress_root),
            method="GET",
            path="/queue",
            token=self.operator_token,
            body=None,
            timeout=UPSTREAM_TIMEOUT_SECONDS,
        )

    def shutdown(self) -> None:
        if self.stream_hub is not None:
            self.stream_hub.close()
        super().shutdown()

    def server_close(self) -> None:
        if self.stream_hub is not None:
            self.stream_hub.close()
        super().server_close()

    def _load_spa(self) -> None:
        if not self.spa_dist.is_dir():
            LOG.error("admin spa dist missing path=%s", self.spa_dist)
            return
        dist = self.spa_dist.resolve()
        index_path = dist / "index.html"
        if index_path.is_symlink() or not index_path.is_file():
            LOG.error("admin spa index missing or not a regular file path=%s", index_path)
            return
        try:
            self.spa_index = index_path.read_bytes()
        except OSError as exc:
            LOG.error("admin spa index unreadable path=%s error=%s", index_path, exc)
            return
        total_bytes = 0
        skipped = 0
        for path in sorted(dist.rglob("*")):
            rel = path.relative_to(dist)
            if rel.as_posix() == "index.html" or (path.is_dir() and not path.is_symlink()):
                continue
            # Only regular, non-hidden files that really live under dist are
            # served: a symlink (to a file or a directory) or a dotfile in the
            # build output is dropped, so nothing outside dist can be reached.
            hidden = any(part.startswith(".") for part in rel.parts)
            if rel.as_posix() == SPA_BUILD_MANIFEST and path.is_file() and not path.is_symlink():
                # The build's own record of its inputs: known, never served, and no news at startup.
                skipped += 1
                LOG.debug("admin spa skip path=%s (build manifest)", rel.as_posix())
                continue
            resolved = path.resolve()
            if (
                hidden
                or path.is_symlink()
                or not path.is_file()
                or not resolved.is_relative_to(dist)
            ):
                skipped += 1
                LOG.warning("admin spa skip path=%s", rel.as_posix())
                continue
            url_path = "/" + rel.as_posix()
            self.spa_allowlist[url_path] = (resolved, _content_type_for(url_path))
            total_bytes += resolved.stat().st_size
        LOG.info(
            "admin spa mode enabled dist=%s files=%d bytes=%d skipped=%d",
            dist,
            len(self.spa_allowlist),
            total_bytes,
            skipped,
        )

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            LOG.info("admin client disconnect host=%s port=%s", client_address[0], client_address[1])
            return
        super().handle_error(request, client_address)


class AdminRequestHandler(BaseHTTPRequestHandler):
    """Routes app shell/static and egress panel API calls."""

    server: AdminHTTPServer  # type: ignore[assignment]
    # Set by do_HEAD: headers are sent as for GET, the body is not.
    _suppress_body = False

    def log_message(self, format: str, *args: Any) -> None:
        # The request line carries the full request target — a
        # `GET /session?key=…` would otherwise drop the session key into the
        # log. Redact at this single choke point: the first argument of the
        # standard log_request format is the request line, so strip its query
        # string before rendering. Any other format passes through untouched.
        if args and format.startswith('"%s"') and isinstance(args[0], str):
            args = (args[0].split("?", 1)[0],) + tuple(args[1:])
        LOG.info("admin http %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        # Legacy mode keeps BaseHTTPRequestHandler's answer for a method it
        # never implemented; spa mode answers HEAD exactly like GET, minus
        # the body, through the same routing and session gate.
        if not self.server.spa_mode:
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command!r})")
            return
        self._suppress_body = True
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if self.server.spa_mode:
            self._dispatch_spa(method, path)
            return
        routes: dict[tuple[str, str], Callable[[], None]] = {
            ("GET", "/"): self._handle_root,
            ("GET", "/health"): self._handle_health,
            ("GET", "/session"): self._handle_session_get,
            ("GET", "/app.js"): self._handle_app_js,
            ("GET", "/vendor/htm-preact-standalone.module.js"): self._handle_vendor_js,
            ("GET", "/manifest.webmanifest"): self._handle_manifest,
            ("GET", "/sw.js"): self._handle_sw,
            ("GET", "/icon-192.png"): self._handle_icon_192,
            ("GET", "/icon-512.png"): self._handle_icon_512,
            ("GET", "/api/egress/queue"): self._handle_egress_queue_get,
            ("GET", "/api/egress/recent"): self._handle_egress_recent_get,
            ("POST", "/api/egress/decide"): self._handle_egress_decide_post,
        }
        handler = routes.get((method, path))
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        handler()

    def _dispatch_spa(self, method: str, path: str) -> None:
        if method == "GET":
            if path in self.server.spa_allowlist:
                self._handle_spa_asset(path)
                return
            if path in ("/", "/egress", "/denylist", "/bottles", "/backup"):
                self._handle_spa_app_route()
                return
            if path == "/health":
                self._handle_health()
                return
            if path == "/session":
                self._handle_session_get()
                return
            if path == "/api/egress/queue":
                self._handle_egress_queue_get()
                return
            if path == "/api/egress/recent":
                self._handle_egress_recent_get()
                return
            if path == STREAM_PATH:
                self._handle_egress_stream()
                return
            if path in (
                "/app.js",
                "/vendor/htm-preact-standalone.module.js",
                "/manifest.webmanifest",
                "/sw.js",
                "/icon-192.png",
                "/icon-512.png",
            ):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
        elif method == "POST" and path == "/api/egress/decide":
            self._handle_egress_decide_post()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_spa_app_route(self) -> None:
        if self.server.spa_index is None:
            self._send_bytes(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b"admin UI not built",
                content_type="text/plain; charset=utf-8",
            )
            return
        if self._cookie_matches():
            self._send_bytes(
                HTTPStatus.OK,
                self.server.spa_index,
                content_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            POINTER_HTML.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    def _handle_spa_asset(self, path: str) -> None:
        file_path, content_type = self.server.spa_allowlist[path]
        body = file_path.read_bytes()
        cache = "public, max-age=31536000, immutable" if path.startswith("/assets/") else "no-cache"
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type=content_type,
            headers={"Cache-Control": cache},
        )

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
        if not self._suppress_body:
            self.wfile.write(body)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        self._send_bytes(status, _json_bytes(body), content_type="application/json")

    MAX_REQUEST_BODY_BYTES = 64 * 1024

    def _request_json_body(self) -> tuple[dict[str, Any] | None, int]:
        # Unvalidated client input even after the session gate: a malformed
        # or hostile Content-Length must become a JSON 400, not a ValueError
        # traceback that closes the connection with no response.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, 0
        if length < 0 or length > self.MAX_REQUEST_BODY_BYTES:
            return None, 0
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
        # No cookie is ever set here: a browser (or a bottle over the host
        # gateway) landing on the bare page address gets either the app (a
        # session it already holds) or the pointer page — never a session.
        if self._cookie_matches():
            self._send_bytes(
                HTTPStatus.OK,
                APP_HTML.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            POINTER_HTML.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    def _handle_session_get(self) -> None:
        # `djinn egress url` prints this route with the per-run key: one
        # page-load that mints the session cookie and lands on the app. The
        # key never appears in a log line (log_message strips the query) and
        # a wrong or missing key gets 403 with no cookie.
        provided = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query
        ).get("key", [""])[0]
        if provided and hmac.compare_digest(provided, self.server.admin_key):
            self._send_bytes(
                HTTPStatus.FOUND,
                b"",
                content_type="text/html; charset=utf-8",
                headers={
                    "Location": "/",
                    "Set-Cookie": (
                        f"{SESSION_COOKIE_NAME}={self.server.session_secret}; "
                        "SameSite=Strict; Path=/; HttpOnly"
                    ),
                },
            )
            LOG.info("admin session granted path=/session")
            return
        LOG.info("admin session refused path=/session reason=%s", "missing" if not provided else "mismatch")
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})

    def _handle_health(self) -> None:
        # Liveness for `./djinn egress status` and the compose health wait:
        # no session, no cookie, no upstream call, nothing revealed. The
        # broker answers the same route; without this the status line read
        # "unreachable" for a perfectly healthy admin.
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def _handle_manifest(self) -> None:
        body = json.dumps(MANIFEST, separators=(",", ":")).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, body, content_type="application/manifest+json")

    def _handle_app_js(self) -> None:
        self._send_bytes(
            HTTPStatus.OK,
            self.server.app_js,
            content_type="text/javascript; charset=utf-8",
        )

    def _handle_vendor_js(self) -> None:
        self._send_bytes(
            HTTPStatus.OK,
            self.server.vendor_js,
            content_type="text/javascript; charset=utf-8",
        )

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
        self._proxy_egress_get(path="/api/egress/queue", upstream_path="/queue")

    def _handle_egress_recent_get(self) -> None:
        raw_query = self.path.partition("?")[2]
        # Only the five history parameters reach the broker; anything else the
        # browser sends (or a forged link adds) is dropped here, so the admin
        # never widens what the broker's /recent is asked.
        params = urllib.parse.parse_qsl(raw_query)
        kept: dict[str, str] = {}
        for name, value in params:
            if name in RECENT_QUERY_PARAMS and name not in kept:
                kept[name] = value
        LOG.info(
            "admin recent query params_in=%d params_forwarded=%d",
            len(params),
            len(kept),
        )
        query = urllib.parse.urlencode(kept)
        self._proxy_egress_get(
            path="/api/egress/recent",
            upstream_path="/recent" + (f"?{query}" if query else ""),
            pass_400=True,
        )

    def _handle_egress_stream(self) -> None:
        """Server-sent events: the queue snapshot on connect and on every change (see QueueStreamHub)."""
        LOG.info("admin request enter method=GET path=%s bytes=0", STREAM_PATH)
        if not self._cookie_matches():
            LOG.info(
                "admin session gate failed check=%s",
                "cookie_missing" if not self._read_session_cookie() else "cookie_mismatch",
            )
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        hub = self.server.stream_hub
        assert hub is not None
        if self._suppress_body:   # HEAD: the headers a GET would open with, no stream
            self._send_bytes(HTTPStatus.OK, b"", content_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-store"})
            return
        try:
            stream = hub.open()
        except StreamUnavailable as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        self.close_connection = True
        heartbeat = self.server.stream_heartbeat_seconds
        frames = sent = 0
        reason = "closed"
        try:
            self.connection.settimeout(STREAM_WRITE_TIMEOUT_SECONDS)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            LOG.info("admin stream open streams=%d", hub.stream_count())
            last_write = time.monotonic()
            while True:
                due = max(0.0, heartbeat - (time.monotonic() - last_write))
                readable, _, _ = select.select([self.connection, stream.wake_r], [], [], due)
                if self.connection in readable:
                    # An EventSource sends nothing after its request: a read is the client closing (or noise to discard).
                    if self.connection.recv(4096) == b"":
                        reason = "client_closed"
                        break
                if stream.wake_r in readable:
                    stream.drain_wake()
                    item = None
                    while True:   # a burst is one frame: every frame is the whole snapshot, so the last one wins
                        try:
                            later = stream.mailbox.get_nowait()
                        except queue.Empty:
                            break
                        if later is _CLOSE:
                            item = _CLOSE
                            break
                        item = later
                    if item is _CLOSE:
                        reason = "ended"
                        break
                    if item is not None:
                        self.wfile.write(item)
                        frames += 1
                        sent += len(item)
                        last_write = time.monotonic()
                elif not readable:
                    self.wfile.write(b": hb\n\n")
                    sent += 5
                    last_write = time.monotonic()
        except OSError as exc:   # BrokenPipeError, ConnectionResetError, a write that timed out
            reason = f"socket_error:{type(exc).__name__}"
        finally:
            hub.close_stream(stream)
            stream.release()
            LOG.info(
                "admin stream close streams=%d duration_ms=%d frames=%d bytes=%d reason=%s",
                hub.stream_count(),
                int((time.monotonic() - stream.opened) * 1000),
                frames,
                sent,
                reason,
            )

    def _proxy_egress_get(
        self, *, path: str, upstream_path: str, pass_400: bool = False
    ) -> None:
        LOG.info("admin request enter method=GET path=%s bytes=0", path)
        # The read needs the session too: without this a bottle that can
        # reach the published port over the host gateway (Docker Desktop) reads
        # every open request with a forged nothing. The page's own fetch is
        # same-origin and sends the cookie. POST-only checks (content type,
        # X-Admin-UI, origin/host) stay POST-only — a GET carries no body.
        if not self._cookie_matches():
            LOG.info(
                "admin session gate failed check=%s",
                "cookie_missing" if not self._read_session_cookie() else "cookie_mismatch",
            )
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        try:
            status, upstream, _bytes = _upstream_json(
                base_url=self._daemon_base_url(),
                method="GET",
                path=upstream_path,
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
            LOG.warning("admin upstream auth rejected path=%s", upstream_path.split("?", 1)[0])
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": TOKEN_REJECTED_ERROR})
            return
        if pass_400 and status == HTTPStatus.BAD_REQUEST:
            # The broker's validation message about the caller's own query
            # (a malformed cursor or date), not an upstream failure.
            error = upstream.get("error")
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": error if isinstance(error, str) else "bad request"},
            )
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

    def _cookie_matches(self) -> bool:
        """Cookie present and equal to this run's secret (constant time)."""
        cookie = self._read_session_cookie()
        if not cookie:
            return False
        return hmac.compare_digest(cookie, self.server.session_secret)

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
        if not self._cookie_matches():
            if not self._read_session_cookie():
                LOG.info("admin session gate failed check=cookie_missing")
            else:
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
    admin_key = ensure_admin_key(egress_root)
    session_secret = secrets.token_urlsafe(32)
    server = AdminHTTPServer(
        (host, port),
        egress_root=egress_root,
        session_secret=session_secret,
        operator_token=operator_token,
        admin_key=admin_key,
    )
    LOG.info(
        "admin daemon listen host=%s port=%d family=%s",
        host,
        server.server_address[1],
        "AF_INET6" if server.address_family == socket.AF_INET6 else "AF_INET",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="djinn admin daemon")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--bind-any",
        action="store_true",
        help=(
            "bind 0.0.0.0 instead of loopback — refused unless DJINN_CONTAINER=1 "
            "is set (the docker service binds all interfaces inside the container "
            "and publishes 127.0.0.1 on the host)"
        ),
    )
    return parser


def _egress_root_from_env(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = env.get("DJINN_HOME", "").strip()
    if not home:
        raise RuntimeError("DJINN_HOME is required")
    return Path(home).expanduser() / "run" / "egress"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bind_any:
        if os.environ.get(CONTAINER_MARKER_ENV) != "1":
            print(
                "Error: --bind-any refused: it is only valid inside the egress "
                "admin container (DJINN_CONTAINER=1) — run ./djinn egress start",
                file=sys.stderr,
            )
            return 1
        host = "0.0.0.0"
    else:
        host = args.host
        if not _is_loopback_value(host):
            print(f"Error: --host must be loopback (got {host!r})", file=sys.stderr)
            return 1
    try:
        egress_root = _egress_root_from_env()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        run_daemon(host=host, port=args.port, egress_root=egress_root)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"Error: cannot bind {host}:{args.port} (is djinn admin already running?)",
                file=sys.stderr,
            )
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
