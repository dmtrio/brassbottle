"""Shared test kit for per-agent wiring contract tests (stdlib only).

Portable unit API for third-party agent authors:
- Put ``test_wiring.py`` beside your ``agent.yml`` under ``agents/<name>/``.
- ``import agent_test_kit`` — on import the repo's ``src/`` and ``tests/`` dirs
  are prepended to ``sys.path`` (resolved from this file), so the module works
  both from the repo-wide suite and from ``cd agents/<name> && python3 -m unittest discover``.
- ``load_descriptor(name)`` parses the real ``agent.yml`` via ``yq``.
- ``wire(name, ...)`` runs the REAL derive → build-payload → run pipeline with
  the canonical scenario below; assert on invariants in test code, rendered
  bytes in ``golden/`` via ``assert_matches_golden()``.
- Adjacent tests passing = the drop-in install contract for a new agent folder.

Canonical scenario (``wire()`` defaults):
- Agent-bound plugin ``test-agent-bound`` with remote ``remote-srv`` (url
  ``https://example.test/mcp``, ``Authorization: Bearer ${TEST_TOKEN}``) and
  local ``local-srv`` (``bridge --stdio``), plus local-only plugin
  ``test-local`` (``plugin-local`` stdio bridge).
- Synthetic manifest enables the target agent (+ optional extras), declares
  ``TEST_TOKEN`` → ``TEST_SECRET`` for MCP-capable agents, scratch layout
  ``home/`` + ``workspace/repos/``.

SCENARIO_VERSION contract:
- ``SCENARIO_VERSION`` is API for every adjacent golden, including third-party
  agent dirs. Changing ANY fixture detail (server names, urls, slots, plugin
  spec, scratch layout) requires bumping this integer and regenerating every
  ``agents/*/golden/`` in the same commit.
- Capture writes ``golden/scenario`` (single line, the integer). Verification
  reads the stamp first; mismatch or a missing stamp fails with a distinct
  message naming both versions and pointing at ``GOLDEN_REGEN=1``.

Surface:
- ``wire(name, ...)`` → ``WireResult`` with ``home``, ``workspace``, ``read()``.
- ``assert_matches_golden(result, golden_dir, wire_fn=...)`` — byte-level tree
  parity for everything wiring produced under scratch dirs. ``GOLDEN_REGEN=1``
  requires ``wire_fn`` so regen always double-runs for determinism.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
AGENTS = REPO_ROOT / "agents"

for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import manifest  # noqa: E402
import wire_plugins  # noqa: E402
from golden_tree import (  # noqa: E402
    assert_snapshot_equal,
    capture_tree,
    read_tree_golden,
    write_tree_golden,
)

# Bump and regenerate every agents/*/golden/ when ANY canonical scenario detail changes.
SCENARIO_VERSION = 1

DERIVE_ENV = {
    "PRESENT_SECRET_VARS": "TEST_SECRET",
    "SECRETS_FILE": "/sec/secrets.env",
}

# Per-agent scratch roots under $TMPDIR/agent-test-kit/<name>/ (variant suffixes
# become sibling dirs like claude-no-remote). Fixed paths keep .claude.json
# path-embedding deterministic. Each agent owns its tree; wire() never deletes
# another agent's directory. Not parallel-safe within one agent.
AGENT_SCRATCH_ROOT = Path(tempfile.gettempdir()) / "agent-test-kit"

AGENT_BOUND_PLUGIN = "test-agent-bound"
LOCAL_PLUGIN = "test-local"

DEFAULT_REMOTE_SPEC = {
    "url": "https://example.test/mcp",
    "headers": {"Authorization": "Bearer ${TEST_TOKEN}"},
    "requires": ["TEST_TOKEN"],
}

DEFAULT_LOCAL_AGENT_SPEC = {
    "command": "bridge",
    "args": ["--stdio"],
    "requires": ["TEST_TOKEN"],
}

DEFAULT_LOCAL = {
    "install": "x",
    "mcp": {"plugin-local": {"command": "stdio-bridge", "args": []}},
}


def _agent_bound_plugin(*, remote=True, local=True):
    """One plugin doc so TEST_TOKEN is declared once for both agent-scoped servers."""
    mcp = {}
    if remote:
        mcp["remote-srv"] = dict(DEFAULT_REMOTE_SPEC)
    if local:
        mcp["local-srv"] = dict(DEFAULT_LOCAL_AGENT_SPEC)
    return {"install": "x", "secrets": {"TEST_TOKEN": {}}, "mcp": mcp}


def load_descriptor(agent_dir_name: str) -> dict:
    """Return the parsed agent.yml for agents/<agent_dir_name>/ via yq."""
    path = AGENTS / agent_dir_name / "agent.yml"
    if shutil.which("yq") is None:
        raise unittest.SkipTest("yq not available")
    proc = subprocess.run(
        ["yq", "-o=json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"yq failed on {path}: {proc.stderr}")
    return json.loads(proc.stdout)


def _load_all_agent_files() -> dict[str, dict]:
    if shutil.which("yq") is None:
        raise unittest.SkipTest("yq not available")
    return {name: load_descriptor(name)
            for name in sorted(p.name for p in AGENTS.iterdir() if p.is_dir())
            if (AGENTS / name / "agent.yml").is_file()}


def _identity_secrets(agent_secrets: str, remote_slots: str, literal_key_agents: str) -> tuple[str, int]:
    """Replicate up.sh ~447–464 IDENTITY_SECRETS assembly."""
    slots_set = f" {remote_slots} "
    literal_set = f" {literal_key_agents} "
    entries = []
    i = 0
    for line in agent_secrets.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        agent, slot, _source = parts
        if not agent:
            continue
        if f" {slot} " not in slots_set:
            continue
        if f" {agent} " not in literal_set:
            continue
        entries.append(f"{agent}:IDENTITY_KEY_{i}:{slot}")
        i += 1
    return " ".join(entries), i


def _scratch_path(agent_dir_name: str, *, remote_server, local_server,
                  extra_agents, key_env_values) -> Path:
    """Return the deterministic scratch dir for one wire() invocation."""
    suffix_parts = []
    if remote_server is False:
        suffix_parts.append("no-remote")
    if local_server is False:
        suffix_parts.append("no-local")
    if extra_agents:
        suffix_parts.extend(extra_agents)
    if key_env_values:
        suffix_parts.extend(sorted(key_env_values))
    if suffix_parts:
        return AGENT_SCRATCH_ROOT / f"{agent_dir_name}-{'-'.join(suffix_parts)}"
    return AGENT_SCRATCH_ROOT / agent_dir_name


def _build_run_env(derived: dict, ident: str, count: int,
                   key_env_values: dict | None) -> dict:
    """Build the container-side env for wire_plugins.run from test values only."""
    run_env = {
        "AGENTS_MCP_JSON": derived.get("AGENTS_MCP_JSON", ""),
        "PLUGIN_MCP_ENTRIES": derived.get("PLUGIN_MCP_ENTRIES", ""),
        "AGENT_SERVERS_JSON": derived.get("AGENT_SERVERS_JSON", ""),
        "AGENT_SECRETS": derived.get("AGENT_SECRETS", ""),
        "IDENTITY_SECRETS": ident,
    }
    if key_env_values:
        run_env.update(key_env_values)
    else:
        for i in range(count):
            run_env[f"IDENTITY_KEY_{i}"] = "TEST_SECRET"
    return run_env


class WireResult:
    """Outcome of wire(): temp home/workspace dirs plus derive/payload artifacts."""

    def __init__(self, tmp: tempfile.TemporaryDirectory, home: Path, workspace: Path,
                 derived: dict, payload: dict, run_env: dict):
        self._tmp = tmp
        self.home = home
        self.workspace = workspace
        self.derived = derived
        self.payload = payload
        self._run_env = run_env

    def read(self, relpath: str) -> str:
        """Read a file under home (~/) or workspace/ by relative path."""
        if relpath.startswith("workspace/"):
            path = self.workspace / relpath.removeprefix("workspace/")
        else:
            path = self.home / relpath
        return path.read_text()


def wire(agent_dir_name: str, *, remote_server=True, local_server=True,
         extra_agents=(), key_env_values=None):
    """Run derive → build-payload → run for one enabled agent (+ extras).

    remote_server / local_server may be False to omit that agent-scoped server.
    Pass a full plugin doc dict to replace the default bound plugin entirely.
    key_env_values supplies IDENTITY_KEY_n env for literal-key agents (defaults
    to TEST_SECRET for each derived slot).
    """
    agent_files = _load_all_agent_files()
    enabled = [agent_dir_name, *extra_agents]

    plugin_files = {}
    plugins = []
    want_bound = remote_server is not False or local_server is not False
    if want_bound:
        if isinstance(remote_server, dict) and "mcp" in remote_server:
            bound = remote_server
        else:
            bound = _agent_bound_plugin(
                remote=remote_server is not False,
                local=local_server is not False,
            )
        plugin_files[AGENT_BOUND_PLUGIN] = bound
        plugins.append(AGENT_BOUND_PLUGIN)
        plugin_files[LOCAL_PLUGIN] = DEFAULT_LOCAL
        plugins.append(LOCAL_PLUGIN)

    mcp_enabled = [name for name in enabled if "mcp" in agent_files.get(name, {})]
    man = {
        "agents": enabled,
        "plugins": plugins,
        "agent_secrets": [
            {"agent": agent_files[name]["binary"], "slot": "TEST_TOKEN", "secret": "TEST_SECRET"}
            for name in mcp_enabled
        ],
    }

    derived = manifest.derive(man, plugin_files, agent_files, DERIVE_ENV)
    ident, count = _identity_secrets(
        derived.get("AGENT_SECRETS", ""),
        derived.get("AGENT_SERVER_REMOTE_SLOTS", ""),
        derived.get("LITERAL_KEY_AGENTS", ""),
    )

    run_env = _build_run_env(derived, ident, count, key_env_values)

    payload = wire_plugins.build_payload(run_env)

    scratch = _scratch_path(
        agent_dir_name,
        remote_server=remote_server,
        local_server=local_server,
        extra_agents=extra_agents,
        key_env_values=key_env_values,
    )
    if scratch.exists():
        shutil.rmtree(scratch)
    home = scratch / "home"
    workspace = scratch / "workspace"
    home.mkdir(parents=True)
    (workspace / "repos").mkdir(parents=True)

    with contextlib.redirect_stdout(io.StringIO()):
        wire_plugins.run(payload, home, workspace, run_env)

    return WireResult(None, home, workspace, derived, payload, run_env)


def _capture_wire_snapshot(result: WireResult) -> dict:
    home_files, home_symlinks, home_modes = capture_tree(result.home, "home")
    ws_files, ws_symlinks, ws_modes = capture_tree(result.workspace, "workspace")
    return {
        "files": {**home_files, **ws_files},
        "symlinks": {**home_symlinks, **ws_symlinks},
        "modes": {**home_modes, **ws_modes},
    }


def _read_scenario_stamp(golden_dir: Path) -> int | None:
    path = golden_dir / "scenario"
    if not path.is_file():
        return None
    text = path.read_text().strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _scenario_mismatch_message(golden_version: int | None) -> str:
    if golden_version is None:
        captured = "<missing>"
    else:
        captured = f"v{golden_version}"
    return (
        f"golden captured against scenario {captured}; kit provides v{SCENARIO_VERSION} "
        f"— regenerate with GOLDEN_REGEN=1 (the agent itself may be fine)"
    )


def assert_matches_golden(
    result: WireResult,
    golden_dir: Path,
    *,
    wire_fn: Callable[[], WireResult] | None = None,
) -> None:
    """Compare wiring output under result.home/workspace to golden_dir byte-for-byte."""
    golden_dir = Path(golden_dir)
    regen = os.environ.get("GOLDEN_REGEN") == "1"

    if regen:
        if wire_fn is None:
            raise RuntimeError(
                "GOLDEN_REGEN=1 requires wire_fn=... so golden capture double-runs "
                "for determinism; pass the same callable used to produce result"
            )
        first = _capture_wire_snapshot(wire_fn())
        second = _capture_wire_snapshot(wire_fn())
        assert_snapshot_equal(str(golden_dir), first, second)
        snapshot = first
        write_tree_golden(golden_dir, snapshot)
        (golden_dir / "scenario").write_text(f"{SCENARIO_VERSION}\n")
        return

    golden_version = _read_scenario_stamp(golden_dir)
    if golden_version != SCENARIO_VERSION:
        raise AssertionError(_scenario_mismatch_message(golden_version))

    golden = read_tree_golden(golden_dir)
    live = _capture_wire_snapshot(result)
    assert_snapshot_equal(str(golden_dir), golden, live)
