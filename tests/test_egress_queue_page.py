#!/usr/bin/env python3
"""Unit tests for the read-only egress queue operator page."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
import egress_broker_host as broker  # noqa: E402
import egress_queue_page as queue_page  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402


class _QueueStubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.server.last_path = self.path  # type: ignore[attr-defined]
        self.server.last_auth = self.headers.get("Authorization")  # type: ignore[attr-defined]
        status = self.server.reply_status  # type: ignore[attr-defined]
        body = self.server.reply_body  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EgressQueuePageTests(unittest.TestCase):
    def _start_queue_server(
        self, egress_root: Path, operator_token: str
    ) -> tuple[queue_page.EgressQueueHTTPServer, threading.Thread]:
        server = queue_page.EgressQueueHTTPServer(("127.0.0.1", 0), egress_root, operator_token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)
        return server, thread

    def _start_stub_daemon(
        self, status: int, body: bytes
    ) -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _QueueStubHandler)
        server.reply_status = status  # type: ignore[attr-defined]
        server.reply_body = body  # type: ignore[attr-defined]
        server.last_auth = None  # type: ignore[attr-defined]
        server.last_path = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)
        return server, thread

    def _json_get(
        self, host: str, port: int, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
        conn.close()
        return status, body

    def _stop_server(self, server: ThreadingHTTPServer, thread: threading.Thread, label: str) -> None:
        server.shutdown()
        join_thread_or_fail(thread, label=label)
        server.server_close()

    def test_server_starts_and_index_does_not_render_operator_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "operator-secret-token"
            (egress_root / broker.OPERATOR_TOKEN_FILENAME).write_text(
                token + "\n", encoding="utf-8"
            )
            server, thread = self._start_queue_server(egress_root, token)
            try:
                host, port = server.server_address
                status, body = self._json_get(host, port, "/")
                html = body.decode("utf-8")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertIn("Egress queue", html)
                self.assertNotIn(token, html)
            finally:
                self._stop_server(server, thread, "queue page server")

    def test_api_queue_proxies_with_operator_token_and_verbatim_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            # Mirrors the daemon's documented GET /queue contract
            # (docs/egress.md): open/count/generated_at, hit_count per row.
            canned = {
                "open": [
                    {
                        "request_id": "deadbeef",
                        "container": "coding-brassbottle",
                        "host": "api.example.com",
                        "port": 443,
                        "host_is_ip": False,
                        "opened_at": "2026-08-31T19:59:46Z",
                        "age_seconds": 14,
                        "hit_count": 3,
                        "uid": 1000,
                        "comm": "python3",
                        "reason": "sync index",
                    }
                ],
                "count": 1,
                "generated_at": "2026-08-31T20:00:00Z",
            }
            canned_bytes = json.dumps(canned, separators=(",", ":")).encode("utf-8")
            token = "operator-from-file-token"
            (egress_root / broker.OPERATOR_TOKEN_FILENAME).write_text(
                token + "\n", encoding="utf-8"
            )
            stub, stub_thread = self._start_stub_daemon(HTTPStatus.OK, canned_bytes)
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                broker.write_daemon_endpoint(egress_root, stub_host, stub_port)
                ensured = broker.ensure_operator_token(egress_root)
                server, thread = self._start_queue_server(egress_root, ensured)
                host, port = server.server_address
                status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, canned_bytes)
                self.assertEqual(stub.last_path, "/queue")  # type: ignore[attr-defined]
                self.assertEqual(stub.last_auth, f"Bearer {token}")  # type: ignore[attr-defined]
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_client_authorization_header_is_not_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "server-side-operator-token"
            (egress_root / broker.OPERATOR_TOKEN_FILENAME).write_text(
                token + "\n", encoding="utf-8"
            )
            stub, stub_thread = self._start_stub_daemon(
                HTTPStatus.OK, b'{"open":[],"count":0,"generated_at":"x"}'
            )
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                broker.write_daemon_endpoint(egress_root, stub_host, stub_port)
                server, thread = self._start_queue_server(egress_root, token)
                host, port = server.server_address
                status, _body = self._json_get(
                    host,
                    port,
                    "/api/queue",
                    headers={"Authorization": "Bearer client-supplied-token"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(stub.last_auth, f"Bearer {token}")  # type: ignore[attr-defined]
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_api_queue_returns_503_when_daemon_missing_or_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "operator-token"
            server, thread = self._start_queue_server(egress_root, token)
            try:
                host, port = server.server_address
                status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(body, b'{"error":"egress daemon unreachable"}')
            finally:
                self._stop_server(server, thread, "queue page server")

        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "operator-token"
            broker.write_daemon_endpoint(egress_root, "127.0.0.1", _unused_port())
            server, thread = self._start_queue_server(egress_root, token)
            try:
                host, port = server.server_address
                status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(body, b'{"error":"egress daemon unreachable"}')
            finally:
                self._stop_server(server, thread, "queue page server")

    def test_upstream_500_becomes_503_without_echoing_upstream_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "operator-token"
            upstream_body = b'{"error":"upstream internals"}'
            stub, stub_thread = self._start_stub_daemon(HTTPStatus.INTERNAL_SERVER_ERROR, upstream_body)
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                broker.write_daemon_endpoint(egress_root, stub_host, stub_port)
                server, thread = self._start_queue_server(egress_root, token)
                host, port = server.server_address
                status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(body, b'{"error":"egress daemon unreachable"}')
                self.assertNotIn(upstream_body, body)
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_non_loopback_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DJINN_HOME": tmp}):
                with self.assertRaises(SystemExit) as exc:
                    queue_page.main(["--host", "0.0.0.0", "--port", "0"])
                self.assertEqual(exc.exception.code, 2)

    def test_missing_djinn_home_exits_cleanly_with_message(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(stderr):
                rc = queue_page.main([])
        self.assertEqual(rc, 1)
        self.assertIn("DJINN_HOME", stderr.getvalue())

    def test_operator_token_never_appears_in_proxy_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "do-not-log-this-token"
            stub, stub_thread = self._start_stub_daemon(
                HTTPStatus.OK, b'{"open":[],"count":0,"generated_at":"x"}'
            )
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                broker.write_daemon_endpoint(egress_root, stub_host, stub_port)
                server, thread = self._start_queue_server(egress_root, token)
                host, port = server.server_address
                with self.assertLogs(queue_page.LOG.name, level="INFO") as logs:
                    status, _body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.OK)
                rendered = "\n".join(logs.output)
                self.assertNotIn(token, rendered)
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_env_override_reaches_daemon_without_daemon_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token = "operator-token"
            canned = b'{"open":[],"count":0,"generated_at":"x"}'
            stub, stub_thread = self._start_stub_daemon(HTTPStatus.OK, canned)
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                # No daemon.json anywhere: the documented EGRESS_BROKER_URL
                # override alone must carry the proxy to the daemon.
                server, thread = self._start_queue_server(egress_root, token)
                host, port = server.server_address
                with mock.patch.dict(
                    os.environ,
                    {broker.EGRESS_BROKER_URL_ENV: f"http://{stub_host}:{stub_port}"},
                ):
                    status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, canned)
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_upstream_401_reports_token_rejected_not_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            stub, stub_thread = self._start_stub_daemon(
                HTTPStatus.UNAUTHORIZED, b'{"error":"unauthorized"}'
            )
            server = thread = None
            try:
                stub_host, stub_port = stub.server_address
                broker.write_daemon_endpoint(egress_root, stub_host, stub_port)
                server, thread = self._start_queue_server(egress_root, "stale-token")
                host, port = server.server_address
                status, body = self._json_get(host, port, "/api/queue")
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(body, queue_page.TOKEN_REJECTED_BODY)
                self.assertNotIn(b"unauthorized", body)
            finally:
                if server is not None and thread is not None:
                    self._stop_server(server, thread, "queue page server")
                self._stop_server(stub, stub_thread, "stub daemon server")

    def test_ipv6_loopback_host_binds_and_serves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            try:
                server = queue_page.EgressQueueHTTPServer(("::1", 0), egress_root, "tok")
            except OSError as exc:  # pragma: no cover - IPv6-less environments
                self.skipTest(f"IPv6 loopback unavailable: {exc}")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address[:2]
                wait_for_tcp_listening(host, port)
                status, body = self._json_get(host, port, "/")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertIn("Egress queue", body.decode("utf-8"))
            finally:
                self._stop_server(server, thread, "ipv6 queue page server")

    def test_client_disconnect_logs_one_line_not_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = queue_page.EgressQueueHTTPServer(
                ("127.0.0.1", 0), Path(tmp), "tok"
            )
            try:
                with self.assertLogs(queue_page.LOG.name, level="INFO") as logs:
                    try:
                        raise ConnectionResetError("peer went away")
                    except ConnectionResetError:
                        server.handle_error(None, ("127.0.0.1", 12345))
                self.assertTrue(
                    any("client disconnected" in line for line in logs.output)
                )
            finally:
                server.server_close()

    def test_bind_conflict_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupier.bind(("127.0.0.1", 0))
            occupier.listen(1)
            port = occupier.getsockname()[1]
            stderr = io.StringIO()
            try:
                with mock.patch.dict(os.environ, {"DJINN_HOME": tmp}, clear=True):
                    with redirect_stderr(stderr):
                        rc = queue_page.main(["--port", str(port)])
            finally:
                occupier.close()
            self.assertEqual(rc, 1)
            self.assertIn("already running", stderr.getvalue())

    def test_empty_token_file_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp) / "run" / "egress"
            egress_root.mkdir(parents=True)
            (egress_root / broker.OPERATOR_TOKEN_FILENAME).write_text(
                "", encoding="utf-8"
            )
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"DJINN_HOME": tmp}, clear=True):
                with redirect_stderr(stderr):
                    rc = queue_page.main([])
            self.assertEqual(rc, 1)
            self.assertIn("operator token unavailable", stderr.getvalue())

    def test_token_create_race_recovers_by_rereading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            token_path = egress_root / broker.OPERATOR_TOKEN_FILENAME

            def lose_race(_root: Path) -> str:
                # The winner (daemon under DaemonLock) lands its token between
                # our is_file() miss and the O_EXCL create.
                token_path.write_text("winners-token\n", encoding="utf-8")
                raise FileExistsError(str(token_path))

            with mock.patch.object(
                queue_page, "ensure_operator_token", side_effect=lose_race
            ):
                token = queue_page._read_operator_token(egress_root)
            self.assertEqual(token, "winners-token")


if __name__ == "__main__":
    unittest.main()
