"""Wiring contract for agents/codex/agent.yml — run via agent_test_kit.wire()."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CodexWiring(unittest.TestCase):
    def test_local_in_managed_block_remote_not_baked(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("codex")

        r = do_wire()
        toml = r.read(".codex/config.toml")
        self.assertNotIn("remote-srv", toml)
        self.assertNotIn("TEST_SECRET", toml)
        self.assertNotIn("Bearer", toml)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
