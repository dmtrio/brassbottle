"""Contract for the Tinfoil env-only plugin descriptor."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import manifest


PLUGIN_YML = Path(__file__).with_name("plugin.yml")
PLUGIN_TEXT = PLUGIN_YML.read_text()


class TinfoilPlugin(unittest.TestCase):
    def test_derives_only_the_agent_secret_and_egress(self):
        plugin = {
            "install": "baked",
            "secrets": {
                "TINFOIL_API_KEY": {
                    "hint": "Tinfoil API key (tinfoil.sh dashboard → API Keys)"
                }
            },
            "egress": ["tinfoil.sh"],
        }
        derived = manifest.derive(
            {"plugins": ["tinfoil"], "agent_secrets": [
                {"agent": "pi", "slot": "TINFOIL_API_KEY", "secret": "TINFOIL_KEY_PI"}
            ]},
            {"tinfoil": plugin},
            {"pi": {"binary": "pi", "install": "npm install -g pi",
                     "mcp": {"config_path": ".pi/agent/mcp.json",
                              "format": "json", "dialect": "type-http",
                              "env_refs": False}}},
            env={"PRESENT_SECRET_VARS": "TINFOIL_KEY_PI",
                 "SECRETS_FILE": "/sec/secrets.env"},
        )

        self.assertEqual(derived["AGENT_SECRETS"],
                         "pi\tTINFOIL_API_KEY\tTINFOIL_KEY_PI\n")
        self.assertEqual(derived["AGENT_SERVERS_JSON"], "{}")
        self.assertEqual(derived["PLUGIN_SERVICES"], "")
        self.assertEqual(derived["PLUGIN_SETUP"], "")
        self.assertIn("tinfoil.sh", derived["EGRESS"].split(","))

    def test_yaml_pins_package_and_has_no_runtime_wiring(self):
        self.assertIn("@tinfoilsh/pi-provider@0.1.2", PLUGIN_TEXT)
        self.assertIn(
            "sha512-qe6Z/Vy7Io4Jip2nhnaN87Fw+Czl2s+IeBbt/oM7ly1feCyql64wWludMv+pPtJFJ+vR1CUxcoZgviyowE8osg==",
            PLUGIN_TEXT,
        )
        self.assertIn("exit 1", PLUGIN_TEXT)
        self.assertIn("npm pkg delete peerDependencies", PLUGIN_TEXT)
        self.assertIn("dependencies.tinfoil=1.2.1 dependencies.zod=4.6.3", PLUGIN_TEXT)
        self.assertIn("cp /opt/plugins/tinfoil/package-lock.json .", PLUGIN_TEXT)
        self.assertIn("npm ci --omit=dev", PLUGIN_TEXT)
        self.assertNotIn("npm cache clean", PLUGIN_TEXT)
        self.assertRegex(PLUGIN_TEXT, r"(?m)^  TINFOIL_API_KEY: \{hint: ")
        for key in ("mcp:", "services:", "setup:"):
            self.assertNotRegex(PLUGIN_TEXT, rf"(?m)^{key}")

    def test_lockfile_pins_the_runtime_tree_without_pi_peers(self):
        lock = json.loads(PLUGIN_YML.with_name("package-lock.json").read_text())
        packages = lock["packages"]
        self.assertEqual("1.2.1", packages["node_modules/tinfoil"]["version"])
        self.assertEqual("4.6.3", packages["node_modules/zod"]["version"])
        self.assertEqual("0.3.2", packages["node_modules/ehbp"]["version"])
        self.assertIn("node_modules/@tinfoilsh/verifier", packages)
        self.assertFalse([k for k in packages if "@earendil-works" in k])
        self.assertTrue(all("integrity" in v for k, v in packages.items() if k))

    def test_yaml_declares_exactly_one_egress_zone(self):
        self.assertIn("egress: [tinfoil.sh]", PLUGIN_TEXT)
        self.assertNotRegex(PLUGIN_TEXT, r"(?m)^\s+-\s+(?!tinfoil\.sh\s*$)\S+")


if __name__ == "__main__":
    unittest.main()
