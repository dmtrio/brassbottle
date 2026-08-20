"""Wiring contract for agents/gemini/agent.yml — run via agent_test_kit.wire()."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class GeminiWiring(unittest.TestCase):
    def test_literal_remote_httpurl_dialect(self):
        r = kit.wire("gemini", key_env_values={"IDENTITY_KEY_0": "GEMINI_KEY"})
        path = r.home / ".gemini" / "settings.json"
        mcp = json.loads(path.read_text())
        entry = mcp["mcpServers"]["remote-srv"]
        self.assertIn("httpUrl", entry)
        self.assertNotIn("url", entry)
        self.assertEqual(entry["httpUrl"], "https://example.test/mcp")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer GEMINI_KEY")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
