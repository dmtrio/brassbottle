#!/usr/bin/env python3
"""Unit tests for src/plugin_services.py — the generated per-service restart
wrapper (Phase 1 Hardening PLN §2, services: schema, PR [1/3])."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import plugin_services as ps


class TestWrapperScript(unittest.TestCase):
    def test_contains_log_path(self):
        script = ps.wrapper_script("bm-server", "bm mcp --port 8801")
        self.assertIn(">> /tmp/djinn-services/bm-server.log", script)

    def test_contains_has_session_guard(self):
        script = ps.wrapper_script("bm-server", "bm mcp --port 8801")
        self.assertIn("tmux has-session -t svc-bm-server", script)
        # The guard actually gates the start — a re-run must be a no-op, not
        # just print a matching string.
        self.assertIn("tmux new-session -d -s svc-bm-server", script)
        self.assertIn("already running", script)

    def test_contains_backoff(self):
        script = ps.wrapper_script("bm-server", "bm mcp --port 8801")
        self.assertIn(f"sleep {ps.BACKOFF_FIRST}", script)
        self.assertIn(f"sleep {ps.BACKOFF_ESCALATED}", script)
        self.assertIn(f'-lt {ps.FAST_EXIT_SECS}', script)

    def test_contains_the_command_verbatim(self):
        script = ps.wrapper_script("capture", "collabrain capture --watch --interval 60")
        self.assertIn("collabrain capture --watch --interval 60", script)

    def test_log_records_every_restart_not_just_the_first(self):
        # No cap on retries: the while true loop has no break/exit, so every
        # attempt logs unconditionally.
        script = ps.wrapper_script("x", "true")
        self.assertIn("while true; do", script)
        self.assertNotIn("break", script)
        self.assertIn('log "start', script)
        self.assertIn('log "exit code=', script)

    def test_session_and_script_names_are_derived_from_service_name(self):
        script = ps.wrapper_script("my-service", "true")
        self.assertIn("svc-my-service", script)
        self.assertIn("/tmp/djinn-services/my-service.sh", script)

    def test_heredoc_open_and_close_tags_match(self):
        script = ps.wrapper_script("my-service", "true")
        lines = script.splitlines()
        open_lines = [l for l in lines if "<<'" in l]
        self.assertEqual(len(open_lines), 1)
        tag = open_lines[0].split("<<'")[1].rstrip("'")
        self.assertIn(tag, lines)
        # Closing tag line must be unindented (heredoc requirement).
        close_line = next(l for l in lines if l == tag)
        self.assertEqual(close_line, close_line.strip())

    def test_rejects_uppercase_name(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("BadName", "true")

    def test_rejects_underscore_name(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("bad_name", "true")

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("", "true")

    def test_rejects_empty_command(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("svc", "")

    def test_rejects_whitespace_only_command(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("svc", "   ")

    def test_rejects_non_string_command(self):
        with self.assertRaises(ValueError):
            ps.wrapper_script("svc", 5)


class TestMain(unittest.TestCase):
    def test_wrong_argc_errors(self):
        self.assertEqual(ps.main(["only-one"]), 2)

    def test_bad_name_errors(self):
        self.assertEqual(ps.main(["Bad Name", "true"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestTmuxFailureSurfaces(unittest.TestCase):
    """Review finding: tmux failure must not report success / exit 0."""

    def test_wrapper_fails_loudly_when_tmux_cannot_start(self):
        script = ps.wrapper_script("demo", "sleep 1")
        self.assertIn("FAILED to start", script)
        # the success echo must be conditional on tmux new-session succeeding
        self.assertIn("if tmux new-session", script)
        self.assertIn("exit 1", script)

    def test_wrapper_fails_at_runtime_without_tmux(self):
        script = ps.wrapper_script("demo", "sleep 1")
        with tempfile.TemporaryDirectory() as td:
            env = {"PATH": td}  # no tmux, no coreutils beyond builtins
            proc = subprocess.run(
                ["/bin/bash", "-c", script.replace("/tmp/djinn-services", td)],
                capture_output=True, text=True, env=env,
            )
        self.assertNotEqual(0, proc.returncode)
        self.assertNotIn("started", proc.stdout)
