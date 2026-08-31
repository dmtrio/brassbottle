#!/usr/bin/env python3
"""Unit tests for the interactive egress approval watcher."""

from __future__ import annotations

import io
import logging
import os
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

    def _touch_token(self, root: Path, bottle: str) -> None:
        """Seed tokens/<bottle>.token so persist_deny's bottle-existence
        check (validate_bottle_scope) passes — real watcher requests always
        have this already (the bottle had to authenticate to file the
        request in the first place); tests that go through the real
        persist_deny with scope=bottle must set it up by hand."""
        tokens_dir = root / broker.TOKENS_DIRNAME
        tokens_dir.mkdir(parents=True, exist_ok=True)
        (tokens_dir / f"{bottle}.token").write_text("tok\n", encoding="utf-8")

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

    def _file_request(
        self,
        b: broker.EgressBroker,
        container: str = "coding-brassbottle",
        *,
        already: set[str] | None = None,
    ) -> str:
        """File one request and return its id.

        `already` names the ids this test has filed before: the sync helper
        returns an ARBITRARY open id, so without it a second call can hand back
        the first request's id — and cleanup then denies the wrong one, leaving
        the second file_request thread blocked until its hold expires.
        """
        already = already or set()
        thread = threading.Thread(
            target=b.file_request,
            args=(container, "docs.stripe.com", 443),
        )
        thread.start()
        wait_for_broker_open_request(b, count=len(already) + 1)
        with b._lock:
            request_id = next(iter(set(b._requests) - already))

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
                        watch.apply_operator_choice(b, request_id, choice, self._details())
                    with b._lock:
                        self.assertNotIn(request_id, b._requests)
                    if decision == "allow":
                        argv = mocked.call_args[0][0]
                        self.assertNotIn("firewall", argv)
                        self.assertEqual(argv[-1], "yml" if scope == "manifest" else "none")

    def test_new_choices_parse_deny_bottle_and_deny_global(self):
        self.assertEqual(watch.parse_terminal_choice("D"), watch.OperatorChoice("deny_bottle"))
        self.assertEqual(watch.parse_terminal_choice("G"), watch.OperatorChoice("deny_global"))
        # Case matters: lowercase 'd' stays the narrow one-shot deny, and
        # lowercase 'g' is unmapped (re-prompt) since only uppercase G means
        # "deny always, all bottles" — a deliberately harder gesture.
        self.assertEqual(watch.parse_terminal_choice("d"), watch.OperatorChoice("deny"))
        self.assertIsNone(watch.parse_terminal_choice("g"))
        # a/p/s remain case-insensitive.
        self.assertEqual(watch.parse_terminal_choice("A"), watch.OperatorChoice("allow_live"))
        self.assertEqual(watch.parse_terminal_choice("P"), watch.OperatorChoice("allow_manifest"))
        self.assertEqual(watch.parse_terminal_choice("S"), watch.OperatorChoice("skip"))

    def test_apply_operator_choice_deny_bottle_and_deny_global_call_persist_deny(self):
        """finding #5: D/G route through EgressBroker.persist_deny (zone =
        details.host), not a direct decide(request_id, scope=...) call —
        that is what makes the sweep-every-covered-request behaviour work.

        finding #7: details.reason is the requesting AGENT's justification
        for wanting the host, not the operator's — persist_deny must always
        be called with reason=None from the watcher, never details.reason."""
        cases = [
            (watch.OperatorChoice("deny_bottle"), "bottle"),
            (watch.OperatorChoice("deny_global"), "global"),
        ]
        for choice, expected_scope in cases:
            with self.subTest(action=choice.action):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    b = self._broker(root)
                    request_id = self._file_request(b)
                    details = self._details(reason="npm install")
                    fake_result = broker.PersistDenyResult(
                        decided=[request_id], entry=mock.Mock(), error=None
                    )
                    with mock.patch.object(b, "persist_deny") as mocked_persist:
                        mocked_persist.return_value = fake_result
                        error, entry = watch.apply_operator_choice(
                            b, request_id, choice, details
                        )
                    self.assertIsNone(error)
                    self.assertIsNotNone(entry)
                    mocked_persist.assert_called_once_with(
                        details.host,
                        expected_scope,
                        container=details.container,
                        reason=None,
                        trigger_request_id=request_id,
                    )
                    # persist_deny (mocked) never actually closed the held
                    # request in this test — decide it directly to avoid
                    # leaking a thread past the test.
                    b.decide(request_id, "deny")

    def test_deny_bottle_writes_denylist_entry_scoped_to_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            b = self._broker(root)
            request_id = self._file_request(b)
            watch.apply_operator_choice(
                b, request_id, watch.OperatorChoice("deny_bottle"), self._details()
            )
            entry = b._denylist.matches("coding-brassbottle", "docs.stripe.com")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.scope, "coding-brassbottle")
            # persist_deny sweeps the triggering request itself too.
            with b._lock:
                self.assertNotIn(request_id, b._requests)

    def test_deny_global_writes_global_denylist_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            request_id = self._file_request(b)
            watch.apply_operator_choice(
                b, request_id, watch.OperatorChoice("deny_global"), self._details()
            )
            entry = b._denylist.matches("any-other-bottle", "docs.stripe.com")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.scope, "global")
            with b._lock:
                self.assertNotIn(request_id, b._requests)

    def test_deny_global_sweeps_other_containers_open_requests_too(self):
        """finding #5: a G on one bottle's request must also close a
        DIFFERENT bottle's already-open request for the same host — not
        just leave it to time out."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            request_id = self._file_request(b)  # coding-brassbottle

            other_result: dict[str, object] = {}

            def other_waiter() -> None:
                body, _ = b.file_request("other-bottle", "docs.stripe.com", 443)
                other_result["body"] = body

            other_thread = threading.Thread(target=other_waiter)
            other_thread.start()
            wait_for_broker_open_request(b, count=2)

            watch.apply_operator_choice(
                b, request_id, watch.OperatorChoice("deny_global"), self._details()
            )
            join_thread_or_fail(other_thread, label="other bottle file_request")
            self.assertEqual(
                other_result["body"],
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "docs.stripe.com",
                    "scope": "global",
                },
            )

    def test_deny_bottle_persist_failure_surfaces_via_format_apply_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            b = self._broker(root)
            request_id = self._file_request(b)
            with mock.patch.object(b._denylist, "add", side_effect=OSError("disk full")):
                error, entry = watch.apply_operator_choice(
                    b, request_id, watch.OperatorChoice("deny_bottle"), self._details()
                )
            self.assertEqual(error, watch.DENYLIST_PERSIST_FAILED_REASON)
            self.assertIsNone(entry)
            self.assertIn(
                "deny-list entry NOT written", watch.format_apply_failure(error)
            )
            # The persist failure must still close the TRIGGERING request as
            # a one-shot deny (persist_deny's trigger_request_id path) —
            # "Denied once; deny-list entry NOT written" must be true, not
            # aspirational: the request is gone from the open queue and the
            # held waiter already got a deny body (asserted via fold_queue,
            # not just b._requests, since that's what the watcher itself
            # polls to decide whether to prompt again).
            with b._lock:
                self.assertNotIn(request_id, b._requests)
            log = egress_log.EgressLog(root)
            open_ids = log.fold_queue(now=b.now()).open_requests
            self.assertNotIn(request_id, open_ids)

    def test_run_once_prints_denylist_ack_after_deny_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._touch_token(root, "coding-brassbottle")
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            request_id = self._file_request(b)
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b, log, input_fn=lambda _p: "D", output=out, platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertIn("Deny-list entry written: zone=docs.stripe.com", out.getvalue())
            self.assertIn(
                "undo with: ./djinn undeny docs.stripe.com --bottle coding-brassbottle",
                out.getvalue(),
            )
            with b._lock:
                self.assertNotIn(request_id, b._requests)

    def test_run_once_prints_nothing_extra_after_plain_deny(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            self._file_request(b)
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b, log, input_fn=lambda _p: "d", output=out, platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertNotIn("Deny-list entry written", out.getvalue())

    def test_format_denylist_status_reports_entry_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            d = watch.DenyList(path)
            self.assertEqual(watch.format_denylist_status(d), "Deny list: 0 entries")
            d.add(zone="example.com", scope="global")
            d.add(zone="example.net", scope="global")
            self.assertEqual(watch.format_denylist_status(d), "Deny list: 2 entries")

    def test_format_denylist_status_reports_corrupt_file(self):
        """finding #8b: a corrupt denylist.json must not fail silently —
        the status line says so, and says entries are not applied."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text("{BROKEN", encoding="utf-8")
            d = watch.DenyList(path)
            status = watch.format_denylist_status(d)
            self.assertIn("CORRUPT", status)
            self.assertIn("entries NOT applied", status)
            self.assertIn("prompts will resume", status)

    def test_run_once_prompt_block_includes_denylist_status(self):
        """finding #8b: each prompt block shows the deny-list status, not
        just the startup banner — the operator needs it visible right where
        they're deciding, not only once at launch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            b._denylist.add(zone="example.com", scope="global")
            self._file_request(b)
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b, log, input_fn=lambda _p: "d", output=out, platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertIn("Deny list: 1 entries", out.getvalue())

    def test_run_once_deny_bottle_on_container_named_global_leaves_request_open(self):
        """finding #4: a request whose container is literally "global"
        hitting the watcher's D key must not crash the watcher —
        persist_deny's own validate_bottle_scope raises before writing
        anything; run_once catches it, prints the error, and the request
        stays open (nothing was decided) rather than the exception
        propagating out of run_once."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            b = self._broker(root)
            request_id = self._file_request(b, container="global")
            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b, log, input_fn=lambda _p: "D", output=out, platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertIn("Error:", out.getvalue())
            self.assertIn("reserved", out.getvalue())
            with b._lock:
                self.assertIn(request_id, b._requests)
            open_ids = log.fold_queue(now=b.now()).open_requests
            self.assertIn(request_id, open_ids)

    def test_dialog_output_deny_always_maps_to_deny_bottle(self):
        self.assertEqual(
            watch._dialog_choice_from_output("Deny always"),
            watch.OperatorChoice("deny_bottle"),
        )
        self.assertEqual(
            watch._dialog_choice_from_output("Deny always\n"),
            watch.OperatorChoice("deny_bottle"),
        )

    def test_dialog_buttons_are_deny_then_deny_always_then_allow(self):
        """finding #6: "Deny" keeps its ORIGINAL position (first — it was
        {"Deny", "Allow"} before "Deny always" existed); "Deny always" is
        inserted BETWEEN Deny and Allow, not in front of Deny. The default
        button stays "Deny", unchanged."""
        argv = watch.build_osascript_argv(title="t", message="m")
        script_lines = [arg for i, arg in enumerate(argv) if i > 0 and argv[i - 1] == "-e"]
        dialog_line = next(line for line in script_lines if "display dialog" in line)
        self.assertIn('"Deny always"', dialog_line)
        self.assertIn('"Deny"', dialog_line)
        self.assertIn('"Allow"', dialog_line)
        # .index('"Deny"') finds the FIRST exact `"Deny"` — the button list
        # entry, not `"Deny always"` (no exact `"Deny"` substring inside
        # that) and not the later `default button "Deny"`.
        self.assertLess(
            dialog_line.index('"Deny"'),
            dialog_line.index('"Deny always"'),
        )
        self.assertLess(
            dialog_line.index('"Deny always"'),
            dialog_line.index('"Allow"'),
        )
        self.assertIn('default button "Deny"', dialog_line)

    def test_dialog_message_states_deny_always_scope(self):
        """finding #6: the dialog message must say what "Deny always"
        actually does (this host, this bottle — not the broader "all
        bottles" scope, which stays terminal/CLI-only) and how to undo it."""
        _title, message = watch.build_dialog_message(self._details())
        self.assertIn(
            "Deny always = this host, this bottle (persistent; undo with ./djinn undeny)",
            message,
        )

    def test_dialog_deny_always_prompt_resolves_to_deny_bottle(self):
        proc = mock.Mock()
        proc.stdout = io.StringIO("Deny always\n")
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        gate = threading.Event()

        def input_fn(_prompt: str) -> str:
            gate.wait()
            return "s"

        watcher = watch.EgressWatcher(
            self._broker(Path(tempfile.mkdtemp())),
            egress_log.EgressLog(Path(tempfile.mkdtemp())),
            input_fn=input_fn,
            output=io.StringIO(),
            platform="darwin",
            popen_factory=lambda *args, **kwargs: proc,
            notify_runner=lambda _argv: None,
        )
        choice = watcher._prompt_with_dialog(self._details())
        self.assertEqual(choice.action, "deny_bottle")

    def test_prompt_line_renders_deny_always_keys(self):
        details = self._details()
        block = watch.format_request_block(details)
        for key in ("[d]", "[D]", "[G]"):
            self.assertIn(key, block)
        # finding #5: honest about scope — "this host" in both, "this
        # bottle" vs "all bottles" is what actually differs.
        self.assertIn("deny always: this host, this bottle", block)
        self.assertIn("deny always: this host, all bottles", block)

    def test_ip_prompt_line_also_renders_deny_always_keys(self):
        details = self._details(host="192.0.2.55", port=5432)
        block = watch.format_request_block(details)
        for key in ("[d]", "[D]", "[G]"):
            self.assertIn(key, block)

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
                    self._details(),
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

    def test_dialog_prompt_dispatches_dialog_only(self):
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
        self.assertEqual(len(popen_calls), 1)
        self.assertTrue(any("display dialog" in a for a in popen_calls[0]))
        # The banner moved to run_once: welded to the prompt it could not fire
        # for request B until request A had been answered.
        self.assertFalse(notified.is_set())
        self.assertEqual(notify_argvs, [])

    def test_stalled_notification_does_not_delay_the_poll(self):
        """Notification Center hanging must not hold up the operator (review P2).

        The banner moved from the dialog prompt to run_once, so this now pins
        the same property one level up: a wedged osascript must not stop
        run_once from reaching the prompt.
        """
        release = threading.Event()
        self.addCleanup(release.set)

        def notify_runner(_argv: list[str]) -> None:
            release.wait(timeout=10)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            self._file_request(b)
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="darwin",
                notify_runner=notify_runner,
            )
            start = time.monotonic()
            self.assertTrue(watcher.run_once())
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 5.0, "poll waited on the notification")

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

    def test_skip_survives_the_next_poll(self):
        # run_once used to clear _deferred whenever every open request was
        # deferred, so [s] lasted exactly one 0.5s poll and the operator was
        # re-prompted immediately — unescapable for a request that could not
        # be decided any other way.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            request_id = self._file_request(b)
            prompts: list[str] = []

            def input_fn(_prompt: str) -> str:
                prompts.append("prompted")
                return "s"

            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=input_fn,
                output=io.StringIO(),
                platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertFalse(watcher.run_once(), "a skipped request must go quiet")
            self.assertFalse(watcher.run_once())
            self.assertEqual(len(prompts), 1, "skipped request was re-prompted")
            with b._lock:
                self.assertIn(request_id, b._requests)

    def test_a_new_request_still_prompts_while_another_is_skipped(self):
        # Deferring must silence one request, not the whole queue.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            first = self._file_request(b, container="bottle-one")
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertFalse(watcher.run_once())
            self._file_request(b, container="bottle-two", already={first})
            self.assertTrue(watcher.run_once(), "a newly filed request must prompt")

    def test_allow_on_an_ip_request_does_not_reprompt_forever(self):
        # decide() cannot install a rule for an IP literal (it needs a manifest
        # CIDR) and leaves the request OPEN. Without deferring it here, the
        # next poll re-prompts the same id: the 127.0.0.1:3128 loop, where
        # neither [a] nor [s] could clear the prompt.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            thread = threading.Thread(
                target=b.file_request,
                args=("coding-brassbottle", "127.0.0.1", 3128),
                kwargs={"host_is_ip": True},
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

            prompts: list[str] = []

            def input_fn(_prompt: str) -> str:
                prompts.append("prompted")
                return "a"

            out = io.StringIO()
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=input_fn,
                output=out,
                platform="linux",
            )
            self.assertTrue(watcher.run_once())
            self.assertIn("ALLOWED_CIDRS", out.getvalue())
            self.assertFalse(watcher.run_once(), "IP allow must not re-prompt")
            self.assertEqual(len(prompts), 1)

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

    def test_a_poll_reads_the_log_once_regardless_of_queue_depth(self):
        """Announcing every open request must not re-read the log per request.

        request_details_from_log folds the queue AND re-reads the whole month
        each call, so calling it per open request made a poll
        O(open requests x log size) — the backlog case is exactly when the
        operator can least afford the delay.
        """
        reads: list[int] = []
        folds: list[int] = []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            log = egress_log.EgressLog(root)
            filed = set()
            for name in ("bottle-one", "bottle-two", "bottle-three", "bottle-four"):
                filed.add(self._file_request(b, container=name, already=set(filed)))

            watcher = watch.EgressWatcher(
                b,
                log,
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="linux",
            )

            real_month_records = watch._month_records
            real_fold = log.fold_queue

            def counting_month_records(*args, **kwargs):
                reads.append(1)
                return real_month_records(*args, **kwargs)

            def counting_fold(*args, **kwargs):
                folds.append(1)
                return real_fold(*args, **kwargs)

            with mock.patch.object(watch, "_month_records", counting_month_records), \
                    mock.patch.object(log, "fold_queue", counting_fold):
                self.assertTrue(watcher.run_once())

        self.assertEqual(len(folds), 1, "the queue must be folded once per poll")
        self.assertEqual(
            len(reads), 1, "the month log must be read once per poll, not per request"
        )

    def test_all_four_requests_are_announced_from_the_one_snapshot(self):
        # The single-pass optimisation must not cost coverage: every open
        # request still gets its banner, with its own details.
        hosts: list[str] = []
        all_seen = threading.Event()

        def notify_runner(argv: list[str]) -> None:
            # _osascript_argv ends "-- <message> <title>"; the message is the
            # half naming the container and destination.
            hosts.append(argv[-2])
            if len(hosts) >= 4:
                all_seen.set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            filed = set()
            for name in ("bottle-one", "bottle-two", "bottle-three", "bottle-four"):
                filed.add(self._file_request(b, container=name, already=set(filed)))
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="darwin",
                notify_runner=notify_runner,
            )
            watcher.run_once()
            self.assertTrue(all_seen.wait(timeout=5), f"only announced {hosts}")

        containers = {h for host in hosts for h in host.split() if h.startswith("bottle-")}
        self.assertEqual(len(containers), 4, hosts)

    def test_banner_fires_once_per_request_across_polls(self):
        # The old code suppressed the banner when the operator answered inside
        # the scheduling gap. Announcing on first sight replaces that: fire
        # exactly once per request, however many times the poll comes round.
        notify_argvs: list[list[str]] = []
        fired = threading.Event()

        def notify_runner(argv: list[str]) -> None:
            notify_argvs.append(argv)
            fired.set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            self._file_request(b)
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="darwin",
                notify_runner=notify_runner,
            )
            watcher.run_once()
            self.assertTrue(fired.wait(timeout=5), "banner never dispatched")
            watcher.run_once()
            watcher.run_once()

        self.assertEqual(len(notify_argvs), 1)

    def test_banner_is_not_blocked_by_an_unanswered_earlier_request(self):
        # The bug this fixes: with the banner welded to the prompt, a request
        # the operator had not answered suppressed the banner for every
        # request behind it. run_once prompts one request but announces all.
        seen: list[str] = []
        both = threading.Event()

        def notify_runner(argv: list[str]) -> None:
            seen.append(argv[-2])
            if len(seen) >= 2:
                both.set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            first = self._file_request(b, container="bottle-one")
            self._file_request(b, container="bottle-two", already={first})
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="darwin",
                notify_runner=notify_runner,
            )
            watcher.run_once()
            self.assertTrue(both.wait(timeout=5), f"only announced {seen}")

    def test_no_banner_on_a_non_darwin_host(self):
        # There is no Notification Center to talk to; ntfy push is dispatched
        # daemon-side at file time and is unaffected.
        notify_argvs: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            self._file_request(b)
            watcher = watch.EgressWatcher(
                b,
                egress_log.EgressLog(root),
                input_fn=lambda _p: "s",
                output=io.StringIO(),
                platform="linux",
                notify_runner=notify_argvs.append,
            )
            watcher.run_once()
        self.assertEqual(notify_argvs, [])

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
        # finding #8b: the deny-list status must be visible in the startup
        # banner too, not only per-prompt — a fresh base_path has no
        # denylist.json yet, which reads as 0 entries (not corrupt).
        self.assertIn("Deny list: 0 entries", out)
        for key in ("[a]", "[p]", "[d]", "[D]", "[G]", "[s]"):
            self.assertIn(key, out, f"key legend must mention {key}")
        # finding E: D/G's uppercase-means-writes-to-disk convention is
        # deliberate but easy to fat-finger — the banner must say so
        # explicitly, not leave it to be inferred from the key legend.
        self.assertIn("D/G are uppercase on purpose", out)
        self.assertIn("./djinn undeny", out)

    def test_run_watch_prints_the_listen_line(self):
        """finding (PR #85 review, daemon endpoint): the banner must say what
        address ./djinn deny will target — otherwise a --host bind (e.g. the
        documented ntfy VPN bind from #84) is invisible to the operator."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            buf = io.StringIO()
            with mock.patch.object(watch.EgressWatcher, "run_forever",
                                   return_value=None), \
                 mock.patch.object(sys, "stdout", buf):
                watch.run_watch(base, host="127.0.0.1", port=0)
            out = buf.getvalue()

        self.assertIn("  listen: http://127.0.0.1:", out)

    def test_run_watch_writes_and_removes_daemon_endpoint(self):
        """run_watch writes $egress_root/daemon.json with the real bound
        port right after constructing the server, and removes it in its
        finally block, before the singleton lock is released."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = watch.resolve_egress_root(base)
            captured: dict[str, object] = {}

            def fake_run_forever(self) -> None:
                captured["endpoint"] = broker.read_daemon_endpoint(egress_root)

            with mock.patch.object(watch.EgressWatcher, "run_forever", fake_run_forever), \
                 mock.patch.object(sys, "stdout", io.StringIO()):
                watch.run_watch(base, host="127.0.0.1", port=0)

            endpoint = captured.get("endpoint")
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.host, "127.0.0.1")
            self.assertEqual(endpoint.pid, os.getpid())
            self.assertFalse((egress_root / broker.ENDPOINT_FILENAME).exists())

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



