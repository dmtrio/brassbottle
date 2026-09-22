#!/usr/bin/env python3
"""Unit tests for the host-side egress broker daemon (store-backed, non-blocking)."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
import egress_broker  # noqa: E402  # the container-side client: body parser contract
import egress_broker_host as broker  # noqa: E402
import egress_notify  # noqa: E402
import egress_store  # noqa: E402
from egress_test_sync import (  # noqa: E402
    join_thread_or_fail,
    wait_for_tcp_listening,
)

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
NOW_PLUS_1 = NOW + timedelta(seconds=1)
NOW_PLUS_2 = NOW + timedelta(seconds=2)
STALE_TEST_HOURS = broker.STALE_HOURS
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

    def _serve(self, egress_root: Path, b: broker.EgressBroker, tokens_dir: Path):
        server = self._http_server(egress_root, b, tokens_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)

        def stop_server() -> None:
            server.shutdown()
            join_thread_or_fail(thread, label="broker server")

        self.addCleanup(stop_server)
        return host, port

    def _touch_token(self, root: Path, bottle: str, token: str = "tok") -> None:
        tokens_dir = root / broker.TOKENS_DIRNAME
        tokens_dir.mkdir(parents=True, exist_ok=True)
        (tokens_dir / f"{bottle}.token").write_text(f"{token}\n", encoding="utf-8")

    # ── destination normalization ────────────────────────────────────────

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

    def test_ipv6_literal_accepted(self):
        host, is_ip = broker.normalize_destination("2001:db8::1")
        self.assertTrue(is_ip)
        self.assertEqual(host, "2001:db8::1")

    def test_host_covered_by_zone(self):
        self.assertTrue(broker.host_covered_by_zone("docs.stripe.com", "stripe.com"))
        self.assertTrue(broker.host_covered_by_zone("stripe.com", "stripe.com"))
        self.assertFalse(broker.host_covered_by_zone("notstripe.com", "stripe.com"))

    # ── filing: instant, pending, store-backed ───────────────────────────

    def test_filing_returns_within_one_second_when_nothing_decides(self):
        """PIN — this test fails on the pre-change code: file_request blocked
        for the whole hold waiting on an operator. After the cut-over the
        broker only listens: the filing returns at once with the pending
        body, no matter how long the hold is."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), hold_seconds=300)
            started = time.monotonic()
            body, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, "the broker must not wait on an operator")
            self.assertEqual(body["decision"], "pending")
            self.assertEqual(body["request_id"], request_id)

    def test_filing_body_shape_and_store_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=90)
            body, request_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
                uid=1000,
                comm="curl",
                reason="npm install",
                host_is_ip=False,
            )
            self.assertEqual(
                body,
                {
                    "decision": "pending",
                    "request_id": request_id,
                    "status": "open",
                    "poll": f"/egress/{request_id}",
                    "attempt": 0,
                },
            )
            row = b._store.get(request_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.container, "coding-brassbottle")
            self.assertEqual(row.host, "neon.tech")
            self.assertEqual(row.port, 443)
            self.assertEqual(row.uid, 1000)
            self.assertEqual(row.comm, "curl")
            self.assertEqual(row.reason, "npm install")
            self.assertEqual(row.hold_seconds, 90)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_attempts, 0)
            self.assertIsNone(row.decision_body)
            self.assertEqual(
                [(event.kind, event.fields) for event in b._store.events_for(request_id)],
                [
                    (
                        "requested",
                        {
                            "container": "coding-brassbottle",
                            "host": "neon.tech",
                            "port": 443,
                            "uid": 1000,
                            "comm": "curl",
                            "reason": "npm install",
                            "hold_seconds": 90,
                        },
                    ),
                    ("notified", {}),
                ],
            )

    def test_rehit_increments_hit_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock)
            body1, id1 = b.file_request("c", "neon.tech", 443)
            clock.advance(1)
            body2, id2 = b.file_request("c", "neon.tech", 443)
            self.assertEqual(id1, id2)
            self.assertEqual(body2["decision"], "pending")
            self.assertEqual(b._store.get(id1).hit_count, 2)
            self.assertEqual(b._store.count_events(kind="requested"), 1)
            self.assertEqual(b._store.count_events(kind="hit"), 1)

    def test_replayed_foreign_request_id_refused(self):
        """A replayed id is honoured only for its own (container, host, port);
        anything else is refused with no hit recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp))
            body, request_id = b.file_request("bottle-a", "neon.tech", 443, request_id="cafebabe")
            self.assertEqual(request_id, "cafebabe")
            events_before = b._store.count_events()
            hits_before = b._store.get("cafebabe").hit_count

            with self.assertRaises(broker.UnknownRequest):
                b.file_request("bottle-b", "api.github.com", 443, request_id="cafebabe")
            # Same container, different host: still a key mismatch.
            with self.assertRaises(broker.UnknownRequest):
                b.file_request("bottle-a", "api.github.com", 443, request_id="cafebabe")

            self.assertEqual(b._store.count_events(), events_before)
            self.assertEqual(b._store.get("cafebabe").hit_count, hits_before)

    def test_client_request_id_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=1)
            body, request_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
                request_id="deadbeef",
            )
            self.assertEqual(request_id, "deadbeef")
            self.assertEqual(body["decision"], "pending")
            self.assertIsNotNone(b._store.get("deadbeef"))
            events = b._store.events_for("deadbeef")
            self.assertEqual(events[0].kind, "requested")
            self.assertEqual(events[0].fields["host"], "neon.tech")

    def test_malformed_request_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=1)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.file_request(
                    "coding-brassbottle",
                    "neon.tech",
                    443,
                    request_id="not-valid",
                )

    def test_open_row_coalesces_by_key_regardless_of_supplied_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp))
            b.file_request("c", "neon.tech", 443, request_id="deadbeef")
            body, request_id = b.file_request("c", "neon.tech", 443, request_id="cafebabe")
            self.assertEqual(request_id, "deadbeef", "the open row's own id wins")
            self.assertEqual(b._store.get("deadbeef").hit_count, 2)
            self.assertIsNone(b._store.get("cafebabe"))

    def test_http_filing_returns_pending_row_in_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            started = time.monotonic()
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"host": "neon.tech", "port": 443, "uid": 7, "reason": "docs"}),
                {"Content-Type": "application/json", "Authorization": "Bearer tok"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            elapsed = time.monotonic() - started
            conn.close()
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertLess(elapsed, 1.0)
            self.assertEqual(body["decision"], "pending")
            self.assertIn("attempt", body)
            row = b._store.get(body["request_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row.container, "c")
            self.assertEqual(row.uid, 7)
            self.assertEqual(row.reason, "docs")

    def test_ip_destination_accepted_and_filed(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"host": "192.0.2.55", "port": 5432}),
                {"Content-Type": "application/json", "Authorization": "Bearer tok"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(body["decision"], "pending")
            row = b._store.get(body["request_id"])
            self.assertEqual(row.host, "192.0.2.55")
            self.assertTrue(row.host_is_ip)
            self.assertEqual(row.port, 5432)

    # ── GET /egress/<id> ─────────────────────────────────────────────────

    def test_get_request_view_pending_and_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp))
            _, request_id = b.file_request("bottle-a", "neon.tech", 443)
            view = b.request_view(request_id, "bottle-a")
            self.assertEqual(view["status"], "open")
            self.assertEqual(view["attempt"], 0)
            self.assertNotIn("decision_body", view)
            self.assertIsNone(b.request_view(request_id, "bottle-b"))
            self.assertIsNone(b.request_view("ffffffff", "bottle-a"))

    def test_get_endpoint_requires_bottle_token_and_answers_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "bottle-a", "token-a")
            self._touch_token(egress_root, "bottle-b", "token-b")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            _, request_id = b.file_request("bottle-a", "neon.tech", 443)
            host, port = self._serve(
                egress_root, b, egress_root / broker.TOKENS_DIRNAME
            )

            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", f"/egress/{request_id}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                f"/egress/{request_id}",
                headers={"Authorization": "Bearer token-b"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, HTTPStatus.NOT_FOUND)
            self.assertEqual(body, {"error": "unknown request"})
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                f"/egress/{request_id}",
                headers={"Authorization": "Bearer token-a"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(body["request_id"], request_id)
            self.assertEqual(body["status"], "open")
            self.assertEqual(body["attempt"], 0)
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                "/egress/ffffffff",
                headers={"Authorization": "Bearer token-a"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.NOT_FOUND)
            conn.close()

    # ── authentication on POST /egress ────────────────────────────────────

    def test_auth_missing_and_incorrect_bearer_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "bottle-a", "test-token")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(
                egress_root, b, egress_root / broker.TOKENS_DIRNAME
            )
            for headers in (
                {"Content-Type": "application/json"},
                {"Content-Type": "application/json", "Authorization": "Bearer wrong"},
            ):
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "neon.tech", "port": 443}),
                    headers,
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
                conn.close()

    def test_token_for_bottle_a_cannot_file_for_bottle_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "bottle-a", "token-a")
            self._touch_token(egress_root, "bottle-b", "token-b")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(
                egress_root, b, egress_root / broker.TOKENS_DIRNAME
            )
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"container": "bottle-b", "host": "neon.tech", "port": 443}),
                {"Content-Type": "application/json", "Authorization": "Bearer token-a"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.FORBIDDEN)
            conn.close()

    def test_auth_correct_bearer_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "my-bottle", "good-token")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(
                egress_root, b, egress_root / broker.TOKENS_DIRNAME
            )
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"container": "my-bottle", "host": "neon.tech", "port": 443}),
                {"Content-Type": "application/json", "Authorization": "Bearer good-token"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(body["decision"], "pending")

    def test_http_replayed_foreign_id_answers_404_without_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "bottle-a", "token-a")
            self._touch_token(egress_root, "bottle-b", "token-b")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            b.file_request("bottle-a", "neon.tech", 443, request_id="cafebabe")
            host, port = self._serve(
                egress_root, b, egress_root / broker.TOKENS_DIRNAME
            )
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps(
                    {"container": "bottle-b", "host": "api.github.com", "port": 22,
                     "request_id": "cafebabe"}
                ),
                {"Content-Type": "application/json", "Authorization": "Bearer token-b"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, HTTPStatus.NOT_FOUND)
            self.assertEqual(body, {"error": "unknown request"})
            row = b._store.get("cafebabe")
            self.assertEqual(row.hit_count, 1)
            self.assertEqual(row.container, "bottle-a")

    def test_http_malformed_request_id_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"host": "neon.tech", "port": 443, "request_id": "BAD-ID"}),
                {"Content-Type": "application/json", "Authorization": "Bearer tok"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(body["error"], "invalid request_id")
            conn.close()

    # ── deciding ─────────────────────────────────────────────────────────

    def test_allow_live_closes_row_with_expected_columns_and_body(self):
        self._decide_allow_branch(scope="live", expected_body={"decision": "allow", "scope": "live"})

    def test_allow_manifest_scope(self):
        self._decide_allow_branch(scope="manifest", expected_body={"decision": "allow", "scope": "manifest"})

    def _decide_allow_branch(self, *, scope: str, expected_body: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                err = b.decide(request_id, "allow", scope=scope)
            self.assertIsNone(err)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "allowed")
            self.assertEqual(row.scope, scope)
            self.assertEqual(row.decided_by, "operator")
            self.assertIsNone(row.last_error)
            self.assertEqual(row.apply_status, "applied")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(row.decision_body, expected_body)
            # The stored per-request body is exactly what a bottle parses.
            self.assertEqual(egress_broker._decision_outcome(row.decision_body), "allow")
            self.assertEqual(
                [event.kind for event in b._store.events_for(request_id)],
                ["requested", "notified", "applied", "allowed"],
            )

    def test_approve_live_builds_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
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

    def test_approve_manifest_builds_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="manifest")
                self.assertEqual(mocked.call_args[0][0][-1], "yml")

    def test_firewall_save_unreachable_in_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
                argv = mocked.call_args[0][0]
                self.assertNotIn("firewall", argv)

    def test_apply_allow_sets_skip_notify_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            envs: list[dict] = []

            def track_run(*args, **kwargs) -> mock.Mock:
                envs.append(kwargs.get("env") or {})
                return mock.Mock(returncode=0)

            with mock.patch("subprocess.run", side_effect=track_run):
                b.decide(request_id, "allow", scope="live")
            self.assertEqual(envs[0].get(broker.DAEMON_SKIP_NOTIFY_ENV), "1")

    def test_apply_runs_before_allowed_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            order: list[str] = []

            def track_run(*args, **kwargs) -> mock.Mock:
                order.append("apply")
                return mock.Mock(returncode=0)

            with mock.patch("subprocess.run", side_effect=track_run):
                err = b.decide(request_id, "allow", scope="live")
            self.assertIsNone(err)
            # The store's own event ids give the write order: applied first.
            events = b._store.events_for(request_id)
            kinds = [event.kind for event in events]
            self.assertEqual(kinds, ["requested", "notified", "applied", "allowed"])
            applied_at = [e.id for e in events if e.kind == "applied"][0]
            allowed_at = [e.id for e in events if e.kind == "allowed"][0]
            self.assertLess(applied_at, allowed_at)

    def test_apply_failure_keeps_row_open_and_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=1)
                err = b.decide(request_id, "allow", scope="live")
                self.assertEqual(err, broker.APPLY_FAILED_REASON)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "apply_failed")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(
                row.last_error,
                {"reason": "apply_failed", "attempt": 1, "at": "2026-09-23T12:00:00Z"},
            )
            self.assertIsNone(row.decision_body)
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["attempt"], 1)
            self.assertEqual(view["last_error"]["attempt"], 1)
            # The stored body fed to the bottle parser is still pending —
            # the error reaches the client through last_error/attempt.
            self.assertEqual(
                egress_broker._decision_outcome(
                    {"decision": "error", "reason": "apply_failed"}
                ),
                "daemon_error",
            )

    def test_ip_approval_never_invokes_allow_egress(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request(
                "coding-brassbottle", "192.0.2.55", 5432, host_is_ip=True
            )
            with mock.patch("subprocess.run") as mocked:
                err = b.decide(request_id, "allow", scope="live")
                mocked.assert_not_called()
            self.assertEqual(err, broker.IP_REQUIRES_CIDR_REASON)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "ip_requires_cidr")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(row.last_error["reason"], "ip_requires_cidr")
            self.assertIsNone(row.decision_body)
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["last_error"]["attempt"], 1)
            self.assertEqual(
                egress_broker._decision_outcome(
                    {"decision": "error", "reason": "ip_requires_cidr"}
                ),
                "daemon_error",
            )

    def test_apply_failure_then_successful_allow_closes_with_last_error_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=1)
                self.assertEqual(
                    b.decide(request_id, "allow", scope="live"),
                    broker.APPLY_FAILED_REASON,
                )
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["attempt"], 1)
            self.assertEqual(view["last_error"]["attempt"], 1)
            self.assertIsNone(view.get("decision_body"))
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                self.assertIsNone(b.decide(request_id, "allow", scope="live"))
            row = b._store.get(request_id)
            self.assertEqual(row.status, "allowed")
            self.assertIsNone(row.last_error)
            self.assertEqual(row.apply_attempts, 2)
            self.assertEqual(row.decision_body, {"decision": "allow", "scope": "live"})

    def test_deny_once_writes_denied_row_generic_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch("subprocess.run") as mocked:
                err = b.decide(request_id, "deny", reason="typo host")
                self.assertIsNone(err)
                mocked.assert_not_called()
            row = b._store.get(request_id)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.scope, "once")
            self.assertEqual(row.decided_by, "operator")
            self.assertEqual(row.deny_reason, "typo host")
            self.assertIsNone(row.denylist_zone)
            self.assertIsNone(row.persist_status)
            self.assertEqual(row.decision_body, {"decision": "deny"})
            self.assertEqual(egress_broker._decision_outcome(row.decision_body), "deny")

    def test_decide_deny_bottle_and_global_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_token(Path(tmp), "coding-brassbottle")
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, once_id = b.file_request("coding-brassbottle", "a.example.com", 443)
            b.decide(once_id, "deny")
            b.file_request("coding-brassbottle", "b.example.com", 443)
            result_bottle = b.persist_deny("b.example.com", "bottle", container="coding-brassbottle")
            self.assertIsNone(result_bottle.error)
            bottle_row = b._store.get(result_bottle.decided[0])
            self.assertEqual(bottle_row.status, "denied")
            self.assertEqual(bottle_row.decided_by, "operator")
            self.assertEqual(bottle_row.scope, "bottle")
            self.assertEqual(bottle_row.denylist_zone, "b.example.com")
            self.assertEqual(bottle_row.denylist_scope, "coding-brassbottle")
            self.assertEqual(
                bottle_row.decision_body,
                {"decision": "deny", "reason": "denylist", "zone": "b.example.com",
                 "scope": "coding-brassbottle"},
            )
            self.assertEqual(bottle_row.persist_status, "persisted")
            self.assertEqual(egress_broker._decision_outcome(bottle_row.decision_body), "deny")

            b.file_request("coding-brassbottle", "c.example.com", 443)
            result_global = b.persist_deny("c.example.com", "global")
            global_row = b._store.get(result_global.decided[0])
            self.assertEqual(global_row.status, "denied")
            self.assertEqual(global_row.scope, "global")
            self.assertEqual(global_row.denylist_zone, "c.example.com")
            self.assertEqual(global_row.denylist_scope, "global")
            self.assertEqual(global_row.persist_status, "persisted")

    def test_persist_failure_degrades_to_one_shot_deny_and_records_persist_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_token(Path(tmp), "coding-brassbottle")
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                result = b.persist_deny(
                    "neon.tech", "global", trigger_request_id=request_id
                )
            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.scope, "once")
            self.assertEqual(row.persist_status, "persist_failed")
            self.assertIsNone(row.decision_body.get("zone"))
            self.assertEqual(egress_broker._decision_outcome(row.decision_body), "deny")

    def test_decide_deny_response_body_via_poll_carry_denylist_zone_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_token(Path(tmp), "coding-brassbottle")
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            result = b.persist_deny(
                "neon.tech", "bottle", container="coding-brassbottle", reason="noisy"
            )
            self.assertIsNone(result.error)
            row = b._store.get(request_id)
            self.assertEqual(
                row.decision_body,
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "neon.tech",
                    "scope": "coding-brassbottle",
                },
            )
            # The operator's free text survives as the row's reason; the
            # machine cause lives in the decision body.
            self.assertEqual(row.deny_reason, "noisy")
            self.assertEqual(row.denylist_zone, "neon.tech")
            self.assertEqual(row.denylist_scope, "coding-brassbottle")
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["decision_body"], row.decision_body)
            self.assertEqual(egress_broker._decision_outcome(row.decision_body), "deny")

    def test_decide_allow_for_zone_releases_matching_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, stripe_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            _, github_id = b.file_request("coding-brassbottle", "api.github.com", 443)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                result = b.decide_allow_for_zone("coding-brassbottle", "stripe.com", scope="live")
            self.assertEqual(result.decided, [stripe_id])
            self.assertEqual(result.apply_failures, [])
            self.assertEqual(b._store.get(stripe_id).status, "allowed")
            self.assertEqual(b._store.get(github_id).status, "open")
            b.decide(github_id, "deny")

    def test_decide_deny_for_zone_releases_matching_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, s1 = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            _, s2 = b.file_request("coding-brassbottle", "api.stripe.com", 443)
            _, other_id = b.file_request("coding-brassbottle", "example.com", 443)
            _, foreign_id = b.file_request("other-container", "docs.stripe.com", 443)
            decided = b.decide_deny_for_zone("coding-brassbottle", "stripe.com", reason="not needed")
            self.assertEqual(set(decided), {s1, s2})
            for rid in (s1, s2):
                row = b._store.get(rid)
                self.assertEqual(row.status, "denied")
                self.assertEqual(row.deny_reason, "not needed")
                self.assertEqual(row.decision_body, {"decision": "deny"})
            self.assertEqual(b._store.get(other_id).status, "open")
            self.assertEqual(b._store.get(foreign_id).status, "open")

    def test_zone_sweeps_exclude_apply_in_progress_requests(self):
        """A concurrent allow mid-apply must be counted neither as decided
        nor as an apply failure — the in-flight apply resolves it."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            b._applying.add(request_id)

            allow_result = b.decide_allow_for_zone("coding-brassbottle", "stripe.com", scope="live")
            self.assertEqual(allow_result.decided, [])
            self.assertEqual(allow_result.apply_failures, [])
            self.assertEqual(b._store.get(request_id).status, "open")

            deny_decided = b.decide_deny_for_zone("coding-brassbottle", "stripe.com")
            self.assertEqual(deny_decided, [])
            self.assertEqual(b._store.get(request_id).status, "open")

            persist_result = b.persist_deny("stripe.com", "global")
            self.assertIsNone(persist_result.error)
            self.assertEqual(persist_result.decided, [])
            self.assertEqual(b._store.get(request_id).status, "open")

            # Release the in-flight apply so the row can be resolved.
            b._applying.discard(request_id)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                b.decide(request_id, "allow", scope="live")
            self.assertEqual(b._store.get(request_id).status, "allowed")

    def test_decide_allow_for_zone_skip_is_logged_not_swallowed(self):
        """A per-candidate EgressBrokerHostError is logged, not swallowed."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
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
            self.assertIn(f"request_id={request_id}", skip_lines[0])
            self.assertIn("reason=already decided", skip_lines[0])

    def test_decide_deny_for_zone_skip_is_logged_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
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
            self.assertIn(f"request_id={request_id}", skip_lines[0])

    def test_decide_on_unknown_and_decided_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide("deadbeef", "deny")
            _, request_id = b.file_request("c", "neon.tech", 443)
            b.decide(request_id, "deny")
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide(request_id, "allow")

    def test_decide_deny_invalid_scope_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide(request_id, "deny", scope="banana")
            b.decide(request_id, "deny")

    def test_decide_allow_invalid_scope_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide(request_id, "allow", scope="banana")
            b.decide(request_id, "deny")

    def test_decide_invalid_decision_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("c", "neon.tech", 443)
            with self.assertRaises(broker.EgressBrokerHostError):
                b.decide(request_id, "maybe")
            b.decide(request_id, "deny")

    def test_decide_deny_for_zone_no_longer_accepts_scope(self):
        """decide_deny_for_zone is scope=once only — a persistent deny goes
        through EgressBroker.persist_deny."""
        b = self._broker(Path(tempfile.mkdtemp()), FakeClock(NOW), hold_seconds=5)
        with self.assertRaises(TypeError):
            b.decide_deny_for_zone("coding-brassbottle", "stripe.com", scope="global")  # type: ignore[call-arg]

    def test_decide_public_wrapper_rejects_internal_kwargs(self):
        """decide() is a thin wrapper whose signature has no
        denylist_zone/denylist_scope/persist_failed — an operator surface
        passing one must fail loudly rather than silently reaching
        persist_deny()-only behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", persist_failed=True)
            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", denylist_zone="x.com")
            with self.assertRaises(TypeError):
                b.decide("deadbeef", "deny", denylist_scope="global")

    def test_concurrent_decide_allow_applies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            calls: list[list[str]] = []
            gate = threading.Event()

            def slow_run(*args, **kwargs) -> mock.Mock:
                calls.append(list(args[0]) if args else [])
                gate.wait(timeout=10)
                return mock.Mock(returncode=0)

            with mock.patch("subprocess.run", side_effect=slow_run):
                first = threading.Thread(
                    target=b.decide, args=(request_id, "allow"), kwargs={"scope": "live"}
                )
                first.start()
                # Second decide while the first apply is in flight is a
                # documented no-op, not a second apply.
                self.assertEqual(
                    b.decide(request_id, "allow", scope="live"),
                    broker.APPLY_IN_PROGRESS_REASON,
                )
                gate.set()
                join_thread_or_fail(first, label="decide")
            self.assertEqual(len(calls), 1)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "allowed")
            self.assertEqual(row.apply_attempts, 1)

    # ── /decide HTTP endpoint ─────────────────────────────────────────────

    def _post_decide(self, host, port, payload, token=None):
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/decide",
            json.dumps(payload),
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token or self.OPERATOR_TOKEN}",
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = None
        return resp.status, body

    def test_decide_endpoint_requires_operator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "bottle-tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, _body = self._post_decide(
                host,
                port,
                {
                    "container": "coding-brassbottle",
                    "host": "docs.stripe.com",
                    "decision": "allow",
                    "scope": "live",
                },
                token="bottle-tok",
            )
            self.assertEqual(status, HTTPStatus.UNAUTHORIZED)

    def test_decide_endpoint_allow_releases_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "bottle-tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
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
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(len(body["decided"]), 1)
            self.assertEqual(body["apply_failures"], [])
            row = b._store.get(request_id)
            self.assertEqual(row.status, "allowed")
            self.assertEqual(row.decision_body, {"decision": "allow", "scope": "live"})

    def test_decide_endpoint_allow_ip_literal_reports_apply_failure_and_keeps_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "bottle-tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "192.0.2.55", 443)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
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
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(body["decided"], [])
            self.assertEqual(
                body["apply_failures"],
                [{"request_id": request_id, "reason": broker.IP_REQUIRES_CIDR_REASON}],
            )
            row = b._store.get(request_id)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "ip_requires_cidr")
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["attempt"], 1)
            self.assertEqual(view["last_error"]["attempt"], 1)
            b.decide(request_id, "deny")

    def test_decide_endpoint_allow_apply_failed_reports_failure_and_keeps_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "bottle-tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
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
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(body["decided"], [])
            self.assertEqual(
                body["apply_failures"],
                [{"request_id": request_id, "reason": broker.APPLY_FAILED_REASON}],
            )
            row = b._store.get(request_id)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "apply_failed")
            b.decide(request_id, "deny")

    def test_decide_endpoint_deny_releases_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "bottle-tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            with mock.patch("subprocess.run") as mocked:
                status, body = self._post_decide(
                    host,
                    port,
                    {
                        "container": "coding-brassbottle",
                        "host": "stripe.com",
                        "decision": "deny",
                        "reason": "typo host",
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(len(body["decided"]), 1)
                self.assertNotIn("apply_failures", body)
                mocked.assert_not_called()
            row = b._store.get(request_id)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.decision_body, {"decision": "deny"})

    def test_decide_endpoint_rejects_unknown_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, body = self._post_decide(
                host,
                port,
                {"container": "coding-brassbottle", "host": "stripe.com", "decision": "maybe"},
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(body["error"], "decision must be allow or deny")

    def test_decide_endpoint_deny_scope_validation_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            for scope, expected_status in (
                ("banana", HTTPStatus.BAD_REQUEST),
                ("", HTTPStatus.BAD_REQUEST),
                (None, HTTPStatus.OK),  # absent scope -> once
                ("once", HTTPStatus.OK),
                ("bottle", HTTPStatus.OK),  # container "c" exists (a token on disk)
                ("global", HTTPStatus.OK),
            ):
                with self.subTest(scope=scope):
                    payload = {"container": "c", "host": "matrix.example.com", "decision": "deny"}
                    if scope is not None:
                        payload["scope"] = scope
                    status, _body = self._post_decide(host, port, payload)
                    self.assertEqual(status, expected_status)
                # keep the queue clean between matrix rows
                for row in b._store.list_open():
                    b.decide(row.request_id, "deny")

    def test_decide_endpoint_deny_scope_bottle_unknown_bottle_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "ghost-bottle",
                    "host": "stripe.com",
                    "decision": "deny",
                    "scope": "bottle",
                },
            )
            # bottle scope requires the NAMED bottle to exist, not the caller's
            self.assertEqual(status, HTTPStatus.BAD_REQUEST)

    def test_decide_endpoint_deny_scope_global_persist_failure_returns_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                status, body = self._post_decide(
                    host,
                    port,
                    {"host": "stripe.com", "decision": "deny", "scope": "global"},
                )
            self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(body["error"], broker.DENYLIST_PERSIST_FAILED_REASON)

    def test_decide_endpoint_allow_rejects_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "c",
                    "host": "stripe.com",
                    "decision": "allow",
                    "scope": "live",
                    "reason": "why",
                },
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(body["error"], "reason only applies to deny")

    def test_decide_endpoint_deny_rejects_long_or_non_string_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            for reason in ("x" * 201, 42, None):
                with self.subTest(reason=reason):
                    status, body = self._post_decide(
                        host,
                        port,
                        {
                            "container": "c",
                            "host": "stripe.com",
                            "decision": "deny",
                            "reason": reason,
                        },
                    )
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(
                        body["error"], "reason must be a string of at most 200 characters"
                    )

    def test_decide_endpoint_deny_unhashable_scope_returns_400_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "c",
                    "host": "stripe.com",
                    "decision": "deny",
                    "scope": ["once"],
                },
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(body["error"], "invalid scope")

    def test_decide_endpoint_deny_container_optional_for_global_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            status, body = self._post_decide(
                host,
                port,
                {"host": "stripe.com", "decision": "deny", "scope": "global"},
            )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(
                body["persisted"], {"zone": "stripe.com", "scope": "global"}
            )

    def test_decide_endpoint_deny_container_required_for_once_and_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            for scope in ("once", "bottle"):
                status, body = self._post_decide(
                    host,
                    port,
                    {"host": "stripe.com", "decision": "deny", "scope": scope},
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(body["error"], "container is required")

    def test_decide_endpoint_allow_live_flow_via_http_end_to_end(self):
        """The operator HTTP reply stays byte-identical in shape while the
        store records the outcome and the stored body parses as allow."""
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c", "tok")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=5)
            conn = HTTPConnection(*self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME), timeout=5)
            conn.request(
                "POST",
                "/egress",
                json.dumps({"host": "docs.stripe.com", "port": 443}),
                {"Content-Type": "application/json", "Authorization": "Bearer tok"},
            )
            filed = json.loads(conn.getresponse().read().decode("utf-8"))
            conn.close()
            request_id = filed["request_id"]
            self.assertEqual(filed["decision"], "pending")

            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                status, body = self._post_decide(
                    *self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME),
                    {
                        "container": "c",
                        "host": "stripe.com",
                        "decision": "allow",
                        "scope": "live",
                    },
                )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(body["decided"], [request_id])
            self.assertEqual(body["apply_failures"], [])
            row = b._store.get(request_id)
            self.assertEqual(row.status, "allowed")
            self.assertEqual(row.decided_by, "operator")
            self.assertEqual(row.decision_body, {"decision": "allow", "scope": "live"})
            self.assertEqual(egress_broker._decision_outcome(row.decision_body), "allow")

    # ── apply failure while a client polls (the attempt-baseline contract) ─

    def test_apply_failure_reaches_only_clients_that_filed_before_the_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)
            # First filing: its baseline is 0 (nothing has completed yet).
            body1, id1 = b.file_request("c", "neon.tech", 443)
            self.assertEqual(body1["attempt"], 0)

            release = threading.Event()

            def blocked_run(*args, **kwargs) -> mock.Mock:
                release.wait(timeout=30)
                return mock.Mock(returncode=1)

            apply_thread = threading.Thread(
                target=lambda: b.decide(id1, "allow", scope="live"),
                daemon=True,
            )
            with mock.patch("subprocess.run", side_effect=blocked_run):
                apply_thread.start()
                # Wait until the apply subprocess is actually in flight.
                deadline = time.monotonic() + 10
                while not b._applying and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(b._applying)

                # Second filing DURING the blocked attempt: baseline 0 too.
                body2, id2 = b.file_request("c", "neon.tech", 443)
                self.assertEqual(id2, id1, "coalesces onto the same open row")
                self.assertEqual(body2["attempt"], 0)

                release.set()
                join_thread_or_fail(apply_thread, label="decide-with-blocked-apply")

            # The failed attempt is recorded; the row stays open.
            row = b._store.get(id1)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(row.last_error["attempt"], 1)

            # Both pre-attempt filings now see the error on their next poll.
            view1 = b.request_view(id1, "c")
            self.assertGreater(view1["last_error"]["attempt"], body1["attempt"])
            self.assertGreater(view1["last_error"]["attempt"], body2["attempt"])

            # A filing AFTER the failure takes the new baseline and must NOT
            # treat the old error as its own.
            body3, id3 = b.file_request("c", "neon.tech", 443)
            self.assertEqual(body3["attempt"], 1)
            self.assertEqual(body3["last_error"]["attempt"], 1)
            self.assertFalse(
                body3["last_error"]["attempt"] > body3["attempt"],
                "a client that filed after the failed attempt ignores it",
            )

            # A successful allow closes the row and clears last_error.
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                self.assertIsNone(b.decide(id1, "allow", scope="live"))
            row = b._store.get(id1)
            self.assertEqual(row.status, "allowed")
            self.assertIsNone(row.last_error)
            self.assertEqual(row.decision_body, {"decision": "allow", "scope": "live"})

    # ── persistent deny list ─────────────────────────────────────────────

    def test_persist_deny_writes_caller_named_zone_even_with_no_open_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            result = b.persist_deny("datadoghq.com", "global", reason="telemetry")
            self.assertIsNone(result.error)
            self.assertEqual(result.decided, [])
            self.assertEqual(result.entry.zone, "datadoghq.com")
            self.assertEqual(result.entry.scope, "global")
            on_disk = b._denylist.matches("any-bottle", "us5.datadoghq.com")
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.zone, "datadoghq.com")

    def test_persist_deny_bottle_scope_requires_container(self):
        b = self._broker(Path(tempfile.mkdtemp()), FakeClock(NOW), hold_seconds=5)
        with self.assertRaises(broker.EgressBrokerHostError):
            b.persist_deny("example.com", "bottle")

    def test_persist_deny_bottle_scope_rejects_unknown_bottle(self):
        """A typo'd bottle must never produce a dead entry — validate runs
        BEFORE any write, and nothing lands on disk when it fails."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(broker.EgressBrokerHostError) as ctx:
                b.persist_deny("example.com", "bottle", container="ghost-bottle")
            self.assertIn("unknown bottle", str(ctx.exception))
            self.assertFalse((Path(tmp) / broker.DENYLIST_FILENAME).exists())

    def test_persist_deny_bottle_scope_rejects_container_named_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            with self.assertRaises(broker.EgressBrokerHostError) as ctx:
                b.persist_deny("example.com", "bottle", container="global")
            self.assertIn("reserved", str(ctx.exception))
            self.assertFalse((Path(tmp) / broker.DENYLIST_FILENAME).exists())

    def test_persist_deny_bottle_scope_sweeps_only_that_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_token(Path(tmp), "coding-brassbottle")
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, id_a = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            _, id_b = b.file_request("other-bottle", "docs.stripe.com", 443)

            result = b.persist_deny("docs.stripe.com", "bottle", container="coding-brassbottle")
            self.assertIsNone(result.error)
            self.assertEqual(result.decided, [id_a])
            self.assertEqual(result.entry.scope, "coding-brassbottle")

            row_a = b._store.get(id_a)
            self.assertEqual(row_a.status, "denied")
            self.assertEqual(row_a.denylist_scope, "coding-brassbottle")
            self.assertEqual(
                row_a.decision_body,
                {"decision": "deny", "reason": "denylist", "zone": "docs.stripe.com",
                 "scope": "coding-brassbottle"},
            )
            # The other bottle's open request must NOT be swept.
            self.assertEqual(b._store.get(id_b).status, "open")
            b.decide(id_b, "deny")

    def test_persist_deny_global_scope_sweeps_every_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, id_a = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            _, id_b = b.file_request("other-bottle", "docs.stripe.com", 443)
            result = b.persist_deny("docs.stripe.com", "global")
            self.assertIsNone(result.error)
            self.assertEqual(set(result.decided), {id_a, id_b})
            for rid in (id_a, id_b):
                row = b._store.get(rid)
                self.assertEqual(row.status, "denied")
                self.assertEqual(
                    row.decision_body,
                    {"decision": "deny", "reason": "denylist", "zone": "docs.stripe.com",
                     "scope": "global"},
                )
                self.assertEqual(row.persist_status, "persisted")

    def test_persist_deny_invalid_scope_raises(self):
        b = self._broker(Path(tempfile.mkdtemp()), FakeClock(NOW), hold_seconds=5)
        with self.assertRaises(broker.EgressBrokerHostError):
            b.persist_deny("example.com", "once")

    def test_persist_deny_accepts_ip_literal_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            result = b.persist_deny("93.184.216.34", "global")
            self.assertIsNone(result.error)
            self.assertEqual(result.entry.zone, "93.184.216.34")
            self.assertIsNotNone(b._denylist.matches("any-bottle", "93.184.216.34"))
            self.assertIsNone(b._denylist.matches("any-bottle", "93.184.216.35"))

    def test_persist_deny_write_happens_outside_lock_then_reloads_under_it(self):
        """DenyList.add() (its own flock + write + os.replace) must run
        OUTSIDE self._lock; immediately after add() returns, persist_deny
        takes self._lock again just long enough to force self._denylist to
        reload, so a concurrent matches() cannot race the post-write state."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
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
            self.assertTrue(observed["lock_was_free_during_add"])
            self.assertFalse(observed["lock_was_free_during_reload"])

    def test_persist_deny_write_failure_returns_reason_and_sweeps_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                result = b.persist_deny("docs.stripe.com", "global")
            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(result.entry)
            self.assertEqual(result.decided, [])
            self.assertEqual(b._store.get(request_id).status, "open")
            b.decide(request_id, "deny")

    def test_persist_deny_trigger_request_id_closed_on_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                result = b.persist_deny(
                    "docs.stripe.com", "global", trigger_request_id=request_id
                )
            self.assertEqual(result.error, broker.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(result.entry)
            self.assertEqual(result.decided, [])
            row = b._store.get(request_id)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.scope, "once")
            self.assertEqual(row.persist_status, "persist_failed")
            self.assertEqual(row.decision_body, {"decision": "deny"})
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "docs.stripe.com"))

    def test_persist_deny_write_failure_with_stale_trigger_id_logs_not_swallows(self):
        """The write failed AND trigger_request_id no longer names an open
        request — one INFO line instead of vanishing; persist_deny returns
        normally (no raise)."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
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
        """persist_failed is internal-only — driven through _close_request()
        directly, exactly like persist_deny() itself does."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            err = b._close_request(request_id, "deny", scope="bottle", persist_failed=True)
            self.assertEqual(err, broker.DENYLIST_PERSIST_FAILED_REASON)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.scope, "once")
            self.assertEqual(row.decision_body, {"decision": "deny"})
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))

    def test_decide_deny_scope_once_does_not_write_denylist_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            b.decide(request_id, "deny", scope="once", reason="no")
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))

    def test_decide_deny_scope_bottle_no_longer_writes_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_token(Path(tmp), "coding-brassbottle")
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            b.decide(request_id, "deny", scope="bottle")
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))

    def test_decide_deny_scope_global_no_longer_writes_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            b.decide(request_id, "deny", scope="global")
            self.assertIsNone(b._denylist.matches("coding-brassbottle", "neon.tech"))
            denied = b._store.get(request_id)
            self.assertEqual(denied.scope, "global")
            self.assertIsNone(denied.denylist_zone)

    # ── denylist short-circuit ─────────────────────────────────────────────

    def test_denylist_short_circuit_returns_deny_body_and_never_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global", reason="telemetry")

            with mock.patch.object(b, "_dispatch_notifier") as notify:
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
            row = b._store.get(request_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.decided_by, "denylist")
            self.assertEqual(row.denylist_zone, "datadoghq.com")
            self.assertEqual(row.denylist_scope, "global")
            self.assertEqual(row.deny_reason, "denylist")
            self.assertEqual(row.decision_body, body)
            self.assertEqual(
                [(event.kind) for event in b._store.events_for(request_id)],
                ["requested", "denied"],
            )
            # The requested event carries the FULL filing fields, not a
            # stripped-down container/host/port-only version.
            requested = b._store.events_for(request_id)[0]
            self.assertEqual(requested.fields["container"], "coding-brassbottle")
            self.assertEqual(requested.fields["host"], "http-intake.logs.us5.datadoghq.com")
            self.assertEqual(requested.fields["port"], 443)
            # Nothing open: the request never entered the operator queue.
            self.assertEqual(b._store.list_open(), [])

    def test_denylist_short_circuit_requested_event_carries_full_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
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
            requested = b._store.events_for(request_id)[0]
            self.assertEqual(requested.fields.get("uid"), 1000)
            self.assertEqual(requested.fields.get("comm"), "curl")
            self.assertEqual(requested.fields.get("reason"), "ci fetch")

        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="93.184.216.34", scope="global")

            body, request_id = b.file_request(
                "coding-brassbottle",
                "93.184.216.34",
                443,
                host_is_ip=True,
            )
            self.assertEqual(body["decision"], "deny")
            row = b._store.get(request_id)
            self.assertTrue(row.host_is_ip)
            requested = b._store.events_for(request_id)[0]
            self.assertEqual(requested.fields.get("host_is_ip"), True)

    def test_denylist_short_circuit_id_matches_row_with_client_supplied_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            body, request_id = b.file_request(
                "coding-brassbottle",
                "datadoghq.com",
                443,
                request_id="cafebabe",
            )
            self.assertEqual(request_id, "cafebabe")
            self.assertEqual(body["decision"], "deny")
            row = b._store.get("cafebabe")
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.decided_by, "denylist")

    def test_denylist_repeat_within_window_suppresses_without_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            body1, id1 = b.file_request("coding-brassbottle", "datadoghq.com", 443)
            events_after_first = b._store.count_events()
            self.assertEqual(
                [event.kind for event in b._store.events_for(id1)],
                ["requested", "denied"],
            )

            clock.advance(1)
            body2, id2 = b.file_request("coding-brassbottle", "datadoghq.com", 443)
            self.assertEqual(body2, body1, "the response body is identical")
            self.assertEqual(id2, id1, "the repeat suppresses onto the SAME denied row")
            self.assertEqual(b._store.get(id1).hit_count, 2)
            self.assertEqual(b._store.count_events(), events_after_first)

            clock.advance(broker.DENYLIST_SUPPRESS_SECONDS + 1)
            body3, id3 = b.file_request("coding-brassbottle", "datadoghq.com", 443)
            self.assertEqual(body3["decision"], "deny")
            self.assertNotEqual(id3, id1, "a repeat after the window opens a NEW row")
            self.assertEqual(b._store.get(id3).hit_count, 1)

    def test_denylist_coalescing_keys_by_matched_zone_not_raw_host(self):
        """Distinct subdomains under the same denylisted zone coalesce
        TOGETHER (one row per zone per window), not one window per raw host."""
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            ids = []
            for host in ("a.datadoghq.com", "b.datadoghq.com", "c.datadoghq.com"):
                body, rid = b.file_request("coding-brassbottle", host, 443)
                self.assertEqual(body["decision"], "deny")
                ids.append(rid)
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(len(b._denylist_hits), 1)
            self.assertIn(("coding-brassbottle", "datadoghq.com"), b._denylist_hits)

    def test_denylist_hit_last_prunes_stale_keys_on_insert(self):
        """_denylist_hits must not grow without bound — a stale entry is
        dropped the next time any key is inserted."""
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=5)
            b._denylist.add(zone="datadoghq.com", scope="global")

            b.file_request("coding-brassbottle", "a.datadoghq.com", 443)
            self.assertEqual(len(b._denylist_hits), 1)
            clock.advance(broker.DENYLIST_SUPPRESS_SECONDS * 2)
            b.file_request("coding-brassbottle", "b.datadoghq.com", 443)
            self.assertEqual(len(b._denylist_hits), 1, "the stale key was pruned")
            self.assertIn(("coding-brassbottle", "datadoghq.com"), b._denylist_hits)

    def test_open_request_not_short_circuited_by_a_later_denylist_entry(self):
        """An already-open request never reaches the denylist short-circuit,
        so a later denylist entry cannot retroactively deny it."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            _, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            b._denylist.add(zone="neon.tech", scope="global")
            body, request_id2 = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(request_id2, request_id)
            self.assertEqual(body["decision"], "pending")
            self.assertEqual(b._store.get(request_id).status, "open")
            self.assertIsNone(b._store.get(request_id).decided_by)

    def test_denylist_matches_reload_picks_up_cli_edit_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=5)
            self.assertIsNone(b._denylist.matches("any-bottle", "docs.stripe.com"))
            b._denylist.add(zone="stripe.com", scope="global", reason="no")
            self.assertIsNotNone(b._denylist.matches("any-bottle", "docs.stripe.com"))

    # ── stale sweep ────────────────────────────────────────────────────────

    def test_stale_sweep_closes_old_requests_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=60)
            body, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(body["decision"], "pending")
            clock.advance((STALE_TEST_HOURS + 1) * 3600)
            closed = b.sweep_stale()
            self.assertEqual(closed, 1)
            row = b._store.get(request_id)
            self.assertEqual(row.status, "stale")
            self.assertEqual(row.decided_by, "sweep")
            self.assertEqual(row.deny_reason, "stale")
            self.assertEqual(row.decision_body, {"decision": "deny", "reason": "stale"})
            # What a polling client sees on its next poll.
            view = b.request_view(request_id, "coding-brassbottle")
            self.assertEqual(view["decision_body"], {"decision": "deny", "reason": "stale"})
            self.assertEqual(
                egress_broker._decision_outcome(view["decision_body"]), "deny"
            )
            # A later filing of the same host opens a NEW row.
            body2, request_id2 = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(body2["decision"], "pending")
            self.assertNotEqual(request_id2, request_id)
            self.assertEqual(b._store.get(request_id2).status, "open")
            b.decide(request_id2, "deny")

    def test_stale_sweep_leaves_fresh_requests_and_in_flight_applies_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=60)
            _, old_id = b.file_request("c", "old.example.com", 443)
            clock.advance((broker.STALE_HOURS + 1) * 3600)
            _, fresh_id = b.file_request("c", "neon.tech", 443)
            self.assertEqual(b.sweep_stale(), 1)
            self.assertEqual(b._store.get(old_id).status, "stale")
            self.assertEqual(b._store.get(fresh_id).status, "open")
            b.decide(fresh_id, "deny")

    # ── queue snapshot ─────────────────────────────────────────────────────

    def test_get_queue_requires_operator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/queue")
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
            conn.close()
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/queue", headers={"Authorization": "Bearer wrong"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
            conn.close()

    def test_get_queue_with_operator_token_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/queue", headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(body["open"], [])
            self.assertEqual(body["count"], 0)
            self.assertEqual(body["recent"], [])
            self.assertTrue(body["generated_at"].endswith("Z"))

    def test_queue_snapshot_details_ordering_attempt_and_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            b = self._broker(Path(tmp), clock, hold_seconds=1)
            body1, id1 = b.file_request(
                "c", "docs.stripe.com", 443, uid=1000, comm="curl", reason="npm install",
                hold_seconds=0,
            )
            clock.advance(3)
            body2, id2 = b.file_request(
                "c", "192.0.2.55", 5432, uid=1001, comm="python", reason="db connect",
                hold_seconds=0,
            )
            # One apply failure so attempt/last_error ride on the row.
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=1)
                b.decide(id1, "allow", scope="live")

            clock.advance(2)
            snapshot = b.queue_snapshot()
            self.assertEqual(snapshot["count"], 2)
            self.assertEqual(
                [item["request_id"] for item in snapshot["open"]], [id1, id2]
            )
            first_row = snapshot["open"][0]
            self.assertEqual(first_row["container"], "c")
            self.assertEqual(first_row["host"], "docs.stripe.com")
            self.assertEqual(first_row["port"], 443)
            self.assertFalse(first_row["host_is_ip"])
            self.assertGreaterEqual(first_row["age_seconds"], 0)
            self.assertEqual(first_row["hit_count"], 1)
            self.assertEqual(first_row["uid"], 1000)
            self.assertEqual(first_row["comm"], "curl")
            self.assertEqual(first_row["reason"], "npm install")
            self.assertEqual(first_row["attempt"], 1)
            self.assertEqual(first_row["last_error"]["reason"], "apply_failed")

            self.assertEqual(snapshot["recent"], [])

            # Decide the second row; it moves into `recent`.
            b.decide(id2, "deny", scope="bottle")
            clock.advance(1)
            snapshot = b.queue_snapshot()
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(len(snapshot["recent"]), 1)
            recent = snapshot["recent"][0]
            self.assertEqual(
                set(recent),
                {
                    "request_id", "container", "host", "port", "status", "scope",
                    "decided_at", "decided_by", "apply_status", "deny_reason",
                },
            )
            self.assertEqual(recent["request_id"], id2)
            self.assertEqual(recent["status"], "denied")
            self.assertEqual(recent["scope"], "bottle")
            self.assertEqual(recent["decided_by"], "operator")
            self.assertIsNone(recent["apply_status"])

    def test_get_queue_coalesced_hits_increase_hit_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            for _ in range(2):
                conn = HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/egress",
                    json.dumps({"host": "docs.stripe.com", "port": 443, "hold_seconds": 0}),
                    {"Content-Type": "application/json", "Authorization": "Bearer tok"},
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, HTTPStatus.OK)
                _ = resp.read()
                conn.close()
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/queue", headers={"Authorization": f"Bearer {self.OPERATOR_TOKEN}"})
            body = json.loads(conn.getresponse().read().decode("utf-8"))
            conn.close()
            self.assertEqual(body["count"], 1)
            self.assertGreaterEqual(body["open"][0]["hit_count"], 2)

    def test_get_queue_excludes_decided_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            _, request_id = b.file_request("c", "docs.stripe.com", 443, hold_seconds=0)
            b.decide(request_id, "deny")
            snapshot = b.queue_snapshot()
            self.assertEqual(snapshot["open"], [])
            self.assertEqual(snapshot["count"], 0)
            self.assertEqual(len(snapshot["recent"]), 1)

    # ── misc endpoints ─────────────────────────────────────────────────────

    def test_health_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(body, {"status": "ok"})
            conn.close()

    def test_get_health_no_auth_and_unknown_get_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            self._touch_token(egress_root, "c")
            b = self._broker(egress_root, FakeClock(NOW), hold_seconds=1)
            host, port = self._serve(egress_root, b, egress_root / broker.TOKENS_DIRNAME)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/nope")
            resp = conn.getresponse()
            self.assertEqual(resp.status, HTTPStatus.NOT_FOUND)
            self.assertEqual(json.loads(resp.read().decode("utf-8")), {"error": "not found"})
            conn.close()

    # ── notifications ───────────────────────────────────────────────────────

    def test_file_request_invokes_notifier_once_per_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)
            recorded: list[egress_notify.EgressNotification] = []

            def recorder(notification: egress_notify.EgressNotification) -> None:
                recorded.append(notification)

            b = self._broker(Path(tmp), clock, hold_seconds=60)
            b._notifier = recorder
            body1, request_id = b.file_request(
                "coding-brassbottle",
                "neon.tech",
                443,
                uid=1000,
                comm="curl",
                reason="npm install",
            )
            body2, request_id2 = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(request_id2, request_id)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0].request_id, request_id)
            self.assertEqual(recorded[0].container, "coding-brassbottle")
            self.assertEqual(recorded[0].host, "neon.tech")
            self.assertEqual(recorded[0].port, 443)
            self.assertEqual(recorded[0].uid, 1000)
            self.assertEqual(recorded[0].comm, "curl")
            self.assertEqual(recorded[0].reason, "npm install")

    def test_notifier_exception_does_not_break_file_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(NOW)

            def broken_notifier(_notification: egress_notify.EgressNotification) -> None:
                raise RuntimeError("boom")

            b = self._broker(Path(tmp), clock, hold_seconds=60)
            b._notifier = broken_notifier
            with self.assertLogs("egress_broker_host", level="WARNING") as captured:
                body, request_id = b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertTrue(any("notifier raised" in line for line in captured.output))
            row = b._store.get(request_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "open")

    def test_notifier_called_outside_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._broker(Path(tmp), FakeClock(NOW), hold_seconds=60)

            acquired_during_notify: list[bool] = []

            def lock_probe(_notification: egress_notify.EgressNotification) -> None:
                acquired = b._lock.acquire(timeout=1)
                acquired_during_notify.append(acquired)
                if acquired:
                    b._lock.release()

            b._notifier = lock_probe
            b.file_request("coding-brassbottle", "neon.tech", 443)
            self.assertEqual(acquired_during_notify, [True])

    def test_notify_egress_daemon_curls_use_max_time(self):
        script = (REPO_ROOT / "bin" / "allow-egress.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(script.count("--max-time"), 2)

    # ── singleton lock, tokens, base path ──────────────────────────────────

    def test_second_daemon_instance_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / broker.LOCK_FILENAME
            first = broker.DaemonLock(lock_path)
            first.acquire()
            second = broker.DaemonLock(lock_path)
            with self.assertRaises(broker.DaemonAlreadyRunning):
                second.acquire()
            first.release()

    def test_bottle_token_store_reload_on_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens_dir = Path(tmp) / "tokens"
            tokens_dir.mkdir()
            store = broker.BottleTokenStore(tokens_dir)
            self.assertIsNone(store.resolve_bottle("new-token"))
            (tokens_dir / "late.token").write_text("new-token\n", encoding="utf-8")
            self.assertEqual(store.resolve_bottle("new-token"), "late")

    def test_resolve_base_path_expands_user_and_trims(self):
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
        self.assertEqual(broker.resolve_base_path("~/djinn"), Path.home() / "djinn")

    # ── cutover import ─────────────────────────────────────────────────────

    def _write_legacy_log(self, root: Path, when: datetime, lines: list[dict]) -> Path:
        path = egress_store._legacy_log_path(
            root, egress_store._legacy_month_filename(when)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n"
            for line in lines
        )
        path.write_text(payload, encoding="utf-8")
        return path

    def test_first_start_imports_open_requests_from_legacy_log_once(self):
        """PIN — first start with an empty database carries still-open
        requests across from the legacy month file, exactly once."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                NOW,
                [
                    {
                        "ts": "2026-09-23T11:00:00Z",
                        "kind": "requested",
                        "request_id": "req-carry",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                        "uid": 1000,
                        "comm": "curl",
                        "reason": "api docs",
                    },
                    {
                        "ts": "2026-09-23T11:00:01Z",
                        "kind": "hit",
                        "request_id": "req-carry",
                        "count": 1,
                    },
                    {
                        "ts": "2026-09-23T11:30:00Z",
                        "kind": "allowed",
                        "request_id": "req-closed",
                    },
                ],
            )

            clock = FakeClock(NOW)
            b = self._broker(root, clock, hold_seconds=5)
            row = b._store.get("req-carry")
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.container, "coding-brassbottle")
            self.assertEqual(row.host, "docs.stripe.com")
            self.assertEqual(row.uid, 1000)
            self.assertEqual(row.reason, "api docs")
            self.assertEqual(row.hit_count, 2)
            # The imported row is approvable.
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                self.assertIsNone(b.decide("req-carry", "allow", scope="live"))
            self.assertEqual(b._store.get("req-carry").status, "allowed")

            # A second broker instance imports NOTHING (the database is no
            # longer empty; import is once).
            events_before = b._store.count_events()
            b2 = self._broker(root, clock, hold_seconds=5)
            self.assertEqual(b2._store.request_count(), 1)
            self.assertEqual(b2._store.count_events(), events_before)
            self.assertEqual(b2._store.get("req-carry").status, "allowed")


