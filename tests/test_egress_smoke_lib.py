#!/usr/bin/env python3
"""Unit tests for tests/egress_smoke_lib.py (headless)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent / "src"))

import egress_smoke_lib as smoke  # noqa: E402
import egress_store as store  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class EgressSmokeLibTests(unittest.TestCase):
    def test_broker_host_env_set(self):
        self.assertTrue(smoke.broker_host_env_set("172.30.0.252"))
        self.assertFalse(smoke.broker_host_env_set(""))
        self.assertFalse(smoke.broker_host_env_set("   "))

    def test_container_name_for_bottle(self):
        self.assertEqual(smoke.container_name_for_bottle("coding-demo"), "djinn-coding-demo")
        self.assertEqual(
            smoke.container_name_for_bottle("djinn-coding-demo"),
            "djinn-coding-demo",
        )

    def _row(self, request_id, host, port, *, hit_count=1):
        return store.RequestRow(
            request_id=request_id,
            container="demo",
            host=host,
            port=port,
            host_is_ip=False,
            uid=None,
            comm=None,
            reason=None,
            hold_seconds=None,
            opened_at=NOW,
            last_hit_at=NOW,
            hit_count=hit_count,
            status="open",
            scope=None,
            decided_at=None,
            decided_by=None,
            deny_reason=None,
            apply_status=None,
            apply_attempts=0,
            last_error=None,
            persist_status=None,
            denylist_zone=None,
            denylist_scope=None,
            decision_body=None,
        )

    def test_find_open_request_filters_host_and_port(self):
        requests = {
            "a": self._row("a", "docs.stripe.com", 443),
            "b": self._row("b", "192.0.2.55", 5432),
        }
        found = smoke.find_open_request(requests, host="docs.stripe.com", port=443)
        self.assertIsNotNone(found)
        self.assertEqual(found.request_id, "a")
        self.assertIsNone(smoke.find_open_request(requests, host="docs.stripe.com", port=5432))

    def test_count_events_filters_kind_host_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            egress_root = base_path / "run" / "egress"
            egress_root.mkdir(parents=True)
            db = store.EgressStore(egress_root)
            db.open_or_hit(
                request_id="req-1",
                container="demo",
                host="docs.stripe.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.open_or_hit(
                request_id="req-1",
                container="demo",
                host="docs.stripe.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.open_or_hit(
                request_id="req-2",
                container="demo",
                host="www.example.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.shutdown()

            self.assertEqual(
                smoke.count_events(
                    base_path,
                    kind="requested",
                    host="docs.stripe.com",
                    port=443,
                ),
                1,
            )
            self.assertEqual(smoke.count_events(base_path, kind="hit"), 1)
            self.assertEqual(
                smoke.count_hits_for_request(base_path, "req-1"), 2
            )

    def test_count_events_by_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            egress_root = base_path / "run" / "egress"
            egress_root.mkdir(parents=True)
            db = store.EgressStore(egress_root)
            db.open_or_hit(
                request_id="req-1",
                container="demo",
                host="docs.stripe.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.mark_notified("req-1", NOW)
            db.close(
                request_id="req-1",
                status="denied",
                now=NOW,
                decided_by="cli",
                decision_body={"decision": "deny"},
            )
            db.shutdown()

            self.assertEqual(
                smoke.count_events(base_path, kind="denied", request_id="req-1"), 1
            )
            self.assertEqual(
                smoke.count_events(base_path, kind="requested", request_id="req-1"), 1
            )

    def test_fold_open_requests_reads_store_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            egress_root = base_path / "run" / "egress"
            egress_root.mkdir(parents=True)
            db = store.EgressStore(egress_root)
            db.open_or_hit(
                request_id="req-1",
                container="demo",
                host="docs.stripe.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.close(
                request_id="req-1",
                status="denied",
                now=NOW,
                decided_by="cli",
                decision_body={"decision": "deny"},
            )
            db.open_or_hit(
                request_id="req-2",
                container="demo",
                host="www.example.com",
                port=443,
                host_is_ip=False,
                uid=None,
                comm=None,
                reason=None,
                hold_seconds=None,
                now=NOW,
            )
            db.shutdown()

            open_rows = smoke.fold_open_requests(base_path)
            self.assertEqual(set(open_rows), {"req-2"})
            found = smoke.find_open_request(
                open_rows, host="www.example.com", port=443
            )
            self.assertEqual(found.request_id, "req-2")

    def test_queue_mount_violations_detects_run_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            run_root = base_path / "run"
            run_root.mkdir()
            mounts = json.dumps(
                [{"Source": str(run_root / "egress"), "Destination": "/mnt/egress"}]
            )
            with mock.patch.object(
                smoke,
                "list_running_bottle_containers",
                return_value=["djinn-demo"],
            ), mock.patch.object(
                smoke.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout=mounts),
            ):
                violations = smoke.queue_mount_violations(base_path)
            self.assertEqual(len(violations), 1)
            self.assertIn("djinn-demo", violations[0])

    def test_derive_kill_switch_ports(self):
        ok, derived, message = smoke.derive_kill_switch_ports(TESTS_DIR.parent)
        self.assertTrue(ok, message)
        self.assertEqual(derived.get("ENABLE_EGRESS_BROKER"), "false")
        self.assertEqual(derived.get("EGRESS_BROKER_HOST", ""), "")

    def test_format_summary_counts(self):
        summary = smoke.SmokeSummary()
        summary.pass_("one")
        summary.fail("two", "detail")
        summary.skip("three")
        text = smoke.format_summary(summary)
        self.assertIn("1 passed, 1 failed, 1 skipped", text)
        self.assertIn("FAILED", text)
        self.assertEqual(summary.exit_code(), 1)

    def test_main_skips_inside_container(self):
        with mock.patch.object(smoke, "is_inside_container", return_value=True):
            self.assertEqual(smoke.main([]), 0)

    def test_main_skips_off_mac(self):
        with mock.patch.object(smoke, "is_inside_container", return_value=False), mock.patch.object(
            smoke,
            "is_mac_host",
            return_value=False,
        ):
            self.assertEqual(smoke.main([]), 0)


if __name__ == "__main__":
    unittest.main()
