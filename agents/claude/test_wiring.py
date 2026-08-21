"""Wiring contract for agents/claude/agent.yml — run via agent_test_kit.wire()."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class ClaudeWiring(unittest.TestCase):
    def test_remote_ref_plugins_and_preapproval(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("claude")

        r = do_wire()
        self.assertTrue((r.workspace / ".mcp.generated").exists())
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
