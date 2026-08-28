#!/usr/bin/env python3
"""Unit tests for the interactive egress approval watcher."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_osascript_argv_passes_untrusted_fields_as_arguments_not_in_script(self):
        evil = 'evil" & do shell script "rm -rf /"'
        details = self._details(host=evil)
        _, message = watch.build_dialog_message(details)
        argv = watch.build_osascript_argv(title=evil, message=message)
        self.assertIn(evil, argv)
        self.assertIn(message, argv)
        dash = argv.index("--")
        self.assertGreater(dash, 0)
        self.assertEqual(argv[dash + 1], message)
        self.assertEqual(argv[dash + 2], evil)
        for arg in argv[1:dash]:
            if arg == "-e":
                continue
            self.assertNotIn(evil, arg)
            self.assertNotIn("evil", arg)

    def test_dialog_script_activates_self_and_defaults_to_deny(self):
        argv = watch.build_osascript_argv(
            title="title",
            message="message",
        )
        script_lines = [arg for i, arg in enumerate(argv) if i > 0 and argv[i - 1] == "-e"]
        activate_idx = script_lines.index("activate")
        dialog_idx = next(
            i for i, line in enumerate(script_lines) if "display dialog" in line
        )
        self.assertLess(activate_idx, dialog_idx)
        self.assertIn('default button "Deny"', script_lines[dialog_idx])
        self.assertIn("with icon caution", script_lines[dialog_idx])
        for line in script_lines:
            self.assertNotIn("System Events", line)

    def test_notification_argv_passes_fields_as_arguments_not_in_script(self):
        evil_host = 'evil" & do shell script "rm -rf /"'
        evil_msg = 'msg" & do shell script "touch /tmp/pwned"'
        argv = watch.build_notification_argv(
            title="title",
            message=evil_msg,
        )
        self.assertIn(evil_msg, argv)
        dash = argv.index("--")
        self.assertGreater(dash, 0)
        self.assertEqual(argv[dash + 1], evil_msg)
        self.assertEqual(argv[dash + 2], "title")
        for arg in argv[1:dash]:
            if arg == "-e":
                continue
            self.assertNotIn(evil_host, arg)
            self.assertNotIn(evil_msg, arg)
            self.assertNotIn("evil", arg)
            self.assertNotIn("pwned", arg)
        script_lines = [arg for i, arg in enumerate(argv) if i > 0 and argv[i - 1] == "-e"]
        self.assertTrue(any("display notification" in line for line in script_lines))

    def test_notification_message_names_container_host_port(self):
        details = self._details()
        title, message = watch.build_notification_message(details)
        self.assertEqual(title, "Egress approval")
        self.assertEqual(
            message,
            "coding-brassbottle wants docs.stripe.com:443",
        )
        details_with_reason = self._details(reason="npm install")
        _, message_with_reason = watch.build_notification_message(details_with_reason)
        self.assertEqual(
            message_with_reason,
            "coding-brassbottle wants docs.stripe.com:443 — npm install",
        )

    def test_dialog_prompt_dispatches_notification_and_dialog(self):
        notified = threading.Event()
        notify_argvs: list[list[str]] = []
        popen_calls: list[list[str]] = []
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        terminal_gate = threading.Event()

        def notify_runner(argv: list[str]) -> None:
            notify_argvs.append(argv)
            notified.set()

        def popen_factory(*args, **kwargs) -> mock.Mock:
            popen_calls.append(list(args[0]))
            return proc

        def input_fn(_prompt: str) -> str:
            terminal_gate.wait()
            return "a"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=popen_factory,
            notify_runner=notify_runner,
        )
        details = self._details()
        choice = watcher._prompt_with_dialog(details)
        self.assertEqual(choice.action, "allow_live")
        self.assertTrue(notified.wait(timeout=5), "notification never dispatched")
        self.assertEqual(len(notify_argvs), 1)
        dash = notify_argvs[0].index("--")
        self.assertEqual(notify_argvs[0][dash + 1], watch.build_notification_message(details)[1])
        self.assertEqual(len(popen_calls), 1)
        self.assertTrue(any("display dialog" in a for a in popen_calls[0]))

    def test_stalled_notification_does_not_delay_dialog(self):
        """Notification Center hanging must not hold the dialog back (review P2)."""
        release = threading.Event()
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        terminal_gate = threading.Event()

        def notify_runner(_argv: list[str]) -> None:
            release.wait(timeout=10)

        def input_fn(_prompt: str) -> str:
            terminal_gate.wait()
            return "a"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=notify_runner,
        )
        start = time.monotonic()
        try:
            choice = watcher._prompt_with_dialog(self._details())
        finally:
            release.set()
        self.assertEqual(choice.action, "allow_live")
        self.assertLess(time.monotonic() - start, 5.0, "dialog waited on the notification")

    def _orphan_guard_watcher(self, *, stall: str):
        """Build a watcher whose dialog thread is held at `stall` ("before_spawn"
        or "after_spawn") until the main thread has resolved the prompt and made
        its terminate pass (observed via the patched ``_terminate_process``).
        Returns (run, proc, popen_calls); run() executes the prompt.
        """
        resolved_pass = threading.Event()
        popen_calls: list[list[str]] = []
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        real_terminate = watch._terminate_process
        real_message = watch.build_dialog_message

        def recording_terminate(p) -> None:
            resolved_pass.set()
            real_terminate(p)

        def stalled_message(details):
            if stall == "before_spawn":
                resolved_pass.wait(timeout=5)
            return real_message(details)

        def popen_factory(*args, **kwargs) -> mock.Mock:
            if stall == "after_spawn":
                resolved_pass.wait(timeout=5)
            popen_calls.append(list(args[0]))
            return proc

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=lambda _p: "d",
            output=io.StringIO(),
            platform="darwin",
            popen_factory=popen_factory,
            notify_runner=lambda _argv: None,
        )

        def run():
            with mock.patch.object(watch, "_terminate_process", recording_terminate), \
                    mock.patch.object(watch, "build_dialog_message", stalled_message):
                choice = watcher._prompt_with_dialog(self._details())
            self.assertTrue(resolved_pass.is_set())
            return choice

        return run, proc, popen_calls

    def test_dialog_not_spawned_when_terminal_answers_first(self):
        """Terminal answers before the dialog thread reaches its spawn: no dialog."""
        run, proc, popen_calls = self._orphan_guard_watcher(stall="before_spawn")
        choice = run()
        self.assertEqual(choice.action, "deny")
        self.assertEqual(popen_calls, [], "dialog spawned after the prompt was resolved")
        proc.terminate.assert_not_called()

    def test_dialog_terminated_when_terminal_answers_during_spawn(self):
        """Terminal answers while Popen is in flight: the dialog is torn down."""
        run, proc, popen_calls = self._orphan_guard_watcher(stall="after_spawn")
        choice = run()
        self.assertEqual(choice.action, "deny")
        self.assertEqual(len(popen_calls), 1)
        proc.terminate.assert_called()

    def test_notification_failure_does_not_block_dialog(self):
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        terminal_gate = threading.Event()

        def notify_runner(_argv: list[str]) -> None:
            raise OSError("notification unavailable")

        def input_fn(_prompt: str) -> str:
            terminal_gate.wait()
            return "a"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=notify_runner,
        )
        choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "allow_live")

    def test_run_notification_timeout_is_logged_as_timeout(self):
        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            platform="darwin",
        )
        argv = ["osascript", "-e", "display notification"]

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(argv, 5)

        with self.assertLogs(watch.LOG, level="INFO") as captured:
            watcher._run_notification(argv, request_id="r1", run_fn=fake_run)
        warnings = [r for r in captured.records if r.levelno == logging.WARNING]
        infos = [r for r in captured.records if r.levelno == logging.INFO]
        self.assertEqual(len(warnings), 1)
        self.assertIn("notification failed", warnings[0].getMessage())
        done = [r for r in infos if "notification done" in r.getMessage()]
        self.assertEqual(len(done), 1)
        self.assertIn("status=timeout", done[0].getMessage())

    def test_run_notification_oserror_is_logged_as_error(self):
        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            platform="darwin",
        )
        argv = ["osascript", "-e", "display notification"]

        def fake_run(*args, **kwargs):
            raise OSError("boom")

        with self.assertLogs(watch.LOG, level="INFO") as captured:
            watcher._run_notification(argv, request_id="r1", run_fn=fake_run)
        warnings = [r for r in captured.records if r.levelno == logging.WARNING]
        infos = [r for r in captured.records if r.levelno == logging.INFO]
        self.assertEqual(len(warnings), 1)
        self.assertIn("notification failed", warnings[0].getMessage())
        done = [r for r in infos if "notification done" in r.getMessage()]
        self.assertEqual(len(done), 1)
        self.assertIn("status=error", done[0].getMessage())

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

    def test_watcher_run_once_apply_failure_surfaces_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            request_id = self._file_request(b)
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b,
                log,
                input_fn=lambda _p: "a",
                output=out,
                platform="linux",
            )
            with mock.patch("subprocess.run") as mocked:
                mocked.return_value = mock.Mock(returncode=1)
                self.assertTrue(watcher.run_once())
            self.assertIn("NOT applied", out.getvalue())
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
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        gate = threading.Event()

        def input_fn(_prompt: str) -> str:
            gate.wait()
            return "a"

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

    def test_dialog_allow_without_terminal_input(self):
        """Dialog Allow must win with no tmux keypress (the original bug)."""
        gate = threading.Event()
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0

        def input_fn(_prompt: str) -> str:
            gate.wait()
            return "a"

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
        self.assertFalse(gate.is_set())

    def test_terminal_wins_when_it_answers_first(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=lambda _p: "d",
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=lambda _argv: None,
        )
        choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "deny")
        proc.terminate.assert_called_once()

    def test_prompt_threads_join_after_resolution(self):
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        threads: list[threading.Thread] = []
        real_thread = threading.Thread

        def track_thread(*args, **kwargs) -> threading.Thread:
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=lambda _p: "s",
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
        )
        with mock.patch.object(watch.threading, "Thread", track_thread):
            watcher._prompt_with_dialog(self._details())
        for thread in threads:
            join_thread_or_fail(thread, label="prompt worker")

    def test_dialog_nonzero_exit_with_empty_output_is_logged(self):
        dialog_done = threading.Event()
        proc = mock.Mock()
        proc.stdout = io.StringIO("")
        proc.stderr = io.StringIO(
            "execution error: Not authorized to send Apple events to System Events. (-1743)\n"
        )
        proc.wait.return_value = 1
        proc.returncode = 1
        real_warning = watch.LOG.warning

        def warning_hook(msg, *args, **kwargs):
            formatted = msg % args if args else msg
            if "gave no answer" in formatted:
                dialog_done.set()
            return real_warning(msg, *args, **kwargs)

        def popen_factory(*args, **kwargs) -> mock.Mock:
            return proc

        def input_fn(_prompt: str) -> str:
            dialog_done.wait(timeout=5)
            return "s"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=popen_factory,
            notify_runner=lambda _argv: None,
        )

        with mock.patch.object(watch.LOG, "warning", warning_hook):
            with self.assertLogs(watch.LOG, level="WARNING") as captured:
                choice = watcher._prompt_with_dialog(self._details())
        warnings = [r for r in captured.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertIn("gave no answer", warnings[0].getMessage())
        self.assertIn("-1743", warnings[0].getMessage())
        self.assertEqual(choice.action, "skip")

    def test_dialog_user_cancel_is_logged_at_info(self):
        dialog_done = threading.Event()
        proc = mock.Mock()
        proc.stdout = io.StringIO("")
        proc.stderr = io.StringIO("execution error: User canceled. (-128)\n")
        proc.wait.return_value = 1
        proc.returncode = 1
        real_info = watch.LOG.info

        def info_hook(msg, *args, **kwargs):
            formatted = msg % args if args else msg
            if "dialog dismissed" in formatted:
                dialog_done.set()
            return real_info(msg, *args, **kwargs)

        def popen_factory(*args, **kwargs) -> mock.Mock:
            return proc

        def input_fn(_prompt: str) -> str:
            dialog_done.wait(timeout=5)
            return "s"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=popen_factory,
            notify_runner=lambda _argv: None,
        )

        with mock.patch.object(watch.LOG, "info", info_hook):
            with self.assertLogs(watch.LOG, level="INFO") as captured:
                choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "skip")
        dismissed = [
            r for r in captured.records
            if r.levelno == logging.INFO and "dialog dismissed" in r.getMessage()
        ]
        self.assertEqual(len(dismissed), 1)
        warnings = [r for r in captured.records if r.levelno == logging.WARNING]
        self.assertEqual(warnings, [])

    def test_notification_skipped_when_already_resolved(self):
        resolved_pass = threading.Event()
        notify_argvs: list[list[str]] = []
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        real_terminate = watch._terminate_process
        real_build_notification_message = watch.build_notification_message

        def recording_terminate(p) -> None:
            resolved_pass.set()
            real_terminate(p)

        def stalled_notification_message(details):
            resolved_pass.wait(timeout=5)
            return real_build_notification_message(details)

        def notify_runner(argv: list[str]) -> None:
            notify_argvs.append(argv)

        def input_fn(_prompt: str) -> str:
            return "d"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=notify_runner,
        )

        with mock.patch.object(watch, "_terminate_process", recording_terminate), \
                mock.patch.object(
                    watch,
                    "build_notification_message",
                    stalled_notification_message,
                ), \
                self.assertLogs(watch.LOG, level="INFO") as captured:
            choice = watcher._prompt_with_dialog(self._details())
            # The skip is logged on the un-joined notification thread; wait
            # for it (bounded) rather than sleeping a fixed interval.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                "notification skipped" in r.getMessage() for r in captured.records
            ):
                time.sleep(0.01)
        self.assertEqual(choice.action, "deny")
        self.assertEqual(notify_argvs, [])
        skipped = [
            r for r in captured.records
            if "notification skipped" in r.getMessage()
        ]
        self.assertEqual(len(skipped), 1)

    def test_terminal_thread_exits_quietly_on_eof(self):
        proc = mock.Mock()
        proc.stdout = io.StringIO("Allow\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0

        def input_fn(_prompt: str) -> str:
            raise EOFError

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=lambda _argv: None,
        )
        with self.assertLogs(watch.LOG, level="INFO") as captured:
            choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "allow_live")
        closed = [
            r for r in captured.records
            if "terminal input closed" in r.getMessage()
        ]
        self.assertEqual(len(closed), 1)


class WatcherStartupSurfaceTests(unittest.TestCase):
    """Pin the operator-visible surface of ./djinn allow --watch.

    These exist because the watcher shipped setting the root logger to INFO
    while polling the queue about twice a second, which buried the approval
    prompts under roughly ten lines a second of boundary logging. Nothing in
    the suite covered main() or run_watch(), so the regression was invisible
    until someone ran it. Both the level selection and the banner are pinned
    here so the usability fix cannot quietly come undone.
    """

    def setUp(self) -> None:
        root = logging.getLogger()
        self._prev_level = root.level
        self._prev_handlers = root.handlers[:]
        self.addCleanup(self._restore_logging)

    def _restore_logging(self) -> None:
        root = logging.getLogger()
        root.handlers[:] = self._prev_handlers
        root.setLevel(self._prev_level)

    def _main_level(self, argv: list[str]) -> int:
        """Run main() with run_watch stubbed out; return the root logger level."""
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.NOTSET)
        with mock.patch.object(watch, "run_watch") as run:
            rc = watch.main(argv)
        self.assertEqual(rc, 0)
        run.assert_called_once()
        return logging.getLogger().level

    def test_default_is_quiet(self):
        # WARNING, not INFO: the prompts are the signal, the boundary logs are
        # noise at two polls a second.
        self.assertEqual(self._main_level([]), logging.WARNING)

    def test_single_v_gives_boundary_logs(self):
        self.assertEqual(self._main_level(["-v"]), logging.INFO)

    def test_double_v_gives_reads_too(self):
        self.assertEqual(self._main_level(["-vv"]), logging.DEBUG)

    def test_run_watch_prints_a_startup_banner(self):
        """A silent watcher reads as hung — it must say it is listening."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            buf = io.StringIO()
            # run_forever() would block; the banner is printed before it.
            with mock.patch.object(watch.EgressWatcher, "run_forever",
                                   return_value=None), \
                 mock.patch.object(sys, "stdout", buf):
                watch.run_watch(base, host="127.0.0.1", port=0)
            out = buf.getvalue()

        self.assertIn("Watching for egress requests on 127.0.0.1:", out)
        self.assertIn("queue:", out)
        self.assertIn(str(watch.resolve_egress_root(base)), out)
        for key in ("[a]", "[p]", "[d]", "[s]"):
            self.assertIn(key, out, f"key legend must mention {key}")

    def test_run_watch_creates_the_run_directory(self):
        """RUN_PATH is a side-effect-free derivation; the daemon creates it.

        This asserts the behaviour, not a particular line: the egress root and
        tokens/ mkdir calls are redundant with each other (tokens_dir is created
        with parents=True), so removing either one alone still passes. Removing
        both fails, which is the property worth having.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "fresh"
            self.assertFalse(base.exists())
            with mock.patch.object(watch.EgressWatcher, "run_forever",
                                   return_value=None), \
                 mock.patch.object(sys, "stdout", io.StringIO()):
                watch.run_watch(base, host="127.0.0.1", port=0)
            egress_root = watch.resolve_egress_root(base)
            self.assertTrue(egress_root.is_dir())
            self.assertTrue((egress_root / watch.TOKENS_DIRNAME).is_dir())


if __name__ == "__main__":
    unittest.main()
