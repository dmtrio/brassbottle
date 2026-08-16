#!/usr/bin/env python3
"""Merge the declared multi-repo list into /workspace/dev.code-workspace.

up.sh used to write the multi-root workspace file once with a heredoc guarded
by if-not-exists, so manifest edits on a live container never updated it.
This helper replaces that: it MERGES REPO_NAMES (space-separated repo dir
names) into the file idempotently — adding any missing repos/<n> folders,
never deleting worktree or hand-added entries, and always leaving /artifacts
last. A freshly-created file also pins the integrated terminal's default cwd
to /workspace/repos, so new containers don't open shells in /artifacts.

Runs in-container as:
  python3 /usr/local/lib/djinn/code_workspace.py /workspace/dev.code-workspace
with REPO_NAMES in the environment. Stdlib only; atomic writes (tmp + replace).
Parse failures refuse to touch the file — an agent may have hand-edited it.
"""

import json
import os
import sys
from pathlib import Path


def _dump_json(obj):
    # jq-style output: 2-space indent, raw UTF-8, trailing newline.
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _write_atomic(path, text):
    """Write via tmp + os.replace so a crash never leaves a half-written file."""
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def parse_repo_names(env):
    """Space-separated REPO_NAMES; empty tokens ignored (names validated upstream)."""
    return [t for t in (env.get("REPO_NAMES") or "").split() if t]


def default_document(names):
    folders = [{"path": f"repos/{n}", "name": n} for n in sorted(names)]
    folders.append({"path": "/artifacts", "name": "artifacts"})
    # New integrated terminals default to the workspace's first folder unless
    # told otherwise; with /artifacts last in the list that mostly holds, but
    # VS Code can still land a fresh terminal in whichever folder last had
    # focus. Pin it explicitly so a new container always opens shells in
    # /workspace/repos, never /artifacts.
    return {"folders": folders, "settings": {"terminal.integrated.cwd": "/workspace/repos"}}


def merge_folders(existing, names):
    """Rebuild folders as: repos/* (sorted by path) + others (order kept) + /artifacts last.

    Existing repos/* entries are kept verbatim (unknown keys survive); a declared
    name missing by path gets a fresh {"path","name"} entry. Nothing is deleted.
    """
    repo_entries = []
    other_entries = []
    artifacts_entries = []
    seen_repo_paths = set()

    for entry in existing:
        if not isinstance(entry, dict):
            other_entries.append(entry)
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.startswith("repos/"):
            repo_entries.append(entry)
            seen_repo_paths.add(path)
        elif path == "/artifacts":
            artifacts_entries.append(entry)
        else:
            other_entries.append(entry)

    for n in names:
        p = f"repos/{n}"
        if p not in seen_repo_paths:
            repo_entries.append({"path": p, "name": n})
            seen_repo_paths.add(p)

    repo_entries.sort(key=lambda e: e["path"])
    return repo_entries + other_entries + artifacts_entries


def sync_workspace(path, names):
    """Create or merge path for the given repo names. Returns None on success,
    or an error string (caller prints to stderr and exits 1) when the existing
    file must not be touched."""
    path = Path(path)
    if not (path.is_file() and path.stat().st_size > 0):
        _write_atomic(path, _dump_json(default_document(names)))
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return f"{path} is not valid JSON: {e}"

    if not isinstance(data, dict):
        return f"{path}: expected a JSON object at the top level"

    folders = data.get("folders")
    if not isinstance(folders, list):
        return f"{path}: 'folders' is missing or not a list"

    data["folders"] = merge_folders(folders, names)
    _write_atomic(path, _dump_json(data))
    return None


def main(argv, env):
    if len(argv) != 1:
        print("Usage: code_workspace.py <path-to-dev.code-workspace>", file=sys.stderr)
        return 1
    err = sync_workspace(argv[0], parse_repo_names(env))
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
