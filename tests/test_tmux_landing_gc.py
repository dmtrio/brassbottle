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
