"""Wiring contract for agents/antigravity-cli/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class AntigravityWiring(unittest.TestCase):
    def test_literal_remote_serverurl_dialect(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("antigravity-cli", key_env_values={"IDENTITY_KEY_0": "LITERAL_SECRET"})

        r = do_wire()
        mcp_path = r.home / ".gemini" / "config" / "mcp_config.json"
        raw = mcp_path.read_text()
        # agy cannot expand refs: the key is baked, and the ref never survives.
        self.assertNotIn("${TEST_TOKEN}", raw)
        # url/httpUrl are rejected by agy — the remote must carry serverUrl.
        self.assertIn("serverUrl", raw)
        self.assertNotIn('"url"', raw)
        self.assertNotIn("httpUrl", raw)
        self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
