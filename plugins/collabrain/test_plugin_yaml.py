#!/usr/bin/env python3
"""Unit tests for plugins/collabrain/plugin.yml.

Two layers of coverage:

  1. Structural checks on the YAML file itself, read via `yq` — the same
     tool tests/plugins.test.sh and tests/test_golden_parity.py already use
     to turn a real plugin.yml into JSON in Python, rather than adding a
     PyYAML dependency the rest of the suite does not carry. Skips (not
     fails) when yq is unavailable, matching test_golden_parity.py's
     convention.
  2. The real validator: src/manifest.py's derive(), with a manifest
     enabling only `collabrain` — so the mcp:/volumes:/egress: shape rules
     (name charsets, local-vs-remote wiring, volume path/overlap checks) are
     exercised by the authoritative code path instead of a hand-rolled copy
     of its rules that could quietly drift from it.

NOTE ON services: — at the time this plugin lands (PR [2/3] of Phase 1
Hardening workstream 2, plugins/collabrain/README.md "Dependency on PR
[1/3]"), src/manifest.py does not understand `services:` yet; that lands in
PR [1/3] (branch plugin-services). manifest.py has no top-level-key
allowlist for plugin.yml documents (only agent descriptors get that
treatment — see its _normalize_agent_docs), so an unrecognized `services:`
map is silently ignored rather than rejected. derive() is therefore expected
to succeed on the checkout this file ships against AND after [1/3] merges;
the services-specific assertions below run only once manifest.py actually
exports PLUGIN_SERVICES, so this file needs no edit when the branches merge.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_YML = Path(__file__).resolve().parent / "plugin.yml"

sys.path.insert(0, str(REPO_ROOT / "src"))
import manifest as m  # noqa: E402

# A single minimal mcp-capable agent descriptor, shaped like
# tests/test_manifest.py's AGENT_FILES["claude"] — manifest.py's derive()
# requires at least one agent descriptor to run at all, and this plugin's
# own coverage is about the collabrain plugin file, not the agent roster.
AGENT_FILES = {
    "claude": {
        "binary": "claude",
        "install": "npm install -g @anthropic-ai/claude-code",
        "state_dirs": [{"path": ".claude", "volume": "claude-auth"}],
        "rules_file": ".claude/CLAUDE.md",
        "mcp": {
            "config_path": ".mcp.json",
            "format": "json",
            "dialect": "mcpServers",
            "env_refs": True,
            "strategy": "claude_preapprove",
        },
    },
}
ENV = {"PRESENT_SECRET_VARS": "", "SECRETS_FILE": "/sec/secrets.env"}


def _yq_json(path):
    if shutil.which("yq") is None:
        return None
    r = subprocess.run(
        ["yq", "-o=json", "-I=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


class PluginYamlStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _yq_json(PLUGIN_YML)
        if cls.doc is None:
            raise unittest.SkipTest("yq not available")

    def test_install_pins_basic_memory(self):
        self.assertIn("uv tool install basic-memory==0.22.1", self.doc["install"])

    def test_egress_covers_huggingface(self):
        self.assertEqual(sorted(self.doc["egress"]), ["hf.co", "huggingface.co"])

    def test_volumes_cover_index_and_model_cache(self):
        self.assertEqual(self.doc["volumes"], {
            "bm-state": "/home/coder/.basic-memory",
            "bm-hf-cache": "/home/coder/.cache/huggingface",
        })

    def test_three_mcp_entries_with_the_dual_basic_memory_shape(self):
        mcp = self.doc["mcp"]
        self.assertEqual(set(mcp), {"basic-memory", "basic-memory-stdio", "collabrain"})
        # basic-memory: url form — reaches Claude only (wire_plugins.py: a
        # LOCAL command: entry wires into every agent, a REMOTE url: entry
        # wires into Claude's .mcp.json only).
        self.assertEqual(mcp["basic-memory"], {"url": "http://127.0.0.1:8801/mcp"})
        # basic-memory-stdio: plain `bm mcp` stdio form for every other vendor.
        self.assertEqual(mcp["basic-memory-stdio"], {"command": "bm", "args": ["mcp"]})
        self.assertEqual(mcp["collabrain"], {
            "command": "uv",
            "args": ["--directory", "/workspace/repos/collabrain", "run",
                     "collabrain", "tripwires-mcp"],
        })

    def test_four_services_match_the_pln_sketch(self):
        services = self.doc.get("services")
        self.assertIsInstance(
            services, dict,
            "services: missing from the parsed YAML (independent of whether "
            "manifest.py understands the key yet)")
        self.assertEqual(set(services), {"bm-server", "capture", "scheduler", "review"})
        self.assertIn("streamable-http", services["bm-server"])
        self.assertIn("8801", services["bm-server"])
        self.assertIn("collabrain capture --watch", services["capture"])
        self.assertIn("collabrain schedule", services["scheduler"])
        self.assertIn("collabrain review --port 8830", services["review"])


class PluginYamlValidatorTests(unittest.TestCase):
    """Runs the plugin through the real src/manifest.py validator — the
    authoritative rules for mcp/volumes/egress shape, name/path charsets, and
    volume-overlap checks. Enabling ONLY `collabrain` isolates this from
    every other shipped plugin (cross-plugin checks are plugins.test.sh's
    job, over every shipped file at once)."""

    @classmethod
    def setUpClass(cls):
        cls.doc = _yq_json(PLUGIN_YML)
        if cls.doc is None:
            raise unittest.SkipTest("yq not available")

    def _derive(self):
        return m.derive(
            {"plugins": ["collabrain"]},
            {"collabrain": self.doc},
            AGENT_FILES,
            ENV,
        )

    def test_derive_succeeds(self):
        # Must succeed whether or not services: is a recognized key yet —
        # see the module docstring.
        derived = self._derive()
        self.assertIn("collabrain", derived["PLUGINS"].split())

    def test_mcp_entries_reach_the_wiring_payload(self):
        derived = self._derive()
        entries = derived["PLUGIN_MCP_ENTRIES"]
        self.assertIn("basic-memory", entries)
        self.assertIn("basic-memory-stdio", entries)
        self.assertIn("collabrain", entries)

    def test_volumes_render_into_the_compose_overlay(self):
        derived = self._derive()
        overlay = derived["PLUGIN_COMPOSE_YAML"]
        self.assertIn("bm-state:/home/coder/.basic-memory", overlay)
        self.assertIn("bm-hf-cache:/home/coder/.cache/huggingface", overlay)

    def test_no_host_port_grant(self):
        derived = self._derive()
        self.assertEqual(derived["HOST_MCP_PORTS"], "")

    def test_services_export_once_manifest_py_supports_it(self):
        derived = self._derive()
        if "PLUGIN_SERVICES" not in derived:
            self.skipTest(
                "src/manifest.py on this checkout does not export "
                "PLUGIN_SERVICES yet — the services: schema lands in PR "
                "[1/3] (branch plugin-services); re-run once it has merged")
        services = derived["PLUGIN_SERVICES"]
        for name in ("bm-server", "capture", "scheduler", "review"):
            self.assertIn(f"{name}\t", services)


if __name__ == "__main__":
    unittest.main()
