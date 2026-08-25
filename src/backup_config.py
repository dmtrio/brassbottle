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
from pathlib import Path

# Singleton identity prefix — suffix is derived per resolved DJINN_HOME.
IDENTITY_PREFIX = "djinn-backup"
SERVICE_NAME = "backup"
BROWSER_SERVICE_NAME = "backrest-browser"
IDENTITY_SUFFIX_LENGTH = 8

# Pinned Backrest release — never use :latest (official GHCR package).
BACKREST_IMAGE = "ghcr.io/garethgeorge/backrest:v1.14.1"
BACKREST_CONTAINER_PORT = 9898
BACKREST_DEFAULT_HOST = "127.0.0.1"
BACKREST_DEFAULT_PORT = 9898
BACKREST_CONFIG_VERSION = 6  # migrations.CurrentVersion in Backrest v1.14.1

# In-container mount targets for the read-only browser UI service.
BROWSER_REPO_MOUNT = "/repo"
BROWSER_PASSWORD_MOUNT = "/run/secrets/restic-password"
BROWSER_CONFIG_MOUNT = "/config"
BROWSER_DATA_MOUNT = "/data"
BROWSER_CACHE_MOUNT = "/cache"

# Stable repo id seeded into Backrest (browse-only; djinn owns backup policy).
BROWSER_REPO_ID = "djinn-artifacts"

# Host-side overrides for the browser UI bind address.
ENV_BROWSER_PORT = "DJINN_BACKUP_BROWSER_PORT"
ENV_BROWSER_HOST = "DJINN_BACKUP_BROWSER_HOST"


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


def browser_container_name(identity: BackupIdentity) -> str:
    """Container name for the Backrest browse-only service."""
    return f"{identity.container_name}-browser"

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
    return f"      - {_yaml_double_quoted(spec)}"


