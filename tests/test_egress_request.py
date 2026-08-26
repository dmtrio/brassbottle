#!/usr/bin/env python3
"""Unit tests for src/egress_request.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import egress_request as er  # noqa: E402


class EgressRequestTests(unittest.TestCase):
    def test_parse_host_target_default_port(self):
        target = er.parse_host_target("docs.stripe.com")
        self.assertEqual(target.host, "docs.stripe.com")
        self.assertEqual(target.port, 443)
        self.assertFalse(target.host_is_ip)

    def test_parse_host_target_with_port(self):
        target = er.parse_host_target("neon.tech:5432")
        self.assertEqual(target.host, "neon.tech")
        self.assertEqual(target.port, 5432)

    def test_request_hosts_allowed(self):
        def fake_file(**_kwargs):
            return {"decision": "allow", "scope": "live"}, None

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            reason="test",
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            file_fn=fake_file,
        )
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertEqual(results[0].decision, "allowed")

    def test_request_hosts_pending(self):
        def fake_file(**_kwargs):
            return {"decision": "pending", "request_id": "abcd1234"}, None

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            file_fn=fake_file,
        )
        self.assertEqual(code, er.EXIT_PENDING)
        self.assertEqual(results[0].decision, "pending")

    def test_check_host_allowed_when_ipset_matches(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(er, "resolve_ipv4", return_value=["93.184.216.34"]):
            result = er.check_host("example.com", runner=runner)
        self.assertEqual(result.status, "allowed")

    def test_main_check_json(self):
        with mock.patch.object(
            er,
            "check_hosts",
            return_value=[er.HostCheckResult("example.com", 443, "blocked")],
        ):
            with mock.patch("sys.stdout") as stdout:
                code = er.main(["--check", "--json", "example.com"])
        self.assertEqual(code, er.EXIT_ALLOWED)
        stdout.write.assert_called()


if __name__ == "__main__":
    unittest.main()
