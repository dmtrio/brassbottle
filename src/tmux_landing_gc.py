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


def _list_rows() -> list[PaneRow]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _LIST_PANES_FORMAT],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    rows: list[PaneRow] = []
    for line in result.stdout.splitlines():
        row = parse_pane_row(line)
        if row is not None:
            rows.append(row)
    return rows


def main() -> int:
    rows = _list_rows()
    if not rows:
        return 0
    for name in sorted(sessions_to_kill(rows, has_children)):
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"killed {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
