"""Wiring contract for agents/cursor/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CursorWiring(unittest.TestCase):
    def test_literal_remote_url_dialect_and_sidecar(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("cursor", key_env_values={"IDENTITY_KEY_0": "LITERAL_SECRET"})

        r = do_wire()
        mcp_path = r.home / ".cursor" / "mcp.json"
        self.assertNotIn("${TEST_TOKEN}", mcp_path.read_text())
        self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
