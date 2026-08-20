"""Wiring contract for agents/kimi/agent.yml — run via agent_test_kit.wire()."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class KimiWiring(unittest.TestCase):
    def test_bearer_token_env_var_remote_and_local_plugins(self):
        r = kit.wire("kimi")
        path = r.home / ".kimi-code" / "mcp.json"
        raw = path.read_text()
        mcp = json.loads(raw)
        entry = mcp["mcpServers"]["remote-srv"]
        self.assertEqual(entry["url"], "https://example.test/mcp")
        self.assertEqual(entry["bearerTokenEnvVar"], "TEST_TOKEN")
        self.assertNotIn("Authorization", entry.get("headers", {}))
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertNotIn("Bearer", raw)

        self.assertIn("plugin-local", mcp["mcpServers"])

        servers_sidecar = json.loads(
            (path.parent / "mcp.json.djinn-servers").read_text())
        self.assertIn("remote-srv", servers_sidecar)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
