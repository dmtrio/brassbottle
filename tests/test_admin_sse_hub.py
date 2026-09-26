#!/usr/bin/env python3
"""The admin daemon's live queue stream (GET /api/egress/stream), over real sockets.

The real AdminHTTPServer runs in front of the contract-validated stub broker
(tests/admin_ui_stub_broker.py); every client here is a raw socket, so what is
asserted is what a browser's EventSource would receive.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

import admin_daemon as admin  # noqa: E402
import admin_ui_stub_broker as stub  # noqa: E402
from admin_contract_validator import validate_document  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402

SECRET = "session-secret"
POLL = 0.1
HEARTBEAT = 30.0   # long unless a test is about the heartbeat, so a comment never hides a missing frame


class Frame:
    """One parsed server-sent event, or a comment line."""

    def __init__(self, raw: bytes):
        self.raw = raw
        text = raw.decode("utf-8")
        self.comment = text[2:].strip() if text.startswith(": ") else None
        self.event = None
        self.data = None
        for line in text.split("\n"):
            if line.startswith("event: "):
                self.event = line[len("event: "):]
            elif line.startswith("data: "):
                self.data = json.loads(line[len("data: "):])

    def as_contract_event(self) -> dict:
        return {"event": self.event, "data": self.data}


class Client:
    """A raw-socket EventSource: status, headers, then frames."""

    def __init__(self, port: int, *, cookie: str | None = SECRET, path: str = admin.STREAM_PATH):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        head = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n"
        if cookie is not None:
            head += f"Cookie: {admin.SESSION_COOKIE_NAME}={cookie}\r\n"
        self.sock.sendall((head + "\r\n").encode("ascii"))
        self.buffer = b""
        self.status, self.headers = self._read_head()

    def _read_head(self) -> tuple[int, dict[str, str]]:
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError(f"closed before headers: {self.buffer!r}")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return int(lines[0].split()[1]), headers

    def body(self) -> bytes:
        """The whole body of a non-stream reply (the server closes after it)."""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                return self.buffer
            self.buffer += chunk

    def frame(self, timeout: float = 3.0) -> Frame | None:
        """The next frame, or None when nothing arrives within `timeout`; raises on EOF."""
        deadline = time.monotonic() + timeout
        while b"\n\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise EOFError("stream closed")
            self.buffer += chunk
        raw, _, self.buffer = self.buffer.partition(b"\n\n")
        return Frame(raw)

    def queue_frame(self, timeout: float = 3.0) -> Frame:
        """The next `queue` event, skipping comments."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self.frame(max(0.0, deadline - time.monotonic()))
            if frame is None:
                raise AssertionError("no queue frame")
            if frame.event == "queue":
                return frame

    def frames_for(self, seconds: float) -> list[Frame]:
        """Every frame that arrives in the next `seconds`."""
        deadline = time.monotonic() + seconds
        found = []
        while (left := deadline - time.monotonic()) > 0:
            frame = self.frame(left)
            if frame is not None:
                found.append(frame)
        return found

    def at_eof(self, timeout: float = 5.0) -> bool:
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.frame(max(0.0, deadline - time.monotonic()))
        except EOFError:
            return True
        return False

    def close(self) -> None:
        self.sock.close()


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class HubTestCase(unittest.TestCase):
    spa = True

    def setUp(self) -> None:
        self.broker = stub.StubBroker()
        broker_url = self.broker.start()
        self.addCleanup(self.broker.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        root = home / "run" / "egress"
        root.mkdir(parents=True)
        (root / admin.OPERATOR_TOKEN_FILENAME).write_text("operator-test-token\n", encoding="utf-8")
        dist = home / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
        env = {"DJINN_HOME": str(home), "EGRESS_BROKER_URL": broker_url,
               "DJINN_ADMIN_UI_DIST": str(dist)}
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        if self.spa:
            os.environ["DJINN_ADMIN_UI"] = "spa"
        else:
            os.environ.pop("DJINN_ADMIN_UI", None)
        self.root = root
        self.clients: list[Client] = []
        self.addCleanup(self._close_clients)
        self.baseline = set(threading.enumerate())

    def _close_clients(self) -> None:
        for client in self.clients:
            client.close()

    def start_admin(self, **options) -> admin.AdminHTTPServer:
        options.setdefault("stream_poll_seconds", POLL)
        options.setdefault("stream_heartbeat_seconds", HEARTBEAT)
        server = admin.AdminHTTPServer(
            ("127.0.0.1", 0), egress_root=self.root, session_secret=SECRET,
            operator_token="operator-test-token", admin_key="admin-test-key", **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for_tcp_listening(*server.server_address)
        self.baseline.add(thread)
        self.server = server

        def stop():
            server.shutdown()
            server.server_close()
            join_thread_or_fail(thread, label="admin")
        self.addCleanup(stop)
        self.port = server.server_address[1]
        return server

    def connect(self, **kwargs) -> Client:
        client = Client(self.port, **kwargs)
        self.clients.append(client)
        return client

    def leaked_threads(self) -> list[str]:
        return [t.name for t in threading.enumerate() if t not in self.baseline and t.is_alive()]

    def assert_no_threads_leak(self) -> None:
        self.assertTrue(wait_until(lambda: not self.leaked_threads(), 5.0),
                        f"threads still alive: {self.leaked_threads()}")

    def queue_polls(self) -> int:
        return self.broker.queue_gets


class StreamTests(HubTestCase):
    def test_first_frame_is_the_current_snapshot_at_once_and_validates(self):
        # a poll interval far longer than the assertion: the frame cannot be a poll's
        self.start_admin(stream_poll_seconds=60.0)
        started = time.monotonic()
        client = self.connect()
        self.assertEqual(client.status, 200)
        self.assertTrue(client.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(client.headers["cache-control"], "no-store")
        frame = client.queue_frame(timeout=2.0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(frame.event, "queue")
        self.assertEqual(frame.data["count"], len(stub.OPEN_ROWS))
        self.assertEqual({row["request_id"] for row in frame.data["open"]}, {r[0] for r in stub.OPEN_ROWS})
        self.assertEqual(validate_document(frame.as_contract_event(), "sse_event.schema.json"), [])

    def test_a_change_is_one_queue_frame_per_stream_and_nothing_more(self):
        self.start_admin()
        first, second = self.connect(), self.connect()
        for client in (first, second):
            client.queue_frame()
        self.broker.file_request("new.example.com", container="alpha")
        for client in (first, second):
            frames = client.frames_for(1.0)
            queue = [f for f in frames if f.event == "queue"]
            self.assertEqual(len(queue), 1, [f.raw[:60] for f in frames])
            self.assertEqual(len(queue[0].data["open"]), len(stub.OPEN_ROWS) + 1)
            self.assertIn("new.example.com", [row["host"] for row in queue[0].data["open"]])
            self.assertEqual(validate_document(queue[0].as_contract_event(), "sse_event.schema.json"), [])

    def test_no_frame_while_only_the_clock_moves(self):
        """`generated_at` and every row's `age_seconds` change on each poll; neither is a change."""
        self.start_admin()
        client = self.connect()
        client.queue_frame()
        polls = self.queue_polls()
        for tick in range(1, 6):
            with self.broker.lock:
                self.broker.queue["generated_at"] = f"2030-01-01T00:00:0{tick}Z"
                for row in self.broker.queue["open"]:
                    row["age_seconds"] += 10
            time.sleep(POLL * 1.5)
        self.assertGreaterEqual(self.queue_polls() - polls, 4, "the poller was not polling")
        self.assertEqual(client.frames_for(0.3), [])

    def test_a_hit_count_change_is_a_change(self):
        self.start_admin()
        client = self.connect()
        client.queue_frame()
        with self.broker.lock:
            self.broker.queue["open"][0]["hit_count"] += 1
        self.assertEqual(client.queue_frame(timeout=2.0).event, "queue")

    def test_a_decided_row_moving_to_recent_is_a_change(self):
        self.start_admin()
        client = self.connect()
        client.queue_frame()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = {"action": "deny", "host": "a1.example.com", "container": "alpha"}
        conn.request("POST", "/api/egress/decide", json.dumps(payload), {
            "Content-Type": "application/json", "X-Admin-UI": "1",
            "Cookie": f"{admin.SESSION_COOKIE_NAME}={SECRET}"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        frame = client.queue_frame(timeout=2.0)
        self.assertEqual(frame.data["count"], len(stub.OPEN_ROWS) - 1)

    def test_an_idle_stream_gets_a_heartbeat_comment(self):
        self.start_admin(stream_heartbeat_seconds=0.2)
        client = self.connect()
        client.queue_frame()
        frames = client.frames_for(0.9)
        self.assertGreaterEqual(len(frames), 2)
        self.assertTrue(all(f.comment == "hb" and f.event is None for f in frames), [f.raw for f in frames])

    def test_eight_streams_open_and_the_ninth_is_refused_with_the_error_body(self):
        self.start_admin()
        eight = [self.connect() for _ in range(8)]
        for client in eight:
            self.assertEqual(client.status, 200)
            client.queue_frame()
        ninth = self.connect()
        self.assertEqual(ninth.status, 503)
        body = json.loads(ninth.body())
        self.assertEqual(body, {"error": "too many live streams"})
        self.assertEqual(validate_document(body, "error_response.schema.json"), [])
        # the other routes are served while all eight streams hold their threads
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/egress/queue", headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}={SECRET}"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        # a slot freed by a disconnect is reusable
        eight[0].close()
        self.assertTrue(wait_until(lambda: self.server.stream_hub.stream_count() == 7))
        again = self.connect()
        self.assertEqual(again.status, 200)
        again.queue_frame()

    def test_a_disconnect_ends_its_handler_and_the_poller_stops_when_none_remain(self):
        self.start_admin(stream_heartbeat_seconds=0.3)
        clients = [self.connect(), self.connect()]
        for client in clients:
            client.queue_frame()
        self.assertTrue([t for t in self.leaked_threads() if t == "admin-sse-poller"])
        handlers = len(self.leaked_threads())
        clients[0].close()
        self.assertTrue(wait_until(lambda: len(self.leaked_threads()) == handlers - 1),
                        f"handler thread survived its client: {self.leaked_threads()}")
        self.assertEqual(self.server.stream_hub.stream_count(), 1)
        self.assertIn("admin-sse-poller", self.leaked_threads())   # one stream still open: still polling
        clients[1].close()
        self.assert_no_threads_leak()
        self.assertEqual(self.server.stream_hub.stream_count(), 0)
        polls = self.queue_polls()
        time.sleep(POLL * 5)
        self.assertEqual(self.queue_polls(), polls, "the poller kept polling with no stream open")

    def test_a_reset_client_is_a_write_error_that_ends_its_handler(self):
        self.start_admin(stream_poll_seconds=0.05, stream_heartbeat_seconds=30.0)
        client = self.connect()
        client.queue_frame()
        client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        client.close()   # RST, not FIN
        self.broker.file_request("after-reset.example.com")   # the next fan-out write hits the dead socket
        self.assert_no_threads_leak()
        self.assertEqual(self.server.stream_hub.stream_count(), 0)

    def test_no_cookie_or_a_wrong_cookie_is_refused_like_the_other_api_routes(self):
        self.start_admin()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/egress/queue")
        response = conn.getresponse()
        expected = (response.status, response.read())
        conn.close()
        self.assertEqual(expected[0], 403)
        for cookie in (None, "not-the-secret"):
            client = self.connect(cookie=cookie)
            self.assertEqual((client.status, client.body()), expected, cookie)
        self.assertEqual(self.server.stream_hub.stream_count(), 0)
        self.assert_no_threads_leak()

    def test_head_answers_the_headers_and_opens_no_stream(self):
        self.start_admin()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("HEAD", admin.STREAM_PATH, headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}={SECRET}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(response.read(), b"")
        conn.close()
        self.assertEqual(self.server.stream_hub.stream_count(), 0)

    def test_a_failed_poll_keeps_the_streams_open_and_emits_nothing(self):
        self.start_admin(stream_failure_limit=100)
        client = self.connect()
        client.queue_frame()
        self.broker.outage = True
        polls = self.queue_polls()
        self.assertEqual(client.frames_for(POLL * 6), [])
        self.assertGreaterEqual(self.queue_polls() - polls, 3)
        self.broker.outage = False
        self.broker.file_request("back.example.com")
        frame = client.queue_frame(timeout=2.0)   # the same stream carries the recovery
        self.assertIn("back.example.com", [row["host"] for row in frame.data["open"]])

    def test_a_lasting_outage_ends_the_streams_and_a_connect_during_it_is_refused(self):
        self.start_admin(stream_failure_limit=2)
        client = self.connect()
        client.queue_frame()
        self.broker.outage = True
        self.assertTrue(client.at_eof(timeout=3.0), "the stream outlived the outage")
        self.assert_no_threads_leak()
        refused = self.connect()
        self.assertEqual(refused.status, 503)
        body = json.loads(refused.body())
        self.assertEqual(body, {"error": admin.UNREACHABLE_ERROR})
        self.assertEqual(validate_document(body, "error_response.schema.json"), [])
        self.assertEqual(self.server.stream_hub.stream_count(), 0)
        self.broker.outage = False
        recovered = self.connect()
        self.assertEqual(recovered.status, 200)
        recovered.queue_frame()

    def test_shutdown_ends_every_stream_and_its_threads(self):
        server = self.start_admin()
        clients = [self.connect() for _ in range(3)]
        for client in clients:
            client.queue_frame()
        server.shutdown()
        server.server_close()
        for client in clients:
            self.assertTrue(client.at_eof(timeout=3.0))
        self.assert_no_threads_leak()

    def test_frames_never_carry_the_session_or_operator_secrets(self):
        self.start_admin()
        client = self.connect()
        with self.assertLogs(admin.LOG, level="INFO") as logs:
            client.queue_frame()
            client.close()
            self.assert_no_threads_leak()
        text = client.buffer.decode("utf-8", "replace") + "\n".join(logs.output)
        for secret in (SECRET, "operator-test-token", "admin-test-key"):
            self.assertNotIn(secret, text)

    def test_boundary_logs_cover_the_poll_the_fanout_and_open_close(self):
        self.start_admin()
        with self.assertLogs(admin.LOG, level="INFO") as logs:
            client = self.connect()
            client.queue_frame()
            self.broker.file_request("logged.example.com")
            client.queue_frame()
            client.close()
            self.assert_no_threads_leak()
        joined = "\n".join(logs.output)
        self.assertRegex(joined, r"admin stream poll status=200 duration_ms=\d+ bytes=\d+ ok=true changed=true fanout=1")
        self.assertRegex(joined, r"admin stream open streams=1")
        self.assertRegex(joined, r"admin stream close streams=0 duration_ms=\d+ frames=2 bytes=\d+ reason=")


class LegacyModeTests(HubTestCase):
    spa = False

    def test_legacy_has_no_stream_and_answers_its_path_like_any_unknown_one(self):
        server = self.start_admin()
        self.assertIsNone(server.stream_hub)
        replies = []
        for path in (admin.STREAM_PATH, "/api/egress/nothing-here"):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", path, headers={"Cookie": f"{admin.SESSION_COOKIE_NAME}={SECRET}"})
            response = conn.getresponse()
            replies.append((response.status, response.getheader("Content-Type"), response.read()))
            conn.close()
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0][0], 404)
        self.assert_no_threads_leak()   # nothing but finished request threads


class ContractTests(unittest.TestCase):
    def test_the_event_schema_describes_the_stream_and_rejects_anything_else(self):
        schema = json.loads((REPO_ROOT / "admin" / "contract" / "sse_event.schema.json").read_text())
        self.assertNotIn("No stream exists", schema["description"])
        self.assertIn(admin.STREAM_PATH, schema["description"])
        snapshot = stub.build_queue()
        self.assertEqual(validate_document({"event": "queue", "data": snapshot}, "sse_event.schema.json"), [])
        self.assertNotEqual(validate_document({"event": "error", "data": snapshot}, "sse_event.schema.json"), [])
        self.assertNotEqual(validate_document({"event": "queue", "data": {"open": []}}, "sse_event.schema.json"), [])

    def test_change_key_ignores_only_the_per_poll_clock(self):
        base = stub.build_queue()
        aged = json.loads(json.dumps(base))
        aged["generated_at"] = "2031-01-01T00:00:00Z"
        for row in aged["open"]:
            row["age_seconds"] += 99
        self.assertEqual(admin._queue_change_key(base), admin._queue_change_key(aged))
        for mutate in (
            lambda q: q["open"][0].update(hit_count=q["open"][0]["hit_count"] + 1),
            lambda q: q["open"].pop(),
            lambda q: q["recent"].pop(),
            lambda q: q["open"][0].update(attempt=9),
        ):
            changed = json.loads(json.dumps(base))
            mutate(changed)
            self.assertNotEqual(admin._queue_change_key(base), admin._queue_change_key(changed))


if __name__ == "__main__":
    unittest.main()