def _compose_volume_lines(*mounts: tuple[Path, str, bool]) -> str:
    """Render indented compose volume list entries (readonly flag per mount)."""
    lines: list[str] = []
    for host, container, readonly in mounts:
        lines.append(_volume_mount(host, container, readonly=readonly))
    return "\n".join(lines)


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
    browser_root = base / "backups" / "browser"
    return {
        "base": base,
        "artifacts_root": base / "artifacts",
        "browser_tmp_root": base / "browser-tmp",
        "backup_root": base / "backups",
        "repo": base / "backups" / "restic-repo",
        "password_file": base / "backups" / "restic-password",
        "browser_root": browser_root,
        "browser_config_dir": browser_root / "config",
        "browser_config_file": browser_root / "config" / "config.json",
        "browser_data_dir": browser_root / "data",
        "browser_cache_dir": browser_root / "cache",
        "browser_import_doc": browser_root / "IMPORT.md",
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


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackupConfigError(f"{name} must be an integer port (1-65535), got {raw!r}") from exc
    if value < 1 or value > 65535:
        raise BackupConfigError(f"{name} port {value} out of range (1-65535)")
    return value


def load_browser_bind_config() -> dict[str, str | int]:
    """Read validated host bind settings for the Backrest UI."""
    host = os.environ.get(ENV_BROWSER_HOST, BACKREST_DEFAULT_HOST).strip()
    if not host:
        raise BackupConfigError(f"{ENV_BROWSER_HOST} must not be empty")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise BackupConfigError(
            f"{ENV_BROWSER_HOST} must be a loopback address "
            f"(127.0.0.1, localhost, or ::1), got {host!r}"
        )
    return {
        "host": host,
        "port": _env_port(ENV_BROWSER_PORT, BACKREST_DEFAULT_PORT),
    }


def ensure_layout(base_path: Path) -> dict[str, Path]:
    """Create backup directories and a restic password file if missing."""
    p = paths(base_path)
    for key in (
        "artifacts_root",
        "browser_tmp_root",
        "backup_root",
        "repo",
        "browser_config_dir",
        "browser_data_dir",
        "browser_cache_dir",
        "compose_dir",
    ):
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


def _render_browser_service_yaml(
    *,
    identity: BackupIdentity,
    backup_repo: Path,
    password_file: Path,
    browser_config_dir: Path,
    browser_data_dir: Path,
    browser_cache_dir: Path,
    browser_bind_host: str,
    browser_bind_port: int,
) -> str:
    """Render the Backrest browse-only service (read-only repo, no source mounts)."""
    for label, path in (
        ("backup repo", backup_repo),
        ("restic password file", password_file),
        ("backrest config dir", browser_config_dir),
        ("backrest data dir", browser_data_dir),
        ("backrest cache dir", browser_cache_dir),
    ):
        if not path.exists():
            raise BackupConfigError(f"{label} does not exist: {path}")

    volumes = _compose_volume_lines(
        (backup_repo, BROWSER_REPO_MOUNT, True),
        (password_file, BROWSER_PASSWORD_MOUNT, True),
        (browser_config_dir, BROWSER_CONFIG_MOUNT, False),
        (browser_data_dir, BROWSER_DATA_MOUNT, False),
        (browser_cache_dir, BROWSER_CACHE_MOUNT, False),
    )
    browser_name = browser_container_name(identity)
    port_map = f"{browser_bind_host}:{browser_bind_port}:{BACKREST_CONTAINER_PORT}"

    return f"""  {BROWSER_SERVICE_NAME}:
    image: {BACKREST_IMAGE}
    container_name: {browser_name}
    hostname: {browser_name}
    restart: unless-stopped
    environment:
      BACKREST_PORT: 0.0.0.0:{BACKREST_CONTAINER_PORT}
      BACKREST_CONFIG: {BROWSER_CONFIG_MOUNT}/config.json
      BACKREST_DATA: {BROWSER_DATA_MOUNT}
      XDG_CACHE_HOME: {BROWSER_CACHE_MOUNT}
    ports:
      - {_yaml_double_quoted(port_map)}
    volumes:
{volumes}
"""


def render_compose_yaml(
    *,
    identity: BackupIdentity,
    artifacts_root: Path,
    browser_tmp_root: Path,
    backup_repo: Path,
    password_file: Path,
    browser_config_dir: Path,
    browser_data_dir: Path,
    browser_cache_dir: Path,
    browser_bind_host: str,
    browser_bind_port: int,
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

    volumes = _compose_volume_lines(
        (artifacts_root, SOURCE_ARTIFACTS_MOUNT, True),
        (browser_tmp_root, SOURCE_BROWSER_TMP_MOUNT, True),
        (backup_repo, REPO_MOUNT, False),
        (password_file, PASSWORD_MOUNT, True),
    )

    browser_service = _render_browser_service_yaml(
        identity=identity,
        backup_repo=backup_repo,
        password_file=password_file,
        browser_config_dir=browser_config_dir,
        browser_data_dir=browser_data_dir,
        browser_cache_dir=browser_cache_dir,
        browser_bind_host=browser_bind_host,
        browser_bind_port=browser_bind_port,
    )

    return f"""# Generated by brassbottle backup — do not hand-edit.
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
{browser_service}"""


def write_compose_file(base_path: Path) -> Path:
    """Ensure layout and write the generated compose overlay."""
    p = ensure_layout(base_path)
    cfg = load_host_backup_config()
    browser_bind = load_browser_bind_config()
    identity = derive_identity(base_path)
    content = render_compose_yaml(
        identity=identity,
        artifacts_root=p["artifacts_root"],
        browser_tmp_root=p["browser_tmp_root"],
        backup_repo=p["repo"],
        password_file=p["password_file"],
        browser_config_dir=p["browser_config_dir"],
        browser_data_dir=p["browser_data_dir"],
        browser_cache_dir=p["browser_cache_dir"],
        browser_bind_host=str(browser_bind["host"]),
        browser_bind_port=int(browser_bind["port"]),
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


def _browser_service_block(compose_text: str) -> str:
    """Return the backrest-browser service section (always the last service)."""
    marker = f"{BROWSER_SERVICE_NAME}:"
    if marker not in compose_text:
        raise BackupConfigError(f"compose missing {BROWSER_SERVICE_NAME} service")
    return compose_text[compose_text.rindex(marker) :]


def _service_env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {str(key): str(value) for key, value in env.items()}
    if isinstance(env, list):
        parsed: dict[str, str] = {}
        for item in env:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
        return parsed
    return {}


def _service_volume_specs(service: dict) -> list[str]:
    volumes = service.get("volumes") or []
    if not isinstance(volumes, list):
        raise BackupConfigError("compose service volumes must be a list")
    return [str(item) for item in volumes]


def validate_generated_compose_structure(data: dict) -> None:
    """Raise when generated compose violates browse-only / scheduler isolation."""
    services = data.get("services")
    if not isinstance(services, dict):
        raise BackupConfigError("compose must define a services mapping")

    backup = services.get(SERVICE_NAME)
    browser = services.get(BROWSER_SERVICE_NAME)
    if not isinstance(backup, dict):
        raise BackupConfigError(f"compose missing {SERVICE_NAME} service")
    if not isinstance(browser, dict):
        raise BackupConfigError(f"compose missing {BROWSER_SERVICE_NAME} service")

    backup_env = _service_env(backup)
    browser_env = _service_env(browser)
    for name in (
        "BACKUP_SOURCES",
        "BACKUP_INTERVAL_SECONDS",
        "RETENTION_HOURLY",
        "RETENTION_DAILY",
        "PRUNE_INTERVAL_SECONDS",
    ):
        if name in browser_env:
            raise BackupConfigError(
                f"{BROWSER_SERVICE_NAME} must not define scheduler env {name}"
            )

    browser_volumes = _service_volume_specs(browser)
    backup_volumes = _service_volume_specs(backup)
    browser_repo_mounts = [spec for spec in browser_volumes if f":{BROWSER_REPO_MOUNT}" in spec]
    backup_repo_mounts = [spec for spec in backup_volumes if f":{REPO_MOUNT}" in spec]
    if len(browser_repo_mounts) != 1:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} must mount the repository once")
    if not browser_repo_mounts[0].endswith(":ro"):
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} repository mount must be read-only")
    if len(backup_repo_mounts) != 1:
        raise BackupConfigError(f"{SERVICE_NAME} must mount the repository once")
    if backup_repo_mounts[0].endswith(":ro"):
        raise BackupConfigError(f"{SERVICE_NAME} repository mount must be read-write")

    for token in (SOURCE_ARTIFACTS_MOUNT, SOURCE_BROWSER_TMP_MOUNT):
        if any(token in spec for spec in browser_volumes):
            raise BackupConfigError(
                f"{BROWSER_SERVICE_NAME} must not mount live sources ({token})"
            )

    ports = browser.get("ports") or []
    if not isinstance(ports, list) or not ports:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} must publish a localhost port")
    port_spec = str(ports[0])
    if not port_spec.startswith(("127.0.0.1:", "localhost:", "[::1]:")):
        raise BackupConfigError(
            f"{BROWSER_SERVICE_NAME} must bind to loopback, got {port_spec!r}"
        )

    image = str(browser.get("image", ""))
    if image != BACKREST_IMAGE:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} must pin image {BACKREST_IMAGE}")


