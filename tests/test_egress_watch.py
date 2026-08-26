#!/usr/bin/env python3
"""Unit tests for the interactive egress approval watcher."""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
import egress_broker_host as broker  # noqa: E402
import egress_log  # noqa: E402
import egress_watch as watch  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_broker_open_request  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now


class EgressWatchTests(unittest.TestCase):
    def _broker(self, root: Path, hold_seconds: int = 60) -> broker.EgressBroker:
        return broker.EgressBroker(
            root,
            repo_root=REPO_ROOT,
            now_fn=FakeClock(NOW).now,
            hold_seconds_default=hold_seconds,
        )

    def _details(self, **overrides: object) -> watch.RequestDetails:
        base = {
            "request_id": "req-1",
            "container": "coding-brassbottle",
            "host": "docs.stripe.com",
            "port": 443,
            "hit_count": 1,
        }
        base.update(overrides)
        return watch.RequestDetails(**base)  # type: ignore[arg-type]

    def _file_request(self, b: broker.EgressBroker) -> str:
        thread = threading.Thread(
            target=b.file_request,
            args=("coding-brassbottle", "docs.stripe.com", 443),
        )
        thread.start()
        request_id = wait_for_broker_open_request(b)

        def cleanup() -> None:
            if thread.is_alive():
                with b._lock:
                    still_open = request_id in b._requests
                if still_open:
                    b.decide(request_id, "deny")
            join_thread_or_fail(thread, label="file_request")

        self.addCleanup(cleanup)
        return request_id

    def test_prompt_contains_host_and_subdomain_coverage(self):
        details = self._details()
        block = watch.format_request_block(details)
        self.assertIn("docs.stripe.com", block)
        self.assertIn("(and everything under it)", block)
        self.assertIn("[p] allow + persist to manifest", block)

    def test_ip_request_prompt_is_distinct_and_omits_persist(self):
        details = self._details(host="192.0.2.55", port=5432)
        block = watch.format_request_block(details)
        self.assertIn("192.0.2.55", block)
        self.assertIn("IP address", block)
        self.assertIn("ALLOWED_CIDRS", block)
        self.assertNotIn("[p] allow + persist", block)
        self.assertNotIn("(and everything under it)", block)

    def test_terminal_choices_map_to_decide_calls(self):
        cases = [
            ("a", "allow", "live"),
            ("p", "allow", "manifest"),
            ("d", "deny", None),
        ]
        for key, decision, scope in cases:
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    b = self._broker(root)
                    request_id = self._file_request(b)
                    choice = watch.parse_terminal_choice(key)
                    self.assertIsNotNone(choice)
                    with mock.patch("subprocess.run") as mocked:
                        mocked.return_value = mock.Mock(returncode=0)
                        watch.apply_operator_choice(b, request_id, choice)
                    with b._lock:
                        self.assertNotIn(request_id, b._requests)
                    if decision == "allow":
                        argv = mocked.call_args[0][0]
                        self.assertNotIn("firewall", argv)
                        self.assertEqual(argv[-1], "yml" if scope == "manifest" else "none")

    def test_skip_does_not_call_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            request_id = self._file_request(b)
            with mock.patch.object(b, "decide") as mocked_decide:
                watch.apply_operator_choice(
                    b,
                    request_id,
                    watch.OperatorChoice("skip"),
                )
                mocked_decide.assert_not_called()
            with b._lock:
                self.assertIn(request_id, b._requests)

    def test_osascript_argv_passes_hostname_as_argument_not_in_script(self):
        evil = 'evil" & do shell script "rm -rf /"'
        argv = watch.build_osascript_argv(
            title="title",
            message="message",
            hostname=evil,
        )
        self.assertIn(evil, argv)
        dash = argv.index("--")
        self.assertGreater(dash, 0)
        self.assertEqual(argv[dash + 1], evil)
        for arg in argv[1:dash]:
            if arg == "-e":
                continue
            self.assertNotIn(evil, arg)
            self.assertNotIn("evil", arg)

    def test_non_darwin_skips_dialog_and_terminal_still_works(self):
        out = io.StringIO()
        inputs = iter(["a"])

        def input_fn(_prompt: str) -> str:
            return next(inputs)

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=out,
            platform="linux",
        )
        choice = watcher._prompt_operator(self._details())
        self.assertEqual(choice.action, "allow_live")

    def test_watcher_run_once_skip_leaves_request_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            request_id = self._file_request(b)
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b,
                log,
                input_fn=lambda _p: "s",
                output=out,
                platform="linux",
            )
            self.assertTrue(watcher.run_once())
            with b._lock:
                self.assertIn(request_id, b._requests)

    def test_watcher_run_once_allow_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            request_id = self._file_request(b)
            watcher = watch.EgressWatcher(
                b,
                log,
                input_fn=lambda _p: "a",
                output=io.StringIO(),
                platform="linux",
            )
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=0)
                self.assertTrue(watcher.run_once())
                argv = mocked.call_args[0][0]
                self.assertNotIn("firewall", argv)
            with b._lock:
                self.assertNotIn(request_id, b._requests)

    def test_request_details_reads_uid_comm_reason_and_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-meta",
                ts=NOW,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
                uid=1000,
                comm="curl",
                reason="npm install",
            )
            log.append("hit", "req-meta", ts=NOW, count=2)
            details = watch.request_details_from_log(log, "req-meta", now=NOW)
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details.uid, 1000)
            self.assertEqual(details.comm, "curl")
            self.assertEqual(details.reason, "npm install")
            self.assertEqual(details.hit_count, 3)

    def test_dialog_allow_maps_to_live(self):
        proc = mock.Mock()
        poll_state = {"n": 0}

        def poll() -> int | None:
            poll_state["n"] += 1
            return 0 if poll_state["n"] > 2 else None

        proc.poll.side_effect = poll
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")

        def input_fn(_prompt: str) -> str:
            return ""

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
        )
        choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "allow_live")


if __name__ == "__main__":
    unittest.main()
