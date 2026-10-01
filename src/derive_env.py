#!/usr/bin/env python3
"""derive_env.py — the environment `manifest.py --derive` runs under.

One source of truth for the variables up.sh has always handed to the derive
step, now shared with the egress broker's policy engine so both derive a
manifest identically:

  PRESENT_SECRET_VARS   names (never values) of the secrets.env assignments
                        that are non-empty
  SECRETS_FILE          the path, used verbatim in manifest.py error messages
  GIT_NAME_DEFAULT / GIT_EMAIL_DEFAULT   host `git config --global`
  NTFY_URL / NTFY_TOPIC                  environment first, then secrets.env
  DJINN_SUBNET / DJINN_EGRESS_IP         environment only

Secret VALUES are read only to decide "non-empty" and to resolve the two NTFY
variables manifest.py consumes; they are never logged. The one boundary log
line reports counts and a duration.

CLI, used by up.sh:

    derive_env.py --secrets-file F -- CMD [ARG...]

execs CMD with that environment added to the inherited one, so a pipe on
stdin passes straight through.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping

LOG = logging.getLogger(__name__)

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_FORWARDED = ("NTFY_URL", "NTFY_TOPIC", "DJINN_SUBNET", "DJINN_EGRESS_IP")
_FROM_FILE = ("NTFY_URL", "NTFY_TOPIC")

GitConfig = Callable[[str], str]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_assignments(secrets_file: Path) -> dict[str, str]:
    """name -> value for every assignment in a secrets.env-style file."""
    try:
        text = secrets_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match:
            found[match.group(1)] = _unquote(match.group(2))
    return found


def _git_global(key: str) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def derive_vars(
    secrets_file: Path,
    environ: Mapping[str, str] | None = None,
    git_config: GitConfig | None = None,
) -> dict[str, str]:
    """The variables manifest.py --derive reads, as up.sh would supply them."""
    started = time.monotonic()
    environ = os.environ if environ is None else environ
    git_config = git_config or _git_global
    assigned = read_assignments(secrets_file)
    present = sorted(
        name for name, value in assigned.items() if (environ.get(name) or value)
    )
    out = {
        "PRESENT_SECRET_VARS": " ".join(present),
        "SECRETS_FILE": str(secrets_file),
        "GIT_NAME_DEFAULT": git_config("user.name"),
        "GIT_EMAIL_DEFAULT": git_config("user.email"),
    }
    for name in _FORWARDED:
        value = environ.get(name) or ""
        if not value and name in _FROM_FILE:
            value = assigned.get(name, "")
        out[name] = value
    LOG.info(
        "derive env built duration_ms=%.1f assigned=%d present=%d",
        (time.monotonic() - started) * 1000,
        len(assigned),
        len(present),
    )
    return out


def child_env(
    secrets_file: Path,
    environ: Mapping[str, str] | None = None,
    git_config: GitConfig | None = None,
) -> dict[str, str]:
    """The inherited environment with the derive variables added."""
    base = dict(os.environ if environ is None else environ)
    base.update(derive_vars(secrets_file, base, git_config))
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secrets-file", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    env = child_env(args.secrets_file)
    os.execvpe(command[0], command, env)
    return 1  # unreachable: execvpe replaced the process


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sys.exit(main())
