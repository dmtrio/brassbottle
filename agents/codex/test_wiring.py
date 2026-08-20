"""Wiring contract for agents/codex/agent.yml — run via agent_test_kit.wire()."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CodexWiring(unittest.TestCase):
    def test_local_in_managed_block_remote_not_baked(self):
        r = kit.wire("codex")
        toml = r.read(".codex/config.toml")
        self.assertIn("[mcp_servers.local-srv]", toml)
        self.assertIn("[mcp_servers.plugin-local]", toml)
        self.assertNotIn("remote-srv", toml)
        self.assertNotIn("TEST_SECRET", toml)
        self.assertNotIn("Bearer", toml)


if __name__ == "__main__":
    unittest.main()
