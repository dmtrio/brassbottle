#!/usr/bin/env python3
"""backup_service.py — in-container scheduled restic backup loop.

Runs as the backup container entrypoint. Emits stage/boundary logs with
status, duration, and aggregate counts/sizes — never secrets or file contents.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class BackupServiceConfigError(Exception):
    """Invalid backup service environment."""


def log_stage(stage: str, status: str, *, duration_sec: float | None = None, **meta: Any) -> None:
    parts = [f"backup {stage} {status}"]
    if duration_sec is not None:
        parts.append(f"duration={duration_sec:.2f}s")
    for key in sorted(meta):
        value = meta[key]
        if value is None:
            continue
        parts.append(f"{key}={value}")
    print(" ".join(parts), flush=True)


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackupServiceConfigError(
            f"{name} must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise BackupServiceConfigError(f"{name} must be positive, got {value}")
    return value


def load_daemon_config() -> dict[str, int]:
    """Read and validate scheduler environment (positive intervals/retention)."""
    return {
        "interval": _env_positive_int("BACKUP_INTERVAL_SECONDS", 600),
        "prune_interval": _env_positive_int("PRUNE_INTERVAL_SECONDS", 86400),
        "retention_hourly": _env_positive_int("RETENTION_HOURLY", 48),
        "retention_daily": _env_positive_int("RETENTION_DAILY", 30),
    }


def _repo_already_initialized(stderr: str, stdout: str) -> bool:
    combined = f"{stderr}\n{stdout}".lower()
    return "already initialized" in combined or "already exists" in combined


def _restic_repo_path() -> Path | None:
    raw = os.environ.get("RESTIC_REPOSITORY", "")
    if raw.startswith("file:"):
        return Path(raw[5:])
    return None


def _repo_directory_has_data() -> bool:
    """True when the repository directory exists and is not empty."""
    repo = _restic_repo_path()
    if repo is None:
        return False
    if not repo.exists():
        return False
    if not repo.is_dir():
        return True
    try:
        return any(repo.iterdir())
    except OSError:
        return True


def ensure_repo_initialized() -> None:
    """Initialize the restic repository if needed (idempotent, race-tolerant)."""
    started = time.monotonic()
    log_stage("init", "start")

    probe = subprocess.run(["restic", "snapshots"], capture_output=True, text=True)
    if probe.returncode == 0:
        log_stage("init", "ok", duration_sec=time.monotonic() - started, note="already-initialized")
        return

    if _repo_directory_has_data():
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        reason = detail[0][:200] if detail else "snapshots probe failed"
        log_stage(
            "init",
            "error",
            duration_sec=time.monotonic() - started,
            reason=reason,
            note="non-empty-repo",
        )
        raise subprocess.CalledProcessError(
            probe.returncode, ["restic", "snapshots"], probe.stdout, probe.stderr
        )

    init = subprocess.run(["restic", "init"], capture_output=True, text=True)
    if init.returncode == 0:
        log_stage("init", "ok", duration_sec=time.monotonic() - started)
        return

    if _repo_already_initialized(init.stderr or "", init.stdout or ""):
        log_stage("init", "ok", duration_sec=time.monotonic() - started, note="race-won-by-peer")
        return

    retry = subprocess.run(["restic", "snapshots"], capture_output=True, text=True)
    if retry.returncode == 0:
        log_stage("init", "ok", duration_sec=time.monotonic() - started, note="initialized-by-peer")
        return

    log_stage("init", "error", duration_sec=time.monotonic() - started, exit_code=init.returncode)
    raise subprocess.CalledProcessError(
        init.returncode, ["restic", "init"], init.stdout, init.stderr
    )


def _run_restic(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["restic", *args]
    log_stage("restic", "start", command=" ".join(cmd))
    started = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True)
    duration = time.monotonic() - started
    log_stage(
        "restic",
        "ok" if result.returncode == 0 else "error",
        duration_sec=duration,
        exit_code=result.returncode,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if stderr:
            # Log only the first line — restic may mention paths; keep it short.
            log_stage("restic", "stderr", detail=stderr.splitlines()[0][:200])
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _parse_backup_summary(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "message_type" in data:
            return {
                "files_new": data.get("files_new"),
                "files_changed": data.get("files_changed"),
                "data_added": data.get("data_added"),
                "total_files_processed": data.get("total_files_processed"),
            }
    return {}


def run_backup(sources: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    log_stage("run", "start", source_count=len(sources))
    result = _run_restic(["backup", *sources, "--json", "--tag", "scheduled"])
    summary = _parse_backup_summary(result.stdout or "")
    log_stage(
        "run",
        "ok",
        duration_sec=time.monotonic() - started,
        source_count=len(sources),
        **{k: v for k, v in summary.items() if v is not None},
    )
    return summary


def run_forget() -> None:
    cfg = load_daemon_config()
    hourly = cfg["retention_hourly"]
    daily = cfg["retention_daily"]
    started = time.monotonic()
    log_stage("forget", "start", keep_hourly=hourly, keep_daily=daily)
    _run_restic(
        [
            "forget",
            "--keep-hourly",
            str(hourly),
            "--keep-daily",
            str(daily),
            "--prune",
        ]
    )
    log_stage("forget", "ok", duration_sec=time.monotonic() - started)
    run_check()


def run_check() -> None:
    started = time.monotonic()
    log_stage("check", "start")
    _run_restic(["check"])
    log_stage("check", "ok", duration_sec=time.monotonic() - started)


def run_restore(snapshot: str, target: str) -> None:
    started = time.monotonic()
    log_stage("restore", "start", snapshot_id=snapshot, target=target)
    _run_restic(["restore", snapshot, "--target", target])
    log_stage("restore", "ok", duration_sec=time.monotonic() - started, snapshot_id=snapshot)


def daemon_loop() -> None:
    sources = os.environ.get("BACKUP_SOURCES", "").split()
    if not sources:
        log_stage("daemon", "error", reason="BACKUP_SOURCES unset")
        sys.exit(1)

    try:
        cfg = load_daemon_config()
    except BackupServiceConfigError as exc:
        log_stage("daemon", "error", reason=str(exc))
        sys.exit(1)

    interval = cfg["interval"]
    prune_interval = cfg["prune_interval"]
    log_stage(
        "daemon",
        "start",
        source_count=len(sources),
        interval_seconds=interval,
        prune_interval_seconds=prune_interval,
    )

    try:
        ensure_repo_initialized()
    except subprocess.CalledProcessError:
        log_stage("daemon", "error", reason="repository init failed")
        sys.exit(1)

    last_prune = 0.0
    while True:
        try:
            run_backup(sources)
        except subprocess.CalledProcessError:
            log_stage("run", "error")
        now = time.monotonic()
        if now - last_prune >= prune_interval:
            try:
                run_forget()
                last_prune = now
            except subprocess.CalledProcessError:
                log_stage("forget", "error")
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if not args:
        daemon_loop()
        return 0

    cmd = args[0]
    if cmd == "daemon":
        daemon_loop()
        return 0
    if cmd == "backup":
        sources = os.environ.get("BACKUP_SOURCES", "").split()
        if not sources:
            print("BACKUP_SOURCES unset", file=sys.stderr)
            return 1
        try:
            ensure_repo_initialized()
            run_backup(sources)
        except subprocess.CalledProcessError:
            return 1
        return 0
    if cmd == "forget":
        try:
            run_forget()
        except subprocess.CalledProcessError:
            return 1
        return 0
    if cmd == "check":
        try:
            run_check()
        except subprocess.CalledProcessError:
            return 1
        return 0
    if cmd == "restore":
        if len(args) != 3:
            print("usage: backup_service.py restore <snapshot-id> <target>", file=sys.stderr)
            return 1
        try:
            run_restore(args[1], args[2])
        except subprocess.CalledProcessError:
            return 1
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
