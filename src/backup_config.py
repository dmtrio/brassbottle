#!/usr/bin/env python3
"""backup_config.py — singleton backup service paths, compose generation, layout.

Owns the fixed compose project/container identity and every host path the backup
service uses. Bottle compose never references the repository or credentials.
Stdlib only (matches ensure_net.py / manifest.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import textwrap
from pathlib import Path

# Singleton identity prefix — suffix is derived per resolved DJINN_HOME.
IDENTITY_PREFIX = "djinn-backup"
SERVICE_NAME = "backup"
IDENTITY_SUFFIX_LENGTH = 8


class BackupIdentity:
    """Stable Docker identity for one djinn installation (all bottles share it)."""

    def __init__(
        self,
        suffix: str,
        compose_project_name: str,
        container_name: str,
        hostname: str,
        image_tag: str,
    ) -> None:
        self.suffix = suffix
        self.compose_project_name = compose_project_name
        self.container_name = container_name
        self.hostname = hostname
        self.image_tag = image_tag

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BackupIdentity):
            return NotImplemented
        return (
            self.suffix == other.suffix
            and self.compose_project_name == other.compose_project_name
            and self.container_name == other.container_name
            and self.hostname == other.hostname
            and self.image_tag == other.image_tag
        )


def identity_suffix(base_path: Path) -> str:
    """Short deterministic suffix from resolved DJINN_HOME (never the full path)."""
    resolved = str(base_path.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return digest[:IDENTITY_SUFFIX_LENGTH]


def derive_identity(base_path: Path) -> BackupIdentity:
    """Compose project/container/hostname/image names scoped to one djinn home."""
    suffix = identity_suffix(base_path)
    stem = f"{IDENTITY_PREFIX}-{suffix}"
    return BackupIdentity(
        suffix=suffix,
        compose_project_name=stem,
        container_name=stem,
        hostname=stem,
        image_tag=f"{IDENTITY_PREFIX}:{suffix}",
    )

# In-container mount targets (stable regardless of bottle count).
SOURCE_ARTIFACTS_MOUNT = "/sources/artifacts"
SOURCE_BROWSER_TMP_MOUNT = "/sources/browser-tmp"
REPO_MOUNT = "/repo"
PASSWORD_MOUNT = "/run/secrets/restic-password"

# Defaults from PLN - Singleton artifact backups.
DEFAULT_BACKUP_INTERVAL_SECONDS = 600
DEFAULT_RETENTION_HOURLY = 48
DEFAULT_RETENTION_DAILY = 30
DEFAULT_PRUNE_INTERVAL_SECONDS = 86400

# Host-side overrides (read at compose generation; not hand-edited in backup.yml).
ENV_BACKUP_INTERVAL = "DJINN_BACKUP_INTERVAL_SECONDS"
ENV_RETENTION_HOURLY = "DJINN_BACKUP_RETENTION_HOURLY"
ENV_RETENTION_DAILY = "DJINN_BACKUP_RETENTION_DAILY"
ENV_PRUNE_INTERVAL = "DJINN_BACKUP_PRUNE_INTERVAL_SECONDS"


class BackupConfigError(Exception):
    """Invalid backup configuration."""


def _yaml_double_quoted(value: str) -> str:
    """Return a YAML-safe double-quoted scalar (handles spaces and colons)."""
    return json.dumps(value)


def _volume_mount(host: Path, container: str, *, readonly: bool = False) -> str:
    spec = f"{host}:{container}"
    if readonly:
        spec += ":ro"
    return f"              - {_yaml_double_quoted(spec)}"


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackupConfigError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise BackupConfigError(f"{name} must be positive, got {value}")
    return value


def load_host_backup_config() -> dict[str, int]:
    """Read validated DJINN_BACKUP_* overrides for compose generation."""
    return {
        "backup_interval_seconds": _env_positive_int(
            ENV_BACKUP_INTERVAL, DEFAULT_BACKUP_INTERVAL_SECONDS
        ),
        "retention_hourly": _env_positive_int(
            ENV_RETENTION_HOURLY, DEFAULT_RETENTION_HOURLY
        ),
        "retention_daily": _env_positive_int(
            ENV_RETENTION_DAILY, DEFAULT_RETENTION_DAILY
        ),
        "prune_interval_seconds": _env_positive_int(
            ENV_PRUNE_INTERVAL, DEFAULT_PRUNE_INTERVAL_SECONDS
        ),
    }


def paths(base_path: Path) -> dict[str, Path]:
    """Resolve every host path for a djinn installation."""
    base = base_path.expanduser().resolve()
    return {
        "base": base,
        "artifacts_root": base / "artifacts",
        "browser_tmp_root": base / "browser-tmp",
        "backup_root": base / "backups",
        "repo": base / "backups" / "restic-repo",
        "password_file": base / "backups" / "restic-password",
        "compose_dir": base / "compose",
        "compose_file": base / "compose" / "backup.yml",
    }


def _validate_password_file(password: Path) -> None:
    if password.is_symlink():
        raise BackupConfigError(f"restic password path must not be a symlink: {password}")
    if not password.is_file():
        raise BackupConfigError(f"restic password must be a regular file: {password}")
    if password.stat().st_mode & 0o077:
        raise BackupConfigError(
            f"restic password file must be mode 600 or tighter: {password}"
        )


def _create_password_atomic(password: Path) -> None:
    """Create the restic password file atomically at mode 600 (race-tolerant)."""
    if password.is_symlink():
        raise BackupConfigError(f"restic password path must not be a symlink: {password}")
    if password.exists():
        if not password.is_file():
            raise BackupConfigError(f"restic password must be a regular file: {password}")
        _validate_password_file(password)
        return

    content = secrets.token_hex(32) + "\n"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(
            password,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(content)
    except FileExistsError:
        if password.is_symlink():
            raise BackupConfigError(f"restic password path must not be a symlink: {password}")
        if not password.is_file():
            raise BackupConfigError(f"restic password must be a regular file: {password}")
        _validate_password_file(password)
    finally:
        if fd is not None:
            os.close(fd)


def ensure_layout(base_path: Path) -> dict[str, Path]:
    """Create backup directories and a restic password file if missing."""
    p = paths(base_path)
    for key in ("artifacts_root", "browser_tmp_root", "backup_root", "repo", "compose_dir"):
        try:
            p[key].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupConfigError(
                f"cannot create {key.replace('_', ' ')} {p[key]}: {exc}"
            ) from exc
    try:
        _create_password_atomic(p["password_file"])
    except OSError as exc:
        raise BackupConfigError(
            f"cannot create restic password file {p['password_file']}: {exc}"
        ) from exc
    return p


def render_compose_yaml(
    *,
    identity: BackupIdentity,
    artifacts_root: Path,
    browser_tmp_root: Path,
    backup_repo: Path,
    password_file: Path,
    backup_interval_seconds: int = DEFAULT_BACKUP_INTERVAL_SECONDS,
    retention_hourly: int = DEFAULT_RETENTION_HOURLY,
    retention_daily: int = DEFAULT_RETENTION_DAILY,
    prune_interval_seconds: int = DEFAULT_PRUNE_INTERVAL_SECONDS,
) -> str:
    """Render the singleton backup compose overlay written under DJINN_HOME."""
    for label, path in (
        ("artifacts root", artifacts_root),
        ("browser-tmp root", browser_tmp_root),
        ("backup repo", backup_repo),
        ("restic password file", password_file),
    ):
        if not path.exists():
            raise BackupConfigError(f"{label} does not exist: {path}")

    volumes = "\n".join(
        [
            _volume_mount(artifacts_root, SOURCE_ARTIFACTS_MOUNT, readonly=True),
            _volume_mount(browser_tmp_root, SOURCE_BROWSER_TMP_MOUNT, readonly=True),
            _volume_mount(backup_repo, REPO_MOUNT),
            _volume_mount(password_file, PASSWORD_MOUNT, readonly=True),
        ]
    )

    return textwrap.dedent(
        f"""\
        # Generated by brassbottle backup — do not hand-edit.
        services:
          {SERVICE_NAME}:
            build:
              context: .
              dockerfile: backup/Dockerfile
            image: {identity.image_tag}
            container_name: {identity.container_name}
            hostname: {identity.hostname}
            restart: unless-stopped
            environment:
              RESTIC_REPOSITORY: file:{REPO_MOUNT}
              RESTIC_PASSWORD_FILE: {PASSWORD_MOUNT}
              BACKUP_SOURCES: {SOURCE_ARTIFACTS_MOUNT} {SOURCE_BROWSER_TMP_MOUNT}
              BACKUP_INTERVAL_SECONDS: "{backup_interval_seconds}"
              RETENTION_HOURLY: "{retention_hourly}"
              RETENTION_DAILY: "{retention_daily}"
              PRUNE_INTERVAL_SECONDS: "{prune_interval_seconds}"
            volumes:
{volumes}
        """
    )


def write_compose_file(base_path: Path) -> Path:
    """Ensure layout and write the generated compose overlay."""
    p = ensure_layout(base_path)
    cfg = load_host_backup_config()
    identity = derive_identity(base_path)
    content = render_compose_yaml(
        identity=identity,
        artifacts_root=p["artifacts_root"],
        browser_tmp_root=p["browser_tmp_root"],
        backup_repo=p["repo"],
        password_file=p["password_file"],
        backup_interval_seconds=cfg["backup_interval_seconds"],
        retention_hourly=cfg["retention_hourly"],
        retention_daily=cfg["retention_daily"],
        prune_interval_seconds=cfg["prune_interval_seconds"],
    )
    compose_path = p["compose_file"]
    try:
        compose_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise BackupConfigError(
            f"cannot write compose overlay {compose_path}: {exc}"
        ) from exc
    return compose_path


def bottle_compose_must_not_reference_backup(compose_text: str) -> None:
    """Guardrail: bottle static compose must not mount backup paths."""
    forbidden = ("/backups", "restic-password", "restic-repo", IDENTITY_PREFIX)
    for token in forbidden:
        if token in compose_text:
            raise BackupConfigError(
                f"bottle compose must not reference backup internals ({token})"
            )
