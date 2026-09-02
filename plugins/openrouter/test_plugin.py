"""Contract for the OpenRouter env-only plugin descriptor."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import manifest


class OpenRouterPlugin(unittest.TestCase):
    def test_declares_the_api_key_as_an_env_only_secret_slot(self):
        plugin = {
            "secrets": {"OPENROUTER_API_KEY": {"hint": "OpenRouter API key"}},
            "egress": ["openrouter.ai"],
        }
        derived = manifest.derive(
            {"plugins": ["openrouter"], "agent_secrets": [
                {"agent": "pi", "slot": "OPENROUTER_API_KEY", "secret": "OPENROUTER_KEY_PI"}
            ]},
            {"openrouter": plugin},
            {"pi": {"binary": "pi", "install": "npm install -g pi", "mcp": {"config_path": ".pi/agent/mcp.json",
                                                   "format": "json", "dialect": "type-http",
                                                   "env_refs": False}}},
            env={"PRESENT_SECRET_VARS": "OPENROUTER_KEY_PI", "SECRETS_FILE": "/sec/secrets.env"},
        )

        self.assertEqual(derived["AGENT_SECRETS"], "pi\tOPENROUTER_API_KEY\tOPENROUTER_KEY_PI\n")
        self.assertEqual(derived["AGENT_SERVERS_JSON"], "{}")
        self.assertIn("openrouter.ai", derived["EGRESS"].split(","))
        self.assertIn("egress: [openrouter.ai]", (Path(__file__).parent / "plugin.yml").read_text())


if __name__ == "__main__":
    unittest.main()
