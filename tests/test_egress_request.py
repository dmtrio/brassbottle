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

    # ── reason option (PIN) ───────────────────────────────────────────────

    def _run_main(self, argv):
        """Drive main() through request_hosts/request_host/file_egress with only
        urllib's urlopen faked; return (exit code, stderr text, filed bodies)."""
        import io
        import json as jsonlib

        filed: list[dict] = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return jsonlib.dumps({"decision": "allow", "scope": "live"}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.full_url.endswith("/egress"))
            filed.append(jsonlib.loads(request.data))
            return _Resp()

        stderr = io.StringIO()
        env = {"EGRESS_BROKER_TOKEN": "tok", "CONTAINER_NAME": "bottle"}
        with mock.patch.dict("os.environ", env), mock.patch(
            "urllib.request.urlopen", fake_urlopen
        ), mock.patch("sys.stdout"), mock.patch("sys.stderr", stderr):
            code = er.main(argv)
        return code, stderr.getvalue(), filed

    def test_main_reason_option_reaches_filed_request_body(self):
        """PIN — `request-egress HOST... --reason TEXT` POSTs one body per host
        carrying reason == TEXT."""
        code, _, filed = self._run_main(
            ["docs.stripe.com", "neon.tech:5432", "--reason", "fetch API reference"]
        )
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertEqual(
            [(body["host"], body["port"], body["reason"]) for body in filed],
            [
                ("docs.stripe.com", 443, "fetch API reference"),
                ("neon.tech", 5432, "fetch API reference"),
            ],
        )

    def test_main_reason_short_option_and_leading_position(self):
        code, _, filed = self._run_main(["-r", "db:migrate", "neon.tech"])
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertEqual([body["reason"] for body in filed], ["db:migrate"])

    def test_main_without_reason_files_no_reason_key(self):
        code, _, filed = self._run_main(["docs.stripe.com"])
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertNotIn("reason", filed[0])

    def test_main_positional_reason_is_rejected_naming_it(self):
        """PIN — a positional that is not a valid host is an error naming it,
        and nothing is filed (pre-change it was treated as the reason)."""
        code, stderr, filed = self._run_main(["example.com", "fetch API reference"])
        self.assertEqual(code, er.EXIT_DENIED)
        self.assertIn("'fetch API reference'", stderr)
        self.assertEqual(filed, [])

    def test_validate_hosts_names_first_invalid_token(self):
        self.assertEqual(er.validate_hosts(["a.example.com", "b.io:22"]), ["a.example.com", "b.io:22"])
        with self.assertRaises(ValueError) as ctx:
            er.validate_hosts(["a.example.com", "why not", "also bad"])
        self.assertIn("'why not'", str(ctx.exception))
        self.assertNotIn("also bad", str(ctx.exception))

    # ── filing + polling ─────────────────────────────────────────────────

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

    def test_request_hosts_denylist_denied_surfaces_zone_and_scope(self):
        def fake_file(**_kwargs):
            return (
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "datadoghq.com",
                    "scope": "global",
                },
                None,
            )

        results, code = er.request_hosts(
            ["us5.datadoghq.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            file_fn=fake_file,
        )
        self.assertEqual(code, er.EXIT_DENIED)
        self.assertEqual(results[0].decision, "denied")
        self.assertEqual(results[0].detail, "denylist: zone=datadoghq.com scope=global")

    def test_request_hosts_plain_denied_has_no_denylist_detail(self):
        def fake_file(**_kwargs):
            return {"decision": "deny"}, None

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            file_fn=fake_file,
        )
        self.assertEqual(code, er.EXIT_DENIED)
        self.assertEqual(results[0].decision, "denied")
        self.assertEqual(results[0].detail, "")

    def test_request_hosts_polls_to_decision(self):
        filed: list[dict] = []

        def fake_file(**kwargs):
            filed.append(kwargs)
            return {
                "decision": "pending",
                "request_id": "abcd1234",
                "attempt": 0,
            }, None

        def fake_poll(**kwargs):
            self.assertEqual(kwargs["request_id"], "abcd1234")
            self.assertEqual(kwargs["baseline_attempt"], 0)
            return {"decision": "allow", "scope": "live"}

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            hold_seconds=5,
            file_fn=fake_file,
            poll_fn=fake_poll,
        )
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertEqual(results[0].decision, "allowed")
        self.assertEqual(filed[0]["reason"], None)

    def test_request_hosts_poll_reports_error_newer_than_baseline(self):
        def fake_file(**_kwargs):
            return {"decision": "pending", "request_id": "abcd1234", "attempt": 0}, None

        def fake_poll(**_kwargs):
            return {"decision": "error", "reason": "apply_failed"}

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            hold_seconds=5,
            file_fn=fake_file,
            poll_fn=fake_poll,
        )
        self.assertEqual(code, er.EXIT_DENIED)
        self.assertEqual(results[0].decision, "error")
        self.assertEqual(results[0].detail, "apply_failed")

    def test_request_hosts_pending_at_deadline(self):
        """Past the client's own deadline the pending result stands — the
        row stays open for a late allow."""
        def fake_file(**_kwargs):
            return {"decision": "pending", "request_id": "abcd1234", "attempt": 0}, None

        def fake_poll(**_kwargs):
            return None  # deadline passed with the row still open

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            hold_seconds=1,
            file_fn=fake_file,
            poll_fn=fake_poll,
        )
        self.assertEqual(code, er.EXIT_PENDING)
        self.assertEqual(results[0].decision, "pending")

    def test_request_hosts_ignores_pre_baseline_error(self):
        """A filing made after a failed apply has baseline == the recorded
        attempt; the old error is not this client's news."""
        def fake_file(**_kwargs):
            return {
                "decision": "pending",
                "request_id": "abcd1234",
                "attempt": 1,
                "last_error": {"reason": "apply_failed", "attempt": 1, "at": "x"},
            }, None

        def fake_poll(**_kwargs):
            # The row still carries last_error for attempt 1 == baseline;
            # the real poll_decision filters it, so the poll keeps waiting.
            return {"decision": "allow", "scope": "live"}

        results, code = er.request_hosts(
            ["docs.stripe.com"],
            container="demo",
            broker_url="http://127.0.0.1:8816",
            broker_token="tok",
            hold_seconds=5,
            file_fn=fake_file,
            poll_fn=fake_poll,
        )
        self.assertEqual(code, er.EXIT_ALLOWED)
        self.assertEqual(results[0].decision, "allowed")

    def test_poll_decision_error_filter_uses_baseline(self):
        """poll_decision: last_error with attempt <= baseline is ignored; a
        later attempt surfaces as an error body."""
        import json as jsonlib

        calls = {"n": 0}

        def fake_opener(request, timeout=None):
            self.assertEqual(request.method, "GET")
            self.assertTrue(request.full_url.endswith("/egress/abcd1234"))
            self.assertEqual(request.headers.get("Authorization"), "Bearer tok")
            calls["n"] += 1

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    body = {"status": "open", "attempt": 1}
                    if calls["n"] >= 3:
                        body["last_error"] = {
                            "reason": "apply_failed",
                            "attempt": 2,
                            "at": "x",
                        }
                    return jsonlib.dumps(body).encode("utf-8")

            return _Resp()

        base = 1000.0
        clock = {"now": base}

        def fake_sleep(seconds):
            clock["now"] += 1.0

        result = er.poll_decision(
            url="http://broker:8816",
            token="tok",
            request_id="abcd1234",
            baseline_attempt=1,
            deadline=base + 10.0,
            now_fn=lambda: clock["now"],
            sleep_fn=fake_sleep,
            opener=fake_opener,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "error")
        self.assertEqual(result["reason"], "apply_failed")
        self.assertGreater(calls["n"], 1)

    def test_poll_decision_returns_decision_body_when_decided(self):
        import json as jsonlib

        def fake_opener(request, timeout=None):
            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return jsonlib.dumps(
                        {
                            "status": "allowed",
                            "attempt": 0,
                            "decision_body": {"decision": "allow", "scope": "live"},
                        }
                    ).encode("utf-8")

            return _Resp()

        result = er.poll_decision(
            url="http://broker:8816",
            token="tok",
            request_id="abcd1234",
            baseline_attempt=0,
            deadline=1000.0 + 5.0,
            now_fn=lambda: 1000.0,
            sleep_fn=lambda _s: None,
            opener=fake_opener,
        )
        self.assertEqual(result, {"decision": "allow", "scope": "live"})

    def test_poll_decision_returns_none_at_deadline(self):
        def fake_opener(request, timeout=None):
            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"status": "open", "attempt": 0}'

            return _Resp()

        result = er.poll_decision(
            url="http://broker:8816",
            token="tok",
            request_id="abcd1234",
            baseline_attempt=0,
            deadline=1000.0,
            now_fn=lambda: 1000.0,
            sleep_fn=lambda _s: None,
            opener=fake_opener,
        )
        self.assertIsNone(result)

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
