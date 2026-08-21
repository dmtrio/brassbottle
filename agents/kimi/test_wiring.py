"""Wiring contract for agents/kimi/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class KimiWiring(unittest.TestCase):
    def test_bearer_token_env_var_remote_and_local_plugins(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("kimi")

        r = do_wire()
        path = r.home / ".kimi-code" / "mcp.json"
        raw = path.read_text()
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertNotIn("Bearer", raw)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
