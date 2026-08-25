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

    def test_sandbox_mode_is_declared_and_rendered_above_every_table(self):
        """The container is the sandbox, so codex runs with its own layer off.
        A bare TOML key is only top-level while no table has opened above it —
        assert the ordering, not just the presence."""
        self.assertEqual(
            kit.load_descriptor("codex")["config_settings"]["sandbox_mode"],
            "danger-full-access")
        toml = kit.wire("codex").read(".codex/config.toml")
        self.assertIn('sandbox_mode = "danger-full-access"', toml)
        self.assertLess(toml.index("sandbox_mode"), toml.index("[mcp_servers."))


if __name__ == "__main__":
    unittest.main()
