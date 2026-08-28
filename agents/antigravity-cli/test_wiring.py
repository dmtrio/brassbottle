"""Wiring contract for agents/antigravity-cli/agent.yml — run via agent_test_kit.wire()."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_test_kit as kit


class AntigravityWiring(unittest.TestCase):
    def test_remote_server_arrives_as_a_shim_with_no_baked_key(self):
        golden = Path(__file__).parent / "golden"

        def do_wire():
            return kit.wire("antigravity-cli")

        r = do_wire()
        mcp_path = r.home / ".gemini" / "config" / "mcp_config.json"
        raw = mcp_path.read_text()
        # agy cannot expand a ref inside a remote header, which is exactly why
        # it used to get the key baked in. Over the shim the ref survives, and
        # the dialect question (agy rejects url/httpUrl, wants serverUrl) does
        # not arise at all — there is no remote entry to shape.
        self.assertIn("${TEST_TOKEN}", raw)
        self.assertNotIn("TEST_SECRET", raw)
        self.assertNotIn("serverUrl", raw)
        self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
        kit.assert_matches_golden(r, golden, wire_fn=do_wire)


if __name__ == "__main__":
    unittest.main()
