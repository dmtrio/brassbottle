"""Shared byte-level tree capture for config golden parity (stdlib only)."""

from __future__ import annotations

import os
from pathlib import Path


def capture_tree(root: Path, prefix: str) -> tuple[dict[str, bytes], dict[str, str], dict[str, str]]:
    """Walk *root*, returning (files, symlinks, modes) keyed by posix relpaths under *prefix*."""
    files: dict[str, bytes] = {}
    symlinks: dict[str, str] = {}
    modes: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        here = Path(dirpath)

        for dirname in list(dirnames):
            child = here / dirname
            if child.is_symlink():
                rel = (Path(prefix) / child.relative_to(root)).as_posix()
                symlinks[rel] = os.readlink(child)
                dirnames.remove(dirname)

        for filename in filenames:
            child = here / filename
            rel_s = (Path(prefix) / child.relative_to(root)).as_posix()
            if child.is_symlink():
                symlinks[rel_s] = os.readlink(child)
                continue
            if not child.is_file():
                continue
            files[rel_s] = child.read_bytes()
            mode = os.stat(child, follow_symlinks=False).st_mode & 0o777
            modes[rel_s] = format(mode, "04o")

    return files, symlinks, modes


def parse_map_file(path: Path, splitter) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        key, value = splitter(line)
        out[key] = value
    return out


def read_tree_golden(base: Path) -> dict:
    """Read a golden tree directory (home/workspace files + modes.txt + symlinks.txt)."""
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if rel in ("modes.txt", "symlinks.txt", "scenario"):
            continue
        files[rel] = path.read_bytes()

    modes = parse_map_file(base / "modes.txt", lambda line: line.rsplit(" ", 1))
    symlinks = parse_map_file(base / "symlinks.txt", lambda line: line.split("\t", 1))
    return {"files": files, "symlinks": symlinks, "modes": modes}


def write_tree_golden(base: Path, snapshot: dict) -> None:
    import shutil

    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)

    for rel, blob in sorted(snapshot["files"].items()):
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)

    modes_text = "\n".join(
        f"{rel} {mode}" for rel, mode in sorted(snapshot["modes"].items()))
    symlink_text = "\n".join(
        f"{rel}\t{target}" for rel, target in sorted(snapshot["symlinks"].items()))
    (base / "modes.txt").write_text(modes_text + ("\n" if modes_text else ""))
    (base / "symlinks.txt").write_text(symlink_text + ("\n" if symlink_text else ""))


def assert_snapshot_equal(name: str, golden: dict, live: dict) -> None:
    golden_files = set(golden["files"])
    live_files = set(live["files"])
    if golden_files != live_files:
        missing = sorted(golden_files - live_files)
        extra = sorted(live_files - golden_files)
        raise AssertionError(
            f"{name}: generated file set changed; "
            f"missing={missing or '[]'} extra={extra or '[]'}")
    for rel in sorted(golden_files):
        if live["files"][rel] != golden["files"][rel]:
            raise AssertionError(f"{name}: file bytes changed: {rel}")
    if live["modes"] != golden["modes"]:
        raise AssertionError(f"{name}: file mode map changed")
    if live["symlinks"] != golden["symlinks"]:
        raise AssertionError(f"{name}: symlink targets changed")
