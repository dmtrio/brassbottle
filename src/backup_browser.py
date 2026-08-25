#!/usr/bin/env python3
"""backup_browser.py — Backrest browse-only UI seeding and helpers.

Seeds an existing djinn restic repository into Backrest without plans or
scheduler access. Never overwrites an existing Backrest config. Stdlib only.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

from backup_config import (
    BACKREST_CONFIG_VERSION,
    BROWSER_PASSWORD_MOUNT,
    BROWSER_REPO_ID,
    BROWSER_REPO_MOUNT,
    BackupConfigError,
    derive_identity,
    paths,
)

RESTIC_REPO_CONFIG_JSON = "config.json"

IMPORT_DOC = textwrap.dedent(
    """\
    # Backrest first-run import

    Automatic seeding did not create `config/config.json` (the repository may
    not be initialized yet, or the restic config export could not be read).
    Backrest will start with an empty configuration — **do not create backup
    plans**; djinn's Python scheduler owns backup policy and retention.

    ## When the restic repository exists

    1. Run `./djinn backup start` and wait for at least one snapshot.
    2. Run `./djinn backup browser start` again (seeding is retried when
       `config/config.json` is still absent).
    3. Open the UI URL from `./djinn backup browser url`.

    ## Manual import (if seeding keeps skipping)

    In Backrest → Add Repo → connect to an **existing** repository:

    | Field | Value |
    |-------|-------|
    | Name | `djinn-artifacts` (or any stable id) |
    | URI | `/repo` |
    | Environment | `RESTIC_PASSWORD_FILE=/run/secrets/restic-password` |
    | Flags | `--no-lock` |
    | Prune / check / forget schedules | **Disabled** |

    Do **not** add backup plans. Use the UI only to browse snapshots and restore
    files. Repository maintenance stays with `./djinn backup` (scheduled forget,
    prune, and check).

    After import, click **Index Snapshots** in the repository view.
    """
)


class BrowserSeedError(Exception):
    """Backrest config seeding failed."""


def _disabled_schedule() -> dict[str, bool]:
    return {"disabled": True}


def build_seed_config(*, instance: str, repo_guid: str) -> dict:
    """Minimal Backrest config: one read-only repo, zero plans, auth disabled."""
    return {
        "modno": 0,
        "version": BACKREST_CONFIG_VERSION,
        "instance": instance,
        "repos": [
            {
                "id": BROWSER_REPO_ID,
                "uri": BROWSER_REPO_MOUNT,
                "guid": repo_guid,
                "password": "",
                "env": [
                    f"RESTIC_PASSWORD_FILE={BROWSER_PASSWORD_MOUNT}",
                    f"RESTIC_REPOSITORY=file:{BROWSER_REPO_MOUNT}",
                ],
                "flags": ["--no-lock"],
                "prunePolicy": {"schedule": _disabled_schedule()},
                "checkPolicy": {"schedule": _disabled_schedule()},
                "autoUnlock": False,
                "autoInitialize": False,
            }
        ],
        "plans": [],
        "auth": {"disabled": True},
    }


def validate_restic_repo_id(value: object) -> str | None:
    """Return a 64-char lowercase hex restic repository id, or None."""
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return None
    if len(decoded) != 32:
        return None
    return value.lower()


def parse_restic_config_json(text: str) -> str | None:
    """Parse restic `cat config --json` output and return the repository id."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return validate_restic_repo_id(data.get("id"))


def read_restic_repo_guid(repo: Path, password_file: Path | None = None) -> str | None:
    """Return the 64-char restic repository id from the exported config JSON."""
    del password_file  # password is only used when the backup container exports config
    config_path = repo / RESTIC_REPO_CONFIG_JSON
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_restic_config_json(text)


def repo_looks_initialized(repo: Path) -> bool:
    """True when the restic repo directory contains a config blob."""
    config_path = repo / "config"
    return config_path.is_file() and config_path.stat().st_size > 0


def write_import_doc(import_path: Path) -> None:
    import_path.parent.mkdir(parents=True, exist_ok=True)
    if import_path.exists():
        return
    import_path.write_text(IMPORT_DOC, encoding="utf-8")


def seed_backrest_config(base_path: Path) -> str:
    """Seed Backrest config when absent. Returns a short boundary status token."""
    p = paths(base_path)
    config_path = p["browser_config_file"]
    if config_path.is_file():
        return "skipped-existing-config"

    if not repo_looks_initialized(p["repo"]):
        write_import_doc(p["browser_import_doc"])
        return "skipped-repo-not-initialized"

    guid = read_restic_repo_guid(p["repo"], p["password_file"])
    if guid is None:
        write_import_doc(p["browser_import_doc"])
        return "skipped-guid-unavailable"

    identity = derive_identity(base_path)
    instance = f"djinn-{identity.suffix}"
    payload = build_seed_config(instance=instance, repo_guid=guid)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2) + "\n"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(
            config_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(content)
    except FileExistsError:
        return "skipped-existing-config"
    except OSError as exc:
        raise BrowserSeedError(f"cannot write Backrest seed config {config_path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)

    if p["browser_import_doc"].is_file():
        try:
            p["browser_import_doc"].unlink()
        except OSError:
            pass
    return "seeded"


def validate_seed_config(config_path: Path) -> None:
    """Raise when a seed config violates browse-only policy."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupConfigError(f"invalid Backrest config {config_path}: {exc}") from exc
    if data.get("plans"):
        raise BackupConfigError("Backrest config must not define backup plans")
    repos = data.get("repos") or []
    for repo in repos:
        flags = repo.get("flags") or []
        if "--no-lock" not in flags:
            raise BackupConfigError("Backrest repo must include --no-lock flag")
        if repo.get("autoInitialize"):
            raise BackupConfigError("Backrest repo must not auto-initialize")
