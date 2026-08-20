"""Wiring contract for agents/aider/agent.yml — run via agent_test_kit.wire()."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class AiderWiring(unittest.TestCase):
    def test_enabled_without_mcp_wiring(self):
        r = kit.wire("aider", remote_server=False, local_server=False)
        self.assertIn("aider", r.derived["AGENTS_ENABLED"].split())
        self.assertNotIn("aider", r.derived.get("SHIM_AGENTS", "").split())
        mcp_agents = json.loads(r.derived["AGENTS_MCP_JSON"])
        self.assertFalse(any(a["binary"] == "aider" for a in mcp_agents))


if __name__ == "__main__":
    unittest.main()
