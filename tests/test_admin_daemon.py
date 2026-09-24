#!/usr/bin/env python3
"""Unit tests for the djinn admin plane daemon."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

import admin_daemon as admin  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402


class _StubBrokerState:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.queue_status = 200
        self.queue_body: dict[str, object] = {
            "open": [
                {
                    "request_id": "req-1",
                    "container": "coding-brassbottle",
                    "host": "api.example.com",
                    "port": 443,
                    "host_is_ip": False,
                    "opened_at": "2026-08-31T12:00:00Z",
                    "age_seconds": 120,
                    "hit_count": 3,
                    "uid": 1000,
                    "comm": "python",
                    "reason": "build",
                }
            ],
            "count": 1,
            "generated_at": "2026-08-31T12:00:00Z",
        }
        self.decide_status = 200
        self.decide_body: dict[str, object] = {"decided": ["req-1"], "apply_failures": []}
        self._lock = threading.Lock()

    def record(self, call: dict[str, object]) -> None:
        with self._lock:
            self.calls.append(call)

    def reset_calls(self) -> None:
        with self._lock:
            self.calls = []

    def snapshot_calls(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self.calls)


class StubBrokerHandler(BaseHTTPRequestHandler):
    server: "StubBrokerHTTPServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path != "/queue":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.server.state.record(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": None,
            }
        )
        self._send(self.server.state.queue_status, self.server.state.queue_body)

    def do_POST(self) -> None:
        if self.path != "/decide":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8")) if raw else {}
        self.server.state.record(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "body": body,
            }
        )
        self._send(self.server.state.decide_status, self.server.state.decide_body)


class StubBrokerHTTPServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], state: _StubBrokerState) -> None:
        self.state = state
        super().__init__(server_address, StubBrokerHandler)


class AdminDaemonTests(unittest.TestCase):
    def _start_stub(self, state: _StubBrokerState) -> tuple[StubBrokerHTTPServer, threading.Thread]:
        server = StubBrokerHTTPServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for_tcp_listening(*server.server_address)
        return server, thread

    def _start_admin(
        self,
        home: Path,
        *,
        host: str = "127.0.0.1",
        env: dict[str, str] | None = None,
        session_secret: str = "session-secret",
        operator_token: str = "operator-test-token",
        admin_key: str = "admin-test-key",
    ) -> tuple[admin.AdminHTTPServer, threading.Thread]:
        egress_root = home / "run" / "egress"
        egress_root.mkdir(parents=True, exist_ok=True)
        token_path = egress_root / admin.OPERATOR_TOKEN_FILENAME
        token_path.write_text(operator_token + "\n", encoding="utf-8")
        env_map = {"DJINN_HOME": str(home)}
        if env:
            env_map.update(env)
        patcher = mock.patch.dict(os.environ, env_map, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        server = admin.AdminHTTPServer(
            (host, 0),
            egress_root=egress_root,
            session_secret=session_secret,
            operator_token=operator_token,
            admin_key=admin_key,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for_tcp_listening(server.server_address[0], server.server_address[1])
        return server, thread

    def _request(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str], bytes]:
        conn = HTTPConnection(host, port, timeout=5)
        body_bytes: str | None = None
        send_headers = dict(headers or {})
        if body is not None:
            body_bytes = json.dumps(body)
            send_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body_bytes, send_headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k: v for k, v in resp.getheaders()}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        conn.close()
        return resp.status, payload, resp_headers, raw

    def _session_cookie(self, host: str, port: int) -> str:
        status, _payload, headers, _raw = self._request(host, port, "GET", "/")
        self.assertEqual(status, HTTPStatus.OK)
        cookie_raw = headers.get("Set-Cookie", "")
        parsed = SimpleCookie()
        parsed.load(cookie_raw)
        morsel = parsed.get(admin.SESSION_COOKIE_NAME)
        self.assertIsNotNone(morsel)
        return morsel.value

    def test_host_must_be_loopback(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = admin.main(["--host", "10.8.0.5", "--port", "8817"])
        self.assertEqual(rc, 1)
        self.assertIn("loopback", err.getvalue())

    @unittest.skipUnless(socket.has_ipv6, "no IPv6 support on this host")
    def test_ipv6_loopback_server_address_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            server, thread = self._start_admin(home, host="::1")
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_djinn_home_required(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("sys.stderr", err):
                rc = admin.main([])
        self.assertEqual(rc, 1)
        self.assertIn("DJINN_HOME", err.getvalue())

    def test_eaddrinuse_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"DJINN_HOME": str(home)}):
                with mock.patch("sys.stderr", err):
                    rc = admin.main(["--port", str(port)])
            sock.close()
        self.assertEqual(rc, 1)
        self.assertIn("is djinn admin already running?", err.getvalue())

    def test_handle_error_connection_error_one_log_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            egress_root = home / "run" / "egress"
            egress_root.mkdir(parents=True)
            server = admin.AdminHTTPServer(
                ("127.0.0.1", 0),
                egress_root=egress_root,
                session_secret="s",
                operator_token="tok",
                admin_key="k",
            )
            try:
                with self.assertLogs(admin.LOG, level="INFO") as captured:
                    try:
                        raise ConnectionError("closed")
                    except ConnectionError:
                        server.handle_error(object(), ("127.0.0.1", 40123))
                self.assertEqual(len(captured.output), 1)
                self.assertIn("client disconnect", captured.output[0])
            finally:
                server.server_close()

    def test_handle_error_other_errors_defer_to_super(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            egress_root = home / "run" / "egress"
            egress_root.mkdir(parents=True)
            server = admin.AdminHTTPServer(
                ("127.0.0.1", 0),
                egress_root=egress_root,
                session_secret="s",
                operator_token="tok",
                admin_key="k",
            )
            try:
                with mock.patch.object(ThreadingHTTPServer, "handle_error") as mocked_super:
                    try:
                        raise ValueError("boom")
                    except ValueError:
                        server.handle_error(object(), ("127.0.0.1", 40123))
                mocked_super.assert_called_once()
            finally:
                server.server_close()

    def test_root_without_cookie_serves_pointer_page_and_sets_no_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token = "never-leak-this-token"
            token_path = home / "run" / "egress" / admin.OPERATOR_TOKEN_FILENAME
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            server, thread = self._start_admin(home)
            host, port = server.server_address
            try:
                status, _payload, headers, raw = self._request(host, port, "GET", "/")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertNotIn("Set-Cookie", headers)
                text = raw.decode("utf-8")
                # the pointer page, not the app shell
                self.assertIn("./djinn egress url", text)
                self.assertNotIn("appMount", text)
                self.assertNotIn('<script type="module" src="/app.js">', text)
                self.assertNotIn(token, text)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_get_shell_with_valid_cookie_serves_app_and_sets_no_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token = "never-leak-this-token"
            token_path = home / "run" / "egress" / admin.OPERATOR_TOKEN_FILENAME
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            server, thread = self._start_admin(home)
            host, port = server.server_address
            try:
                status, _payload, headers, raw = self._request(
                    host,
                    port,
                    "GET",
                    "/",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertNotIn("Set-Cookie", headers)
                text = raw.decode("utf-8")
                self.assertNotIn(token, text)
                self.assertIn("manifest.webmanifest", text)
                self.assertIn('<script type="module" src="/app.js"></script>', text)
                self.assertNotIn("X-Admin-UI", text)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_app_js_served_as_module_text_javascript(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                status, _payload, headers, raw = self._request(host, port, "GET", "/app.js")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "text/javascript; charset=utf-8")
                text = raw.decode("utf-8")
                self.assertIn('from "/vendor/htm-preact-standalone.module.js"', text)
                self.assertIn('fetch("/api/egress/decide"', text)
                self.assertIn('"X-Admin-UI": "1"', text)
                self.assertIn("type exact host to arm global deny", text)
                self.assertIn("recorded - add CIDR to manifest by hand", text)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_vendor_module_route_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            expected = (
                REPO_ROOT / "src" / "admin_vendor" / "htm-preact-standalone-3.1.1.module.js"
            ).read_bytes()
            try:
                status, _payload, headers, raw = self._request(
                    host, port, "GET", "/vendor/htm-preact-standalone.module.js"
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "text/javascript; charset=utf-8")
                self.assertEqual(raw, expected)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_health_answers_without_session_and_without_upstream(self):
        """`./djinn egress status` probes GET /health on the admin; it must
        answer 200 {"status": "ok"} with no cookie, set no cookie, and never
        touch the broker (the stub records every upstream call)."""
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                state.reset_calls()
                status, payload, headers, _raw = self._request(host, port, "GET", "/health")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload, {"status": "ok"})
                self.assertIsNone(headers.get("Set-Cookie"))
                self.assertEqual(state.calls, [])
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_manifest_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                status, payload, _headers, _raw = self._request(host, port, "GET", "/manifest.webmanifest")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload["name"], "Djinn admin")
                self.assertEqual(payload["short_name"], "Djinn")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_sw_has_api_bypass_and_precache_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                status, _payload, _headers, raw = self._request(host, port, "GET", "/sw.js")
                self.assertEqual(status, HTTPStatus.OK)
                text = raw.decode("utf-8")
                self.assertIn("/api/", text)
                self.assertIn("startsWith(\"/api/\")", text)
                self.assertIn('"/app.js"', text)
                self.assertIn('"/vendor/htm-preact-standalone.module.js"', text)
                # "/" is excluded from the shell cache (the array must open
                # with /app.js, not with a "/" entry): without a session
                # cookie / is the pointer page, and a cached pointer page
                # would masquerade as the app shell after a session expires.
                self.assertIn('const SHELL_PATHS = [\n  "/app.js",', text)
                self.assertIn("url.pathname === \"/\"", text)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_icons_are_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                for path in ("/icon-192.png", "/icon-512.png"):
                    status, _payload, _headers, raw = self._request(host, port, "GET", path)
                    self.assertEqual(status, HTTPStatus.OK)
                    self.assertGreaterEqual(len(raw), 8)
                    self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_get_queue_requires_session_cookie(self):
        # Inverts the pre-session-queue test: /api/egress/queue proxies only
        # for a caller that holds the session cookie a bottle never gets.
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                # no cookie → 403, upstream not called
                state.reset_calls()
                status, payload, _hdrs, _raw = self._request(host, port, "GET", "/api/egress/queue")
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual(payload["error"], "forbidden")
                self.assertEqual(state.snapshot_calls(), [])

                # forged (wrong-value) cookie → 403, upstream not called
                state.reset_calls()
                status, payload, _hdrs, _raw = self._request(
                    host,
                    port,
                    "GET",
                    "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=forged"},
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual(payload["error"], "forbidden")
                self.assertEqual(state.snapshot_calls(), [])

                # the real cookie → proxies
                status, payload, _hdrs, _raw = self._request(
                    host,
                    port,
                    "GET",
                    "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload, state.queue_body)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_session_gate_matrix_blocks_without_proxying(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            server, thread = self._start_admin(
                home,
                env={"EGRESS_BROKER_URL": f"http://{stub.server_address[0]}:{stub.server_address[1]}"},
                session_secret="good-cookie",
            )
            host, port = server.server_address
            try:
                valid_headers = {
                    "Cookie": "admin_session=good-cookie",
                    "X-Admin-UI": "1",
                    "Content-Type": "application/json",
                }
                payload = {
                    "action": "deny",
                    "host": "api.example.com",
                    "container": "coding-brassbottle",
                }
                bad_cases = [
                    ("missing_cookie", {}, "403"),
                    ("wrong_cookie", {**valid_headers, "Cookie": "admin_session=bad"}, "403"),
                    ("missing_header", {k: v for k, v in valid_headers.items() if k != "X-Admin-UI"}, "403"),
                    ("wrong_content_type", {**valid_headers, "Content-Type": "text/plain"}, "403"),
                    ("hostile_origin", {**valid_headers, "Origin": "https://evil.example"}, "403"),
                    ("non_loopback_host", {**valid_headers, "Host": "evil.example:8817"}, "403"),
                ]
                for name, headers, _status in bad_cases:
                    with self.subTest(name=name):
                        state.reset_calls()
                        status, payload_body, _hdrs, _raw = self._request(
                            host, port, "POST", "/api/egress/decide", body=payload, headers=headers
                        )
                        self.assertEqual(status, HTTPStatus.FORBIDDEN)
                        self.assertEqual(payload_body["error"], "forbidden")
                        self.assertEqual(state.snapshot_calls(), [])
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_session_gate_allows_loopback_origin_or_absent_origin(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            server, thread = self._start_admin(
                home,
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-1",
            )
            host, port = server.server_address
            try:
                base_headers = {
                    "Cookie": "admin_session=cookie-1",
                    "X-Admin-UI": "1",
                    "Content-Type": "application/json",
                }
                payload = {"action": "deny", "host": "api.example.com", "container": "coding-brassbottle"}
                status, _body, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body=payload,
                    headers={**base_headers, "Origin": "http://127.0.0.1:8817"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                status2, _body2, _h2, _r2 = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body=payload,
                    headers=base_headers,
                )
                self.assertEqual(status2, HTTPStatus.OK)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_session_mints_cookie_on_right_key_and_refuses_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                # right key → 302 to / with the session cookie
                status, _payload, headers, _raw = self._request(
                    host, port, "GET", "/session?key=admin-test-key"
                )
                self.assertEqual(status, HTTPStatus.FOUND)
                cookie_raw = headers.get("Set-Cookie", "")
                parsed = SimpleCookie()
                parsed.load(cookie_raw)
                morsel = parsed.get(admin.SESSION_COOKIE_NAME)
                self.assertIsNotNone(morsel)
                self.assertEqual(morsel.value, "session-secret")
                self.assertEqual(headers.get("Location"), "/")

                # wrong key → 403, no cookie
                status, payload, headers, _raw = self._request(
                    host, port, "GET", "/session?key=wrong"
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(payload["error"], "forbidden")

                # no key → 403, no cookie
                status, payload, headers, _raw = self._request(
                    host, port, "GET", "/session"
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(payload["error"], "forbidden")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_session_key_never_appears_in_logs_or_page_bodies(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            server, thread = self._start_admin(
                home,
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="secret-value",
                admin_key="key-value-42",
            )
            host, port = server.server_address
            try:
                with self.assertLogs(admin.LOG, level="INFO") as captured:
                    self._request(host, port, "GET", "/session?key=key-value-42")
                    self._request(host, port, "GET", "/session?key=wrong")
                    self._request(host, port, "GET", "/")
                    self._request(
                        host,
                        port,
                        "GET",
                        "/api/egress/queue",
                        headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=secret-value"},
                    )
                joined = "\n".join(captured.output)
                self.assertNotIn("key-value-42", joined)
                self.assertNotIn("?key=", joined)
                for path in ("/", "/session?key=key-value-42"):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    raw = resp.read()
                    conn.close()
                    self.assertNotIn(b"key-value-42", raw)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_bind_any_guard(self):
        err = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "DJINN_CONTAINER"}
        with mock.patch.dict(os.environ, {**env, "DJINN_HOME": ""}, clear=True):
            with mock.patch("sys.stderr", err):
                rc = admin.main(["--bind-any"])
        self.assertEqual(rc, 1)
        self.assertIn("--bind-any refused", err.getvalue())

        home = Path(tempfile.mkdtemp())
        try:
            (home / "run" / "egress").mkdir(parents=True)
            # accepted: the marker set, run_daemon actually called with the
            # all-interfaces bind (mocked away, we are proving only the guard).
            with mock.patch.dict(
                os.environ,
                {**env, "DJINN_CONTAINER": "1", "DJINN_HOME": str(home)},
                clear=True,
            ), mock.patch.object(admin, "run_daemon") as run_mock:
                rc = admin.main(["--bind-any"])
            self.assertEqual(rc, 0)
            self.assertEqual(run_mock.call_args.kwargs["host"], "0.0.0.0")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_queue_proxies_recent_list_untouched(self):
        """A broker-side `recent` array passes through the admin proxy with
        every key and value byte-for-byte: the daemon only proxies."""
        state = _StubBrokerState()
        state.queue_body = {
            "open": [],
            "count": 0,
            "generated_at": "2026-09-01T00:05:00Z",
            "recent": [
                {
                    "request_id": "req-1",
                    "container": "coding-brassbottle",
                    "host": "api.example.com",
                    "port": 443,
                    "status": "denied",
                    "scope": "bottle",
                    "decided_at": "2026-08-31T12:05:00Z",
                    "decided_by": "admin",
                    "apply_status": None,
                    "deny_reason": "not needed",
                },
                {
                    "request_id": "req-2",
                    "container": "research-brassbottle",
                    "host": "192.0.2.55",
                    "port": 5432,
                    "status": "allowed",
                    "scope": "live",
                    "decided_at": "2026-08-31T12:01:00Z",
                    "decided_by": "ntfy",
                    "apply_status": "ip_requires_cidr",
                    "deny_reason": None,
                },
            ],
        }
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                status, payload, _hdrs, _raw = self._request(
                    host, port, "GET", "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload, state.queue_body)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_queue_proxies_attempt_and_last_error_untouched(self):
        """Per-row `attempt` and `last_error` from the broker pass through the
        admin proxy unmodified: the daemon adds and drops nothing."""
        state = _StubBrokerState()
        state.queue_body = {
            "open": [
                {
                    "request_id": "req-1",
                    "container": "coding-brassbottle",
                    "host": "api.example.com",
                    "port": 443,
                    "host_is_ip": False,
                    "opened_at": "2026-08-31T12:00:00Z",
                    "age_seconds": 120,
                    "hit_count": 3,
                    "uid": 1000,
                    "comm": "python",
                    "reason": "build",
                    "attempt": 2,
                    "last_error": {"reason": "apply_failed", "attempt": 2, "at": "2026-08-31T12:01:00Z"},
                },
                {
                    "request_id": "req-2",
                    "container": "research-brassbottle",
                    "host": "192.0.2.55",
                    "port": 5432,
                    "host_is_ip": True,
                    "opened_at": "2026-08-31T12:02:00Z",
                    "age_seconds": 30,
                    "hit_count": 1,
                    "uid": None,
                    "comm": None,
                    "reason": None,
                    "attempt": 0,
                },
            ],
            "count": 2,
            "generated_at": "2026-08-31T12:02:30Z",
        }
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                status, payload, _hdrs, _raw = self._request(
                    host, port, "GET", "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload, state.queue_body)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_action_mapping_to_upstream_body(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="map-cookie",
            )
            host, port = server.server_address
            headers = {
                "Cookie": "admin_session=map-cookie",
                "X-Admin-UI": "1",
                "Content-Type": "application/json",
            }
            cases = [
                ("allow_live", {"decision": "allow", "scope": "live", "host": "api.example.com", "container": "ctra"}),
                ("allow_manifest", {"decision": "allow", "scope": "manifest", "host": "api.example.com", "container": "ctra"}),
                ("deny", {"decision": "deny", "scope": "once", "host": "api.example.com", "container": "ctra", "reason": "r"}),
                ("deny_bottle", {"decision": "deny", "scope": "bottle", "host": "api.example.com", "container": "ctra", "reason": "r"}),
                ("deny_global", {"decision": "deny", "scope": "global", "host": "api.example.com", "reason": "r"}),
            ]
            try:
                for action, expected in cases:
                    with self.subTest(action=action):
                        state.reset_calls()
                        body = {"action": action, "host": "api.example.com", "container": "ctra"}
                        if action.startswith("deny"):
                            body["reason"] = "r"
                        status, _payload, _hdrs, _raw = self._request(
                            host, port, "POST", "/api/egress/decide", body=body, headers=headers
                        )
                        self.assertEqual(status, HTTPStatus.OK)
                        calls = state.snapshot_calls()
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0]["body"], expected)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_decide_validation_400s(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp), session_secret="cookie-v")
            host, port = server.server_address
            headers = {
                "Cookie": "admin_session=cookie-v",
                "X-Admin-UI": "1",
                "Content-Type": "application/json",
            }
            too_long = "x" * 201
            cases = [
                {"action": "x", "host": "a", "container": "c"},
                {"action": "allow_live", "host": "", "container": "c"},
                {"action": "deny", "host": "a", "container": "c", "reason": too_long},
                {"action": "allow_live", "host": "a", "container": "c", "reason": "nope"},
            ]
            try:
                for body in cases:
                    status, payload, _hdrs, _raw = self._request(
                        host, port, "POST", "/api/egress/decide", body=body, headers=headers
                    )
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                    self.assertIn("error", payload)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_decide_malformed_content_length_returns_400_not_traceback(self):
        """A gated request with a hostile Content-Length must get a JSON 400,
        not a ValueError traceback and a dropped connection (PR #98 review)."""
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp), session_secret="cookie-cl")
            host, port = server.server_address
            try:
                for bad_length in ("nope", "-5", str(1024 * 1024)):
                    with socket.create_connection((host, port), timeout=5) as sock:
                        request = (
                            "POST /api/egress/decide HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{port}\r\n"
                            "Cookie: admin_session=cookie-cl\r\n"
                            "X-Admin-UI: 1\r\n"
                            "Content-Type: application/json\r\n"
                            f"Content-Length: {bad_length}\r\n"
                            "Connection: close\r\n\r\n"
                        )
                        sock.sendall(request.encode("ascii"))
                        response = b""
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            response += chunk
                    self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
                    self.assertIn(b'"error"', response)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_decide_response_mapping_and_apply_failures_filter(self):
        state = _StubBrokerState()
        state.decide_body = {
            "decided": ["a", "b"],
            "apply_failures": [
                {"request_id": "a", "reason": "ip_requires_cidr", "extra": "drop"},
                {"request_id": "b", "reason": "apply_failed"},
                {"bad": "shape"},
            ],
            "persisted": {"zone": "example.com", "scope": "global", "ignore": True},
            "ignore_me": "x",
        }
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-map",
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body={"action": "deny", "host": "example.com", "container": "ctr"},
                    headers={
                        "Cookie": "admin_session=cookie-map",
                        "X-Admin-UI": "1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload["ok"], True)
                self.assertEqual(payload["decided"], 2)
                self.assertEqual(
                    payload["apply_failures"],
                    [
                        {"request_id": "a", "reason": "ip_requires_cidr"},
                        {"request_id": "b", "reason": "apply_failed"},
                    ],
                )
                self.assertEqual(payload["persisted"], {"zone": "example.com", "scope": "global"})
                self.assertNotIn("ignore_me", payload)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_apply_failures_omitted_when_upstream_omits_key(self):
        state = _StubBrokerState()
        state.decide_body = {"decided": ["a"]}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-map2",
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body={"action": "allow_live", "host": "example.com", "container": "ctr"},
                    headers={
                        "Cookie": "admin_session=cookie-map2",
                        "X-Admin-UI": "1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertNotIn("apply_failures", payload)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_upstream_400_text_passthrough_truncated(self):
        state = _StubBrokerState()
        state.decide_status = 400
        state.decide_body = {"error": "x" * 350}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-400",
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body={"action": "deny", "host": "example.com", "container": "ctr"},
                    headers={
                        "Cookie": "admin_session=cookie-400",
                        "X-Admin-UI": "1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(len(payload["error"]), 200)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_upstream_401_maps_to_token_rejected_503(self):
        state = _StubBrokerState()
        state.decide_status = 401
        state.decide_body = {"error": "unauthorized"}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-401",
            )
            host, port = server.server_address
            try:
                with self.assertLogs(admin.LOG, level="WARNING") as captured:
                    status, payload, _h, _r = self._request(
                        host,
                        port,
                        "POST",
                        "/api/egress/decide",
                        body={"action": "deny", "host": "example.com", "container": "ctr"},
                        headers={
                            "Cookie": "admin_session=cookie-401",
                            "X-Admin-UI": "1",
                            "Content-Type": "application/json",
                        },
                    )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["error"], admin.TOKEN_REJECTED_ERROR)
                self.assertTrue(any("auth rejected" in line for line in captured.output))
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_decide_500_maps_to_502(self):
        state = _StubBrokerState()
        state.decide_status = 500
        state.decide_body = {"error": "boom"}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-500",
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body={"action": "deny", "host": "example.com", "container": "ctr"},
                    headers={
                        "Cookie": "admin_session=cookie-500",
                        "X-Admin-UI": "1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                self.assertEqual(payload["error"], "decide failed on the daemon")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_decide_unreachable_maps_to_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": "http://127.0.0.1:9"},
                session_secret="cookie-u",
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "POST",
                    "/api/egress/decide",
                    body={"action": "deny", "host": "example.com", "container": "ctr"},
                    headers={
                        "Cookie": "admin_session=cookie-u",
                        "X-Admin-UI": "1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["error"], admin.UNREACHABLE_ERROR)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_queue_401_maps_to_token_rejected_503(self):
        state = _StubBrokerState()
        state.queue_status = 401
        state.queue_body = {"error": "nope"}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                headers = {"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"}
                with self.assertLogs(admin.LOG, level="WARNING") as captured:
                    status, payload, _h, _r = self._request(
                        host, port, "GET", "/api/egress/queue", headers=headers
                    )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["error"], admin.TOKEN_REJECTED_ERROR)
                self.assertTrue(any("auth rejected" in line for line in captured.output))
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_queue_5xx_and_unreachable_map_to_503(self):
        state = _StubBrokerState()
        state.queue_status = 500
        state.queue_body = {"error": "daemon fail"}
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(
                Path(tmp),
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
            )
            host, port = server.server_address
            try:
                status, payload, _h, _r = self._request(
                    host,
                    port,
                    "GET",
                    "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["error"], admin.UNREACHABLE_ERROR)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

        with tempfile.TemporaryDirectory() as tmp2:
            server2, thread2 = self._start_admin(Path(tmp2), env={"EGRESS_BROKER_URL": "http://127.0.0.1:9"})
            host2, port2 = server2.server_address
            try:
                status, payload, _h, _r = self._request(
                    host2,
                    port2,
                    "GET",
                    "/api/egress/queue",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["error"], admin.UNREACHABLE_ERROR)
            finally:
                server2.shutdown()
                server2.server_close()
                join_thread_or_fail(thread2, label="admin")

    def test_queue_forwards_operator_token_not_client_auth(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token = "operator-token-abc"
            token_path = home / "run" / "egress" / admin.OPERATOR_TOKEN_FILENAME
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            server, thread = self._start_admin(
                home,
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                operator_token=token,
            )
            host, port = server.server_address
            try:
                status, _payload, _h, _r = self._request(
                    host,
                    port,
                    "GET",
                    "/api/egress/queue",
                    headers={
                        "Authorization": "Bearer client-supplied",
                        "Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret",
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                calls = state.snapshot_calls()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["authorization"], f"Bearer {token}")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_operator_token_fileexists_race_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_path = root / admin.OPERATOR_TOKEN_FILENAME
            token_path.write_text("recovered-token\n", encoding="utf-8")
            with mock.patch.object(admin, "ensure_operator_token", side_effect=FileExistsError()):
                with mock.patch.object(admin.time, "sleep") as sleep_mock:
                    token = admin._ensure_admin_operator_token(root)
            self.assertEqual(token, "recovered-token")
            sleep_mock.assert_called_once()

    def test_operator_token_fileexists_race_still_empty_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_path = root / admin.OPERATOR_TOKEN_FILENAME
            token_path.write_text("", encoding="utf-8")
            with mock.patch.object(admin, "ensure_operator_token", side_effect=FileExistsError()):
                with self.assertRaises(RuntimeError):
                    admin._ensure_admin_operator_token(root)

    def test_logs_exclude_token_and_reason_text(self):
        state = _StubBrokerState()
        stub, stub_thread = self._start_stub(state)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token = "secret-operator-token"
            reason = "secret reason text should not hit logs"
            token_path = home / "run" / "egress" / admin.OPERATOR_TOKEN_FILENAME
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            server, thread = self._start_admin(
                home,
                env={"EGRESS_BROKER_URL": f"http://127.0.0.1:{stub.server_address[1]}"},
                session_secret="cookie-log",
            )
            host, port = server.server_address
            try:
                with self.assertLogs(admin.LOG, level="INFO") as captured:
                    status, _payload, _h, _r = self._request(
                        host,
                        port,
                        "POST",
                        "/api/egress/decide",
                        body={
                            "action": "deny_bottle",
                            "host": "example.com",
                            "container": "ctr",
                            "reason": reason,
                        },
                        headers={
                            "Cookie": "admin_session=cookie-log",
                            "X-Admin-UI": "1",
                            "Content-Type": "application/json",
                        },
                    )
                self.assertEqual(status, HTTPStatus.OK)
                merged = "\n".join(captured.output)
                self.assertNotIn(token, merged)
                self.assertNotIn(reason, merged)
                self.assertIn("reason_len=", merged)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")
        stub.shutdown()
        stub.server_close()
        join_thread_or_fail(stub_thread, label="stub")

    def test_app_js_structural_grouping_and_recent_markers(self):
        """Structural checks only: the admin UI is client-side and there is no
        browser in this suite, so the browser rig supplies the behavioural
        Evidence for grouping, the recent move and the local-time render.
        These assertions pin the source shape those steps depend on."""
        text = (REPO_ROOT / "src" / "admin_app.js").read_text(encoding="utf-8")
        # The typed-host arm moved to the request row; no host-group remains.
        self.assertNotIn("HostGroup", text)
        self.assertNotIn("host-group", text)
        # Open rows group by container (the bottle), not by host.
        self.assertIn("String(row.container || \"\")", text)
        # The recent-decisions section is rendered below the table.
        self.assertIn("Recent decisions (24 h)", text)
        # Request and decision dates render through the local-time formatter.
        self.assertIn("localTimestamp(row.opened_at)", text)
        self.assertIn("localTimestamp(row.decided_at)", text)
        # Apply failures surface as the attempt-counted chip.
        self.assertIn("apply failed \\u00d7", text)

    def test_legacy_routes_match_recorded_fixture(self):
        """With DJINN_ADMIN_UI unset, every legacy route returns exactly the
        status, headers and body that the pre-step code returned."""
        fixture = json.loads(
            (TESTS_DIR / "fixtures" / "admin_legacy_responses.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_admin(Path(tmp))
            host, port = server.server_address
            try:
                for entry in fixture:
                    headers: dict[str, str] = {}
                    if entry["with_session"]:
                        headers["Cookie"] = f"{admin.SESSION_COOKIE_NAME}=session-secret"
                    status, _payload, resp_headers, raw = self._request(
                        host, port, "GET", entry["route"], headers=headers
                    )
                    self.assertEqual(
                        status,
                        entry["status"],
                        f"{entry['route']} session={entry['with_session']}",
                    )
                    self.assertEqual(
                        resp_headers.get("Content-Type"),
                        entry["headers"].get("Content-Type"),
                        f"{entry['route']} Content-Type",
                    )
                    self.assertEqual(
                        resp_headers.get("Cache-Control"),
                        entry["headers"].get("Cache-Control"),
                        f"{entry['route']} Cache-Control",
                    )
                    self.assertEqual(
                        resp_headers.get("Location"),
                        entry["headers"].get("Location"),
                        f"{entry['route']} Location",
                    )
                    self.assertEqual(
                        "Set-Cookie" in resp_headers,
                        entry["headers"].get("Set-Cookie", False),
                        f"{entry['route']} Set-Cookie presence",
                    )
                    self.assertEqual(
                        hashlib.sha256(raw).hexdigest(),
                        entry["body_sha256"],
                        f"{entry['route']} body sha256",
                    )
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def _build_spa_dist(self, parent: Path, *, with_index: bool = True) -> Path:
        dist = parent / "dist"
        dist.mkdir(parents=True)
        if with_index:
            (dist / "index.html").write_text(
                "<!doctype html><html><body>SPA</body></html>", encoding="utf-8"
            )
        assets = dist / "assets"
        assets.mkdir()
        (assets / "app-abc123.js").write_text("console.log('app')", encoding="utf-8")
        (assets / "app-abc123.css").write_text("body{color:red}", encoding="utf-8")
        (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
        return dist

    def test_spa_mode_serves_app_routes_and_assets(self):
        """With DJINN_ADMIN_UI=spa the daemon serves index.html at the five app
        routes to session holders, the pointer page without a session, hashed
        assets with the right content type and cache header, and 404 for legacy
        assets and traversal attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dist = self._build_spa_dist(home)
            env = {
                "DJINN_HOME": str(home),
                "DJINN_ADMIN_UI": "spa",
                "DJINN_ADMIN_UI_DIST": str(dist),
            }
            server, thread = self._start_admin(home, env=env)
            host, port = server.server_address
            try:
                # app routes with cookie -> index.html, no-store
                for route in ("/", "/egress", "/denylist", "/bottles", "/backup"):
                    with self.subTest(route=route, session=True):
                        status, _payload, headers, raw = self._request(
                            host,
                            port,
                            "GET",
                            route,
                            headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                        )
                        self.assertEqual(status, HTTPStatus.OK)
                        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
                        self.assertEqual(headers.get("Cache-Control"), "no-store")
                        self.assertIn(b"SPA", raw)

                # app routes without cookie -> pointer page
                for route in ("/", "/egress", "/denylist", "/bottles", "/backup"):
                    with self.subTest(route=route, session=False):
                        status, _payload, headers, raw = self._request(host, port, "GET", route)
                        self.assertEqual(status, HTTPStatus.OK)
                        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
                        self.assertIn(b"./djinn egress url", raw)

                # query string ignored for app routes
                status, _payload, _headers, raw = self._request(
                    host,
                    port,
                    "GET",
                    "/egress?foo=bar",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertIn(b"SPA", raw)

                # allowlisted assets
                status, _payload, headers, raw = self._request(host, port, "GET", "/assets/app-abc123.js")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "text/javascript; charset=utf-8")
                self.assertEqual(headers.get("Cache-Control"), "public, max-age=31536000, immutable")

                status, _payload, headers, raw = self._request(host, port, "GET", "/assets/app-abc123.css")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "text/css; charset=utf-8")
                self.assertEqual(headers.get("Cache-Control"), "public, max-age=31536000, immutable")

                status, _payload, headers, raw = self._request(host, port, "GET", "/favicon.svg")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "image/svg+xml")
                self.assertEqual(headers.get("Cache-Control"), "no-cache")

                # legacy assets 404
                for route in (
                    "/app.js",
                    "/vendor/htm-preact-standalone.module.js",
                    "/manifest.webmanifest",
                    "/sw.js",
                    "/icon-192.png",
                    "/icon-512.png",
                ):
                    with self.subTest(legacy=route):
                        status, _payload, _headers, _raw = self._request(host, port, "GET", route)
                        self.assertEqual(status, HTTPStatus.NOT_FOUND)

                # traversal / outside-allowlist paths are 404
                for route in (
                    "/../src/admin_daemon.py",
                    "/assets/../../src/admin_daemon.py",
                    "/assets/%2e%2e/x",
                    "/nope",
                ):
                    with self.subTest(traversal=route):
                        status, _payload, _headers, _raw = self._request(host, port, "GET", route)
                        self.assertEqual(status, HTTPStatus.NOT_FOUND)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_spa_allowlist_drops_symlinks_and_hidden_files(self):
        """Only regular, non-hidden files under dist are served: a symlinked
        file or directory pointing outside dist, and a dotfile, are 404."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dist = self._build_spa_dist(home)
            outside = home / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("operator-token", encoding="utf-8")
            (dist / "assets" / "leak.txt").symlink_to(outside / "secret.txt")
            (dist / "linked").symlink_to(outside, target_is_directory=True)
            (dist / ".env").write_text("hidden", encoding="utf-8")
            (dist / "assets" / ".cache").mkdir()
            (dist / "assets" / ".cache" / "x.js").write_text("x", encoding="utf-8")
            env = {
                "DJINN_HOME": str(home),
                "DJINN_ADMIN_UI": "spa",
                "DJINN_ADMIN_UI_DIST": str(dist),
            }
            server, thread = self._start_admin(home, env=env)
            host, port = server.server_address
            try:
                self.assertEqual(
                    sorted(server.spa_allowlist),
                    ["/assets/app-abc123.css", "/assets/app-abc123.js", "/favicon.svg"],
                )
                for path in ("/assets/leak.txt", "/linked/secret.txt", "/.env", "/assets/.cache/x.js"):
                    with self.subTest(path=path):
                        status, _payload, _headers, raw = self._request(host, port, "GET", path)
                        self.assertEqual(status, HTTPStatus.NOT_FOUND)
                        self.assertNotIn(b"operator-token", raw)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_spa_head_matches_get_without_body(self):
        """HEAD in spa mode answers like GET (status, type, length, cache,
        session gate) and sends no body; legacy mode keeps the 501."""
        cookie = {"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dist = self._build_spa_dist(home)
            env = {
                "DJINN_HOME": str(home),
                "DJINN_ADMIN_UI": "spa",
                "DJINN_ADMIN_UI_DIST": str(dist),
            }
            server, thread = self._start_admin(home, env=env)
            host, port = server.server_address
            try:
                for path, headers in (
                    ("/egress", cookie),
                    ("/egress", {}),
                    ("/assets/app-abc123.js", {}),
                    ("/nope", {}),
                ):
                    with self.subTest(path=path, session=bool(headers)):
                        g_status, _p, g_headers, g_raw = self._request(host, port, "GET", path, headers=headers)
                        conn = HTTPConnection(host, port, timeout=5)
                        conn.request("HEAD", path, headers=headers)
                        resp = conn.getresponse()
                        h_raw = resp.read()
                        conn.close()
                        self.assertEqual(resp.status, g_status)
                        for name in ("Content-Type", "Content-Length", "Cache-Control"):
                            self.assertEqual(resp.getheader(name), g_headers.get(name), name)
                        self.assertEqual(int(resp.getheader("Content-Length")), len(g_raw))
                        self.assertEqual(h_raw, b"")
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # _start_admin's env patch from the spa server above is still
            # active until test cleanup, so clear the flag explicitly.
            server, thread = self._start_admin(home, env={"DJINN_ADMIN_UI": ""})
            self.assertFalse(server.spa_mode)
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("HEAD", "/")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, HTTPStatus.NOT_IMPLEMENTED)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_spa_mode_missing_dist_returns_503_for_app_routes(self):
        """If the configured dist directory is missing, app routes answer 503
        'admin UI not built' while /health still works."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                "DJINN_HOME": str(home),
                "DJINN_ADMIN_UI": "spa",
                "DJINN_ADMIN_UI_DIST": str(home / "no-such-dist"),
            }
            server, thread = self._start_admin(home, env=env)
            host, port = server.server_address
            try:
                status, _payload, _headers, raw = self._request(
                    host,
                    port,
                    "GET",
                    "/egress",
                    headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}=session-secret"},
                )
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(raw, b"admin UI not built")

                status, _payload, _headers, _raw = self._request(host, port, "GET", "/health")
                self.assertEqual(status, HTTPStatus.OK)
            finally:
                server.shutdown()
                server.server_close()
                join_thread_or_fail(thread, label="admin")

    def test_spa_mode_logs_boundary_at_startup(self):
        """The spa startup logs mode, dist path, file count and total bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dist = self._build_spa_dist(home)
            env = {
                "DJINN_HOME": str(home),
                "DJINN_ADMIN_UI": "spa",
                "DJINN_ADMIN_UI_DIST": str(dist),
            }
            with self.assertLogs(admin.LOG, level="INFO") as captured:
                server, thread = self._start_admin(home, env=env)
                try:
                    joined = "\n".join(captured.output)
                    self.assertIn("admin spa mode enabled", joined)
                    self.assertIn(str(dist), joined)
                    self.assertIn("files=3", joined)
                finally:
                    server.shutdown()
                    server.server_close()
                    join_thread_or_fail(thread, label="admin")
