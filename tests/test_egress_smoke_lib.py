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

import egress_log as el  # noqa: E402
import egress_smoke_lib as smoke  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class EgressSmokeLibTests(unittest.TestCase):
    def test_host_mcp_ports_include_broker(self):
        self.assertTrue(smoke.host_mcp_ports_include_broker("8811,8816"))
        self.assertTrue(smoke.host_mcp_ports_include_broker("8816"))
        self.assertFalse(smoke.host_mcp_ports_include_broker("8811"))
        self.assertFalse(smoke.host_mcp_ports_include_broker(""))

    def test_container_name_for_bottle(self):
        self.assertEqual(smoke.container_name_for_bottle("coding-demo"), "djinn-coding-demo")
        self.assertEqual(
            smoke.container_name_for_bottle("djinn-coding-demo"),
            "djinn-coding-demo",
        )

    def test_find_open_request_filters_host_and_port(self):
        requests = {
            "a": el.OpenRequest("a", "requested", host="docs.stripe.com", port=443),
            "b": el.OpenRequest("b", "requested", host="192.0.2.55", port=5432),
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
            log = el.EgressLog(egress_root)
            log.append(
                "requested",
                "req-1",
                ts=NOW,
                host="docs.stripe.com",
                port=443,
                container="demo",
            )
            log.append("hit", "req-1", ts=NOW, count=4)
            log.append(
                "requested",
                "req-2",
                ts=NOW,
                host="www.example.com",
                port=443,
                container="demo",
            )
            self.assertEqual(
                smoke.count_events(
                    base_path,
                    kind="requested",
                    host="docs.stripe.com",
                    port=443,
                    when=NOW,
                ),
                1,
            )
            self.assertEqual(
                smoke.count_events(base_path, kind="hit", when=NOW), 1
            )

    def test_count_events_reads_the_month_it_is_asked_for(self):
        # Regression. NOW is a fixed date, so the default (real now) reads a
        # DIFFERENT month file than the fixture wrote and silently returns 0 —
        # a pass that expires. This first failed when UTC rolled into
        # September 2026, having passed every day of August.
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp)
            (base_path / "run" / "egress").mkdir(parents=True)
            el.EgressLog(base_path / "run" / "egress").append(
                "requested",
                "req-1",
                ts=NOW,
                host="docs.stripe.com",
                port=443,
                container="demo",
            )
            self.assertEqual(
                smoke.count_events(base_path, kind="requested", when=NOW), 1
            )
            other_month = NOW.replace(month=NOW.month - 1)
            self.assertEqual(
                smoke.count_events(base_path, kind="requested", when=other_month), 0
            )

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
        self.assertFalse(smoke.host_mcp_ports_include_broker(derived.get("HOST_MCP_PORTS", "")))

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
