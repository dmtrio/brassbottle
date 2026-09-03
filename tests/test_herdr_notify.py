#!/usr/bin/env python3
"""Unit tests for herdr event-driven notifications."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import herdr_notify as notify  # noqa: E402


class CountAttachedClientsTests(unittest.TestCase):
    """Tests for count_attached_clients(/proc/net/unix parsing)."""

    def test_zero_attached_clients(self):
        proc_text = (
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 12345 /path/to/herdr.sock\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 12346 /path/to/herdr-client.sock\n"
        )
        count = notify.count_attached_clients(
            proc_text, "/path/to/herdr-client.sock"
        )
        self.assertEqual(count, 0)

    def test_one_attached_client(self):
        proc_text = (
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 12345 /path/to/herdr.sock\n"
            "0000000000000000: 00000003 00000000 00000000 0001 03 12346 /path/to/herdr-client.sock\n"
        )
        count = notify.count_attached_clients(
            proc_text, "/path/to/herdr-client.sock"
        )
        self.assertEqual(count, 1)

    def test_two_attached_clients(self):
        proc_text = (
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000003 00000000 00000000 0001 03 12346 /path/to/herdr-client.sock\n"
            "0000000000000000: 00000003 00000000 00000000 0001 03 12347 /path/to/herdr-client.sock\n"
        )
        count = notify.count_attached_clients(
            proc_text, "/path/to/herdr-client.sock"
        )
        self.assertEqual(count, 2)

    def test_ignores_other_socket_paths(self):
        proc_text = (
            "0000000000000000: 00000003 00000000 00000000 0001 03 12346 /other/socket.sock\n"
            "0000000000000000: 00000003 00000000 00000000 0001 03 12347 /path/to/herdr-client.sock\n"
        )
        count = notify.count_attached_clients(
            proc_text, "/path/to/herdr-client.sock"
        )
        self.assertEqual(count, 1)

    def test_listener_not_counted(self):
        # St=01 is the listener, not a connected client
        proc_text = (
            "0000000000000000: 00000002 00000000 00010000 0001 01 12345 /path/to/herdr-client.sock\n"
        )
        count = notify.count_attached_clients(
            proc_text, "/path/to/herdr-client.sock"
        )
        self.assertEqual(count, 0)


class ConfigFromEnvTests(unittest.TestCase):
    """compose emits NTFY_TOPIC=${NTFY_TOPIC:-}: present but empty is the
    common case and must mean the default topic, not topic ''."""

    def test_empty_topic_means_default(self):
        cfg = notify.config_from_env({"NTFY_URL": "https://ntfy.example/", "NTFY_TOPIC": ""})
        self.assertEqual(cfg.ntfy_topic, "djinn-agents")

    def test_missing_topic_means_default(self):
        cfg = notify.config_from_env({"NTFY_URL": "https://ntfy.example"})
        self.assertEqual(cfg.ntfy_topic, "djinn-agents")

    def test_explicit_values_are_kept_and_stripped(self):
        cfg = notify.config_from_env({
            "NTFY_URL": " https://ntfy.example ",
            "NTFY_TOPIC": " mine ",
            "NTFY_TOKEN": "tk",
            "CONTAINER_NAME": "coding",
            "HERDR_NOTIFY_STATES": "blocked, done",
            "HERDR_NOTIFY_COOLDOWN_SECONDS": "7",
        })
        self.assertEqual(cfg.ntfy_url, "https://ntfy.example")
        self.assertEqual(cfg.ntfy_topic, "mine")
        self.assertEqual(cfg.ntfy_token, "tk")
        self.assertEqual(cfg.container_name, "coding")
        self.assertEqual(cfg.notify_states, ["blocked", "done"])
        self.assertEqual(cfg.cooldown_seconds, 7.0)

    def test_defaults_for_everything_else(self):
        cfg = notify.config_from_env({"NTFY_URL": "https://ntfy.example", "NTFY_TOKEN": "",
                                      "HERDR_NOTIFY_COOLDOWN_SECONDS": "soon"})
        self.assertIsNone(cfg.ntfy_token)
        self.assertEqual(cfg.container_name, "container")
        self.assertEqual(cfg.notify_states, ["blocked", "done", "idle"])
        self.assertEqual(cfg.cooldown_seconds, 30.0)

    def test_empty_url_disables(self):
        self.assertEqual(notify.config_from_env({"NTFY_URL": " "}).ntfy_url, "")


class ShouldNotifyTests(unittest.TestCase):
    """Tests for should_notify status transition logic."""

    def test_working_to_idle_notifies(self):
        result = notify.should_notify("working", "idle", ["idle"])
        self.assertTrue(result)

    def test_blocked_to_idle_notifies(self):
        result = notify.should_notify("blocked", "idle", ["idle"])
        self.assertTrue(result)

    def test_unknown_to_idle_does_not_notify(self):
        result = notify.should_notify("unknown", "idle", ["idle"])
        self.assertFalse(result)

    def test_idle_to_idle_does_not_notify(self):
        result = notify.should_notify("idle", "idle", ["idle"])
        self.assertFalse(result)

    def test_working_to_blocked_notifies(self):
        result = notify.should_notify("working", "blocked", ["blocked"])
        self.assertTrue(result)

    def test_idle_to_blocked_notifies(self):
        result = notify.should_notify("idle", "blocked", ["blocked"])
        self.assertTrue(result)

    def test_blocked_to_done_notifies(self):
        result = notify.should_notify("blocked", "done", ["done"])
        self.assertTrue(result)

    def test_working_to_working_does_not_notify(self):
        result = notify.should_notify("working", "working", ["idle"])
        self.assertFalse(result)

    def test_states_list_respected(self):
        result = notify.should_notify("working", "blocked", ["idle"])
        self.assertFalse(result)
        result = notify.should_notify("working", "blocked", ["blocked", "done"])
        self.assertTrue(result)


class ContextLinesTests(unittest.TestCase):
    """Tests for context_lines text extraction."""

    def test_last_3_lines(self):
        text = "line1\nline2\nline3\nline4"
        result = notify.context_lines(text)
        self.assertEqual(result, "line2\nline3\nline4")

    def test_blank_lines_dropped(self):
        text = "line1\n\nline2\n  \nline3"
        result = notify.context_lines(text)
        self.assertEqual(result, "line1\nline2\nline3")

    def test_truncation_to_200_chars(self):
        long_line = "x" * 250
        text = f"short\n{long_line}"
        result = notify.context_lines(text)
        self.assertIn("x" * 200, result)
        self.assertNotIn("x" * 201, result)

    def test_empty_text_returns_fallback(self):
        result = notify.context_lines("")
        self.assertEqual(result, "<no recent output>")

    def test_only_blank_lines_returns_fallback(self):
        text = "  \n\n  \n"
        result = notify.context_lines(text)
        self.assertEqual(result, "<no recent output>")


class BuildPayloadTests(unittest.TestCase):
    """Tests for build_payload ntfy JSON construction."""

    def test_blocked_has_priority_4(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="blocked",
            workspace_label_or_id="workspace-1",
            context="waiting...",
            topic="djinn-agents",
            container_name="test",
        )
        self.assertEqual(payload["priority"], 4)

    def test_done_has_priority_3(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="done",
            workspace_label_or_id="workspace-1",
            context="finished",
            topic="djinn-agents",
            container_name="test",
        )
        self.assertEqual(payload["priority"], 3)

    def test_idle_has_priority_3(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="idle",
            workspace_label_or_id="workspace-1",
            context="idle...",
            topic="djinn-agents",
            container_name="test",
        )
        self.assertEqual(payload["priority"], 3)

    def test_title_format(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="idle",
            workspace_label_or_id="workspace-1",
            context="",
            topic="djinn-agents",
            container_name="test-container",
        )
        self.assertEqual(payload["title"], "djinn-test-container: claude idle")

    def test_message_format(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="idle",
            workspace_label_or_id="workspace-1",
            context="prompt> ",
            topic="djinn-agents",
            container_name="test",
        )
        self.assertEqual(
            payload["message"],
            "workspace-1 · w1:p1\nprompt> ",
        )

    def test_tags_contains_robot(self):
        payload = notify.build_payload(
            pane_id="w1:p1",
            agent="claude",
            status="idle",
            workspace_label_or_id="workspace-1",
            context="",
            topic="djinn-agents",
            container_name="test",
        )
        self.assertIn("robot", payload["tags"])


class NtfySinkSendTests(unittest.TestCase):
    """Tests for NtfySink.send with fake urlopen."""

    def test_send_posts_json_payload(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        sink = notify.NtfySink(
            "https://ntfy.example",
            "djinn-agents",
            urlopen=fake_urlopen,
        )
        payload = {
            "topic": "djinn-agents",
            "title": "test",
            "message": "message",
            "priority": 3,
            "tags": ["robot"],
        }
        with self.assertLogs("herdr_notify", level="INFO"):
            status = sink.send(payload)

        self.assertEqual(status, "200")
        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")

    def test_http_error_returns_status_code(self):
        def fake_urlopen(request, timeout=0):
            raise urllib.error.HTTPError(
                "https://ntfy.example/", 403, "Forbidden", {}, None
            )

        sink = notify.NtfySink(
            "https://ntfy.example",
            "djinn-agents",
            urlopen=fake_urlopen,
        )
        payload = {"topic": "test", "title": "test", "message": "msg"}
        with self.assertLogs("herdr_notify", level="WARNING"):
            status = sink.send(payload)

        self.assertEqual(status, "403")

    def test_url_error_returns_error(self):
        def fake_urlopen(request, timeout=0):
            raise urllib.error.URLError("boom")

        sink = notify.NtfySink(
            "https://ntfy.example",
            "djinn-agents",
            urlopen=fake_urlopen,
        )
        payload = {"topic": "test", "title": "test", "message": "msg"}
        with self.assertLogs("herdr_notify", level="WARNING"):
            status = sink.send(payload)

        self.assertEqual(status, "error")

    def test_authorization_header_when_token_set(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        sink = notify.NtfySink(
            "https://ntfy.example",
            "djinn-agents",
            token="secret-token",
            urlopen=fake_urlopen,
        )
        payload = {"topic": "test", "title": "test", "message": "msg"}
        with self.assertLogs("herdr_notify", level="INFO"):
            sink.send(payload)

        request = captured["request"]
        self.assertIn("Authorization", request.headers)
        self.assertEqual(
            request.headers["Authorization"],
            "Bearer secret-token",
        )

    def test_no_authorization_header_without_token(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        sink = notify.NtfySink(
            "https://ntfy.example",
            "djinn-agents",
            urlopen=fake_urlopen,
        )
        payload = {"topic": "test", "title": "test", "message": "msg"}
        with self.assertLogs("herdr_notify", level="INFO"):
            sink.send(payload)

        request = captured["request"]
        self.assertNotIn("Authorization", request.headers)


class PaneTrackerTests(unittest.TestCase):
    """Tests for PaneTracker state management."""

    def test_adopt_new_pane_returns_true(self):
        tracker = notify.PaneTracker()
        result = tracker.adopt("w1:p1", "working")
        self.assertTrue(result)

    def test_adopt_existing_pane_returns_false(self):
        tracker = notify.PaneTracker()
        tracker.adopt("w1:p1", "working")
        result = tracker.adopt("w1:p1", "idle")
        self.assertFalse(result)

    def test_apply_status_transition_yields_transition(self):
        tracker = notify.PaneTracker()
        tracker.adopt("w1:p1", "working")
        transition = tracker.apply_status("w1:p1", "claude", "idle", "w1")
        self.assertIsNotNone(transition)
        assert transition is not None
        self.assertEqual(transition.prev_status, "working")
        self.assertEqual(transition.new_status, "idle")

    def test_apply_status_same_status_yields_none(self):
        tracker = notify.PaneTracker()
        tracker.adopt("w1:p1", "working")
        transition = tracker.apply_status("w1:p1", "claude", "working", "w1")
        self.assertIsNone(transition)

    def test_apply_status_unknown_pane_adopts_and_yields_none(self):
        tracker = notify.PaneTracker()
        transition = tracker.apply_status("w1:p1", "claude", "idle", "w1")
        self.assertIsNone(transition)
        self.assertTrue(tracker.is_known("w1:p1"))

    def test_forget_pane(self):
        tracker = notify.PaneTracker()
        tracker.adopt("w1:p1", "working")
        tracker.forget("w1:p1")
        self.assertFalse(tracker.is_known("w1:p1"))

    def test_cooldown_tracking(self):
        tracker = notify.PaneTracker()
        tracker.record_push("w1:p1")
        time_since = tracker.time_since_push("w1:p1")
        self.assertGreaterEqual(time_since, 0)
        self.assertLess(time_since, 1.0)

    def test_time_since_push_infinity_for_unknown(self):
        tracker = notify.PaneTracker()
        time_since = tracker.time_since_push("w1:p1")
        self.assertEqual(time_since, float("inf"))

    def test_resubscribe_flag(self):
        tracker = notify.PaneTracker()
        self.assertFalse(tracker.needs_resubscribe())
        tracker.mark_resubscribe_needed()
        self.assertTrue(tracker.needs_resubscribe())
        tracker.clear_resubscribe_flag()
        self.assertFalse(tracker.needs_resubscribe())


class FakeHerdrServer(threading.Thread):
    """A fake herdr server in a temporary socket for testing."""

    def __init__(self, socket_path: str) -> None:
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.subscription_envelopes: list[dict[str, Any]] = []
        # hold_open: after the scripted envelopes, keep the stream open until
        # stop() — models a live server that has nothing more to say.
        self.hold_open = False
        # subscribe_error: answer events.subscribe with an error instead of
        # subscription_started (e.g. a stale pane_id).
        self.subscribe_error: str | None = None
        # stall: accept, read the request, then never answer (wedged server).
        self.stall = False
        # keep_open_after_response: answer but do not close (a server that
        # multiplexes on one connection) — the client must not wait for EOF.
        self.keep_open_after_response = False
        self._stop_event = threading.Event()
        self._ready = threading.Event()

    def run(self) -> None:
        """Run the server: accept connections and handle requests."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            Path(self.socket_path).unlink(missing_ok=True)
            sock.bind(self.socket_path)
            sock.listen(5)
            sock.settimeout(0.1)
            self._ready.set()
            while not self._stop_event.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                if self._stop_event.is_set():
                    break
                self._handle_connection(conn)
        finally:
            sock.close()
            Path(self.socket_path).unlink(missing_ok=True)

    def _handle_connection(self, conn: socket.socket) -> None:
        """Handle a single connection: read request, send response."""
        try:
            # Read request
            buffer = b""
            while b"\n" not in buffer:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk

            line_end = buffer.find(b"\n")
            request_line = buffer[:line_end].decode("utf-8")
            request = json.loads(request_line)
            self.requests.append(request)

            method = request.get("method")
            request_id = request.get("id")

            if self.stall:
                self._stop_event.wait()
                return

            if method == "events.subscribe":
                if self.subscribe_error is not None:
                    response = {
                        "id": "",
                        "error": {"code": "invalid_request", "message": self.subscribe_error},
                    }
                    conn.sendall(
                        (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
                    )
                    return
                # Stream subscription: send started, then envelopes, then close
                response = {
                    "id": request_id,
                    "result": {"type": "subscription_started"},
                }
                conn.sendall(
                    (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
                )

                # Send scripted envelopes
                for envelope in self.subscription_envelopes:
                    if self._stop_event.is_set():
                        break
                    conn.sendall(
                        (json.dumps(envelope, separators=(",", ":")) + "\n").encode(
                            "utf-8"
                        )
                    )
                if self.hold_open:
                    # Stay open with nothing more to say until the client
                    # hangs up or the server stops; the client must not need
                    # another event to act. Polled, so the single accept loop
                    # is free again once the client is gone.
                    conn.settimeout(0.05)
                    while not self._stop_event.is_set():
                        try:
                            if conn.recv(4096) == b"":
                                break  # peer closed
                        except socket.timeout:
                            continue
                        except OSError:
                            break
                # Close connection to end the subscription stream
            else:
                # Single-request methods: send response and close
                response = self.responses.get(
                    method,
                    {
                        "id": request_id,
                        "result": {"type": method, "version": "0.8.2"},
                    },
                )
                if "id" not in response:
                    response["id"] = request_id
                conn.sendall(
                    (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
                )
                if self.keep_open_after_response:
                    conn.settimeout(0.05)
                    while not self._stop_event.is_set():
                        try:
                            if conn.recv(4096) == b"":
                                break
                        except socket.timeout:
                            continue
                        except OSError:
                            break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Stop the server."""
        self._stop_event.set()

    def wait_ready(self, timeout: float = 1.0) -> bool:
        """Wait for server to be ready."""
        return self._ready.wait(timeout)


class HerdrClientTests(unittest.TestCase):
    """Tests for HerdrClient against a fake server."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmp_dir, "herdr.sock")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_request_works_across_two_calls(self):
        server = FakeHerdrServer(self.socket_path)
        server.responses["ping"] = {"id": "", "result": {"type": "pong", "version": "0.8.2"}}
        server.responses["session.snapshot"] = {
            "id": "",
            "result": {
                "snapshot": {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                            "agent": "claude",
                            "agent_status": "idle",
                        }
                    ]
                }
            },
        }
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))

        try:
            client = notify.HerdrClient(self.socket_path)
            resp1 = client.request("ping", {})
            self.assertEqual(resp1["result"]["type"], "pong")

            resp2 = client.request("session.snapshot", {})
            self.assertIn("snapshot", resp2["result"])
        finally:
            server.stop()
            server.join(timeout=1.0)

    def test_subscribe_yields_envelopes(self):
        server = FakeHerdrServer(self.socket_path)
        server.subscription_envelopes = [
            {
                "event": "pane_created",
                "data": {
                    "type": "pane_created",
                    "pane": {"pane_id": "w1:p1", "agent_status": "unknown"},
                },
            },
            {
                "event": "pane.agent_status_changed",
                "data": {
                    "type": "pane_agent_status_changed",
                    "pane_id": "w1:p1",
                    "agent": "claude",
                    "agent_status": "idle",
                    "workspace_id": "w1",
                },
            },
        ]
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))

        try:
            client = notify.HerdrClient(self.socket_path)
            envelopes = list(client.subscribe([{"type": "pane.created"}]))
            self.assertEqual(len(envelopes), 2)
            self.assertEqual(envelopes[0]["event"], "pane_created")
            self.assertEqual(envelopes[1]["event"], "pane.agent_status_changed")
        finally:
            server.stop()
            server.join(timeout=1.0)

    def test_request_times_out_on_wedged_server(self):
        server = FakeHerdrServer(self.socket_path)
        server.stall = True
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))
        try:
            client = notify.HerdrClient(self.socket_path, timeout=0.2)
            with self.assertRaises(OSError):
                client.request("ping", {})
        finally:
            server.stop()
            server.join(timeout=1.0)

    def test_request_returns_after_one_line_without_eof(self):
        # A server that answers but keeps the connection open must not make
        # request() wait for EOF.
        server = FakeHerdrServer(self.socket_path)
        server.keep_open_after_response = True
        server.responses["ping"] = {"id": "", "result": {"type": "pong", "version": "0.8.2"}}
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))
        try:
            client = notify.HerdrClient(self.socket_path, timeout=2.0)
            self.assertEqual(client.request("ping", {})["result"]["type"], "pong")
        finally:
            server.stop()
            server.join(timeout=1.0)

    def test_subscribe_error_response_raises(self):
        # A rejected subscribe (stale pane_id) must surface as OSError, not
        # as a stream that never yields.
        server = FakeHerdrServer(self.socket_path)
        server.subscribe_error = "unknown pane w7:p1"
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))
        try:
            client = notify.HerdrClient(self.socket_path)
            with self.assertRaises(OSError) as cm:
                list(client.subscribe([{"type": "pane.agent_status_changed", "pane_id": "w7:p1"}]))
            self.assertIn("unknown pane w7:p1", str(cm.exception))
        finally:
            server.stop()
            server.join(timeout=1.0)


