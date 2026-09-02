#!/usr/bin/env python3
"""Host-side registry of running bottles selectable from djinn-jump.

The jump container deliberately has no Docker socket. This module asks Docker
on the host for only running containers bearing the manifest-derived
``djinn.remote.jump=true`` label, validates their names, and atomically writes
a small registry into the directory mounted read-only by the jump.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import jump_config  # noqa: E402

CONTAINER_RE = re.compile(r"^djinn-[A-Za-z0-9_-]+$")


class JumpRegistryError(Exception):
    """The host could not produce a trustworthy jump registry."""


def running_bottles(base_path: Path) -> list[str]:
    """Return sorted, validated names of running jump-enabled bottles."""
    started = time.monotonic()
    scope = jump_config.derive_identity(base_path).suffix
    command = [
        "docker", "ps", "--filter", "label=djinn.remote.jump=true",
        "--filter", f"label=djinn.jump.scope={scope}",
        "--format", "{{.Names}}",
    ]
    print("jump registry query begin filters=jump-enabled,installation-scope")
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        print("jump registry query error reason=docker-not-found", file=sys.stderr)
        raise JumpRegistryError("docker not found") from exc
    if result.returncode != 0:
        print(
            "jump registry query error "
            f"exit_code={result.returncode} duration={time.monotonic() - started:.2f}s",
            file=sys.stderr,
        )
        raise JumpRegistryError(f"docker ps failed with exit code {result.returncode}")

    raw_names = (result.stdout or "").splitlines()
    names: list[str] = []
    for raw in raw_names:
        name = raw.strip()
        if not name:
            continue
        if not CONTAINER_RE.fullmatch(name):
            print("jump registry query error reason=invalid-container-name", file=sys.stderr)
            raise JumpRegistryError("docker returned an invalid djinn container name")
        names.append(name)
    result_names = sorted(set(names))
    print(
        "jump registry query ok "
        f"duration={time.monotonic() - started:.2f}s returned={len(raw_names)} "
        f"accepted={len(result_names)}"
    )
    return result_names


def write_registry(base_path: Path, names: list[str]) -> Path:
    """Atomically replace the mounted registry after validating all entries."""
    if any(not CONTAINER_RE.fullmatch(name) for name in names):
        raise JumpRegistryError("refusing to write an invalid djinn container name")
    paths = jump_config.ensure_layout(base_path)
    target = paths["registry_file"]
    body = "".join(f"{name}\n" for name in sorted(set(names)))
    temp_name = ""
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".bottles-", dir=paths["registry_dir"])
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        print("jump registry write error reason=filesystem", file=sys.stderr)
        raise JumpRegistryError(f"cannot write {target}: {exc}") from exc
    print(
        f"jump registry write ok path={target} entries={len(set(names))} "
        f"bytes={len(body.encode())}"
    )
    return target


def refresh(base_path: Path) -> Path:
    """Query Docker then publish the replacement registry."""
    started = time.monotonic()
    names = running_bottles(base_path)
    path = write_registry(base_path, names)
    print(
        f"jump registry refresh ok duration={time.monotonic() - started:.2f}s "
        f"entries={len(names)}"
    )
    return path
