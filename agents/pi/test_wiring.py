"""Wiring contract for agents/pi/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class PiWiring(unittest.TestCase):
    def test_remote_server_arrives_as_a_shim_with_no_baked_key(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("pi")

        r = do_wire()
        path = r.home / ".pi" / "agent" / "mcp.json"
        raw = path.read_text()
        self.assertIn("${TEST_TOKEN}", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertIn("mcp-remote", raw)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
