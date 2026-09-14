"""Contract for the OpenCode env-only plugin descriptor."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import manifest

PLUGIN_PATH = Path(__file__).parent / "plugin.yml"
PLUGIN_YML = PLUGIN_PATH.read_text()


def load_real_descriptor():
    """Parse plugin.yml the way up.sh does (yq -o=json), so the derive
    contract runs against the shipped descriptor rather than a hand-written
    copy that could drift from it. yq is required by the suite already."""
    if shutil.which("yq") is None:
        raise AssertionError("yq not installed — descriptor contract did NOT run")
    out = subprocess.run(["yq", "-o=json", "-I=0", ".", str(PLUGIN_PATH)],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


class OpenCodePlugin(unittest.TestCase):
    def test_declares_the_api_key_as_an_env_only_secret_slot(self):
        plugin = load_real_descriptor()
        derived = manifest.derive(
            {"plugins": ["opencode"], "agent_secrets": [
                {"agent": "pi", "slot": "OPENCODE_API_KEY", "secret": "OPENCODE_KEY_pi"}
            ]},
            {"opencode": plugin},
            {"pi": {"binary": "pi", "install": "npm install -g pi", "mcp": {"config_path": ".pi/agent/mcp.json",
                                                   "format": "json", "dialect": "type-http",
                                                   "env_refs": False}}},
            env={"PRESENT_SECRET_VARS": "OPENCODE_KEY_pi", "SECRETS_FILE": "/sec/secrets.env"},
        )

        self.assertEqual(derived["AGENT_SECRETS"], "pi\tOPENCODE_API_KEY\tOPENCODE_KEY_pi\n")
        self.assertEqual(derived["AGENT_SERVERS_JSON"], "{}")
        self.assertEqual(derived["PLUGIN_SERVICES"], "")
        self.assertEqual(derived["PLUGIN_SETUP"], "")
        self.assertIn("opencode.ai", derived["EGRESS"].split(","))

    def test_descriptor_is_env_only_with_one_egress_zone(self):
        self.assertIn("  OPENCODE_API_KEY: {hint:", PLUGIN_YML)
        self.assertIn("egress: [opencode.ai]", PLUGIN_YML)
        for key in ("mcp:", "services:", "setup:", "install:"):
            self.assertNotIn("\n" + key, PLUGIN_YML)


if __name__ == "__main__":
    unittest.main()
