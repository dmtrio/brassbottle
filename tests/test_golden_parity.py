#!/usr/bin/env python3
"""Golden-fixture parity for the host-side derive + wiring pipeline.

Captures the byte-level behavior of up.sh's manifest derive step and the
subsequent wire_plugins --build-payload call, so refactors to src/manifest.py
/up.sh/Dockerfile can prove they did not change existing derived values or the
wiring payload. New derived variables may appear post-refactor; existing ones
must keep their values and the payload must stay byte-identical.

Set GOLDEN_REGEN=1 to rewrite tests/fixtures/golden/*.derived.txt and
*.payload.json from a live run.
"""

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden"
CONFIG_FIXTURES = FIXTURES / "configs"
MANIFEST_PY = REPO_ROOT / "src" / "manifest.py"
WIRE_PLUGINS = REPO_ROOT / "src" / "wire_plugins.py"
PLUGINS_DIR = REPO_ROOT / "plugins"
AGENTS_DIR = REPO_ROOT / "agents"
SECRETS_FILE = FIXTURES / "secrets.env"

GIT_NAME_DEFAULT = "Golden Tester"
GIT_EMAIL_DEFAULT = "golden@example.com"
GH_TOKEN_VARS = "GH_TOKEN"

MANIFESTS = ("full", "minimal")

_ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")

# Add src/ and tests/ to path for imports, mirroring tests/test_wire_plugins.py.
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
import wire_plugins
from golden_tree import (
    assert_snapshot_equal,
    capture_tree,
    read_tree_golden,
    write_tree_golden,
)


def load_secrets_env(path):
    """Source fixture secrets into a copy of os.environ (values only)."""
    env = os.environ.copy()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key] = value
    return env


def present_secret_vars(secrets_path, env):
    """Mirror up.sh ~95–100: assigned names in secrets.env with non-empty values."""
    names = set()
    for line in secrets_path.read_text().splitlines():
        m = _ASSIGN_RE.match(line)
        if m:
            names.add(m.group(1))
    return " ".join(sorted(n for n in names if env.get(n, "")))


