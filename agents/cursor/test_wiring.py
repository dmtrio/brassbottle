"""Wiring contract for agents/cursor/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CursorWiring(unittest.TestCase):
    def test_remote_server_arrives_as_a_shim_with_no_baked_key(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("cursor")

        r = do_wire()
        mcp_path = r.home / ".cursor" / "mcp.json"
        raw = mcp_path.read_text()
        # The inverse of what this asserted before. cursor-agent cannot expand
        # ${VAR} inside a remote header, so it used to get the resolved key
        # written here; it takes the mcp-remote shim instead and the ref lives.
        self.assertIn("${TEST_TOKEN}", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertIn("mcp-remote", raw)
        self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
