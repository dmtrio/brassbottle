"""Wiring contract for agents/gemini/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class GeminiWiring(unittest.TestCase):
    def test_literal_remote_httpurl_dialect(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("gemini", key_env_values={"IDENTITY_KEY_0": "GEMINI_KEY"})

        r = do_wire()
        path = r.home / ".gemini" / "settings.json"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
