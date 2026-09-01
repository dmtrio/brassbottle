#!/usr/bin/env python3
"""Garbage-collect stale unattached tmux login-* landing sessions."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable, Iterable, Optional

PaneRow = tuple[str, int, int, int, str, int]

_LIST_PANES_FORMAT = (
    "#{session_name}\t#{session_attached}\t#{session_windows}\t#{window_panes}\t"
    "#{pane_current_command}\t#{pane_pid}"
)
_SHELL_COMMANDS = {"bash", "sh", "dash"}


def parse_pane_row(line: str) -> Optional[PaneRow]:
    """Parse one tab-delimited `tmux list-panes` row."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 6:
        return None
    session_name, attached, windows, panes, pane_cmd, pane_pid = parts
    try:
        return (
            session_name,
            int(attached),
            int(windows),
            int(panes),
            pane_cmd,
            int(pane_pid),
        )
    except ValueError:
        return None


def sessions_to_kill(
    rows: Iterable[object], has_children: Callable[[int], bool]
) -> set[str]:
    """Select stale, empty, unattached login-* sessions."""
    victims: set[str] = set()
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 6:
            continue
        session_name, attached, windows, panes, pane_cmd, pane_pid = row
        if not isinstance(session_name, str) or not session_name.startswith("login-"):
            continue
        if attached != 0 or windows != 1 or panes != 1:
            continue
        if pane_cmd not in _SHELL_COMMANDS:
            continue
        if not isinstance(pane_pid, int):
            continue
        if has_children(pane_pid):
            continue
        victims.add(session_name)
    return victims


def _has_children_proc_task_children(pid: int) -> Optional[bool]:
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return None
    saw_any = False
    for tid in tids:
        children_path = f"{task_dir}/{tid}/children"
        try:
            children = open(children_path, "r", encoding="utf-8").read().strip()
        except OSError:
            continue
        saw_any = True
        if children:
            return True
    if saw_any:
        return False
    return None


def _has_children_proc_stat(pid: int) -> bool:
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        stat_path = f"/proc/{entry}/stat"
        try:
            stat = open(stat_path, "r", encoding="utf-8").read()
        except OSError:
            continue
        close_paren = stat.rfind(")")
        if close_paren < 0:
            continue
        fields = stat[close_paren + 2 :].split()
        if len(fields) < 2:
            continue
        ppid = fields[1]
        if ppid.isdigit() and int(ppid) == pid:
            return True
    return False


def has_children(pid: int) -> bool:
    if pid <= 0:
        return False
    by_task_children = _has_children_proc_task_children(pid)
    if by_task_children is not None:
        return by_task_children
    return _has_children_proc_stat(pid)


def _run_tmux(args: list[str]) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["tmux", *args], check=False, capture_output=True, text=True
        )
    except FileNotFoundError:
        return None


def _list_rows(run_tmux=_run_tmux) -> Optional[list[PaneRow]]:
    """None means "could not list" (no tmux / no server) — distinct from an
    empty list so the caller can log the boundary instead of silently doing
    nothing."""
    result = run_tmux(["list-panes", "-a", "-F", _LIST_PANES_FORMAT])
    if result is None or result.returncode != 0:
        return None
    rows: list[PaneRow] = []
    for line in result.stdout.splitlines():
        row = parse_pane_row(line)
        if row is not None:
            rows.append(row)
    return rows


def _session_attached_now(name: str, run_tmux=_run_tmux) -> Optional[bool]:
    """Fresh attachment check right before the kill. None = session gone."""
    result = run_tmux(
        ["display-message", "-p", "-t", f"={name}", "#{session_attached}"]
    )
    if result is None or result.returncode != 0:
        return None
    out = result.stdout.strip()
    return bool(out and out != "0")


def kill_stale_sessions(rows, run_tmux=_run_tmux, children=has_children) -> list[str]:
    """Kill the selected sessions, re-verifying each is STILL unattached at
    kill time: the snapshot in `rows` races against a client attaching via
    the picker or a mosh reattach, and killing a session under a live client
    is the one unforgivable failure here. '=' forces an exact target name —
    a bare -t prefix-matches, so a vanished login-42 could redirect the kill
    onto a live login-421."""
    killed: list[str] = []
    for name in sorted(sessions_to_kill(rows, children)):
        if _session_attached_now(name, run_tmux) is not False:
            continue
        result = run_tmux(["kill-session", "-t", f"={name}"])
        if result is None:
            break
        if result.returncode == 0:
            killed.append(name)
        else:
            _log(f"kill-session {name} failed: {result.stderr.strip()}")
    return killed


def _log(message: str) -> None:
    # Boundary log (working agreement): callers run this fire-and-forget from
    # login shells and tmux hooks, where stdout would pop a view-mode pager in
    # an attached client — so the run trace goes to a file instead.
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{_now()} {message}\n")
    except OSError:
        pass


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


LOG_PATH = "/tmp/djinn-tmux-landing-gc.log"


def main() -> int:
    rows = _list_rows()
    if rows is None:
        # No tmux server (first login) is normal; no tmux binary is not.
        return 0
    killed = kill_stale_sessions(rows)
    if killed:
        _log(f"scanned {len(rows)} panes, killed: {', '.join(killed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