def yq_json(path):
    """Run yq -o=json -I=0; return None on failure."""
    try:
        r = subprocess.run(
            ["yq", "-o=json", "-I=0", str(path)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def plugin_yq_doc(plugin_yml):
    """Mirror up.sh ~120–122: unreadable or multiline yq output becomes '!'."""
    doc = yq_json(plugin_yml)
    if doc is None:
        return "!"
    # up.sh: [ "$(printf '%s\n' "$DOC" | wc -l)" -eq 1 ]
    if len((doc if doc.endswith("\n") else doc + "\n").splitlines()) != 1:
        return "!"
    return doc.rstrip("\n")


def agent_yq_doc(agent_yml):
    """Agent docs are required; unreadable descriptors are always fatal."""
    doc = yq_json(agent_yml)
    if doc is None:
        raise RuntimeError(f"yq failed on {agent_yml}")
    if len((doc if doc.endswith("\n") else doc + "\n").splitlines()) != 1:
        raise RuntimeError(f"agent descriptor is not single-line JSON: {agent_yml}")
    return doc.rstrip("\n")


def build_derive_stdin(manifest_path):
    """Build the stdin stream up.sh pipes to manifest.py --derive."""
    man_doc = yq_json(manifest_path)
    if man_doc is None:
        raise RuntimeError(f"yq failed on {manifest_path}")
    lines = [man_doc.rstrip("\n")]
    for plugin_yml in sorted(PLUGINS_DIR.glob("*/plugin.yml")):
        name = plugin_yml.parent.name
        lines.append(f"{name}\t{plugin_yq_doc(plugin_yml)}")
    lines.append("---agents---")
    for agent_yml in sorted(AGENTS_DIR.glob("*/agent.yml")):
        name = agent_yml.parent.name
        lines.append(f"{name}\t{agent_yq_doc(agent_yml)}")
    return "\n".join(lines) + "\n"


def parse_derived(text):
    """Parse manifest.py --derive stdout into {VAR: unquoted value}."""
    out = {}
    stream = io.StringIO(text)
    while True:
        pos = stream.tell()
        ch = stream.read(1)
        if not ch:
            break
        if ch == "\n":
            continue
        stream.seek(pos)
        rest = stream.read()
        eq = rest.find("=")
        if eq <= 0:
            break
        key = rest[:eq]
        if not key.replace("_", "").isalnum():
            stream.seek(pos + 1)
            continue
        remainder = rest[eq + 1:]
        lex = shlex.shlex(remainder, posix=True)
        lex.whitespace_split = False
        value = lex.get_token()
        out[key] = value
        consumed = len(remainder) - len(lex.instream.read())
        stream.seek(pos + eq + 1 + consumed)
    return out


def identity_secrets(agent_secrets, remote_slots):
    """Replicate up.sh ~431–447 IDENTITY_SECRETS assembly."""
    slots_set = f" {remote_slots} "
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
        if agent in ("claude", "codex"):
            continue
        entries.append(f"{agent}:IDENTITY_KEY_{i}:{slot}")
        i += 1
    return " ".join(entries)


def run_pipeline(manifest_name, env):
    """Run derive + build-payload; return (derived_stdout, payload_stdout)."""
    stdin = build_derive_stdin(FIXTURES / f"{manifest_name}.yml")
    derive_env = {
        **env,
        "PRESENT_SECRET_VARS": present_secret_vars(SECRETS_FILE, env),
        "GH_TOKEN_VARS": GH_TOKEN_VARS,
        "SECRETS_FILE": str(SECRETS_FILE),
        "GIT_NAME_DEFAULT": GIT_NAME_DEFAULT,
        "GIT_EMAIL_DEFAULT": GIT_EMAIL_DEFAULT,
        "NTFY_URL": "",
        "NTFY_TOPIC": "",
    }
    dr = subprocess.run(
        [sys.executable, str(MANIFEST_PY), "--derive"],
        input=stdin, capture_output=True, text=True, env=derive_env, check=False,
    )
    if dr.returncode != 0:
        raise AssertionError(
            f"manifest.py --derive failed for {manifest_name}:\n{dr.stderr}")

    derived = parse_derived(dr.stdout)
    ident = identity_secrets(
        derived.get("AGENT_SECRETS", ""),
        derived.get("AGENT_SERVER_REMOTE_SLOTS", ""),
    )
    payload_env = {
        **env,
        "AGENTS_MCP_JSON": derived.get("AGENTS_MCP_JSON", ""),
        "PLUGIN_MCP_ENTRIES": derived.get("PLUGIN_MCP_ENTRIES", ""),
        "AGENT_SERVERS_JSON": derived.get("AGENT_SERVERS_JSON", ""),
        "AGENT_SECRETS": derived.get("AGENT_SECRETS", ""),
        "IDENTITY_SECRETS": ident,
    }
    pr = subprocess.run(
        [sys.executable, str(WIRE_PLUGINS), "--build-payload"],
        capture_output=True, text=True, env=payload_env, check=False,
    )
    if pr.returncode != 0:
        raise AssertionError(
            f"wire_plugins.py --build-payload failed for {manifest_name}:\n{pr.stderr}")
    return dr.stdout, pr.stdout


def _capture_config_snapshot(manifest_name, payload):
    # Keep scratch paths deterministic because ~/.claude.json stores absolute
    # project paths and we compare bytes exactly.
    scratch = Path(tempfile.gettempdir()) / "agents-as-plugins-golden" / manifest_name
    if scratch.exists():
        shutil.rmtree(scratch)

    try:
        home = scratch / "home"
        workspace = scratch / "workspace"
        home.mkdir(parents=True)
        workspace.mkdir(parents=True)
        (workspace / "repos").mkdir(parents=True)
        (workspace / "repos" / "alpha" / ".git").mkdir(parents=True)

        key_envs = set()
        for server in payload.get("agent_servers") or []:
            for lit in server.get("literal") or []:
                key_envs.update((lit.get("key_envs") or {}).values())
        env = {name: f"{name}-golden-value" for name in sorted(key_envs) if name}

        with contextlib.redirect_stdout(io.StringIO()):
            wire_plugins.run(payload, home, workspace, env)

        home_files, home_symlinks, home_modes = capture_tree(home, "home")
        ws_files, ws_symlinks, ws_modes = capture_tree(workspace, "workspace")
        files = {**home_files, **ws_files}
        symlinks = {**home_symlinks, **ws_symlinks}
        modes = {**home_modes, **ws_modes}
        return {"files": files, "symlinks": symlinks, "modes": modes}
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def _read_config_golden(manifest_name):
    base = CONFIG_FIXTURES / manifest_name
    if not base.exists():
        raise AssertionError(
            f"{manifest_name}: missing config goldens at {base} "
            "(run with GOLDEN_REGEN=1)")
    return read_tree_golden(base)


def _write_config_golden(manifest_name, snapshot):
    write_tree_golden(CONFIG_FIXTURES / manifest_name, snapshot)


class TestGoldenParity(unittest.TestCase):
    WORKSPACE_MCP_FIXTURE = "workspace/repos/.mcp.json"

    def setUp(self):
        if shutil.which("yq") is None:
            self.skipTest("yq not available")
        self.env = load_secrets_env(SECRETS_FILE)
        self.regen = os.environ.get("GOLDEN_REGEN") == "1"

    def _paths(self, name):
        return (
            FIXTURES / f"{name}.derived.txt",
            FIXTURES / f"{name}.payload.json",
        )

    def test_golden_parity(self):
        for name in MANIFESTS:
            with self.subTest(manifest=name):
                live_derived, live_payload = run_pipeline(name, self.env)
                derived_path, payload_path = self._paths(name)

                if self.regen:
                    derived_path.write_text(live_derived)
                    payload_path.write_text(live_payload)
                    continue

                golden_derived = derived_path.read_text()
                golden_payload = payload_path.read_text()

                golden_vars = parse_derived(golden_derived)
                live_vars = parse_derived(live_derived)
                for var, value in golden_vars.items():
                    self.assertIn(
                        var, live_vars,
                        f"{name}: live derive missing golden var {var}")
                    self.assertEqual(
                        live_vars[var], value,
                        f"{name}: derived var {var} changed")

                self.assertEqual(
                    live_payload, golden_payload,
                    f"{name}: wiring payload is not byte-identical to golden")

    def test_golden_config_parity(self):
        for name in MANIFESTS:
            with self.subTest(manifest=name):
                _derived, live_payload = run_pipeline(name, self.env)
                payload = json.loads(live_payload)
                if self.regen:
                    first = _capture_config_snapshot(name, payload)
                    second = _capture_config_snapshot(name, payload)
                    assert_snapshot_equal(name, first, second)
                    _write_config_golden(name, first)
                    continue

                golden = _read_config_golden(name)
                self.assertIn(
                    self.WORKSPACE_MCP_FIXTURE,
                    golden["files"],
                    f"{name}: golden config set must include {self.WORKSPACE_MCP_FIXTURE} "
                    "(a missing fixture usually means a .gitignore regression)",
                )
                live = _capture_config_snapshot(name, payload)
                assert_snapshot_equal(name, golden, live)

    def test_golden_fixtures_are_non_trivial(self):
        if os.environ.get("GOLDEN_REGEN") == "1":
            self.skipTest("fixture sanity check runs in non-regen mode")
        full_derived = (FIXTURES / "full.derived.txt").read_text()
        full_vars = parse_derived(full_derived)
        for var in (
            "AGENTS_ENABLED", "AGENTS_MCP_JSON", "PLUGINS", "PLUGIN_MCP_ENTRIES",
            "AGENT_SECRETS",
        ):
            self.assertIn(var, full_vars, msg=f"full.derived.txt missing {var}")
            self.assertTrue(full_vars[var], msg=f"full.derived.txt {var} is empty")

        payload = json.loads((FIXTURES / "full.payload.json").read_text())
        self.assertIsInstance(payload, dict)
        self.assertIn("agents", payload)
        self.assertIn("plugin_mcp_entries", payload)

        combined = (FIXTURES / "secrets.env").read_text()
        self.assertIn("xaat-golden-fixture-not-real", combined)
        self.assertIn("ghp_golden_fixture", combined)
        self.assertNotRegex(combined, r"ghp_[A-Za-z0-9]{20,}(?!_fixture)")


if __name__ == "__main__":
    unittest.main()
