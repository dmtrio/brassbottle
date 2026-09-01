"""Unit tests for src/tmux_landing_gc.py."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import tmux_landing_gc  # noqa: E402


def _row(
    name="login-123",
    attached=0,
    windows=1,
    panes=1,
    cmd="bash",
    pid=2000,
):
    return (name, attached, windows, panes, cmd, pid)


class ParsePaneRowTests(unittest.TestCase):
    def test_parses_tab_delimited_row(self):
        parsed = tmux_landing_gc.parse_pane_row("login-123\t0\t1\t1\tbash\t4242")
        self.assertEqual(parsed, ("login-123", 0, 1, 1, "bash", 4242))

    def test_parses_session_name_with_spaces(self):
        parsed = tmux_landing_gc.parse_pane_row("login test 99\t0\t1\t1\tsh\t77")
        self.assertEqual(parsed, ("login test 99", 0, 1, 1, "sh", 77))

    def test_malformed_row_returns_none(self):
        self.assertIsNone(tmux_landing_gc.parse_pane_row("bad\trow"))
        self.assertIsNone(tmux_landing_gc.parse_pane_row("login-1\t0\tx\t1\tbash\t4"))


class SessionsToKillTests(unittest.TestCase):
    def test_bare_empty_unattached_login_session_is_killed(self):
        kills = tmux_landing_gc.sessions_to_kill([_row()], lambda _pid: False)
        self.assertEqual(kills, {"login-123"})

    def test_attached_session_is_kept(self):
        kills = tmux_landing_gc.sessions_to_kill(
            [_row(attached=1)], lambda _pid: False
        )
        self.assertEqual(kills, set())

    def test_extra_window_is_kept(self):
        kills = tmux_landing_gc.sessions_to_kill([_row(windows=2)], lambda _pid: False)
        self.assertEqual(kills, set())

    def test_extra_pane_is_kept(self):
        kills = tmux_landing_gc.sessions_to_kill([_row(panes=2)], lambda _pid: False)
        self.assertEqual(kills, set())

    def test_non_shell_command_is_kept(self):
        kills = tmux_landing_gc.sessions_to_kill([_row(cmd="claude")], lambda _pid: False)
        self.assertEqual(kills, set())
        kills = tmux_landing_gc.sessions_to_kill([_row(cmd="vim")], lambda _pid: False)
        self.assertEqual(kills, set())

    def test_shell_with_children_is_kept(self):
        kills = tmux_landing_gc.sessions_to_kill([_row()], lambda _pid: True)
        self.assertEqual(kills, set())

    def test_non_login_prefixed_sessions_are_kept(self):
        rows = [
            _row(name="work"),
            _row(name="agent"),
            _row(name="1234"),
        ]
        kills = tmux_landing_gc.sessions_to_kill(rows, lambda _pid: False)
        self.assertEqual(kills, set())

    def test_malformed_rows_are_skipped(self):
        rows = [
            _row(),
            ("login-123", 0, 1),  # wrong shape
            "not-a-row",
            (_row()[:-1] + ("not-int",)),  # bad pid type
        ]
        kills = tmux_landing_gc.sessions_to_kill(rows, lambda _pid: False)
        self.assertEqual(kills, {"login-123"})


if __name__ == "__main__":
    unittest.main()


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class KillStaleSessionsTests(unittest.TestCase):
    """The kill path itself: exact-match targets and the attach re-check."""

    def _run_tmux_factory(self, attached_now, kill_log):
        def run_tmux(args):
            if args[0] == "display-message":
                name = args[3].lstrip("=")
                if name not in attached_now:
                    return FakeCompleted(returncode=1)  # session vanished
                return FakeCompleted(stdout=f"{int(attached_now[name])}\n")
            if args[0] == "kill-session":
                kill_log.append(args[2])
                return FakeCompleted()
            raise AssertionError(f"unexpected tmux call: {args}")

        return run_tmux

    def test_kill_uses_exact_match_target(self):
        kills = []
        run_tmux = self._run_tmux_factory({"login-42": False}, kills)
        killed = tmux_landing_gc.kill_stale_sessions(
            [_row(name="login-42")], run_tmux=run_tmux, children=lambda pid: False
        )
        self.assertEqual(killed, ["login-42"])
        self.assertEqual(kills, ["=login-42"])

    def test_session_attached_since_snapshot_is_spared(self):
        kills = []
        run_tmux = self._run_tmux_factory({"login-42": True}, kills)
        killed = tmux_landing_gc.kill_stale_sessions(
            [_row(name="login-42")], run_tmux=run_tmux, children=lambda pid: False
        )
        self.assertEqual(killed, [])
        self.assertEqual(kills, [])

    def test_session_vanished_since_snapshot_is_skipped(self):
        kills = []
        run_tmux = self._run_tmux_factory({}, kills)
        killed = tmux_landing_gc.kill_stale_sessions(
            [_row(name="login-42")], run_tmux=run_tmux, children=lambda pid: False
        )
        self.assertEqual(killed, [])
        self.assertEqual(kills, [])


class ListRowsTests(unittest.TestCase):
    def test_no_server_returns_none_not_empty(self):
        result = tmux_landing_gc._list_rows(
            run_tmux=lambda args: FakeCompleted(returncode=1, stderr="no server")
        )
        self.assertIsNone(result)

    def test_missing_tmux_returns_none(self):
        self.assertIsNone(tmux_landing_gc._list_rows(run_tmux=lambda args: None))

    def test_rows_parsed_and_malformed_lines_dropped(self):
        out = "login-1\t0\t1\t1\tbash\t10\ngarbage line\n"
        rows = tmux_landing_gc._list_rows(
            run_tmux=lambda args: FakeCompleted(stdout=out)
        )
        self.assertEqual(rows, [("login-1", 0, 1, 1, "bash", 10)])


class HasChildrenProcTests(unittest.TestCase):
    """Real /proc: a shell holding a child vs. a lone process."""

    def test_shell_with_child_is_detected(self):
        import subprocess as sp

        proc = sp.Popen(["bash", "-c", "sleep 5"])
        try:
            # bash -c with a single command execs it, so probe the TEST
            # process itself, which now parents `proc`.
            import os

            self.assertTrue(tmux_landing_gc.has_children(os.getpid()))
        finally:
            proc.kill()
            proc.wait()

    def test_childless_process_is_detected(self):
        import subprocess as sp

        proc = sp.Popen(["sleep", "5"])
        try:
            self.assertFalse(tmux_landing_gc.has_children(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_dead_pid_has_no_children(self):
        self.assertFalse(tmux_landing_gc.has_children(2**22 - 3))