class IPv6BindTests(unittest.TestCase):
    def test_address_family_for_host_table(self):
        # NOTE: the "no-such-host.invalid" case performs a real getaddrinfo;
        # on a network-isolated host it hangs on the DNS timeout. It is the
        # one network-bound test in this module and is run separately.
        cases = [
            ("", socket.AF_INET),
            ("0.0.0.0", socket.AF_INET),
            ("127.0.0.1", socket.AF_INET),
            ("10.8.0.5", socket.AF_INET),
            ("::", socket.AF_INET6),
            ("::1", socket.AF_INET6),
            ("fd00::5", socket.AF_INET6),
        ]
        for bind_host, family in cases:
            with self.subTest(bind_host=bind_host):
                self.assertEqual(broker.address_family_for_host(bind_host), family)

    @unittest.skipUnless(socket.has_ipv6, "no IPv6 support on this host")
    def test_server_binds_and_serves_over_ipv6(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            tokens_dir = egress_root / broker.TOKENS_DIRNAME
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "c.token").write_text("tok\n", encoding="utf-8")
            b = EgressBrokerHostTests()._broker(egress_root, FakeClock(NOW))
            token_store = broker.BottleTokenStore(tokens_dir)
            server = broker.EgressBrokerHTTPServer(
                ("::1", 0), b, token_store, "operator-test-token"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                host, port, _flow, _scope = server.server_address
                wait_for_tcp_listening(host, port)
                url = f"http://[{host}]:{port}/health"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertEqual(
                        json.loads(resp.read().decode("utf-8")), {"status": "ok"}
                    )
            finally:
                server.shutdown()
                join_thread_or_fail(thread, label="broker server")


class DaemonEndpointFileTests(unittest.TestCase):
    def test_write_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            broker.write_daemon_endpoint(egress_root, "127.0.0.1", 12345)
            endpoint = broker.read_daemon_endpoint(egress_root)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.host, "127.0.0.1")
            self.assertEqual(endpoint.port, 12345)
            self.assertEqual(endpoint.pid, os.getpid())

    def test_remove_daemon_endpoint_tolerates_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker.remove_daemon_endpoint(Path(tmp))

    def test_read_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(broker.read_daemon_endpoint(Path(tmp)))

    def test_read_corrupt_json_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / broker.ENDPOINT_FILENAME
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(broker.read_daemon_endpoint(Path(tmp)))

    def test_read_wrong_shape_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / broker.ENDPOINT_FILENAME
            path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            self.assertIsNone(broker.read_daemon_endpoint(Path(tmp)))

    def test_read_dead_pid_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / broker.ENDPOINT_FILENAME
            payload = {"version": 1, "host": "127.0.0.1", "port": 1, "pid": 2**22}
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(broker.read_daemon_endpoint(Path(tmp)))


