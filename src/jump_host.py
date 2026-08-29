#!/usr/bin/env python3
"""jump_host.py — host-side operator commands for the singleton jump container.

Thin docker-compose glue around jump_config. Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jump_config import (  # noqa: E402
    JumpConfigError,
    JumpIdentity,
    SERVICE_NAME,
    derive_identity,
    paths,
    resolve_jump_ip,
    resolve_mosh_ports,
    write_compose_file,
)

ENV_AUTHORIZED_KEY = "SSH_AUTHORIZED_KEY"


class JumpHostError(Exception):
    """Operator-facing jump error."""


class DockerCommandMissing(Exception):
    """Docker CLI absent from PATH — boundary error already logged."""

    exit_code = 127


DOCKER_MISSING_EXIT = DockerCommandMissing.exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _compose_cmd(base_path: Path, identity: JumpIdentity, *args: str) -> list[str]:
    p = paths(base_path)
    return [
        "docker",
        "compose",
        "-p",
        identity.compose_project_name,
        "--project-directory",
        str(_repo_root()),
        "-f",
        str(p["compose_file"]),
        *args,
    ]


def _run(
    cmd: list[str],
    *,
    boundary: str | None = None,
    started: float | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
    except FileNotFoundError as exc:
        print(f"jump {boundary or 'run'} error reason=docker-not-found", file=sys.stderr)
        raise DockerCommandMissing(str(exc)) from exc
    if boundary is not None and started is not None:
        print(
            f"jump {boundary} done duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}"
        )
    return result


def _service_running(base_path: Path, identity: JumpIdentity, *, boundary: str) -> bool:
    result = _run(
        _compose_cmd(base_path, identity, "ps", "-q", SERVICE_NAME),
        boundary=boundary,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _authorized_key() -> str:
    key = (os.environ.get(ENV_AUTHORIZED_KEY) or "").strip()
    if not key:
        raise JumpHostError(
            f"{ENV_AUTHORIZED_KEY} is missing from secrets.env — set your public key there"
        )
    return key


def cmd_start(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print(f"jump start begin base={base_path}")
    try:
        key = _authorized_key()
        jump_ip = resolve_jump_ip()
        mosh_ports = resolve_mosh_ports()
        write_compose_file(base_path, key)
    except (JumpConfigError, JumpHostError) as exc:
        print(f"jump start error reason={exc}", file=sys.stderr)
        return 1
    try:
        result = _run(
            _compose_cmd(base_path, identity, "up", "-d", "--build", SERVICE_NAME),
            boundary="start",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if result.returncode != 0:
        print(
            f"jump start error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode
    print(
        f"jump start ok container={identity.container_name} ip={jump_ip} "
        f"mosh_ports={mosh_ports}"
    )
    print("")
    print(f"  Reach it over your tunnel:  mosh coder@{jump_ip}")
    print(f"  Then hop to a bottle:       ssh djinn-<bottle>")
    print("")
    print("  Run './djinn jump pubkey' for the key your bottles must authorise.")
    return 0


def cmd_stop(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    started = time.monotonic()
    if not compose_file.exists():
        print(f"jump stop not-configured project={identity.compose_project_name}")
        return 0
    try:
        result = _run(
            _compose_cmd(base_path, identity, "down"),
            boundary="stop",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    return result.returncode


def cmd_status(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print(f"jump status not-configured project={identity.compose_project_name}")
        return 1
    try:
        running = _service_running(base_path, identity, boundary="status")
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"jump status {'running' if running else 'stopped'} "
        f"container={identity.container_name} project={identity.compose_project_name}"
    )
    return 0 if running else 1


def cmd_logs(base_path: Path, follow: bool) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise JumpHostError("jump container is not configured — run: ./djinn jump start")
    args = ["logs", SERVICE_NAME]
    if follow:
        args.append("-f")
    try:
        return _run(
            _compose_cmd(base_path, identity, *args), boundary="logs", check=False
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def cmd_pubkey(base_path: Path) -> int:
    """Print the jump's client public key — what bottles authorise.

    Read from the host side of the mount, so it works whether or not the
    container is running (it is generated on first start).
    """
    pub = paths(base_path)["client_pubkey"]
    if not pub.exists():
        raise JumpHostError(
            f"no jump key yet at {pub} — run: ./djinn jump start (it is generated on first start)"
        )
    try:
        text = pub.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise JumpHostError(f"cannot read {pub}: {exc}") from exc
    print(text)
    return 0


def resolve_base_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    home = os.environ.get("DJINN_HOME")
    if home:
        return Path(home)
    return _repo_root() / ".djinn"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="djinn jump",
        description="Singleton mosh jump container for this djinn installation.",
    )
    parser.add_argument("--base-path", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="build and start the jump container")
    sub.add_parser("stop", help="stop and remove the jump container")
    sub.add_parser("status", help="report whether the jump container is running")
    logs = sub.add_parser("logs", help="show jump container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("pubkey", help="print the key bottles must authorise")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    try:
        if args.command == "start":
            return cmd_start(base_path)
        if args.command == "stop":
            return cmd_stop(base_path)
        if args.command == "status":
            return cmd_status(base_path)
        if args.command == "logs":
            return cmd_logs(base_path, args.follow)
        if args.command == "pubkey":
            return cmd_pubkey(base_path)
    except (JumpHostError, JumpConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
