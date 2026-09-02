"""Wiring contract for agents/pi/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit

AGENT_DIR = Path(__file__).parent
ADAPTER_NPM_SPEC = "npm:pi-mcp-adapter@"


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
    """pi's MCP consumer contract: the upstream pi-mcp-adapter package.

    The wired ~/.pi/agent/mcp.json is inert without an MCP client;
    pi-mcp-adapter (npm package, pinned) reads that file natively and
    bridges its servers. The contract pins the version in the install
    block — pi skips pinned npm packages on update, so the image build
    decides the version and bumping is a deliberate edit here.
    """

    def read_agent_yml(self) -> str:
        return (AGENT_DIR / "agent.yml").read_text()

    def read_install(self) -> str:
        return self.read_agent_yml().split("install: |", 1)[1].split("\nmcp:", 1)[0]

    def test_install_pins_the_adapter_package(self):
        install = self.read_install()
        self.assertIn("pi install npm:pi-mcp-adapter@", install)
        version = install.split(ADAPTER_NPM_SPEC, 1)[1].split('"', 1)[0].strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$", "pinned semver version required")

    def test_wired_config_path_is_the_file_the_adapter_reads(self):
        # The adapter reads <Pi agent dir>/mcp.json (Pi-global override in its
        # merge chain) — the descriptor must keep pointing wiring there.
        self.assertIn("config_path: .pi/agent/mcp.json", self.read_agent_yml())


if __name__ == "__main__":
    unittest.main()
