#!/usr/bin/env python3
"""build_inputs.py: record what `npm run build` read, so a stale dist/ can be told.

    python3 scripts/build_inputs.py write     (the last step of `npm run build`)

Writes dist/.build-inputs.json: the path, size and sha256 of every build input.
tests/admin_ui_playwright.py compares the current inputs with it and refuses to
run against a dist/ they no longer match: a file added, deleted or changed
since the build. Nothing depends on mtimes, so touching a file, or a directory
gaining a scratch file, never refuses.

INPUTS is the one list of what the build reads. Under src/ and scripts/ the
build never reads dotfiles, editor swap/backup files or bytecode, so those are
left out; public/ is copied into dist/ whole, so everything in it is an input.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DIST = "admin/ui/dist"
MANIFEST = f"{DIST}/.build-inputs.json"

# Relative to the repo root. A plain entry is a file, or a directory taken with
# everything under it; an entry with a wildcard is the files it matches.
INPUTS = (
    "admin/ui/src",
    "admin/ui/public",
    "admin/ui/scripts",
    "admin/ui/index.html",
    "admin/ui/vite.config.ts",
    "admin/ui/package.json",
    "admin/ui/package-lock.json",
    "admin/ui/tsconfig*.json",
    "admin/contract/*.schema.json",
)
UNFILTERED = ("admin/ui/public",)   # trees where every file is an input


def is_noise(name: str) -> bool:
    """A file or directory name the build never reads."""
    return (name.startswith(".") or name.endswith("~") or name == "4913"
            or (name.startswith("#") and name.endswith("#"))
            or name.endswith((".swp", ".swo", ".swx", ".pyc")) or name == "__pycache__")


def input_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for entry in INPUTS:
        if "*" in entry:
            found.update(path for path in root.glob(entry) if path.is_file())
            continue
        path = root / entry
        if path.is_file():
            found.add(path)
        elif path.is_dir():
            for child in path.rglob("*"):
                parts = child.relative_to(path).parts
                if child.is_file() and (entry in UNFILTERED or not any(is_noise(part) for part in parts)):
                    found.add(child)
    return sorted(found)


def collect(root: Path = ROOT) -> dict[str, dict]:
    """{repo-relative path: {"size", "sha256"}} for every current build input."""
    inputs = {}
    for path in input_files(root):
        data = path.read_bytes()
        inputs[path.relative_to(root).as_posix()] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return inputs


def write_manifest(root: Path = ROOT) -> dict[str, dict]:
    started = time.monotonic()
    inputs = collect(root)
    target = root / MANIFEST
    if not target.parent.is_dir():
        raise FileNotFoundError(f"{DIST} does not exist: the build has not produced it")
    target.write_text(json.dumps({"version": 1, "inputs": inputs}, indent=1, sort_keys=True) + "\n")
    print(f"[build-inputs] stage=write files={len(inputs)} bytes={sum(f['size'] for f in inputs.values())} "
          f"duration={time.monotonic() - started:.2f}s -> {MANIFEST}", file=sys.stderr)
    return inputs


def read_manifest(root: Path = ROOT) -> dict[str, dict] | None:
    """The recorded inputs, or None when the manifest is missing or unreadable."""
    try:
        recorded = json.loads((root / MANIFEST).read_text())["inputs"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return recorded if isinstance(recorded, dict) else None


def drift(root: Path, recorded: dict[str, dict]) -> tuple[list[str], list[str], list[str]]:
    """(added, deleted, changed) paths: the current build inputs against the recorded ones."""
    current = collect(root)
    return (sorted(current.keys() - recorded.keys()),
            sorted(recorded.keys() - current.keys()),
            sorted(path for path in current.keys() & recorded.keys() if current[path] != recorded[path]))


def main(argv: list[str]) -> int:
    if argv != ["write"]:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        write_manifest()
    except OSError as exc:
        print(f"[build-inputs] stage=write failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
