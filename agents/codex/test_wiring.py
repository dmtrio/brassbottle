"""Wiring contract for agents/codex/agent.yml — run via agent_test_kit.wire()."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class CodexWiring(unittest.TestCase):
    def test_local_and_remote_baked_no_literal_secret(self):
        """Both the LOCAL and the bearer-header REMOTE agent-scoped server land
        in the managed TOML block — codex's own native url +
        bearer_token_env_var shape, naming the env var it reads at connect
        time. The literal secret value and the header string never appear:
        codex is never handed anything to leak, only the var's NAME."""
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("codex")

        r = do_wire()
        toml = r.read(".codex/config.toml")
        self.assertIn("[mcp_servers.remote-srv]", toml)
        self.assertIn('url = "https://example.test/mcp"', toml)
        self.assertIn('bearer_token_env_var = "TEST_TOKEN"', toml)
        self.assertIn("[mcp_servers.local-srv]", toml)
        self.assertNotIn("TEST_SECRET", toml)
        self.assertNotIn("Bearer", toml)
        self.assertNotIn("headers", toml)
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
