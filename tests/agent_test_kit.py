"""Shared test kit for per-agent wiring contract tests (stdlib only).

Contract for third-party agent authors:
- Put ``test_wiring.py`` beside your ``agent.yml`` under ``agents/<name>/``.
- ``import agent_test_kit`` — on import the repo's ``src/`` and ``tests/`` dirs
  are prepended to ``sys.path`` (resolved from this file), so the module works
  both from the repo-wide suite and from ``cd agents/<name> && python3 -m unittest discover``.
- ``load_descriptor(name)`` parses the real ``agent.yml`` via ``yq``.
- ``wire(name, ...)`` runs the REAL derive → build-payload → run pipeline with
  synthetic manifest/plugins binding just that agent; assert on files your agent
  reads, not on manifest/wire_plugins internals.
- Adjacent tests passing = the drop-in install contract for a new agent folder.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
AGENTS = REPO_ROOT / "agents"

for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import manifest  # noqa: E402
import wire_plugins  # noqa: E402

DERIVE_ENV = {
    "PRESENT_SECRET_VARS": "TEST_SECRET",
    "SECRETS_FILE": "/sec/secrets.env",
}

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

    run_env = dict(os.environ)
    run_env.update({
        "AGENTS_MCP_JSON": derived.get("AGENTS_MCP_JSON", ""),
        "PLUGIN_MCP_ENTRIES": derived.get("PLUGIN_MCP_ENTRIES", ""),
        "AGENT_SERVERS_JSON": derived.get("AGENT_SERVERS_JSON", ""),
        "AGENT_SECRETS": derived.get("AGENT_SECRETS", ""),
        "IDENTITY_SECRETS": ident,
    })
    if key_env_values:
        run_env.update(key_env_values)
    else:
        for i in range(count):
            run_env.setdefault(f"IDENTITY_KEY_{i}", "TEST_SECRET")

    payload = wire_plugins.build_payload(run_env)

    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name) / "home"
    workspace = Path(tmp.name) / "workspace"
    home.mkdir()
    (workspace / "repos").mkdir(parents=True)

    with contextlib.redirect_stdout(io.StringIO()):
        wire_plugins.run(payload, home, workspace, run_env)

    return WireResult(tmp, home, workspace, derived, payload, run_env)