class WaitForServerTests(unittest.TestCase):
    def test_backoff_doubles_until_ping_answers(self):
        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "herdr.sock")
        server = FakeHerdrServer(socket_path)
        server.responses["ping"] = {"id": "", "result": {"type": "pong", "version": "0.8.2"}}
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                server.start()
                self.assertTrue(server.wait_ready(timeout=2.0))

        try:
            with self.assertLogs("herdr_notify", level="INFO") as logs:
                notify.wait_for_server(socket_path, sleep=fake_sleep)
        finally:
            server.stop()
            server.join(timeout=1.0)
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertTrue(any("connected version=0.8.2" in line for line in logs.output))


class RunOnceIntegrationTests(unittest.TestCase):
    """End-to-end tests for run_once with a fake server."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmp_dir, "herdr.sock")
        self.client_socket_path = os.path.join(self.tmp_dir, "herdr-client.sock")
        self.proc_net_unix_path = os.path.join(self.tmp_dir, "proc_net_unix")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_transition_with_no_attached_clients(self):
        """Seeded w1:p1 working; live transition to idle; one ntfy POST."""
        server = FakeHerdrServer(self.socket_path)
        server.responses["session.snapshot"] = {
            "id": "",
            "result": {
                "snapshot": {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                            "agent": "claude",
                            "agent_status": "working",
                        }
                    ],
                    "workspaces": [
                        {"workspace_id": "w1", "label": "main"}
                    ],
                }
            },
        }
        server.responses["pane.read"] = {
            "id": "",
            "result": {"text": "prompt> "},
        }
        server.subscription_envelopes = [
            {
                "event": "pane_created",
                "data": {"pane_id": "w1:p1"},  # Replay
            },
            {
                "event": "pane_agent_detected",
                "data": {"pane_id": "w1:p1", "agent": "claude"},  # Replay
            },
            {
                "event": "pane.agent_status_changed",
                "data": {
                    "type": "pane_agent_status_changed",
                    "pane_id": "w1:p1",
                    "agent": "claude",
                    "agent_status": "idle",
                    "workspace_id": "w1",
                },
            },
        ]
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))

        # No attached clients
        Path(self.proc_net_unix_path).write_text(
            "0000000000000000: 00000002 00000000 00010000 0001 01 12345 /path/to/herdr-client.sock\n"
        )

        posted_payloads: list[dict[str, Any]] = []

        def fake_urlopen(request, timeout=0):
            body = json.loads(request.data.decode("utf-8"))
            posted_payloads.append(body)
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        try:
            with self.assertLogs("herdr_notify", level="DEBUG"):
                reason = notify.run_once(
                    herdr_socket=self.socket_path,
                    herdr_client_socket=self.client_socket_path,
                    proc_net_unix_path=self.proc_net_unix_path,
                    ntfy_url="https://ntfy.example",
                    ntfy_topic="djinn-agents",
                    ntfy_token=None,
                    container_name="test",
                    notify_states=["idle"],
                    cooldown_seconds=30,
                    urlopen=fake_urlopen,
                )
        finally:
            server.stop()
            server.join(timeout=1.0)

        self.assertEqual(reason, "stream_closed")
        self.assertEqual(len(posted_payloads), 1)
        payload = posted_payloads[0]
        self.assertEqual(payload["title"], "djinn-test: claude idle")
        self.assertIn("main", payload["message"])
        subscribe = [r for r in server.requests if r["method"] == "events.subscribe"]
        self.assertEqual(len(subscribe), 1)
        self.assertIn(
            {"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
            subscribe[0]["params"]["subscriptions"],
        )

    def test_suppressed_with_attached_client(self):
        """One attached client should suppress notification."""
        server = FakeHerdrServer(self.socket_path)
        server.responses["session.snapshot"] = {
            "id": "",
            "result": {
                "snapshot": {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                            "agent": "claude",
                            "agent_status": "working",
                        }
                    ],
                    "workspaces": [
                        {"workspace_id": "w1", "label": "main"}
                    ],
                }
            },
        }
        server.responses["pane.read"] = {
            "id": "",
            "result": {"text": "prompt> "},
        }
        server.subscription_envelopes = [
            {
                "event": "pane.agent_status_changed",
                "data": {
                    "pane_id": "w1:p1",
                    "agent": "claude",
                    "agent_status": "idle",
                    "workspace_id": "w1",
                },
            },
        ]
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))

        # One attached client
        Path(self.proc_net_unix_path).write_text(
            f"0000000000000000: 00000003 00000000 00000000 0001 03 12346 {self.client_socket_path}\n"
        )

        posted_payloads: list[dict[str, Any]] = []

        def fake_urlopen(request, timeout=0):
            body = json.loads(request.data.decode("utf-8"))
            posted_payloads.append(body)
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        try:
            with self.assertLogs("herdr_notify", level="INFO"):
                notify.run_once(
                    herdr_socket=self.socket_path,
                    herdr_client_socket=self.client_socket_path,
                    proc_net_unix_path=self.proc_net_unix_path,
                    ntfy_url="https://ntfy.example",
                    ntfy_topic="djinn-agents",
                    ntfy_token=None,
                    container_name="test",
                    notify_states=["idle"],
                    cooldown_seconds=30,
                    urlopen=fake_urlopen,
                )
        finally:
            server.stop()
            server.join(timeout=1.0)

        self.assertEqual(len(posted_payloads), 0)

    def test_new_pane_ends_cycle_and_next_cycle_subscribes_it(self):
        """A lone live pane_created for an unknown pane ends the cycle at once
        (the server has nothing further to say), and the next cycle's
        subscribe request lists the new pane."""
        server = FakeHerdrServer(self.socket_path)
        server.responses["session.snapshot"] = {
            "id": "",
            "result": {
                "snapshot": {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                            "agent": "claude",
                            "agent_status": "idle",
                        }
                    ],
                    "workspaces": [],
                }
            },
        }
        server.subscription_envelopes = [
            {
                "event": "pane_created",
                "data": {
                    "pane": {
                        "pane_id": "w9:p1",
                        "workspace_id": "w9",
                        "agent": None,
                        "agent_status": "unknown",
                    }
                },
            },
        ]
        server.hold_open = True
        server.start()
        self.assertTrue(server.wait_ready(timeout=2.0))

        Path(self.proc_net_unix_path).write_text("")
        posted_payloads: list[dict[str, Any]] = []

        def fake_urlopen(request, timeout=0):
            posted_payloads.append(json.loads(request.data.decode("utf-8")))
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        tracker = notify.PaneTracker()
        kwargs = dict(
            herdr_socket=self.socket_path,
            herdr_client_socket=self.client_socket_path,
            proc_net_unix_path=self.proc_net_unix_path,
            ntfy_url="https://ntfy.example",
            ntfy_topic="djinn-agents",
            ntfy_token=None,
            container_name="test",
            notify_states=["idle"],
            cooldown_seconds=30,
            urlopen=fake_urlopen,
            tracker=tracker,
        )
        results: list[str] = []
        worker = threading.Thread(
            target=lambda: results.append(notify.run_once(**kwargs)), daemon=True
        )
        try:
            with self.assertLogs("herdr_notify", level="INFO"):
                worker.start()
                worker.join(timeout=3.0)
                # Still running here means run_once waited for another event
                # after the new pane instead of ending the cycle.
                self.assertFalse(worker.is_alive(), "run_once did not end on the new pane")
            self.assertEqual(results, ["resubscribe"])
            self.assertEqual(posted_payloads, [])

            # Second cycle: the snapshot now knows w9:p1; the server closes
            # right after the (empty) replay.
            server.responses["session.snapshot"]["result"]["snapshot"]["panes"].append(
                {"pane_id": "w9:p1", "workspace_id": "w9", "agent": "claude",
                 "agent_status": "working"}
            )
            server.subscription_envelopes = []
            server.hold_open = False
            with self.assertLogs("herdr_notify", level="INFO"):
                self.assertEqual(notify.run_once(**kwargs), "stream_closed")
        finally:
            server.stop()
            server.join(timeout=1.0)

        subscribes = [r for r in server.requests if r["method"] == "events.subscribe"]
        self.assertEqual(len(subscribes), 2)
        self.assertNotIn(
            {"type": "pane.agent_status_changed", "pane_id": "w9:p1"},
            subscribes[0]["params"]["subscriptions"],
        )
        self.assertIn(
            {"type": "pane.agent_status_changed", "pane_id": "w9:p1"},
            subscribes[1]["params"]["subscriptions"],
        )


if __name__ == "__main__":
    unittest.main()
