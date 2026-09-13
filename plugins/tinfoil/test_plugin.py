"""Contract for the Tinfoil env-only plugin descriptor."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import manifest


PLUGIN_YML = Path(__file__).with_name("plugin.yml")
PLUGIN_TEXT = PLUGIN_YML.read_text()
DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def image_env_path():
    """The last `ENV PATH=` in the Dockerfile, as a coder shell sees it: the
    PATH a non-interactive `docker exec -u coder bash` (the setup hook)
    inherits. `$USERNAME` is coder; the trailing `$PATH` is the base image's."""
    lines = [l for l in DOCKERFILE.read_text().splitlines() if l.startswith("ENV PATH=")]
    value = lines[-1][len("ENV PATH="):].strip().strip('"')
    return (value.replace("/home/$USERNAME", str(Path.home()))
                 .replace("$PATH", SYSTEM_PATH))


class TinfoilPlugin(unittest.TestCase):
    def test_derives_only_the_agent_secret_and_egress(self):
        plugin = {
            "install": "baked",
            "secrets": {
                "TINFOIL_API_KEY": {
                    "hint": "Tinfoil API key (tinfoil.sh dashboard → API Keys)"
                }
            },
            "setup": "bash /opt/plugins/tinfoil/setup.sh",
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
        self.assertEqual(
            derived["PLUGIN_SETUP"],
            "tinfoil\tbash /opt/plugins/tinfoil/setup.sh\n",
        )
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
        for key in ("mcp:", "services:"):
            self.assertNotRegex(PLUGIN_TEXT, rf"(?m)^{key}")
        setup_lines = re.findall(r"(?m)^setup:.*$", PLUGIN_TEXT)
        self.assertEqual(len(setup_lines), 1)
        self.assertEqual(setup_lines[0], 'setup: "bash /opt/plugins/tinfoil/setup.sh"')

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

    @unittest.skipUnless((Path.home() / ".fnm/aliases/default/bin/pi").exists(),
                         "pi not installed under the image's fnm default alias")
    def test_pi_install_is_idempotent_and_lists_tinfoil_models(self):
        """Registration under the hook's own PATH, twice, then the provider row.

        Runs only where the pi agent is installed under the image's fnm alias
        (a bottle with `agents: [.., pi, ..]`); elsewhere it skips and only the
        descriptor-text tests remain. Offline the extension lists its bundled
        fallback catalogue (llama3-3-70b is in it), so this proves
        registration and load, not verification; the verified path is
        host-side Evidence (PLN step 3)."""
        local_package = Path("/opt/plugins/tinfoil/pkg/package")
        with tempfile.TemporaryDirectory() as td:
            if (local_package / "tinfoil.ts").exists():
                package = local_package
            else:
                # This fixture branch needs npm registry access; image builds
                # use the already-baked local package and do not fetch at up.
                packed = subprocess.run(
                    ["npm", "pack", "@tinfoilsh/pi-provider@0.1.2"],
                    cwd=td, capture_output=True, text=True, timeout=120,
                )
                if packed.returncode:
                    self.skipTest("npm pack needs network: " + packed.stderr[-500:])
                archive = next(Path(td).glob("tinfoilsh-pi-provider-0.1.2.tgz"))
                package = Path(td) / "package"
                subprocess.run(
                    ["tar", "xzf", str(archive), "-C", td],
                    check=True, capture_output=True, text=True, timeout=120,
                )
                installed = subprocess.run(
                    ["npm", "install", "--omit=dev", "--omit=peer",
                     "--ignore-scripts", "--no-audit", "--no-fund",
                     "--save-exact", "tinfoil@1.2.1", "zod@4.6.3"],
                    cwd=package, capture_output=True, text=True, timeout=120,
                )
                if installed.returncode:
                    self.skipTest("local npm fixture install needs network: " +
                                   installed.stderr[-500:])

            # The setup hook's shell: `docker exec -u coder bash`, no
            # ~/.bashrc, the image's FINAL ENV PATH, read from the Dockerfile
            # so the Pin tracks the real hook if that line ever moves. pi
            # keys its state on HOME, so HOME is the tempdir.
            hook_path = image_env_path()
            hook_env = {"HOME": td, "PATH": hook_path, "TINFOIL_API_KEY": "dummy"}
            for _ in range(2):
                installed = subprocess.run(
                    ["bash", str(PLUGIN_YML.with_name("setup.sh")), str(package)],
                    env=hook_env, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(0, installed.returncode,
                                 installed.stdout + installed.stderr)
            settings = json.loads((Path(td) / ".pi/agent/settings.json").read_text())
            self.assertEqual(1, len(settings["packages"]))
            env = {**os.environ, "HOME": td, "TINFOIL_API_KEY": "dummy"}
            listed = subprocess.run(
                ["pi", "--list-models", "tinfoil"], env=env,
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
            self.assertRegex(listed.stdout, r"(?m)^tinfoil\s+llama3-3-70b\s")

    def test_image_env_path_puts_shims_and_fnm_alias_first(self):
        head = image_env_path().split(os.pathsep)[:3]
        home = str(Path.home())
        self.assertEqual([home + "/.agent-shims", home + "/.local/bin",
                          home + "/.fnm/aliases/default/bin"], head)

    def test_setup_script_skips_cleanly_without_pi(self):
        with tempfile.TemporaryDirectory() as td:
            run = subprocess.run(
                ["bash", str(PLUGIN_YML.with_name("setup.sh")), td],
                env={"HOME": td, "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("pi is not installed", run.stderr)

    def test_agents_fragment_is_scoped_and_documents_fail_closed_provider(self):
        fragment = PLUGIN_YML.with_name("AGENTS.md").read_text()
        self.assertRegex(fragment, r"^##\s+", re.MULTILINE)
        self.assertNotRegex(fragment, r"(?m)^# ")
        self.assertIn("Tinfoil: refusing to send", fragment)


if __name__ == "__main__":
    unittest.main()
