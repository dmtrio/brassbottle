#!/usr/bin/env python3
"""Unit tests for append-only egress approval log and queue-state fold."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import egress_log  # noqa: E402

MARCH = datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
AUG_END = datetime(2026, 8, 31, 23, 0, 0, tzinfo=timezone.utc)
SEP_START = datetime(2026, 9, 1, 0, 5, 0, tzinfo=timezone.utc)


class EgressLogTests(unittest.TestCase):
    def _log(self, root: Path) -> egress_log.EgressLog:
        return egress_log.EgressLog(root)

    def test_queue_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-1", ts=MARCH)
            log.append("notified", "req-1", ts=MARCH)
            first = log.fold_queue(now=MARCH)

            restarted = self._log(root)
            second = restarted.fold_queue(now=MARCH)
            self.assertEqual(first.open_requests, second.open_requests)
            self.assertEqual(first.open_requests["req-1"].state, "notified")

    def test_open_request_survives_month_boundary_without_intervening_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-a", ts=AUG_END)

            state = log.fold_queue(now=SEP_START)
            self.assertIn("req-a", state.open_requests)
            self.assertEqual(state.open_requests["req-a"].state, "requested")

    def test_open_request_fields_survive_month_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append(
                "requested",
                "req-a",
                ts=AUG_END,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
            )

            state = log.fold_queue(now=SEP_START)
            req = state.open_requests["req-a"]
            self.assertEqual(req.state, "requested")
            self.assertEqual(req.container, "coding-brassbottle")
            self.assertEqual(req.host, "docs.stripe.com")
            self.assertEqual(req.port, 443)
            self.assertEqual(req.opened_at, "2026-08-31T23:00:00Z")

            log.append("notified", "req-a", ts=SEP_START)
            state = log.fold_queue(now=SEP_START)
            req = state.open_requests["req-a"]
            self.assertEqual(req.state, "notified")
            self.assertEqual(req.container, "coding-brassbottle")
            self.assertEqual(req.host, "docs.stripe.com")
            self.assertEqual(req.port, 443)
            self.assertEqual(req.opened_at, "2026-08-31T23:00:00Z")

            september_path = egress_log._log_path(
                root, egress_log._month_filename(SEP_START)
            )
            header = json.loads(
                september_path.read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(header["kind"], egress_log.CARRY_FORWARD_KIND)
            self.assertEqual(
                header["open"],
                [
                    {
                        "request_id": "req-a",
                        "state": "requested",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                        "opened_at": "2026-08-31T23:00:00Z",
                    }
                ],
            )

    def test_allowed_leaves_no_host_trace_in_fold_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            host = "secret.example.com"
            log.append(
                "requested",
                "req-allowed",
                ts=APRIL,
                container="coding-brassbottle",
                host=host,
                port=443,
            )
            log.append("allowed", "req-allowed", ts=APRIL, host=host, scope="live")
            state = log.fold_queue(now=APRIL)
            self.assertEqual(state.open_requests, {})
            payload = json.dumps(
                {
                    request_id: {
                        "state": req.state,
                        "container": req.container,
                        "host": req.host,
                        "port": req.port,
                        "opened_at": req.opened_at,
                    }
                    for request_id, req in state.open_requests.items()
                }
            )
            self.assertNotIn(host, payload)

    def test_fold_queue_on_empty_root_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            state = log.fold_queue(now=SEP_START)
            self.assertEqual(state.open_requests, {})

    def test_second_fold_in_new_month_stable_without_reading_prior_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-a", ts=AUG_END)

            first = log.fold_queue(now=SEP_START)
            self.assertIn("req-a", first.open_requests)

            august_path = egress_log._log_path(root, egress_log._month_filename(AUG_END))
            august_path.chmod(0o000)
            try:
                second = log.fold_queue(now=SEP_START)
            finally:
                august_path.chmod(0o644)

            self.assertEqual(first.open_requests, second.open_requests)

    def test_carry_forward_at_line_zero_accepted_mid_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-a", ts=APRIL)
            path = egress_log._log_path(root, egress_log._month_filename(APRIL))
            lines = path.read_text(encoding="utf-8").splitlines()
            mid_header = json.dumps(
                {
                    "ts": "2026-04-02T10:00:00Z",
                    "kind": egress_log.CARRY_FORWARD_KIND,
                    "open": [],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            lines.insert(2, mid_header)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(egress_log.EgressLogError):
                log.fold_queue(now=APRIL)

            # carry_forward at line 0 (only line) is valid on a fresh month file.
            sep_root = Path(tmp) / "sep"
            sep_log = self._log(sep_root)
            sep_log.append("requested", "req-b", ts=AUG_END)
            sep_state = sep_log.fold_queue(now=SEP_START)
            self.assertIn("req-b", sep_state.open_requests)

    def test_open_request_carried_via_carry_forward_without_reading_prior_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "cross-month", ts=MARCH)
            log.append("hit", "cross-month", ts=MARCH)
            log.append("notified", "cross-month", ts=APRIL)

            march_path = egress_log._log_path(root, egress_log._month_filename(MARCH))
            march_path.chmod(0o000)
            try:
                state = log.fold_queue(now=APRIL)
            finally:
                march_path.chmod(0o644)

            self.assertIn("cross-month", state.open_requests)
            self.assertEqual(state.open_requests["cross-month"].state, "notified")

            april_path = egress_log._log_path(root, egress_log._month_filename(APRIL))
            first_line = april_path.read_text(encoding="utf-8").splitlines()[0]
            header = json.loads(first_line)
            self.assertEqual(header["kind"], egress_log.CARRY_FORWARD_KIND)
            self.assertEqual(
                header["open"],
                [
                    {
                        "request_id": "cross-month",
                        "state": "hit",
                        "opened_at": "2026-03-31T12:00:00Z",
                    }
                ],
            )

    def test_cursor_pointing_at_previous_file_falls_back_to_full_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-a", ts=APRIL)
            current = egress_log._month_filename(APRIL)
            egress_log._cursor_path(root).write_text(
                json.dumps({"file": egress_log._month_filename(MARCH), "offset": 0})
                + "\n",
                encoding="utf-8",
            )

            state = log.fold_queue(now=APRIL)
            self.assertIn("req-a", state.open_requests)

    def test_missing_cursor_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-b", ts=APRIL)
            self.assertFalse(egress_log._cursor_path(root).exists())
            state = log.fold_queue(now=APRIL)
            self.assertIn("req-b", state.open_requests)

    def test_malformed_cursor_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-c", ts=APRIL)
            egress_log._cursor_path(root).write_text("not-json\n", encoding="utf-8")
            state = log.fold_queue(now=APRIL)
            self.assertIn("req-c", state.open_requests)

    def test_cursor_offset_past_eof_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-d", ts=APRIL)
            current = egress_log._month_filename(APRIL)
            path = egress_log._log_path(root, current)
            past_eof = path.stat().st_size + 1024
            egress_log._cursor_path(root).write_text(
                json.dumps({"file": current, "offset": past_eof}) + "\n",
                encoding="utf-8",
            )
            state = log.fold_queue(now=APRIL)
            self.assertIn("req-d", state.open_requests)

    def test_torn_trailing_line_discarded_without_losing_prior_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-e", ts=APRIL)
            log.append("notified", "req-e", ts=APRIL)
            path = egress_log._log_path(root, egress_log._month_filename(APRIL))
            with path.open("ab") as handle:
                handle.write(b'{"ts":"2026-04-02T10:00:00Z","kind":"hit","request')

            state = log.fold_queue(now=APRIL)
            self.assertEqual(state.open_requests["req-e"].state, "notified")

    def test_unparsable_middle_line_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-f", ts=APRIL)
            path = egress_log._log_path(root, egress_log._month_filename(APRIL))
            lines = path.read_text(encoding="utf-8").splitlines()
            lines.insert(1, "{not valid json")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(egress_log.EgressLogError):
                log.fold_queue(now=APRIL)

    def test_unknown_event_kind_rejected_on_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(Path(tmp))
            with self.assertRaises(egress_log.EgressLogError):
                log.append("expired", "req-g", ts=APRIL)

    def test_denied_stale_closes_request_and_expired_kind_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            log.append("requested", "req-h", ts=APRIL)
            log.append("denied", "req-h", ts=APRIL, reason="stale")
            state = log.fold_queue(now=APRIL)
            self.assertNotIn("req-h", state.open_requests)
            self.assertNotIn("expired", egress_log.EVENT_KINDS)

    def test_allowed_closes_request_without_retaining_host_in_queue_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            host = "secret.example.com"
            log.append("requested", "req-i", ts=APRIL, host=host)
            log.append("allowed", "req-i", ts=APRIL, host=host)
            state = log.fold_queue(now=APRIL)
            self.assertNotIn("req-i", state.open_requests)
            payload = json.dumps(state.open_requests)
            self.assertNotIn(host, payload)
            for req in state.open_requests.values():
                self.assertNotIn("host", req.__dict__)

    def test_security_invariant_no_allowlist_surface_on_public_api(self):
        forbidden = re.compile(r"(?i)(allow|approve|permit)")
        for module_name in ("egress_log",):
            module = sys.modules[module_name]
            for name in dir(module):
                if name.startswith("_"):
                    continue
                with self.subTest(name=name):
                    self.assertIsNone(
                        forbidden.search(name),
                        f"public name {name!r} suggests an egress allowlist API",
                    )

        for cls in (egress_log.EgressLog, egress_log.QueueState, egress_log.OpenRequest):
            for name, _member in cls.__dict__.items():
                if name.startswith("_"):
                    continue
                with self.subTest(cls=cls.__name__, name=name):
                    self.assertIsNone(
                        forbidden.search(name),
                        f"{cls.__name__}.{name} suggests an egress allowlist API",
                    )


if __name__ == "__main__":
    unittest.main()
