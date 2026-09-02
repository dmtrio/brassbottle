"""Wiring contract for agents/pi/agent.yml — run via agent_test_kit.wire()."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit

ADAPTER_DIR = Path(__file__).parent / "mcp-adapter"


class PiWiring(unittest.TestCase):
    def test_remote_server_arrives_as_a_shim_with_no_baked_key(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("pi")

        r = do_wire()
        path = r.home / ".pi" / "agent" / "mcp.json"
        raw = path.read_text()
        self.assertIn("${TEST_TOKEN}", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertIn("mcp-remote", raw)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


class PiMcpAdapter(unittest.TestCase):
    """The pi-mcp-adapter extension contract: source, pinned deps, install.

    The extension is what turns the wired ~/.pi/agent/mcp.json from inert
    bytes into live tools — wiring is only half the story, so the contract
    pins the other half beside it.
    """

    def adapter(self, name: str) -> str:
        return (ADAPTER_DIR / name).read_text(encoding="utf-8")

    def test_extension_source_exists(self):
        self.assertTrue((ADAPTER_DIR / "index.ts").is_file(), "index.ts missing")
        self.assertIn("export default function", self.adapter("index.ts"))
        source = self.adapter("index.ts")
        # The adapter must read the djinn-wired config and register tools.
        self.assertIn(".pi", source)
        self.assertIn("mcp.json", source)
        self.assertIn("registerTool", source)

    def test_sdk_dependency_is_pinned_in_lockfile(self):
        pkg = json.loads(self.adapter("package.json"))
        self.assertIn("@modelcontextprotocol/sdk", pkg.get("dependencies", {}))
        lock = json.loads(self.adapter("package-lock.json"))
        locked = lock.get("packages", {}).get("node_modules/@modelcontextprotocol/sdk")
        self.assertIsNotNone(locked, "SDK not in package-lock.json")
        self.assertEqual(
            pkg["dependencies"]["@modelcontextprotocol/sdk"].lstrip("^~"),
            locked["version"],
            "package.json and lockfile disagree on the SDK version",
        )

    def test_install_bakes_the_extension(self):
        agent_yml = (Path(__file__).parent / "agent.yml").read_text()
        install = agent_yml.split("install: |", 1)[1].split("\nmcp:", 1)[0]
        self.assertIn("mcp-adapter", install)
        self.assertIn("npm ci", install)
        self.assertIn(".pi/agent/extensions", install)


if __name__ == "__main__":
    unittest.main()