class ContainerBindAndManagedEndpointTests(unittest.TestCase):
    """--bind-any / --advertise: the container-mode surface. The docker
    service binds 0.0.0.0 inside the container (a loopback bind is
    unreachable from the published port) and records the published host
    address as a docker-managed daemon.json entry with no pid — liveness
    across a pid-namespace boundary is a /health probe, nothing else."""

    def test_bind_any_refused_without_container_marker(self):
        err = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "DJINN_CONTAINER"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr", err):
            rc = broker.main(["--bind-any"])
        self.assertEqual(rc, 1)
        self.assertIn("--bind-any refused", err.getvalue())
        self.assertIn("DJINN_CONTAINER", err.getvalue())

    def test_bind_any_binds_zero_zero_zero_zero_with_container_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            captured: dict[str, object] = {}

            def fake_serve_forever(self) -> None:
                captured["bind"] = self.server_address[0]

            env = {k: v for k, v in os.environ.items() if k != "DJINN_CONTAINER"}
            env["DJINN_CONTAINER"] = "1"
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                broker.EgressBrokerHTTPServer, "serve_forever", fake_serve_forever
            ):
                broker.run_daemon(base, host="0.0.0.0", port=0, repo_root=REPO_ROOT)
            self.assertEqual(captured.get("bind"), "0.0.0.0")

    def test_resolve_bind_flags(self):
        args = broker.build_parser().parse_args(["--bind-any"])
        env = {k: v for k, v in os.environ.items() if k != "DJINN_CONTAINER"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(broker.EgressBrokerHostError):
                broker._resolve_bind(args)
        env["DJINN_CONTAINER"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(broker._resolve_bind(args), ("0.0.0.0", None))
        plain = broker.build_parser().parse_args([])
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(broker._resolve_bind(plain), ("127.0.0.1", None))

    def test_advertise_writes_version2_managed_endpoint_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)

            captured: dict[str, object] = {}

            # read inside serve_forever: the finally in run_daemon removes the
            # file on shutdown, so it is gone again by the time we return.
            with mock.patch.object(
                broker.EgressBrokerHTTPServer,
                "serve_forever",
                lambda self: captured.update(
                    payload=json.loads(
                        (egress_root / broker.ENDPOINT_FILENAME).read_text()
                    )
                ),
            ):
                broker.run_daemon(
                    base,
                    host="127.0.0.1",
                    port=0,
                    repo_root=REPO_ROOT,
                    advertise=("127.0.0.1", 8816),
                )
            payload = captured["payload"]
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["host"], "127.0.0.1")
            self.assertEqual(payload["port"], 8816)
            self.assertEqual(payload["managed"], "docker")
            self.assertNotIn("pid", payload)
            self.assertIn("started_at", payload)

    def test_managed_endpoint_reads_live_when_health_answers(self):
        stub, thread = self._stub_health_server(200)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                egress_root = Path(tmp)
                broker.write_daemon_endpoint(
                    egress_root,
                    "127.0.0.1",
                    stub.server_address[1],
                    managed=broker.MANAGED_DOCKER,
                )
                endpoint = broker.read_daemon_endpoint(egress_root)
                self.assertIsNotNone(endpoint)
                assert endpoint is not None
                self.assertTrue(endpoint.managed)
                self.assertIsNone(endpoint.pid)
                self.assertEqual(endpoint.port, stub.server_address[1])
        finally:
            stub.shutdown()
            stub.server_close()
            join_thread_or_fail(thread, label="stub")

    def test_managed_endpoint_dead_when_nothing_listens(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            broker.write_daemon_endpoint(
                egress_root, "127.0.0.1", 8816, managed=broker.MANAGED_DOCKER
            )
            with self.assertLogs("egress_broker_host", level="INFO") as captured:
                self.assertIsNone(broker.read_daemon_endpoint(egress_root))
            self.assertTrue(
                any("managed unreachable" in line for line in captured.output)
            )

    def test_print_endpoint_reports_unclean_managed_stop_as_fallback(self):
        # A daemon.json left behind with nothing listening must read as "no
        # live daemon": the exit-3 fallback (the same code a dead version-1
        # pid produces), not a URL to a broker that is not there.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)
            broker.write_daemon_endpoint(
                egress_root, "127.0.0.1", 8816, managed=broker.MANAGED_DOCKER
            )
            out = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(broker.EGRESS_BROKER_URL_ENV, None)
                with mock.patch("sys.stdout", out):
                    rc = broker.main(["--print-endpoint", "--base-path", str(base)])
            self.assertEqual(rc, 3)
            self.assertEqual(out.getvalue().strip(), f"http://127.0.0.1:{broker.DEFAULT_PORT}")

    def _stub_health_server(self, status: int):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for_tcp_listening(*server.server_address)
        return server, thread


if __name__ == "__main__":
    unittest.main()
