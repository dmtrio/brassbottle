#!/usr/bin/env python3
"""Unit tests for the host-side egress approval broker daemon."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
import egress_broker_host as broker  # noqa: E402
import egress_log  # noqa: E402
import egress_notify  # noqa: E402
from egress_test_sync import (  # noqa: E402
    join_thread_or_fail,
    wait_for_broker_open_request,
    wait_for_tcp_listening,
)

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
AUG_END = datetime(2026, 8, 31, 23, 0, 0, tzinfo=timezone.utc)
SEP_START = datetime(2026, 9, 1, 0, 5, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class EgressBrokerHostTests(unittest.TestCase):
    OPERATOR_TOKEN = "operator-test-token"

    def _broker(
        self,
        root: Path,
        clock: FakeClock | None = None,
        hold_seconds: int = 1,
    ) -> broker.EgressBroker:
        clock = clock or FakeClock(NOW)
        return broker.EgressBroker(
            root,
            repo_root=REPO_ROOT,
            now_fn=clock.now,
            hold_seconds_default=hold_seconds,
        )

    def _log_records(self, root: Path, when: datetime = NOW) -> list[dict]:
        path = egress_log._log_path(root, egress_log._month_filename(when))
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _http_server(
        self,
        egress_root: Path,
        b: broker.EgressBroker,
        tokens_dir: Path,
        *,
        operator_token: str | None = None,
    ) -> broker.EgressBrokerHTTPServer:
        store = broker.BottleTokenStore(tokens_dir)
        return broker.EgressBrokerHTTPServer(
            ("127.0.0.1", 0),
            b,
            store,
            operator_token or self.OPERATOR_TOKEN,
        )

    def test_normalize_host_table(self):
        cases = [
            ("*.neon.tech", "neon.tech"),
            ("https://docs.stripe.com/foo", "docs.stripe.com"),
            ("Docs.Stripe.COM", "docs.stripe.com"),
            ("api.github.com:443", "api.github.com"),
            ("http://user@host.example.com/path", "host.example.com"),
        ]
        for raw, expected in cases:
            self.assertEqual(broker.normalize_host(raw), expected)

    def test_normalize_host_rejects_invalid(self):
        for raw in ("", "localhost", "*.com", "notld", "bad..host.com", "-bad.com"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    broker.normalize_host(raw)

    def test_normalize_host_rejects_ip_literals(self):
        with self.assertRaises(ValueError):
            broker.normalize_host("192.0.2.55")

    def test_dedupe_same_key_one_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=1)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            open_id = wait_for_broker_open_request(b)
            second_body, second_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
            )
            self.assertEqual(second_id, open_id)
            self.assertEqual(second_body["decision"], "pending")
            b.decide(open_id, "deny")
            join_thread_or_fail(thread, label="file_request")
            requested = [
                r for r in self._log_records(root) if r.get("kind") == "requested"
            ]
            self.assertEqual(len(requested), 1)

    def test_previously_allowed_host_files_fresh_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "old-req",
                ts=NOW - timedelta(hours=2),
                container="coding-brassbottle",
                host="neon.tech",
                port=443,
            )
            log.append("allowed", "old-req", ts=NOW - timedelta(hours=1), scope="live")

            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=1)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            self.assertNotEqual(request_id, "old-req")
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")
            requested = [
                r for r in self._log_records(root) if r.get("kind") == "requested"
            ]
            self.assertEqual(len(requested), 2)

    def test_hit_coalescing_tight_loop_few_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            for _ in range(100):
                with b._lock:
                    state = b._requests[request_id]
                    b._record_hit(state)
                clock.advance(0.1)
            with b._lock:
                b._flush_hits(b._requests[request_id])
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")
            hits = [r for r in self._log_records(root) if r.get("kind") == "hit"]
            self.assertGreaterEqual(len(hits), 2)
            self.assertLess(len(hits), 20)

    def test_stale_sweep_denies_after_24h(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW - timedelta(hours=25))
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            clock.advance(25 * 3600)
            closed = b.sweep_stale()
            self.assertEqual(closed, 1)
            join_thread_or_fail(thread, label="file_request")
            denied = [
                r
                for r in self._log_records(root, clock.now())
                if r.get("kind") == "denied" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0].get("reason"), "stale")

    def test_auth_missing_and_incorrect_bearer_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "bottle-a.token").write_text("test-token\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "neon.tech", "port": 443}),
                    {"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
                conn.close()

                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "neon.tech", "port": 443}),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer wrong",
                    },
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_token_for_bottle_a_cannot_file_for_bottle_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "bottle-a.token").write_text("token-a\n", encoding="utf-8")
            (tokens_dir / "bottle-b.token").write_text("token-b\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps(
                        {
                            "container": "bottle-b",
                            "host": "neon.tech",
                            "port": 443,
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer token-a",
                    },
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.FORBIDDEN)
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_auth_correct_bearer_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "my-bottle.token").write_text("good-token\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps(
                        {
                            "container": "my-bottle",
                            "host": "neon.tech",
                            "port": 443,
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer good-token",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body["decision"], "pending")
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_firewall_save_unreachable_in_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
                argv = mocked.call_args[0][0]
                self.assertNotIn("firewall", argv)
            join_thread_or_fail(thread, label="file_request")

    def test_approve_live_builds_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
                argv = mocked.call_args[0][0]
                self.assertEqual(
                    argv,
                    [
                        str(REPO_ROOT / "bin" / "allow-egress.sh"),
                        "coding-brassbottle",
                        "neon.tech",
                        "--save",
                        "none",
                    ],
                )
            join_thread_or_fail(thread, label="file_request")

    def test_approve_manifest_builds_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="manifest")
                argv = mocked.call_args[0][0]
                self.assertEqual(argv[-1], "yml")
            join_thread_or_fail(thread, label="file_request")

    def test_second_daemon_instance_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            lock_path = egress_root / broker.LOCK_FILENAME
            first = broker.DaemonLock(lock_path)
            first.acquire()
            second = broker.DaemonLock(lock_path)
            with self.assertRaises(broker.DaemonAlreadyRunning):
                second.acquire()
            first.release()

    def test_decide_releases_long_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)

            result: dict[str, object] = {}
            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            open_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(open_id, "allow", scope="manifest")
            join_thread_or_fail(thread, label="file_request waiter")
            self.assertEqual(
                result["body"],
                {"decision": "allow", "scope": "manifest"},
            )

    def test_health_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("token\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body, {"status": "ok"})
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_queue_requires_operator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/queue")
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
                conn.close()

                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "GET",
                    "/queue",
                    headers={"Authorization": "Bearer wrong"},
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_queue_with_operator_token_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "GET",
                    "/queue",
                    headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"},
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body["open"], [])
                self.assertEqual(body["count"], 0)
                self.assertTrue(isinstance(body["generated_at"], str))
                self.assertTrue(body["generated_at"].endswith("Z"))
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_queue_returns_details_ordering_and_host_is_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps(
                        {
                            "host": "docs.stripe.com",
                            "port": 443,
                            "uid": 1000,
                            "comm": "curl",
                            "reason": "npm install",
                            "hold_seconds": 0,
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                    },
                )
                first = json.loads(conn.getresponse().read().decode("utf-8"))
                first_id = first["request_id"]
                conn.close()

                clock.advance(3)
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps(
                        {
                            "host": "192.0.2.55",
                            "port": 5432,
                            "uid": 1001,
                            "comm": "python",
                            "reason": "db connect",
                            "hold_seconds": 0,
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                    },
                )
                second = json.loads(conn.getresponse().read().decode("utf-8"))
                second_id = second["request_id"]
                conn.close()

                clock.advance(2)
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "GET",
                    "/queue",
                    headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"},
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body["count"], 2)
                self.assertEqual([item["request_id"] for item in body["open"]], [first_id, second_id])

                first_row = body["open"][0]
                second_row = body["open"][1]
                self.assertEqual(first_row["container"], "c")
                self.assertEqual(first_row["host"], "docs.stripe.com")
                self.assertEqual(first_row["port"], 443)
                self.assertFalse(first_row["host_is_ip"])
                self.assertGreaterEqual(first_row["age_seconds"], 0)
                self.assertEqual(first_row["hit_count"], 1)
                self.assertEqual(first_row["uid"], 1000)
                self.assertEqual(first_row["comm"], "curl")
                self.assertEqual(first_row["reason"], "npm install")

                self.assertEqual(second_row["host"], "192.0.2.55")
                self.assertEqual(second_row["port"], 5432)
                self.assertTrue(second_row["host_is_ip"])
                self.assertGreaterEqual(second_row["age_seconds"], 0)
                self.assertEqual(second_row["uid"], 1001)
                self.assertEqual(second_row["comm"], "python")
                self.assertEqual(second_row["reason"], "db connect")
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_queue_excludes_decided_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "docs.stripe.com", "port": 443, "hold_seconds": 0}),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                    },
                )
                body = json.loads(conn.getresponse().read().decode("utf-8"))
                request_id = body["request_id"]
                conn.close()

                b.decide(request_id, "deny")

                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "GET",
                    "/queue",
                    headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"},
                )
                resp = conn.getresponse()
                queue = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(queue["open"], [])
                self.assertEqual(queue["count"], 0)
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_queue_coalesced_hits_increase_hit_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                for _ in range(2):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/egress",
                        json.dumps({"host": "docs.stripe.com", "port": 443, "hold_seconds": 0}),
                        {
                            "Content-Type": "application/json",
                            "Authorization": "Bearer tok",
                        },
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, HTTPStatus.OK)
                    _ = resp.read()
                    conn.close()

                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "GET",
                    "/queue",
                    headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"},
                )
                body = json.loads(conn.getresponse().read().decode("utf-8"))
                self.assertEqual(body["count"], 1)
                self.assertGreaterEqual(body["open"][0]["hit_count"], 2)
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_get_health_no_auth_and_unknown_get_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(json.loads(resp.read().decode("utf-8")), {"status": "ok"})
                conn.close()

                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/nope")
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.NOT_FOUND)
                self.assertEqual(json.loads(resp.read().decode("utf-8")), {"error": "not found"})
                conn.close()
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")

    def test_queue_snapshot_consumes_request_details_for_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=1)
            first, _ = b.file_request("coding-brassbottle", "docs.stripe.com", 443, hold_seconds=0)
            second, _ = b.file_request("coding-brassbottle", "api.github.com", 443, hold_seconds=0)
            first_id = first["request_id"]
            second_id = second["request_id"]

            fake_details = {
                first_id: egress_log.RequestDetails(
                    request_id=first_id,
                    container="coding-brassbottle",
                    host="docs.stripe.com",
                    port=443,
                    hit_count=7,
                    uid=1234,
                    comm="curl",
                    reason="npm install",
                )
            }
            with mock.patch.object(
                broker, "request_details_for_ids", return_value=fake_details
            ) as mocked:
                snapshot = b.queue_snapshot()

            mocked.assert_called_once()
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(len(snapshot["open"]), 1)
            self.assertEqual(snapshot["open"][0]["request_id"], first_id)
            self.assertEqual(snapshot["open"][0]["hit_count"], 7)
            self.assertNotEqual(snapshot["open"][0]["request_id"], second_id)

    def test_rebuild_across_month_rotation_keeps_dedupe_and_approvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-a",
                ts=AUG_END,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
            )
            log.append("notified", "req-a", ts=AUG_END)

            clock = FakeClock(SEP_START)
            b = self._broker(root, clock, hold_seconds=60)

            self.assertIn("req-a", b._requests)
            state = b._requests["req-a"]
            self.assertEqual(state.container, "coding-brassbottle")
            self.assertEqual(state.host, "docs.stripe.com")
            self.assertEqual(state.port, 443)
            self.assertEqual(
                b._key_index[("coding-brassbottle", "docs.stripe.com", 443)],
                "req-a",
            )

            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide("req-a", "allow", scope="live")
                argv = mocked.call_args[0][0]
                self.assertEqual(argv[2], "docs.stripe.com")

            folded = log.fold_queue(now=SEP_START)
            self.assertEqual(folded.open_requests, {})
            payload = json.dumps(
                {
                    request_id: {
                        "state": req.state,
                        "container": req.container,
                        "host": req.host,
                        "port": req.port,
                        "opened_at": req.opened_at,
                    }
                    for request_id, req in folded.open_requests.items()
                }
            )
            self.assertNotIn("docs.stripe.com", payload)

    def test_ip_destination_accepted_and_filed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "192.0.2.55", "port": 5432}),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body["decision"], "pending")
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")
            requested = [
                r for r in self._log_records(egress_root) if r.get("kind") == "requested"
            ]
            self.assertEqual(len(requested), 1)
            self.assertEqual(requested[0]["host"], "192.0.2.55")
            self.assertTrue(requested[0].get("host_is_ip"))

    def test_ipv6_literal_accepted(self):
        host, is_ip = broker.normalize_destination("2001:db8::1")
        self.assertTrue(is_ip)
        self.assertEqual(host, "2001:db8::1")

    def test_approve_ip_never_invokes_allow_egress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request(
                    "coding-brassbottle",
                    "192.0.2.55",
                    5432,
                    host_is_ip=True,
                )
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                err = b.decide(request_id, "allow", scope="live")
                mocked.assert_not_called()
            self.assertEqual(err, broker.IP_REQUIRES_CIDR_REASON)
            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(
                result["body"],
                {"decision": "error", "reason": broker.IP_REQUIRES_CIDR_REASON},
            )
            with b._lock:
                self.assertIn(request_id, b._requests)
            allowed = [
                r for r in self._log_records(root) if r.get("kind") == "allowed"
            ]
            self.assertEqual(allowed, [])
            failed = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "apply_failed" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(failed), 1)
            self.assertIn("egress_cidrs", failed[0].get("reason", ""))

    def test_apply_failure_keeps_request_open_and_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=1)
                err = b.decide(request_id, "allow", scope="live")
                self.assertEqual(err, broker.APPLY_FAILED_REASON)
            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(
                result["body"],
                {"decision": "error", "reason": broker.APPLY_FAILED_REASON},
            )
            with b._lock:
                self.assertIn(request_id, b._requests)
            allowed = [r for r in self._log_records(root) if r.get("kind") == "allowed"]
            self.assertEqual(allowed, [])
            failed = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "apply_failed" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(failed), 1)

    def test_success_appends_allowed_once_and_closes_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                err = b.decide(request_id, "allow", scope="live")
                self.assertIsNone(err)
            join_thread_or_fail(thread, label="file_request")
            with b._lock:
                self.assertNotIn(request_id, b._requests)
            allowed = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "allowed" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(allowed), 1)

    def test_resolve_base_path_expands_user_and_trims(self):
        """DJINN_HOME=~/djinn must not create a directory literally named '~'.

        ./.env is sourced by bash, so DJINN_HOME="$HOME/x" arrives expanded —
        but a literal ~/x does not, and Path("~/x") would silently make a "~"
        directory in the cwd. The symptom is a queue nothing ever writes to.
        """
        with mock.patch.dict(os.environ, {"DJINN_HOME": "~/djinn"}):
            self.assertEqual(
                broker.resolve_base_path(""),
                Path.home() / "djinn",
            )
        with mock.patch.dict(os.environ, {"DJINN_HOME": "  ~/djinn  "}):
            self.assertEqual(
                broker.resolve_base_path(""),
                Path.home() / "djinn",
            )
        # an explicit --base-path is expanded too
        self.assertEqual(broker.resolve_base_path("~/djinn"), Path.home() / "djinn")


    def test_apply_runs_before_allowed_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            order: list[str] = []

            def track_run(*args, **kwargs) -> mock.Mock:
                order.append("apply")
                return mock.Mock(returncode=0)

            original_append = b._log.append

            def track_append(kind: str, request_id: str, **kwargs: object) -> None:
                if kind == "allowed":
                    order.append("allowed")
                original_append(kind, request_id, **kwargs)

            with mock.patch("subprocess.run", side_effect=track_run):
                with mock.patch.object(b._log, "append", track_append):
                    b.decide(request_id, "allow", scope="live")
            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(order, ["apply", "allowed"])

    def test_bottle_token_store_reload_on_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens_dir = Path(tmp) / "tokens"
            tokens_dir.mkdir()
            store = broker.BottleTokenStore(tokens_dir)
            self.assertIsNone(store.resolve_bottle("new-token"))
            (tokens_dir / "late.token").write_text("new-token\n", encoding="utf-8")
            self.assertEqual(store.resolve_bottle("new-token"), "late")

    def test_client_request_id_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=1)
            body, request_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
                request_id="deadbeef",
            )
            self.assertEqual(request_id, "deadbeef")
            self.assertEqual(body["decision"], "pending")
            self.assertIn("deadbeef", b._requests)
            requested = [
                r for r in self._log_records(root) if r.get("kind") == "requested"
            ]
            self.assertEqual(requested[0]["request_id"], "deadbeef")

    def test_duplicate_request_id_coalesces_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
                kwargs={"request_id": "cafebabe"},
            )
            thread.start()
            wait_for_broker_open_request(b)
            second_body, second_id = b.file_request(
                "coding-brassbottle",
                "other.example.com",
                80,
                request_id="cafebabe",
            )
            self.assertEqual(second_id, "cafebabe")
            self.assertEqual(second_body["decision"], "pending")
            self.assertEqual(len(b._requests), 1)
            requested = [
                r for r in self._log_records(root) if r.get("kind") == "requested"
            ]
            self.assertEqual(len(requested), 1)
            b.decide("cafebabe", "deny")
            thread.join(timeout=5)

    def test_malformed_request_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=1)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.file_request(
                    "coding-brassbottle",
                    "neon.tech",
                    443,
                    request_id="not-valid",
                )

    def test_http_malformed_request_id_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps(
                        {
                            "host": "neon.tech",
                            "port": 443,
                            "request_id": "BAD-ID",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(body["error"], "invalid request_id")
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_host_covered_by_zone(self):
        self.assertTrue(broker.host_covered_by_zone("docs.stripe.com", "stripe.com"))
        self.assertTrue(broker.host_covered_by_zone("stripe.com", "stripe.com"))
        self.assertFalse(broker.host_covered_by_zone("notstripe.com", "stripe.com"))

    def test_decide_allow_for_zone_releases_matching_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            results: dict[str, dict[str, object]] = {}

            def waiter(host: str) -> None:
                body, _ = b.file_request("coding-brassbottle", host, 443)
                results[host] = body

            threads = [
                threading.Thread(target=waiter, args=("docs.stripe.com",)),
                threading.Thread(target=waiter, args=("api.github.com",)),
            ]
            for thread in threads:
                thread.start()
            wait_for_broker_open_request(b, count=2)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                result = b.decide_allow_for_zone(
                    "coding-brassbottle",
                    "stripe.com",
                    scope="live",
                )
            self.assertEqual(len(result.decided), 1)
            self.assertEqual(result.apply_failures, [])
            github_id = next(
                rid
                for rid, state in b._requests.items()
                if state.host == "api.github.com"
            )
            self.assertIsNone(b._requests[github_id].decision)
            b.decide(github_id, "deny")
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(
                results["docs.stripe.com"],
                {"decision": "allow", "scope": "live"},
            )
            self.assertEqual(results["api.github.com"], {"decision": "deny"})

    def test_decide_allow_for_zone_skip_is_logged_not_swallowed(self):
        """finding #5: decide_allow_for_zone's sweep used to swallow a
        per-candidate EgressBrokerHostError with a bare `except: continue` —
        now it logs one INFO line naming the request and the reason before
        moving on. A synthetic OpenRequestState is injected directly (no
        real file_request/thread needed) so decide() can be mocked to raise
        deterministically."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            state = broker.OpenRequestState(
                request_id="deadbeef",
                container="coding-brassbottle",
                host="neon.tech",
                port=443,
                opened_at=NOW,
            )
            with b._lock:
                b._requests["deadbeef"] = state
                b._key_index[("coding-brassbottle", "neon.tech", 443)] = "deadbeef"

            with mock.patch.object(
                b, "decide", side_effect=broker.EgressBrokerHostError("already decided")
            ):
                with self.assertLogs(broker.LOG, level="INFO") as captured:
                    result = b.decide_allow_for_zone("coding-brassbottle", "neon.tech")

            self.assertEqual(result.decided, [])
            self.assertEqual(result.apply_failures, [])
            skip_lines = [
                r.getMessage()
                for r in captured.records
                if "decide_allow_for_zone skip" in r.getMessage()
            ]
            self.assertEqual(len(skip_lines), 1)
            self.assertIn("request_id=deadbeef", skip_lines[0])
            self.assertIn("reason=already decided", skip_lines[0])

    def test_decide_deny_for_zone_skip_is_logged_not_swallowed(self):
        """finding #5: same fix as decide_allow_for_zone, mirrored for the
        deny sweep."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            state = broker.OpenRequestState(
                request_id="deadbeef",
                container="coding-brassbottle",
                host="neon.tech",
                port=443,
                opened_at=NOW,
            )
            with b._lock:
                b._requests["deadbeef"] = state
                b._key_index[("coding-brassbottle", "neon.tech", 443)] = "deadbeef"

            with mock.patch.object(
                b, "decide", side_effect=broker.EgressBrokerHostError("already decided")
            ):
                with self.assertLogs(broker.LOG, level="INFO") as captured:
                    decided = b.decide_deny_for_zone("coding-brassbottle", "neon.tech")

            self.assertEqual(decided, [])
            skip_lines = [
                r.getMessage()
                for r in captured.records
                if "decide_deny_for_zone skip" in r.getMessage()
            ]
            self.assertEqual(len(skip_lines), 1)
            self.assertIn("request_id=deadbeef", skip_lines[0])
            self.assertIn("reason=already decided", skip_lines[0])

    def test_decide_endpoint_requires_operator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("bottle-tok\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/decide",
                    json.dumps(
                        {
                            "container": "coding-brassbottle",
                            "host": "docs.stripe.com",
                            "decision": "allow",
                            "scope": "live",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer bottle-tok",
                    },
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_releases_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("bottle-tok\n", encoding="utf-8")
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
                result["body"] = body

            waiter_thread = threading.Thread(target=waiter)
            waiter_thread.start()
            wait_for_broker_open_request(b)
            try:
                with mock.patch("subprocess.run") as mocked:
                    mocked.return_value = mock.Mock(returncode=0)
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "stripe.com",
                                "decision": "allow",
                                "scope": "live",
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.OK)
                    self.assertEqual(len(body["decided"]), 1)
                    self.assertEqual(body["apply_failures"], [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
            waiter_thread.join(timeout=5)
            self.assertEqual(result["body"], {"decision": "allow", "scope": "live"})

    def test_decide_endpoint_allow_ip_literal_reports_apply_failure_and_keeps_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            waiter_result: dict[str, object] = {}

            def waiter() -> None:
                body, request_id = b.file_request("coding-brassbottle", "192.0.2.55", 443)
                waiter_result["body"] = body
                waiter_result["request_id"] = request_id

            waiter_thread = threading.Thread(target=waiter)
            waiter_thread.start()
            request_id = wait_for_broker_open_request(b)
            try:
                status, body = self._post_decide(
                    host,
                    port,
                    {
                        "container": "coding-brassbottle",
                        "host": "192.0.2.55",
                        "decision": "allow",
                        "scope": "live",
                    },
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(body["decided"], [])
            self.assertEqual(
                body["apply_failures"],
                [{"request_id": request_id, "reason": broker.IP_REQUIRES_CIDR_REASON}],
            )
            with b._lock:
                self.assertIn(request_id, b._requests)
            open_ids = egress_log.EgressLog(egress_root).fold_queue(now=b.now()).open_requests
            self.assertIn(request_id, open_ids)
            b.decide(request_id, "deny")
            join_thread_or_fail(waiter_thread, label="file_request")
            self.assertEqual(
                waiter_result["body"],
                {"decision": "error", "reason": broker.IP_REQUIRES_CIDR_REASON},
            )

    def test_decide_endpoint_allow_apply_failed_reports_failure_and_keeps_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            waiter_result: dict[str, object] = {}

            def waiter() -> None:
                body, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
                waiter_result["body"] = body
                waiter_result["request_id"] = request_id

            waiter_thread = threading.Thread(target=waiter)
            waiter_thread.start()
            request_id = wait_for_broker_open_request(b)
            try:
                with mock.patch("subprocess.run") as mocked:
                    mocked.return_value = mock.Mock(returncode=1)
                    status, body = self._post_decide(
                        host,
                        port,
                        {
                            "container": "coding-brassbottle",
                            "host": "stripe.com",
                            "decision": "allow",
                            "scope": "live",
                        },
                    )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(body["decided"], [])
            self.assertEqual(
                body["apply_failures"],
                [{"request_id": request_id, "reason": broker.APPLY_FAILED_REASON}],
            )
            with b._lock:
                self.assertIn(request_id, b._requests)
            open_ids = egress_log.EgressLog(egress_root).fold_queue(now=b.now()).open_requests
            self.assertIn(request_id, open_ids)
            b.decide(request_id, "deny")
            join_thread_or_fail(waiter_thread, label="file_request")
            self.assertEqual(
                waiter_result["body"],
                {"decision": "error", "reason": broker.APPLY_FAILED_REASON},
            )

    def test_decide_deny_for_zone_releases_matching_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            results: dict[str, dict[str, object]] = {}

            def waiter(container: str, host: str, key: str) -> None:
                body, _ = b.file_request(container, host, 443)
                results[key] = body

            threads = [
                threading.Thread(
                    target=waiter,
                    args=("coding-brassbottle", "docs.stripe.com", "docs.stripe.com"),
                ),
                threading.Thread(
                    target=waiter,
                    args=("coding-brassbottle", "api.stripe.com", "api.stripe.com"),
                ),
                threading.Thread(
                    target=waiter,
                    args=("coding-brassbottle", "example.com", "example.com"),
                ),
                threading.Thread(
                    target=waiter,
                    args=("other-container", "docs.stripe.com", "other-container"),
                ),
            ]
            for thread in threads:
                thread.start()
            wait_for_broker_open_request(b, count=4)
            with b._lock:
                ids_by_key = {
                    (state.container, state.host): rid
                    for rid, state in b._requests.items()
                }
            decided = b.decide_deny_for_zone(
                "coding-brassbottle",
                "stripe.com",
                reason="not needed",
            )
            threads[0].join(timeout=5)
            threads[1].join(timeout=5)
            self.assertEqual(
                set(decided),
                {
                    ids_by_key[("coding-brassbottle", "docs.stripe.com")],
                    ids_by_key[("coding-brassbottle", "api.stripe.com")],
                },
            )
            self.assertEqual(
                results["docs.stripe.com"],
                {"decision": "deny"},
            )
            self.assertEqual(
                results["api.stripe.com"],
                {"decision": "deny"},
            )
            with b._lock:
                open_hosts = [
                    (state.container, state.host) for state in b._requests.values()
                ]
            self.assertIn(("coding-brassbottle", "example.com"), open_hosts)
            self.assertIn(("other-container", "docs.stripe.com"), open_hosts)
            denied = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "denied" and r.get("request_id") in decided
            ]
            self.assertEqual(len(denied), 2)
            for record in denied:
                self.assertEqual(record.get("reason"), "not needed")
            for rid, state in list(b._requests.items()):
                b.decide(rid, "deny")
            for thread in threads:
                thread.join(timeout=5)

    def test_decide_endpoint_deny_releases_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("bottle-tok\n", encoding="utf-8")
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
                result["body"] = body

            waiter_thread = threading.Thread(target=waiter)
            waiter_thread.start()
            wait_for_broker_open_request(b)
            try:
                with mock.patch("subprocess.run") as mocked:
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "stripe.com",
                                "decision": "deny",
                                "reason": "typo host",
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.OK)
                    self.assertEqual(len(body["decided"]), 1)
                    self.assertNotIn("apply_failures", body)
                    mocked.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=5)
            waiter_thread.join(timeout=5)
            self.assertEqual(result["body"], {"decision": "deny"})

    def test_decide_endpoint_rejects_unknown_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/decide",
                    json.dumps(
                        {
                            "container": "coding-brassbottle",
                            "host": "stripe.com",
                            "decision": "maybe",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(body["error"], "decision must be allow or deny")
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_rejects_invalid_scope(self):
        # "live"/"manifest" are allow-only scopes; deny's vocabulary is
        # once/bottle/global (deviation #6: per-decision validation replaces
        # the old blanket "scope only applies to allow" rejection).
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                for bad_scope in ("live", "manifest", "banana"):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "stripe.com",
                                "decision": "deny",
                                "scope": bad_scope,
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(body["error"], "invalid scope")
                    conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_unhashable_scope_returns_400_not_a_crash(self):
        """finding #3: `scope not in VALID_DECIDE_SCOPES` hashes scope
        (VALID_DECIDE_SCOPES is a frozenset) — an unhashable JSON value
        (a list or an object) used to raise TypeError instead of producing a
        400, which escapes the handler thread with no response sent at all.
        Assert BOTH a clean 400 for the bad request AND that the daemon
        keeps serving normally afterward (the handler thread surviving, not
        just this one connection)."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                for bad_scope in (["bottle"], {}):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "stripe.com",
                                "decision": "deny",
                                "scope": bad_scope,
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(body["error"], "invalid scope")
                    conn.close()

                # The daemon must still serve the NEXT request normally —
                # proof the handler thread didn't crash/hang on the
                # unhashable value.
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/decide",
                    json.dumps(
                        {
                            "container": "coding-brassbottle",
                            "host": "stripe.com",
                            "decision": "deny",
                            "scope": "once",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_scope_validation_matrix(self):
        """once/bottle/global are all accepted; bottle/global additionally
        persist the CALLER-NAMED zone to disk even with no open request
        covering it (finding #2: persist_deny writes unconditionally, not
        only as an open-request side effect) — assert the file content, not
        just the HTTP status.
        """
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "coding-brassbottle.token").write_text("tok\n", encoding="utf-8")
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                for good_scope, expect_persisted_scope, domain in (
                    ("once", None, "once.example.com"),
                    ("bottle", "coding-brassbottle", "bottle.example.com"),
                    ("global", "global", "global.example.com"),
                ):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": domain,
                                "decision": "deny",
                                "scope": good_scope,
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.OK)
                    # No open requests match any of these hosts, so nothing
                    # gets released either way.
                    self.assertEqual(body["decided"], [])
                    conn.close()

                    if expect_persisted_scope is None:
                        self.assertNotIn("persisted", body)
                        self.assertIsNone(b._denylist.matches("coding-brassbottle", domain))
                        continue

                    self.assertEqual(
                        body["persisted"], {"zone": domain, "scope": expect_persisted_scope}
                    )
                    on_disk = b._denylist.matches("coding-brassbottle", domain)
                    self.assertIsNotNone(on_disk, f"{domain} not written to denylist.json")
                    self.assertEqual(on_disk.zone, domain)
                    self.assertEqual(on_disk.scope, expect_persisted_scope)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_scope_bottle_ip_literal_zone(self):
        """finding #4: the deny path accepts IP literals — normalize_host()
        alone would 400 these; the handler must use normalize_destination()
        for deny so ./djinn deny 93.184.216.34 --global round-trips through
        /decide (notify_daemon_deny posts exactly this shape)."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/decide",
                    json.dumps(
                        {
                            "container": "coding-brassbottle",
                            "host": "93.184.216.34",
                            "decision": "deny",
                            "scope": "global",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK, body)
                self.assertEqual(
                    body["persisted"], {"zone": "93.184.216.34", "scope": "global"}
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            entry = b._denylist.matches("any-bottle", "93.184.216.34")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.zone, "93.184.216.34")

    def _post_decide(self, host: str, port: int, payload: dict) -> tuple[int, dict]:
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/decide",
            json.dumps(payload),
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
            },
        )
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def test_decide_endpoint_deny_ip_literal_scope_once_no_thread_traceback(self):
        """finding #1: an IP-literal host with scope=once must produce a
        clean HTTP response, not a broken connection from an unhandled
        exception on the handler thread (decide_deny_for_zone used to call
        normalize_host(), which rejects IPs outright)."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, body = self._post_decide(
                    host,
                    port,
                    {
                        "container": "coding-brassbottle",
                        "host": "93.184.216.34",
                        "decision": "deny",
                        "scope": "once",
                    },
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(body["decided"], [])

    def test_decide_endpoint_deny_ip_literal_scope_global_no_thread_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, body = self._post_decide(
                    host,
                    port,
                    {
                        "host": "93.184.216.34",
                        "decision": "deny",
                        "scope": "global",
                    },
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(body["persisted"], {"zone": "93.184.216.34", "scope": "global"})

    def test_decide_endpoint_deny_container_optional_for_global_scope(self):
        """The CLI relies on this: `./djinn deny <zone> --global` posts no
        `container` field at all — persist_deny(scope="global") sweeps
        every container itself and never needs one told to it."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, body = self._post_decide(
                    host, port, {"host": "datadoghq.com", "decision": "deny", "scope": "global"}
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(body["persisted"], {"zone": "datadoghq.com", "scope": "global"})

    def test_decide_endpoint_deny_container_required_for_once_and_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                for scope in ("once", "bottle"):
                    status, body = self._post_decide(
                        host, port, {"host": "x.example.com", "decision": "deny", "scope": scope}
                    )
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST, (scope, body))
                    self.assertEqual(body["error"], "container is required")
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_scope_bottle_unknown_bottle_returns_400(self):
        """finding #1: a typo'd bottle must never produce a dead entry — the
        HTTP handler turns persist_deny's EgressBrokerHostError into a 400,
        and nothing lands on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, body = self._post_decide(
                    host,
                    port,
                    {
                        "container": "ghost-bottle",
                        "host": "x.example.com",
                        "decision": "deny",
                        "scope": "bottle",
                    },
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, body)
            self.assertIn("unknown bottle", body["error"])
            self.assertFalse((egress_root / broker.DENYLIST_FILENAME).exists())

    def test_decide_endpoint_deny_scope_global_persist_failure_returns_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            clock = FakeClock(NOW)
            b = self._broker(egress_root, clock, hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                with mock.patch.object(
                    b._denylist, "add", side_effect=OSError("disk full")
                ):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "failure.example.com",
                                "decision": "deny",
                                "scope": "global",
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.assertEqual(body["error"], broker.DENYLIST_PERSIST_FAILED_REASON)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_allow_rejects_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/decide",
                    json.dumps(
                        {
                            "container": "coding-brassbottle",
                            "host": "stripe.com",
                            "decision": "allow",
                            "reason": "x",
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(body["error"], "reason only applies to deny")
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_decide_endpoint_deny_rejects_long_or_non_string_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            server = self._http_server(egress_root, b, tokens_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            error_text = "reason must be a string of at most 200 characters"
            try:
                for reason in ("x" * 201, 123, None):
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/decide",
                        json.dumps(
                            {
                                "container": "coding-brassbottle",
                                "host": "stripe.com",
                                "decision": "deny",
                                "reason": reason,
                            }
                        ),
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
                        },
                    )
                    resp = conn.getresponse()
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(body["error"], error_text)
                    conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_apply_allow_sets_skip_notify_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
                env = mocked.call_args.kwargs["env"]
                self.assertEqual(env[broker.DAEMON_SKIP_NOTIFY_ENV], "1")
            join_thread_or_fail(thread, label="file_request")

    def test_decide_allow_with_notify_script_does_not_deadlock(self):
        """A decide() whose apply script calls back into /decide must not wedge.

        This is a REAL reproduction, and it is fussy on purpose. Two things must
        hold or the test silently proves nothing (both were true of an earlier
        version of it, which passed even with the fix removed):

          * the callback curl must be UNBOUNDED. If the fake script caps it,
            a genuine deadlock just times out and the test goes green.
          * the script must actually reach /decide with a VALID body. The port
            and operator token have to be handed to it, and the args are
            (container, host, --save, <target>) — an earlier version read $2/$3
            and so POSTed host="--save", which the daemon rejects as an invalid
            domain before it ever touches the lock. No mutation could make that
            deadlock.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            egress_root = tmp_path / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")

            port_file = tmp_path / "port"
            token_file = tmp_path / "operator.token"

            fake_repo = tmp_path / "repo"
            bin_dir = fake_repo / "bin"
            bin_dir.mkdir(parents=True)
            allow_script = bin_dir / "allow-egress.sh"
            allow_script.write_text(
                f"""#!/bin/bash
if [ "${{DJINN_EGRESS_SKIP_NOTIFY:-}}" != "1" ]; then
  PORT="$(cat {port_file})"
  TOKEN="$(cat {token_file})"
  curl -s --connect-timeout 2 \\
    -X POST "http://127.0.0.1:${{PORT}}/decide" \\
    -H "Authorization: Bearer ${{TOKEN}}" \\
    -H "Content-Type: application/json" \\
    -d "{{\\"container\\":\\"$1\\",\\"host\\":\\"$2\\",\\"decision\\":\\"allow\\",\\"scope\\":\\"live\\"}}" \\
    >/dev/null 2>&1 || true
fi
exit 0
""",
                encoding="utf-8",
            )
            allow_script.chmod(0o755)

            clock = FakeClock(NOW)
            b = broker.EgressBroker(
                egress_root,
                repo_root=fake_repo,
                now_fn=clock.now,
                hold_seconds_default=5,
            )
            server = self._http_server(egress_root, b, tokens_dir)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            self.addCleanup(server.shutdown)
            host, port = server.server_address
            wait_for_tcp_listening(host, port)
            port_file.write_text(str(port), encoding="utf-8")
            token_file.write_text(server.operator_token, encoding="utf-8")

            filed = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            filed.start()
            self.addCleanup(lambda: filed.join(timeout=10))
            request_id = wait_for_broker_open_request(b)

            done = threading.Event()

            def approve() -> None:
                try:
                    b.decide(request_id, "allow", scope="live")
                finally:
                    done.set()

            approver = threading.Thread(target=approve, daemon=True)
            approver.start()

            # The deadlock manifests as decide() never returning. A generous
            # deadline keeps this from flaking on a loaded runner while still
            # failing fast on a real regression.
            self.assertTrue(
                done.wait(timeout=20),
                "decide() did not return: the apply script's call back into "
                "/decide deadlocked against the lock decide() holds",
            )

    def test_concurrent_decide_allow_applies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=60)
            filed = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            filed.start()
            request_id = wait_for_broker_open_request(b)

            apply_started = threading.Event()
            release_apply = threading.Event()
            call_count = 0

            def slow_apply(*args, **kwargs) -> mock.Mock:
                nonlocal call_count
                call_count += 1
                apply_started.set()
                if not release_apply.wait(timeout=5.0):
                    raise AssertionError("release_apply not signaled")
                return mock.Mock(returncode=0)

            first = threading.Thread(target=lambda: b.decide(request_id, "allow"))
            with mock.patch("subprocess.run", side_effect=slow_apply):
                first.start()
                self.assertTrue(
                    apply_started.wait(timeout=5.0),
                    "subprocess.apply did not start",
                )
                second_error: list[BaseException] = []

                def second_decide() -> None:
                    try:
                        b.decide(request_id, "allow")
                    except BaseException as exc:
                        second_error.append(exc)

                second = threading.Thread(target=second_decide)
                second.start()
                join_thread_or_fail(second, timeout=5.0, label="second decide")
                release_apply.set()
                join_thread_or_fail(first, timeout=5.0, label="first decide")

            self.assertEqual(second_error, [])
            self.assertEqual(call_count, 1)
            join_thread_or_fail(filed, label="file_request")
            allowed = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "allowed" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(allowed), 1)

    def test_notify_egress_daemon_curls_use_max_time(self):
        script = (REPO_ROOT / "bin" / "allow-egress.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(script.count("--max-time"), 2)

    # ── persistent deny list ────────────────────────────────────────────

    def test_decide_deny_scope_once_does_not_write_denylist_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0].get("scope"), "once")

    def test_decide_deny_scope_bottle_no_longer_writes_an_entry(self):
        """Consolidation (finding #2/#3/#5): decide()'s deny path never
        persists any more — ONLY EgressBroker.persist_deny() writes a
        denylist entry. A bare decide(..., scope="bottle") with no
        denylist_zone/denylist_scope is a plain one-shot deny of just this
        request; `scope` is recorded on the audit event for the record, but
        nothing is written to disk and no other request is swept."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            result = b.decide(request_id, "deny", scope="bottle", reason="noisy")
            join_thread_or_fail(thread, label="file_request")

            self.assertIsNone(result)
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0].get("scope"), "bottle")
            self.assertEqual(denied[0].get("reason"), "noisy")
            self.assertNotIn("zone", denied[0])
            self.assertNotIn("via", denied[0])

    def test_decide_deny_scope_global_no_longer_writes_an_entry(self):
        """Same consolidation as above, scope=global."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "datadoghq.com", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            b.decide(request_id, "deny", scope="global")
            join_thread_or_fail(thread, label="file_request")

            self.assertIsNone(b._denylist.matches("coding-brassbottle", "datadoghq.com"))
            self.assertIsNone(b._denylist.matches("any-other-bottle", "datadoghq.com"))

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(denied[0].get("scope"), "global")
            self.assertNotIn("zone", denied[0])

    def test_decide_deny_invalid_scope_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide(request_id, "deny", scope="banana")
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")

    def test_decide_deny_for_zone_no_longer_accepts_scope(self):
        """decide_deny_for_zone is scope=once only now — a persistent deny
        goes through EgressBroker.persist_deny (finding #2)."""
        b = broker.EgressBroker(
            Path(tempfile.mkdtemp()), repo_root=REPO_ROOT, now_fn=FakeClock(NOW).now,
        )
        with self.assertRaises(TypeError):
            b.decide_deny_for_zone("coding-brassbottle", "stripe.com", scope="global")  # type: ignore[call-arg]

    def _touch_token(self, root: Path, bottle: str) -> None:
        tokens_dir = root / broker.TOKENS_DIRNAME
        tokens_dir.mkdir(parents=True, exist_ok=True)
        (tokens_dir / f"{bottle}.token").write_text("tok\n", encoding="utf-8")

    def test_persist_deny_writes_caller_named_zone_even_with_no_open_request(self):
        """finding #2: unlike the old per-request-host behaviour, persist_deny
        writes the exact zone the caller asked for, regardless of whether
        anything is currently open under it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            result = b.persist_deny("datadoghq.com", "global", reason="telemetry")
            self.assertIsNone(result.error)
            self.assertEqual(result.decided, [])
            self.assertEqual(result.entry.zone, "datadoghq.com")
            self.assertEqual(result.entry.scope, "global")
            on_disk = b._denylist.matches("any-bottle", "us5.datadoghq.com")
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.zone, "datadoghq.com")

    def test_persist_deny_bottle_scope_requires_container(self):
        b = broker.EgressBroker(
            Path(tempfile.mkdtemp()), repo_root=REPO_ROOT, now_fn=FakeClock(NOW).now,
        )
        with self.assertRaises(broker.EgressBrokerHostError):
            b.persist_deny("example.com", "bottle")

    def test_persist_deny_bottle_scope_rejects_unknown_bottle(self):
        """finding #1: a typo'd bottle must never produce a dead entry —
        validate_bottle_scope (ported from egress_denylist.py) runs BEFORE
        any write, and nothing lands on disk when it fails."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(broker.EgressBrokerHostError) as ctx:
                b.persist_deny("example.com", "bottle", container="ghost-bottle")
            self.assertIn("unknown bottle", str(ctx.exception))
            self.assertFalse((root / broker.DENYLIST_FILENAME).exists())

    def test_persist_deny_bottle_scope_rejects_container_named_global(self):
        """finding #4: a bottle literally named "global" must be rejected
        everywhere — here via persist_deny's own validate_bottle_scope call
        (scope=bottle, container="global"), so it can never collide with a
        true scope=global entry. Nothing is written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(broker.EgressBrokerHostError) as ctx:
                b.persist_deny("example.com", "bottle", container="global")
            self.assertIn("reserved", str(ctx.exception))
            self.assertFalse((root / broker.DENYLIST_FILENAME).exists())

    def test_persist_deny_bottle_scope_sweeps_only_that_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            results: dict[str, dict] = {}

            def waiter(container: str, key: str) -> None:
                body, _ = b.file_request(container, "docs.stripe.com", 443)
                results[key] = body

            t_a = threading.Thread(target=waiter, args=("coding-brassbottle", "a"))
            t_b = threading.Thread(target=waiter, args=("other-bottle", "b"))
            t_a.start()
            t_b.start()
            wait_for_broker_open_request(b, count=2)

            result = b.persist_deny(
                "docs.stripe.com", "bottle", container="coding-brassbottle"
            )
            self.assertIsNone(result.error)
            self.assertEqual(len(result.decided), 1)
            self.assertEqual(result.entry.scope, "coding-brassbottle")

            t_a.join(timeout=5)
            self.assertEqual(
                results["a"],
                {"decision": "deny", "reason": "denylist", "zone": "docs.stripe.com",
                 "scope": "coding-brassbottle"},
            )
            # The other bottle's open request must NOT be swept by a
            # bottle-scoped entry — it stays open until decided separately.
            with b._lock:
                still_open = [s.container for s in b._requests.values() if s.decision is None]
            self.assertEqual(still_open, ["other-bottle"])
            for rid, state in list(b._requests.items()):
                b.decide(rid, "deny")
            t_b.join(timeout=5)

    def test_persist_deny_global_scope_sweeps_every_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            results: dict[str, dict] = {}

            def waiter(container: str, key: str) -> None:
                body, _ = b.file_request(container, "docs.stripe.com", 443)
                results[key] = body

            t_a = threading.Thread(target=waiter, args=("coding-brassbottle", "a"))
            t_b = threading.Thread(target=waiter, args=("other-bottle", "b"))
            t_a.start()
            t_b.start()
            wait_for_broker_open_request(b, count=2)

            result = b.persist_deny("docs.stripe.com", "global")
            self.assertIsNone(result.error)
            self.assertEqual(len(result.decided), 2)  # persist_deny sweeps synchronously
            t_a.join(timeout=5)
            t_b.join(timeout=5)
            for key in ("a", "b"):
                self.assertEqual(
                    results[key],
                    {"decision": "deny", "reason": "denylist", "zone": "docs.stripe.com",
                     "scope": "global"},
                )

    def test_persist_deny_invalid_scope_raises(self):
        b = broker.EgressBroker(
            Path(tempfile.mkdtemp()), repo_root=REPO_ROOT, now_fn=FakeClock(NOW).now,
        )
        with self.assertRaises(broker.EgressBrokerHostError):
            b.persist_deny("example.com", "once")

    def test_persist_deny_accepts_ip_literal_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            result = b.persist_deny("93.184.216.34", "global")
            self.assertIsNone(result.error)
            self.assertEqual(result.entry.zone, "93.184.216.34")
            self.assertIsNotNone(b._denylist.matches("any-bottle", "93.184.216.34"))
            self.assertIsNone(b._denylist.matches("any-bottle", "93.184.216.35"))

    def test_persist_deny_write_happens_outside_lock_then_reloads_under_it(self):
        """finding #2: DenyList.add() (its own flock + parse + write +
        os.replace) must run OUTSIDE self._lock — holding the broker-wide
        lock for that whole duration would stall every other handler
        thread's file_request/decide for as long as a slow disk write, or a
        concurrent `./djinn undeny` CLI process holding the SAME flock,
        takes. Proven two ways on the same call: (1) another thread CAN
        acquire self._lock while add() is in flight; (2) immediately after
        add() returns, persist_deny takes self._lock again just long enough
        to force self._denylist to reload — proven by showing self._lock is
        HELD (another thread cannot acquire it) for the duration of that
        reload, so a concurrent matches() (always called under self._lock
        too) cannot race it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            original_add = b._denylist.add
            original_load = b._denylist.load
            observed: dict[str, bool | None] = {}

            def _lock_free_from_another_thread() -> bool | None:
                acquired: list[bool] = []

                def attempt() -> None:
                    got = b._lock.acquire(blocking=False)
                    acquired.append(got)
                    if got:
                        b._lock.release()

                t = threading.Thread(target=attempt)
                t.start()
                t.join(timeout=2)
                return acquired[0] if acquired else None

            def spy_add(*args, **kwargs):
                observed["lock_was_free_during_add"] = _lock_free_from_another_thread()
                return original_add(*args, **kwargs)

            def spy_load(*args, **kwargs):
                observed["lock_was_free_during_reload"] = _lock_free_from_another_thread()
                return original_load(*args, **kwargs)

            with mock.patch.object(b._denylist, "add", side_effect=spy_add), \
                    mock.patch.object(b._denylist, "load", side_effect=spy_load):
                result = b.persist_deny("example.com", "global")
            self.assertIsNone(result.error)
            self.assertTrue(
                observed["lock_was_free_during_add"],
                "self._lock must NOT be held while DenyList.add() runs",
            )
            self.assertFalse(
                observed["lock_was_free_during_reload"],
                "self._lock must be held while the post-write DenyList reload runs",
            )

    def test_persist_deny_write_failure_returns_reason_and_sweeps_nothing(self):
        """No trigger_request_id here: a bare write failure sweeps nothing
        and leaves any open request untouched — see
        test_persist_deny_trigger_request_id_closed_on_write_failure below
        for the case that DOES carry one (watcher D/G, /decide with a live
        held connection)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "docs.stripe.com", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)

            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                result = b.persist_deny("docs.stripe.com", "global")
            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(result.entry)
            self.assertEqual(result.decided, [])
            with b._lock:
                self.assertIn(request_id, b._requests)  # left open, not lost
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")

    def test_persist_deny_trigger_request_id_closed_on_write_failure(self):
        """finding #1/#3: when the write fails AND a trigger_request_id was
        given (the watcher's D/G, or an HTTP caller with a live held
        connection), persist_deny closes exactly that request as a one-shot
        deny (persist_failed=True in the audit) so the held client is
        released and the watcher does not re-prompt it — proven against
        fold_queue's open set, the same thing the watcher itself polls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            result_body: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
                result_body["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            request_id = wait_for_broker_open_request(b)

            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                result = b.persist_deny(
                    "docs.stripe.com", "global", trigger_request_id=request_id
                )
            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(result.entry)
            self.assertEqual(result.decided, [])

            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(result_body["body"], {"decision": "deny"})
            with b._lock:
                self.assertNotIn(request_id, b._requests)
            open_ids = egress_log.EgressLog(root).fold_queue(now=b.now()).open_requests
            self.assertNotIn(request_id, open_ids)

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0].get("scope"), "once")
            self.assertTrue(denied[0].get("persist_failed"))
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "docs.stripe.com"))

    def test_persist_deny_write_failure_with_stale_trigger_id_logs_not_swallows(self):
        """finding #5: the write ALSO failed, and trigger_request_id no
        longer names an open request (already decided/evicted/never
        existed) — _close_request() raises EgressBrokerHostError for it,
        and that used to be swallowed by a bare `except
        EgressBrokerHostError: pass`. It must now log one INFO line instead
        of vanishing, and persist_deny still returns normally (no raise)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)

            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                with self.assertLogs(broker.LOG, level="INFO") as captured:
                    result = b.persist_deny(
                        "docs.stripe.com", "global", trigger_request_id="deadbeef"
                    )

            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(result.entry)
            self.assertEqual(result.decided, [])

            skip_lines = [
                r.getMessage()
                for r in captured.records
                if "persist_deny_trigger skip" in r.getMessage()
            ]
            self.assertEqual(len(skip_lines), 1)
            self.assertIn("request_id=deadbeef", skip_lines[0])
            self.assertIn("reason=", skip_lines[0])
            self.assertIn("no open request", skip_lines[0])

    def test_decide_deny_persist_failed_flag_degrades_to_one_shot_deny(self):
        """finding #1: decide() itself never touches DenyList any more, so
        there is nothing left in decide() that can raise from a disk
        failure — persist_failed=True (set only by persist_deny, see the
        two tests above) is just a plain audit flag/return value, never a
        raised exception a caller would need to catch.

        finding #9: persist_failed is internal-only — the public decide()
        wrapper can no longer accept it at all (see
        test_decide_public_wrapper_rejects_internal_kwargs below), so this
        drives it through _close_request() directly, exactly like
        persist_deny() itself does."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            request_id = wait_for_broker_open_request(b)

            err = b._close_request(request_id, "deny", scope="bottle", persist_failed=True)
            self.assertEqual(err, broker.DENYLIST_PERSIST_FAILED_REASON)

            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(result["body"], {"decision": "deny"})
            with b._lock:
                self.assertNotIn(request_id, b._requests)  # closed, not stuck
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0].get("scope"), "once")
            self.assertTrue(denied[0].get("persist_failed"))

    def test_decide_public_wrapper_rejects_internal_kwargs(self):
        """finding #9: decide() is now a thin public wrapper over the
        private _close_request() and its signature simply has no
        denylist_zone/denylist_scope/persist_failed parameters any more —
        an operator surface (the watcher, the /decide HTTP handler) passing
        one of these must fail loudly (TypeError) rather than silently
        reaching persist_deny()-only behavior. No open request is needed:
        argument binding fails before decide() ever touches self._requests."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)

            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", persist_failed=True)
            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", denylist_zone="x.com")
            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", denylist_scope="global")

    def test_decide_deny_response_body_and_waiter_carry_denylist_zone_scope(self):
        """finding #7: the held connection that triggered a scope=bottle|global
        deny must see reason/zone/scope in its own decision body, not the
        generic one-shot deny shape. Driven through persist_deny (the only
        thing that ever sets denylist_zone/denylist_scope on decide()) with
        an operator reason, so this also proves finding #4: the audit event
        KEEPS that free-text reason (never overwritten with the literal
        "denylist") and carries the denylist context in separate fields."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            wait_for_broker_open_request(b)
            persist_result = b.persist_deny(
                "neon.tech", "bottle", container="coding-brassbottle", reason="noisy"
            )
            self.assertIsNone(persist_result.error)
            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(
                result["body"],
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "neon.tech",
                    "scope": "coding-brassbottle",
                },
            )

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 1)
            # The operator's free-text reason survives untouched...
            self.assertEqual(denied[0].get("reason"), "noisy")
            # ...and the denylist context lives in its own fields (finding
            # #5: `scope` keeps meaning request intent — the /decide call
            # that triggered persist_deny asked for scope=bottle — while
            # the entry's own scope, the bottle NAME it was written under,
            # goes in the separate `denylist_scope` key).
            self.assertEqual(denied[0].get("via"), "denylist")
            self.assertEqual(denied[0].get("zone"), "neon.tech")
            self.assertEqual(denied[0].get("scope"), "bottle")
            self.assertEqual(denied[0].get("denylist_scope"), "coding-brassbottle")

    def test_decide_deny_once_response_body_stays_generic(self):
        """A plain scope=once deny (no denylist write) keeps the old bare
        {"decision": "deny"} shape — no regression for the common path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            request_id = wait_for_broker_open_request(b)
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")
            self.assertEqual(result["body"], {"decision": "deny"})

    def test_denylist_short_circuit_returns_deny_body_and_never_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global", reason="telemetry")

            with mock.patch.object(b, "_notify_operator") as notify:
                body, request_id = b.file_request(
                    "coding-brassbottle", "http-intake.logs.us5.datadoghq.com", 443
                )
                notify.assert_not_called()
            self.assertEqual(
                body,
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "datadoghq.com",
                    "scope": "global",
                },
            )
            # No held request: the id must not appear in the open-request map.
            with b._lock:
                self.assertNotIn(request_id, b._requests)
            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(requested), 1)
            self.assertEqual(len(denied), 1)
            # finding #5: the audit event's `reason` is the entry's own
            # operator free text ("telemetry", set on add() above) — the
            # literal "denylist" stays confined to the response body
            # (asserted above), which the container-side reader depends on
            # unchanged. `via`/`denylist_scope` mirror persist_deny's own
            # sweep-closure schema exactly; `scope` (request intent) is
            # absent — there was no /decide call for this short-circuit.
            self.assertEqual(denied[0].get("reason"), "telemetry")
            self.assertEqual(denied[0].get("via"), "denylist")
            self.assertEqual(denied[0].get("zone"), "datadoghq.com")
            self.assertEqual(denied[0].get("denylist_scope"), "global")
            self.assertNotIn("scope", denied[0])
            # finding #6: the audit pair and the id returned to the caller
            # must be the SAME id, not two independently-minted ones.
            self.assertEqual(requested[0]["request_id"], request_id)
            self.assertEqual(denied[0]["request_id"], request_id)
            # fold_queue must see this request as closed (requested->denied),
            # never resurrected as open after a daemon restart.
            folded = egress_log.EgressLog(root).fold_queue(now=NOW)
            self.assertEqual(folded.open_requests, {})

    def test_denylist_caused_denied_events_share_the_same_denylist_key_set(self):
        """finding #5: unify the audit schema for BOTH denylist-caused
        "denied" events — the immediate short-circuit path
        (_denylist_short_circuit) and persist_deny's sweep-closure path
        (_close_request's denylist_zone/denylist_scope branch) — so a
        reader of the audit log has ONE shape for "a denylist entry denied
        this", not two subtly different ones. Drive both paths in the same
        test and assert they carry the identical set of denylist-context
        keys (`scope` is deliberately excluded from the comparison: it
        keeps meaning request intent on the sweep-closure path, and simply
        does not exist on the short-circuit path, which is not a /decide
        call at all)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)

            # Path 1: short-circuit — the zone is ALREADY denylisted, so
            # this deny never opens a held request at all.
            b._denylist.add(zone="datadoghq.com", scope="global", reason="telemetry")
            body1, _rid1 = b.file_request("coding-brassbottle", "datadoghq.com", 443)
            self.assertEqual(body1["decision"], "deny")

            # Path 2: persist_deny's sweep closure — an OPEN request that a
            # brand-new denylist entry then covers and closes.
            result: dict[str, object] = {}

            def waiter() -> None:
                body, _ = b.file_request("coding-brassbottle", "neon.tech", 443)
                result["body"] = body

            thread = threading.Thread(target=waiter)
            thread.start()
            wait_for_broker_open_request(b)
            persist_result = b.persist_deny(
                "neon.tech", "bottle", container="coding-brassbottle", reason="noisy"
            )
            self.assertIsNone(persist_result.error)
            join_thread_or_fail(thread, label="file_request")

            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(denied), 2)
            short_circuit_denied = next(r for r in denied if r.get("zone") == "datadoghq.com")
            sweep_denied = next(r for r in denied if r.get("zone") == "neon.tech")

            denylist_schema_keys = {"via", "zone", "denylist_scope"}
            sc_keys = denylist_schema_keys & short_circuit_denied.keys()
            sw_keys = denylist_schema_keys & sweep_denied.keys()
            self.assertEqual(sc_keys, denylist_schema_keys)
            self.assertEqual(sw_keys, denylist_schema_keys)
            self.assertEqual(short_circuit_denied["via"], "denylist")
            self.assertEqual(sweep_denied["via"], "denylist")
            # And `reason` on BOTH is free text, never the literal
            # "denylist" — that literal is confined to the HTTP response
            # body (a separate assertion, covered by the two tests above).
            self.assertEqual(short_circuit_denied.get("reason"), "telemetry")
            self.assertEqual(sweep_denied.get("reason"), "noisy")

    def test_denylist_short_circuit_requested_event_carries_full_fields(self):
        """finding #7: the "requested" audit event a denylist short-circuit
        appends must carry the SAME fields a normal filing does (uid, comm,
        reason, host_is_ip) — not a stripped-down container/host/port-only
        version, which would make a denylisted request's audit trail less
        informative than an approved/denied-by-operator one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            body, request_id = b.file_request(
                "coding-brassbottle",
                "datadoghq.com",
                443,
                uid=1000,
                comm="curl",
                reason="ci fetch",
            )
            self.assertEqual(body["decision"], "deny")
            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            self.assertEqual(len(requested), 1)
            self.assertEqual(requested[0]["request_id"], request_id)
            self.assertEqual(requested[0].get("uid"), 1000)
            self.assertEqual(requested[0].get("comm"), "curl")
            self.assertEqual(requested[0].get("reason"), "ci fetch")
            self.assertEqual(requested[0].get("container"), "coding-brassbottle")
            self.assertEqual(requested[0].get("host"), "datadoghq.com")
            self.assertEqual(requested[0].get("port"), 443)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="93.184.216.34", scope="global")

            body, request_id = b.file_request(
                "coding-brassbottle",
                "93.184.216.34",
                443,
                host_is_ip=True,
            )
            self.assertEqual(body["decision"], "deny")
            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            self.assertEqual(requested[0].get("host_is_ip"), True)

    def test_denylist_short_circuit_id_matches_audit_with_client_supplied_id(self):
        """Same finding #6 guarantee, exercised on the OTHER file_request
        branch — a client-supplied request_id — which used to mint its own
        internal id for the audit pair while returning the client's id,
        leaving the two permanently out of sync."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root, FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            body, request_id = b.file_request(
                "coding-brassbottle",
                "datadoghq.com",
                443,
                request_id="cafebabe",
            )
            self.assertEqual(request_id, "cafebabe")
            self.assertEqual(body["decision"], "deny")
            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(requested[0]["request_id"], "cafebabe")
            self.assertEqual(denied[0]["request_id"], "cafebabe")

    def test_denylist_short_circuit_coalesces_within_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            for _ in range(5):
                body, _rid = b.file_request("coding-brassbottle", "datadoghq.com", 443)
                self.assertEqual(body["decision"], "deny")
                clock.advance(1)

            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(requested), 1)
            self.assertEqual(len(denied), 1)

            clock.advance(broker.HIT_COALESCE_SECONDS + 1)
            body, _rid = b.file_request("coding-brassbottle", "datadoghq.com", 443)
            self.assertEqual(body["decision"], "deny")
            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(requested), 2)
            self.assertEqual(len(denied), 2)

    def test_denylist_short_circuit_log_info_gated_to_coalesce_window(self):
        """Cleanup (finding C): LOG.info at INFO on every hit floods just as
        badly as the audit log did — gated to the same coalesce window, with
        suppressed=N surfaced on the next logged hit so nothing vanishes
        silently."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            with self.assertLogs(broker.LOG, level="INFO") as captured:
                for _ in range(4):
                    b.file_request("coding-brassbottle", "datadoghq.com", 443)
                    clock.advance(1)
            short_circuit_lines = [
                r.getMessage()
                for r in captured.records
                if "denylist short_circuit" in r.getMessage()
            ]
            # Only the FIRST hit is logged inside the window; the other 3
            # are suppressed (no LOG.info at all) rather than each logging
            # their own line.
            self.assertEqual(len(short_circuit_lines), 1)
            self.assertIn("suppressed=0", short_circuit_lines[0])

            clock.advance(broker.HIT_COALESCE_SECONDS + 1)
            with self.assertLogs(broker.LOG, level="INFO") as captured2:
                for _ in range(3):
                    b.file_request("coding-brassbottle", "datadoghq.com", 443)
                    clock.advance(1)
            short_circuit_lines2 = [
                r.getMessage()
                for r in captured2.records
                if "denylist short_circuit" in r.getMessage()
            ]
            self.assertEqual(len(short_circuit_lines2), 1)
            # 3 hits were suppressed since the last logged one (the 2nd,
            # 3rd, 4th calls of the FIRST loop — the suppressed count isn't
            # reset until it's actually surfaced, which is right here).
            self.assertIn("suppressed=3", short_circuit_lines2[0])

    def test_denylist_hit_coalescing_keys_by_matched_zone_not_raw_host(self):
        """Cleanup: distinct subdomains under the same denylisted zone must
        coalesce TOGETHER (one audit pair per zone per window), not each get
        their own window keyed off the raw host — a rotating-hostname
        telemetry client would otherwise defeat the coalescing entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            for host in ("a.datadoghq.com", "b.datadoghq.com", "c.datadoghq.com"):
                body, _rid = b.file_request("coding-brassbottle", host, 443)
                self.assertEqual(body["decision"], "deny")

            requested = [r for r in self._log_records(root) if r.get("kind") == "requested"]
            denied = [r for r in self._log_records(root) if r.get("kind") == "denied"]
            self.assertEqual(len(requested), 1)
            self.assertEqual(len(denied), 1)
            self.assertEqual(len(b._denylist_hits), 1)
            self.assertIn(("coding-brassbottle", "datadoghq.com"), b._denylist_hits)

    def test_denylist_hit_last_prunes_stale_keys_on_insert(self):
        """Cleanup: _denylist_hits must not grow without bound — a stale
        entry (older than HIT_COALESCE_SECONDS) is dropped the next time any
        key is inserted, not just the one that expired."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="old-zone.example.com", scope="global")
            b._denylist.add(zone="new-zone.example.com", scope="global")

            b.file_request("coding-brassbottle", "old-zone.example.com", 443)
            self.assertEqual(len(b._denylist_hits), 1)

            clock.advance(broker.HIT_COALESCE_SECONDS + 1)
            b.file_request("coding-brassbottle", "new-zone.example.com", 443)
            # The stale old-zone key must be gone, not just the new one added.
            self.assertEqual(len(b._denylist_hits), 1)
            self.assertIn(("coding-brassbottle", "new-zone.example.com"), b._denylist_hits)
            self.assertNotIn(
                ("coding-brassbottle", "old-zone.example.com"), b._denylist_hits
            )

    def test_denylist_suppressed_evicted_hits_are_logged_not_dropped_silently(self):
        """finding #4: _denylist_short_circuit's own docstring promises a
        suppressed hit is surfaced on the "NEXT logged hit" for the same
        key — but a key can be evicted by _prune_denylist_hit_last before
        any next hit ever arrives for it, and the suppressed count used to
        just vanish with it (bare dict.pop, no log). Now eviction with a
        nonzero suppressed count logs one INFO line naming the count.

        finding #8: last-hit-timestamp and suppressed-count now live on one
        _DenylistHitState per key (b._denylist_hits) instead of two parallel
        dicts kept in sync by convention."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            b._denylist.add(zone="old-zone.example.com", scope="global")
            b._denylist.add(zone="new-zone.example.com", scope="global")

            # First hit on old-zone logs (suppressed=0); the second, within
            # the same coalesce window, is suppressed (counted, not logged).
            b.file_request("coding-brassbottle", "old-zone.example.com", 443)
            b.file_request("coding-brassbottle", "old-zone.example.com", 443)
            self.assertEqual(
                b._denylist_hits[("coding-brassbottle", "old-zone.example.com")].suppressed, 1
            )

            clock.advance(broker.HIT_COALESCE_SECONDS + 1)
            with self.assertLogs(broker.LOG, level="INFO") as captured:
                # A hit on a DIFFERENT zone triggers the prune (on insert),
                # evicting the now-stale old-zone key.
                b.file_request("coding-brassbottle", "new-zone.example.com", 443)

            evict_lines = [
                r.getMessage() for r in captured.records if "suppressed_evict" in r.getMessage()
            ]
            self.assertEqual(len(evict_lines), 1)
            self.assertIn("container=coding-brassbottle", evict_lines[0])
            self.assertIn("zone=old-zone.example.com", evict_lines[0])
            self.assertIn("suppressed=1", evict_lines[0])
            self.assertNotIn(
                ("coding-brassbottle", "old-zone.example.com"), b._denylist_hits
            )

    def test_open_request_not_short_circuited_by_a_later_denylist_entry(self):
        """The coalesce path precedes the denylist check: an already-open
        request keeps its normal path even after a matching entry appears."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            open_id = wait_for_broker_open_request(b)

            b._denylist.add(zone="neon.tech", scope="global")

            second_body, second_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(second_id, open_id)
            self.assertEqual(second_body["decision"], "pending")

            b.decide(open_id, "deny")
            join_thread_or_fail(thread, label="file_request")

    def test_denylist_matches_reload_picks_up_cli_edit_live(self):
        """Broker.file_request sees a denylist entry added after daemon start
        without a restart — mtime reload is the whole point."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=1)
            body, _rid = b.file_request("coding-brassbottle", "example.net", 443)
            self.assertEqual(body["decision"], "pending")
            with b._lock:
                for state in list(b._requests.values()):
                    b.decide(state.request_id, "deny")

            # Simulate an external CLI process editing denylist.json directly.
            import egress_denylist as denylist_mod

            external = denylist_mod.DenyList(root / denylist_mod.DENYLIST_FILENAME)
            external.add(zone="example.net", scope="global")

            body2, _rid2 = b.file_request("coding-brassbottle", "example.net", 443)
            self.assertEqual(
                body2,
                {"decision": "deny", "reason": "denylist", "zone": "example.net", "scope": "global"},
            )

    def test_file_request_invokes_notifier_once_per_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            recorded: list[egress_notify.EgressNotification] = []

            def recorder(notification: egress_notify.EgressNotification) -> None:
                recorded.append(notification)

            b = broker.EgressBroker(
                root,
                repo_root=REPO_ROOT,
                now_fn=clock.now,
                hold_seconds_default=60,
                notifier=recorder,
            )
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
                kwargs={
                    "uid": 1000,
                    "comm": "curl",
                    "reason": "npm install",
                },
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            second_body, second_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
            )
            self.assertEqual(second_id, request_id)
            self.assertEqual(second_body["decision"], "pending")
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0].request_id, request_id)
            self.assertEqual(recorded[0].container, "coding-brassbottle")
            self.assertEqual(recorded[0].host, "neon.tech")
            self.assertEqual(recorded[0].port, 443)
            self.assertEqual(recorded[0].uid, 1000)
            self.assertEqual(recorded[0].comm, "curl")
            self.assertEqual(recorded[0].reason, "npm install")
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")

    def test_notifier_exception_does_not_break_file_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)

            def broken_notifier(_notification: egress_notify.EgressNotification) -> None:
                raise RuntimeError("boom")

            b = broker.EgressBroker(
                root,
                repo_root=REPO_ROOT,
                now_fn=clock.now,
                hold_seconds_default=60,
                notifier=broken_notifier,
            )
            with self.assertLogs("egress_broker_host", level="WARNING") as captured:
                thread = threading.Thread(
                    target=b.file_request,
                    args=("coding-brassbottle", "neon.tech", 443),
                )
                thread.start()
                request_id = wait_for_broker_open_request(b)
            self.assertTrue(any("notifier raised" in line for line in captured.output))
            queue_state = b._log.fold_queue(now=clock.now())
            self.assertIn(request_id, queue_state.open_requests)
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")

    def test_notifier_called_outside_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock(NOW)
            b = broker.EgressBroker(
                root,
                repo_root=REPO_ROOT,
                now_fn=clock.now,
                hold_seconds_default=60,
            )

            acquired_during_notify: list[bool] = []

            def lock_probe(_notification: egress_notify.EgressNotification) -> None:
                acquired = b._lock.acquire(timeout=1)
                acquired_during_notify.append(acquired)
                if acquired:
                    b._lock.release()

            b._notifier = lock_probe
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "neon.tech", 443),
            )
            thread.start()
            request_id = wait_for_broker_open_request(b)
            # The request is registered under the lock and the notifier is
            # (by design) called only AFTER it is released, so the open
            # request becoming visible does not mean the probe has run yet —
            # wait for the callback itself, not for the state it follows.
            deadline = time.monotonic() + 10.0
            while not acquired_during_notify and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(acquired_during_notify, [True])
            b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")


