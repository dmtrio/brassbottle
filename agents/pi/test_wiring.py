"""Wiring contract for agents/pi/agent.yml — run via agent_test_kit.wire()."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class PiWiring(unittest.TestCase):
    def test_literal_remote_type_http_dialect(self):
        r = kit.wire("pi", key_env_values={"IDENTITY_KEY_0": "PI_KEY"})
        path = r.home / ".pi" / "agent" / "mcp.json"
        mcp = json.loads(path.read_text())
        entry = mcp["mcpServers"]["remote-srv"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "https://example.test/mcp")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer PI_KEY")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
