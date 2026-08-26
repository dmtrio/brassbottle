#!/usr/bin/env python3
"""Unit tests for the host-side egress approval broker daemon."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import egress_broker_host as broker  # noqa: E402
import egress_log  # noqa: E402

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
            time.sleep(0.2)
            second_body, second_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
            )
            open_id = next(iter(b._requests))
            self.assertEqual(second_id, open_id)
            self.assertEqual(second_body["decision"], "pending")
            b.decide(open_id, "deny")
            thread.join(timeout=5)
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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            self.assertNotEqual(request_id, "old-req")
            b.decide(request_id, "deny")
            thread.join(timeout=5)
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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            for _ in range(100):
                with b._lock:
                    state = b._requests[request_id]
                    b._record_hit(state)
                clock.advance(0.1)
            with b._lock:
                b._flush_hits(b._requests[request_id])
            b.decide(request_id, "deny")
            thread.join(timeout=5)
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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            clock.advance(25 * 3600)
            closed = b.sweep_stale()
            self.assertEqual(closed, 1)
            thread.join(timeout=5)
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
            store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(("127.0.0.1", 0), b, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
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
                thread.join(timeout=5)

    def test_token_for_bottle_a_cannot_file_for_bottle_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "bottle-a.token").write_text("token-a\n", encoding="utf-8")
            (tokens_dir / "bottle-b.token").write_text("token-b\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(("127.0.0.1", 0), b, store)
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
                thread.join(timeout=5)

    def test_auth_correct_bearer_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress_root = root / "egress"
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "my-bottle.token").write_text("good-token\n", encoding="utf-8")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(("127.0.0.1", 0), b, store)
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
                thread.join(timeout=5)

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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
                argv = mocked.call_args[0][0]
                self.assertNotIn("firewall", argv)
            thread.join(timeout=5)

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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
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
            thread.join(timeout=5)

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
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="manifest")
                argv = mocked.call_args[0][0]
                self.assertEqual(argv[-1], "yml")
            thread.join(timeout=5)

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
            time.sleep(0.2)
            open_id = next(iter(b._requests))
            b.decide(open_id, "allow", scope="manifest")
            thread.join(timeout=5)
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
            store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(("127.0.0.1", 0), b, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(body, {"status": "ok"})
            finally:
                server.shutdown()
                thread.join(timeout=5)

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
            store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(("127.0.0.1", 0), b, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
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
                thread.join(timeout=5)
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
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "192.0.2.55", 5432),
                kwargs={"host_is_ip": True},
            )
            thread.start()
            time.sleep(0.2)
            request_id = next(iter(b._requests))
            with mock.patch("subprocess.run") as mocked:
                b.decide(request_id, "allow", scope="live")
                mocked.assert_not_called()
            thread.join(timeout=5)
            failed = [
                r
                for r in self._log_records(root)
                if r.get("kind") == "apply_failed" and r.get("request_id") == request_id
            ]
            self.assertEqual(len(failed), 1)
            self.assertIn("egress_cidrs", failed[0].get("reason", ""))

    def test_bottle_token_store_reload_on_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens_dir = Path(tmp) / "tokens"
            tokens_dir.mkdir()
            store = broker.BottleTokenStore(tokens_dir)
            self.assertIsNone(store.resolve_bottle("new-token"))
            (tokens_dir / "late.token").write_text("new-token\n", encoding="utf-8")
            self.assertEqual(store.resolve_bottle("new-token"), "late")


if __name__ == "__main__":
    unittest.main()
