"""Wiring contract for agents/cursor/agent.yml — run via agent_test_kit.wire()."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CursorWiring(unittest.TestCase):
    def test_literal_remote_url_dialect_and_sidecar(self):
        r = kit.wire("cursor", key_env_values={"IDENTITY_KEY_0": "LITERAL_SECRET"})
        mcp_path = r.home / ".cursor" / "mcp.json"
        mcp = json.loads(mcp_path.read_text())
        entry = mcp["mcpServers"]["remote-srv"]
        self.assertEqual(entry["url"], "https://example.test/mcp")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer LITERAL_SECRET")
        self.assertNotIn("${TEST_TOKEN}", mcp_path.read_text())

        sidecar = json.loads((mcp_path.parent / "mcp.json.djinn-servers").read_text())
        self.assertIn("remote-srv", sidecar)
        self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
