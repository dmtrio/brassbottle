#!/usr/bin/env python3
"""backup_host.py — host-side operator commands for the singleton backup service.

Thin docker-compose glue around backup_config / backup_restore. Stdlib only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from backup_config import (
    BackupConfigError,
    BackupIdentity,
    derive_identity,
    ensure_layout,
    paths,
    SERVICE_NAME,
    write_compose_file,
)
from backup_restore import RestoreTargetError, validate_restore_target


class BackupHostError(Exception):
    """Operator-facing backup error."""


class DockerCommandMissing(Exception):
    """Docker CLI absent from PATH — boundary error already logged."""

    exit_code = 127


DOCKER_MISSING_EXIT = DockerCommandMissing.exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _compose_cmd(base_path: Path, identity: BackupIdentity, *args: str) -> list[str]:
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
        if capture_output:
            return subprocess.run(cmd, text=True, check=check, capture_output=True)
        return subprocess.run(cmd, text=True, check=check)
    except FileNotFoundError as exc:
        reason = f"required command not found on PATH: {cmd[0]}"
        if boundary is not None:
            line = f"backup {boundary} error"
            if started is not None:
                line += (
                    f" duration={time.monotonic() - started:.2f}s"
                    f" exit_code={DOCKER_MISSING_EXIT}"
                )
            line += f" reason={reason}"
            print(line, file=sys.stderr)
            raise DockerCommandMissing() from exc
        raise BackupHostError(reason) from exc


def _service_running(
    base_path: Path,
    identity: BackupIdentity,
    *,
    boundary: str | None = None,
) -> bool:
    result = _run(
        _compose_cmd(base_path, identity, "ps", "--status", "running", "--services"),
        boundary=boundary,
        check=False,
        capture_output=True,
    )
    running_services = (result.stdout or "").split()
    return result.returncode == 0 and SERVICE_NAME in running_services


def _require_service_running(base_path: Path, identity: BackupIdentity, boundary: str) -> None:
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise BackupHostError("backup service is not configured — run: ./djinn backup start")
    if not _service_running(base_path, identity, boundary=boundary):
        raise BackupHostError(
            "backup container is not running — run: ./djinn backup start "
            "(restore works without the daemon via compose run)"
        )


def cmd_start(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print(f"backup start begin base={base_path}")
    try:
        write_compose_file(base_path)
    except BackupConfigError as exc:
        print(f"backup start error reason={exc}", file=sys.stderr)
        return 1
    try:
        result = _run(
            _compose_cmd(base_path, identity, "up", "-d", "--build"),
            boundary="start",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if result.returncode != 0:
        print(
            f"backup start error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode
    print(
        f"backup start ok duration={time.monotonic() - started:.2f}s "
        f"container={identity.container_name} project={identity.compose_project_name}"
    )
    return 0


def cmd_stop(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print("backup stop begin")
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print("backup stop ok duration=0.00s note=not-configured")
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
    if result.returncode != 0:
        print(
            f"backup stop error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode
    print(f"backup stop ok duration={time.monotonic() - started:.2f}s")
    return 0


def cmd_status(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print(f"backup status not-configured project={identity.compose_project_name}")
        return 1
    try:
        running = _service_running(base_path, identity, boundary="status")
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"backup status {'running' if running else 'stopped'} "
        f"container={identity.container_name} project={identity.compose_project_name}"
    )
    return 0 if running else 1


def cmd_logs(base_path: Path, follow: bool) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise BackupHostError("backup service is not configured — run: ./djinn backup start")
    args = ["logs"]
    if follow:
        args.append("-f")
    try:
        return _run(
            _compose_cmd(base_path, identity, *args),
            boundary="logs",
            check=False,
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def _exec_restic(base_path: Path, identity: BackupIdentity, boundary: str, *restic_args: str) -> int:
    try:
        _require_service_running(base_path, identity, boundary)
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    try:
        return _run(
            _compose_cmd(
                base_path,
                identity,
                "exec",
                "-T",
                SERVICE_NAME,
                "restic",
                *restic_args,
            ),
            boundary=boundary,
            check=False,
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def cmd_snapshots(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print("backup snapshots begin")
    try:
        rc = _exec_restic(base_path, identity, "snapshots", "snapshots")
    except BackupHostError:
        raise
    print(
        f"backup snapshots end status={'ok' if rc == 0 else 'error'} "
        f"duration={time.monotonic() - started:.2f}s"
    )
    return rc


def cmd_check(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print("backup check begin")
    try:
        rc = _exec_restic(base_path, identity, "check", "check")
    except BackupHostError:
        raise
    print(
        f"backup check end status={'ok' if rc == 0 else 'error'} "
        f"duration={time.monotonic() - started:.2f}s"
    )
    return rc


def cmd_restore(base_path: Path, snapshot: str, target: str) -> int:
    identity = derive_identity(base_path)
    p = ensure_layout(base_path)
    compose_file = p["compose_file"]
    if not compose_file.exists():
        raise BackupHostError("backup service is not configured — run: ./djinn backup start")
    resolved = validate_restore_target(
        target,
        artifacts_root=p["artifacts_root"],
        browser_tmp_root=p["browser_tmp_root"],
        backup_root=p["backup_root"],
        repo=p["repo"],
        password_file=p["password_file"],
        compose_dir=p["compose_dir"],
        compose_file=p["compose_file"],
    )
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"backup restore error reason=cannot create target directory {resolved}: {exc}",
            file=sys.stderr,
        )
        return 1
    started = time.monotonic()
    print(f"backup restore begin snapshot_id={snapshot} target={resolved}")
    # `run --rm` with an explicit target bind — never restores over live mounts.
    try:
        rc = _run(
            _compose_cmd(
                base_path,
                identity,
                "run",
                "--rm",
                "-v",
                f"{resolved}:/restore",
                SERVICE_NAME,
                "python3",
                "/usr/local/lib/djinn/backup_service.py",
                "restore",
                snapshot,
                "/restore",
            ),
            boundary="restore",
            started=started,
            check=False,
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"backup restore end status={'ok' if rc == 0 else 'error'} "
        f"duration={time.monotonic() - started:.2f}s snapshot_id={snapshot} target={resolved}"
    )
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="djinn singleton artifact backup operator")
    parser.add_argument(
        "--base-path",
        default="",
        help="djinn home (defaults to DJINN_HOME or ./.djinn)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="start or update the singleton backup container")
    sub.add_parser("stop", help="stop the singleton backup container")
    sub.add_parser("status", help="show whether the backup container is running")
    logs = sub.add_parser("logs", help="show backup container logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow log output")
    sub.add_parser("snapshots", help="list restic snapshots")
    sub.add_parser("check", help="run restic check")
    restore = sub.add_parser("restore", help="restore a snapshot to an explicit target directory")
    restore.add_argument("snapshot", help="snapshot ID (or restic selector)")
    restore.add_argument("--target", required=True, help="host directory to restore into")
    return parser


def resolve_base_path(raw: str) -> Path:
    if raw:
        return Path(raw)
    env = __import__("os").environ.get("DJINN_HOME", "")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / ".djinn"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
        if args.command == "snapshots":
            return cmd_snapshots(base_path)
        if args.command == "check":
            return cmd_check(base_path)
        if args.command == "restore":
            return cmd_restore(base_path, args.snapshot, args.target)
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    except (BackupConfigError, RestoreTargetError, BackupHostError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: command failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode or 1
    parser.error(f"unknown command {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
