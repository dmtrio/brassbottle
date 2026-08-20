"""Wiring contract for agents/claude/agent.yml — run via agent_test_kit.wire()."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class ClaudeWiring(unittest.TestCase):
    def test_remote_ref_plugins_and_preapproval(self):
        r = kit.wire("claude")
        mcp = json.loads(r.read("workspace/repos/.mcp.json"))
        remote = mcp["mcpServers"]["remote-srv"]
        self.assertEqual(remote["type"], "http")
        self.assertEqual(remote["headers"]["Authorization"], "Bearer ${TEST_TOKEN}")
        self.assertIn("plugin-local", mcp["mcpServers"])

        self.assertTrue((r.workspace / ".mcp.generated").exists())

        cj = json.loads(r.read(".claude.json"))
        approved = cj["projects"][str(r.workspace / "repos")]["enabledMcpjsonServers"]
        self.assertIn("remote-srv", approved)
        self.assertIn("plugin-local", approved)


if __name__ == "__main__":
    unittest.main()