def browser_compose_must_not_mount_sources_or_scheduler(compose_text: str) -> None:
    """Guardrail: Backrest must not see live sources or djinn scheduler env."""
    browser_block = _browser_service_block(compose_text)
    for token in (
        SOURCE_ARTIFACTS_MOUNT,
        SOURCE_BROWSER_TMP_MOUNT,
        "BACKUP_SOURCES",
        "BACKUP_INTERVAL_SECONDS",
        "RETENTION_HOURLY",
        "RETENTION_DAILY",
        "PRUNE_INTERVAL_SECONDS",
    ):
        if token in browser_block:
            raise BackupConfigError(
                f"{BROWSER_SERVICE_NAME} must not reference djinn scheduler or sources ({token})"
            )
    if BROWSER_REPO_MOUNT not in browser_block:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} must mount the repository")
    if ":ro" not in browser_block:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} repository mount must be read-only")
    if BACKREST_IMAGE not in browser_block:
        raise BackupConfigError(f"{BROWSER_SERVICE_NAME} must pin image {BACKREST_IMAGE}")


def browser_url(host: str, port: int) -> str:
    """Return the operator-facing Backrest UI URL."""
    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{bracketed}:{port}/"


def bottle_compose_must_not_reference_backup(compose_text: str) -> None:
    """Guardrail: bottle static compose must not mount backup paths."""
    forbidden = ("/backups", "restic-password", "restic-repo", IDENTITY_PREFIX)
    for token in forbidden:
        if token in compose_text:
            raise BackupConfigError(
                f"bottle compose must not reference backup internals ({token})"
            )