class DrainPendingInputTests(unittest.TestCase):
    """Type-ahead must not answer a prompt the operator never saw.

    Observed in the field: a `d` typed while the watcher was still polling was
    consumed the instant the first prompt rendered, denying a request the
    operator had not read. `D`/`G` would have written a PERSISTENT deny-list
    entry the same way.
    """

    def _watcher(self, root: Path, **kw) -> watch.EgressWatcher:
        b = broker.EgressBroker(
            root, repo_root=REPO_ROOT, now_fn=FakeClock(NOW).now, hold_seconds_default=60
        )
        return watch.EgressWatcher(
            b, egress_log.EgressLog(root), output=io.StringIO(), platform="linux", **kw
        )

    def test_injected_input_fn_is_never_drained(self):
        # An injected reader reads from somewhere other than the tty; draining
        # stdin for it would be pointless and would break piped answers.
        with tempfile.TemporaryDirectory() as tmp:
            w = self._watcher(Path(tmp), input_fn=lambda _p: "a")
            self.assertFalse(w._owns_stdin)
            with mock.patch.object(watch, "termios") as t:
                w._drain_pending_input()
            t.tcflush.assert_not_called()

    def test_real_stdin_on_a_tty_is_flushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self._watcher(Path(tmp))
            self.assertTrue(w._owns_stdin)
            with mock.patch.object(watch, "termios") as t, mock.patch.object(
                watch.os, "isatty", return_value=True
            ), mock.patch.object(sys.stdin, "fileno", return_value=0):
                w._drain_pending_input()
            t.tcflush.assert_called_once()
            self.assertEqual(t.tcflush.call_args[0][1], t.TCIFLUSH)

    def test_non_tty_stdin_is_left_alone(self):
        # Piped input is a legitimate way to drive this; do not eat it.
        with tempfile.TemporaryDirectory() as tmp:
            w = self._watcher(Path(tmp))
            with mock.patch.object(watch, "termios") as t, mock.patch.object(
                watch.os, "isatty", return_value=False
            ), mock.patch.object(sys.stdin, "fileno", return_value=0):
                w._drain_pending_input()
            t.tcflush.assert_not_called()

    def test_a_closed_stdin_does_not_break_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self._watcher(Path(tmp))
            with mock.patch.object(watch, "termios") as t, mock.patch.object(
                sys.stdin, "fileno", side_effect=ValueError
            ):
                w._drain_pending_input()  # must not raise
            t.tcflush.assert_not_called()

    def test_missing_termios_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self._watcher(Path(tmp))
            with mock.patch.object(watch, "termios", None):
                w._drain_pending_input()  # must not raise

    def test_prompt_drains_after_rendering_not_before(self):
        # Draining before the block is on screen would leave a window in which
        # type-ahead still lands, so ordering is the whole fix.
        order: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = broker.EgressBroker(
                root, repo_root=REPO_ROOT, now_fn=FakeClock(NOW).now, hold_seconds_default=60
            )

            class Recorder(io.StringIO):
                def flush(self) -> None:
                    order.append("render")

            w = watch.EgressWatcher(
                b, egress_log.EgressLog(root), output=Recorder(), platform="linux"
            )
            details = watch.RequestDetails(
                request_id="req-1",
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
                hit_count=1,
            )
            with mock.patch.object(
                w, "_drain_pending_input", side_effect=lambda: order.append("drain")
            ), mock.patch.object(w, "_prompt_terminal", return_value=None):
                w._prompt_operator(details)
        self.assertEqual(order, ["render", "drain"])


if __name__ == "__main__":
    unittest.main()