class IPv6BindTests(unittest.TestCase):
    """PR #85 review (Codex P2): _connect_host_for_bind maps "::"/IPv6
    literals to a connect address, but ThreadingHTTPServer hardcodes
    AF_INET, so binding one of those hosts died at bind() with "Address
    family for hostname not supported" — before daemon.json was ever
    written. See egress_broker_host.address_family_for_host."""

    def test_address_family_for_host_table(self):
        cases = [
            ("", socket.AF_INET),
            ("0.0.0.0", socket.AF_INET),
            ("127.0.0.1", socket.AF_INET),
            ("10.8.0.5", socket.AF_INET),
            ("::", socket.AF_INET6),
            ("::1", socket.AF_INET6),
            ("fd00::5", socket.AF_INET6),
            # A name that will not resolve: fall through to AF_INET so bind()
            # raises the real resolution error instead of a guessed family.
            ("no-such-host.invalid", socket.AF_INET),
        ]
        for host, family in cases:
            with self.subTest(host=host):
                self.assertEqual(broker.address_family_for_host(host), family)

    @unittest.skipUnless(socket.has_ipv6, "no IPv6 support on this host")
    def test_server_binds_and_serves_over_ipv6(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            b = broker.EgressBroker(
                egress_root,
                repo_root=REPO_ROOT,
                now_fn=FakeClock(NOW).now,
                hold_seconds_default=1,
            )
            store = broker.BottleTokenStore(tokens_dir)
            try:
                server = broker.EgressBrokerHTTPServer(
                    ("::1", 0), b, store, "operator-test-token"
                )
            except OSError as exc:  # pragma: no cover - IPv6 loopback absent
                self.skipTest(f"IPv6 loopback unavailable: {exc}")
            self.assertEqual(server.address_family, socket.AF_INET6)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address[0], server.server_address[1]
            wait_for_tcp_listening(host, port)
            try:
                # The connect address the endpoint file would advertise for
                # this bind must actually be reachable, brackets and all.
                broker.write_daemon_endpoint(egress_root, host, port)
                url = broker.daemon_base_url(egress_root)
                self.assertEqual(url, f"http://[::1]:{port}")
                with urllib.request.urlopen(url + "/health", timeout=5) as resp:
                    self.assertEqual(resp.status, HTTPStatus.OK)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class DaemonEndpointFileTests(unittest.TestCase):
    """$egress_root/daemon.json — the single source of truth host-side CLIs
    read to find a daemon that bound to a non-default --host/--port. See
    egress_broker_host.write_daemon_endpoint/read_daemon_endpoint/
    daemon_base_url and the PR #85 review finding this fixes."""

    def _dead_pid(self) -> int:
        """A pid guaranteed not to be alive: spawn a trivial subprocess and
        wait() it — wait() reaps it, so os.kill(pid, 0) afterwards raises
        ProcessLookupError, same as any other daemon that crashed without
        cleaning up after itself."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_write_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            path = broker.write_daemon_endpoint(egress_root, "127.0.0.1", 9123)
            self.assertEqual(path, egress_root / broker.ENDPOINT_FILENAME)
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["host"], "127.0.0.1")
            self.assertEqual(payload["port"], 9123)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["version"], 1)
            self.assertIn("started_at", payload)

            endpoint = broker.read_daemon_endpoint(egress_root)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.host, "127.0.0.1")
            self.assertEqual(endpoint.port, 9123)
            self.assertEqual(endpoint.pid, os.getpid())

    def test_remove_daemon_endpoint_tolerates_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            broker.remove_daemon_endpoint(egress_root)  # must not raise
            broker.write_daemon_endpoint(egress_root, "127.0.0.1", 9123)
            broker.remove_daemon_endpoint(egress_root)
            self.assertFalse((egress_root / broker.ENDPOINT_FILENAME).exists())

    def test_read_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(broker.read_daemon_endpoint(Path(tmp)))

    def test_read_corrupt_json_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            (egress_root / broker.ENDPOINT_FILENAME).write_text("{BROKEN", encoding="utf-8")
            with self.assertLogs("egress_broker_host", level="WARNING") as captured:
                result = broker.read_daemon_endpoint(egress_root)
            self.assertIsNone(result)
            self.assertTrue(any("endpoint unreadable" in line for line in captured.output))

    def test_read_wrong_shape_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            (egress_root / broker.ENDPOINT_FILENAME).write_text(
                json.dumps({"version": 1, "host": "127.0.0.1"}), encoding="utf-8"
            )
            with self.assertLogs("egress_broker_host", level="WARNING") as captured:
                result = broker.read_daemon_endpoint(egress_root)
            self.assertIsNone(result)
            self.assertTrue(any("endpoint unreadable" in line for line in captured.output))

    def test_read_not_a_json_object_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            (egress_root / broker.ENDPOINT_FILENAME).write_text("[1,2,3]", encoding="utf-8")
            with self.assertLogs("egress_broker_host", level="WARNING") as captured:
                result = broker.read_daemon_endpoint(egress_root)
            self.assertIsNone(result)
            self.assertTrue(any("endpoint unreadable" in line for line in captured.output))

    def test_read_stale_pid_logs_info_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            dead_pid = self._dead_pid()
            (egress_root / broker.ENDPOINT_FILENAME).write_text(
                json.dumps(
                    {"version": 1, "host": "127.0.0.1", "port": 8816, "pid": dead_pid}
                ),
                encoding="utf-8",
            )
            with self.assertLogs("egress_broker_host", level="INFO") as captured:
                result = broker.read_daemon_endpoint(egress_root)
            self.assertIsNone(result)
            self.assertTrue(
                any(
                    "endpoint stale" in line and f"pid={dead_pid}" in line
                    for line in captured.output
                )
            )

    def test_daemon_base_url_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)

            # No endpoint file at all → default fallback.
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(broker.EGRESS_BROKER_URL_ENV, None)
                self.assertEqual(
                    broker.daemon_base_url(egress_root),
                    f"http://127.0.0.1:{broker.DEFAULT_PORT}",
                )

            table = [
                ("0.0.0.0", "http://127.0.0.1:9001"),
                ("", "http://127.0.0.1:9001"),
                ("::", "http://[::1]:9001"),
                ("fe80::1", "http://[fe80::1]:9001"),
                ("10.8.0.5", "http://10.8.0.5:9001"),
            ]
            for bind_host, expected in table:
                with self.subTest(bind_host=bind_host):
                    broker.write_daemon_endpoint(egress_root, bind_host, 9001)
                    with mock.patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(broker.EGRESS_BROKER_URL_ENV, None)
                        self.assertEqual(broker.daemon_base_url(egress_root), expected)

            # EGRESS_BROKER_URL wins over a live endpoint file.
            with mock.patch.dict(
                os.environ, {broker.EGRESS_BROKER_URL_ENV: "http://10.9.9.9:1234"}
            ):
                self.assertEqual(
                    broker.daemon_base_url(egress_root), "http://10.9.9.9:1234"
                )

    def test_run_daemon_writes_real_bound_port_and_removes_on_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)
            captured: dict[str, object] = {}

            def fake_serve_forever(self) -> None:
                captured["port"] = self.server_address[1]
                captured["endpoint"] = broker.read_daemon_endpoint(egress_root)

            with mock.patch.object(
                broker.EgressBrokerHTTPServer, "serve_forever", fake_serve_forever
            ):
                broker.run_daemon(base, host="127.0.0.1", port=0, repo_root=REPO_ROOT)

            self.assertIsNotNone(captured.get("endpoint"))
            self.assertNotEqual(captured["port"], 0)
            self.assertEqual(captured["endpoint"].port, captured["port"])
            self.assertEqual(captured["endpoint"].pid, os.getpid())
            # Cleaned up in run_daemon's finally, before the lock is released.
            self.assertFalse((egress_root / broker.ENDPOINT_FILENAME).exists())

    def test_print_endpoint_default_fallback_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(broker.EGRESS_BROKER_URL_ENV, None)
                with mock.patch("sys.stdout", out):
                    rc = broker.main(["--print-endpoint", "--base-path", str(base)])
            self.assertEqual(rc, 3)
            self.assertEqual(out.getvalue().strip(), f"http://127.0.0.1:{broker.DEFAULT_PORT}")

    def test_print_endpoint_live_endpoint_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)
            broker.write_daemon_endpoint(egress_root, "10.8.0.5", 9999)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(broker.EGRESS_BROKER_URL_ENV, None)
                with mock.patch("sys.stdout", out):
                    rc = broker.main(["--print-endpoint", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "http://10.8.0.5:9999")

    def test_print_endpoint_env_override_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = io.StringIO()
            with mock.patch.dict(
                os.environ, {broker.EGRESS_BROKER_URL_ENV: "http://example.internal:7000"}
            ):
                with mock.patch("sys.stdout", out):
                    rc = broker.main(["--print-endpoint", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "http://example.internal:7000")


if __name__ == "__main__":
    unittest.main()
