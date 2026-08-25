#!/usr/bin/env python3
"""backup_restore.py — restore-target validation (never overwrite live artifact data)."""

from __future__ import annotations

from pathlib import Path


class RestoreTargetError(Exception):
    """Unsafe or invalid restore target."""


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _conflicts_with_forbidden(resolved: Path, forbidden: Path) -> bool:
    forbidden_resolved = _resolve(forbidden)
    if resolved == forbidden_resolved:
        return True
    try:
        resolved.relative_to(forbidden_resolved)
    except ValueError:
        pass
    else:
        return True
    try:
        forbidden_resolved.relative_to(resolved)
    except ValueError:
        pass
    else:
        return True
    return False


def validate_restore_target(
    target: str | Path,
    *,
    artifacts_root: Path,
    browser_tmp_root: Path,
    backup_root: Path,
    repo: Path,
    password_file: Path,
    compose_dir: Path,
    compose_file: Path,
) -> Path:
    """Require an explicit restore directory outside live and backup internals."""
    if not target or not str(target).strip():
        raise RestoreTargetError("restore requires an explicit --target path")

    resolved = _resolve(Path(target))

    if resolved.exists() and not resolved.is_dir():
        raise RestoreTargetError(
            f"restore target must be a directory, not an existing file: {resolved}"
        )

    forbidden_roots = [
        artifacts_root,
        browser_tmp_root,
        backup_root,
        repo,
        password_file,
        compose_dir,
        compose_file,
    ]

    for forbidden in forbidden_roots:
        if _conflicts_with_forbidden(resolved, forbidden):
            label = forbidden.name if forbidden.name else str(forbidden)
            raise RestoreTargetError(
                f"restore target must not overlap protected path ({label}): {resolved}"
            )

    return resolved
