#!/usr/bin/env python3
"""Pin the herdr-auto-title plugin's declared contract.

The plugin is data (plugin.yml), but the data carries promises other code
relies on, and each is one edit away from silently breaking:

- the upstream commit is PINNED (a moving branch would make rebuilds
  non-reproducible);
- the Go toolchain purge and the apt cache cleanup share the SAME install:
  block — one image layer, so the cleanup actually shrinks it (the property
  tests/test_dockerfile_cache_clean.py guards for the Dockerfile, applied
  here to a plugin's own install: block);
- `herdr plugin link` runs in the install: block, i.e. registration is
  baked, not deferred to a services: entry (the plugin is herdr-supervised);
- the setup: command exists verbatim — until PR [2/3] makes manifest.py
  consume setup:, it is an unknown key (ignored, not rejected); this test
  pins the value so that PR cannot wire the wrong command;
- the config volume points at the directory upstream actually reads;
- a manifest enabling the plugin passes the real src/manifest.py --derive.

yq (not PyYAML) does the YAML→JSON conversion — the same tool up.sh and
tests/plugins.test.sh use, so the bytes this suite validates are the bytes
the pipeline reads.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
PLUGIN_DIR = REPO / "plugins" / "herdr-auto-title"
sys.path.insert(0, str(REPO / "src"))
import manifest  # noqa: E402

PINNED_COMMIT = "4d4554fb9706a29032cb2082be32f2061b96f2c6"


def _yq(expr):
    """`yq -r <expr>` over the plugin's plugin.yml, as up.sh reads it."""
    return subprocess.run(
        ["yq", "-r", expr, str(PLUGIN_DIR / "plugin.yml")],
        capture_output=True, text=True, check=True,
    ).stdout.rstrip("\n")


def _derive():
    """Run the real derive over a manifest enabling just this plugin — the
    same payload tests/plugins.test.sh builds for its per-plugin check."""
    doc = subprocess.run(
        ["yq", "-o=json", "-I=0", str(PLUGIN_DIR / "plugin.yml")],
        capture_output=True, text=True, check=True,
    ).stdout.rstrip("\n")
    agent_lines = "\n".join(
        f"{p.parent.name}\t"
        + subprocess.run(["yq", "-o=json", "-I=0", str(p)],
                         capture_output=True, text=True, check=True).stdout.rstrip("\n")
        for p in sorted((REPO / "agents").glob("*/agent.yml"))
    )
    stdin = f'{{"plugins": ["herdr-auto-title"]}}\nherdr-auto-title\t{doc}\n---agents---\n{agent_lines}\n'
    return subprocess.run(
        [sys.executable, str(REPO / "src" / "manifest.py"), "--derive"],
        input=stdin, capture_output=True, text=True,
    )


class HerdrAutoTitlePluginTests(unittest.TestCase):
    def test_install_pins_upstream_commit(self):
        self.assertIn(PINNED_COMMIT, _yq(".install"))

    def test_toolchain_purge_precedes_registration_in_one_block(self):
        install = _yq(".install")
        self.assertIn("apt-get purge -y golang-1.24-go", install)
        self.assertIn("rm -rf /var/lib/apt/lists/*", install)
        # Same block is the point: a purge in a later block lands in a later
        # image layer and shrinks nothing.
        self.assertLess(install.index("apt-get purge"),
                        install.index("herdr plugin link"))

    def test_install_registers_the_plugin_at_build(self):
        self.assertIn("herdr plugin link /opt/herdr-auto-title", _yq(".install"))

    def test_install_builds_binary_where_upstream_startup_expects_it(self):
        # The upstream [[startup]] runs ./herdr-auto-title under the plugin
        # root; -o must land the binary there (relative to the build cwd).
        self.assertRegex(_yq(".install"), r"go build [^\n]*-o herdr-auto-title")

    def test_setup_is_the_verbatim_claude_hook_install(self):
        self.assertEqual(_yq(".setup"), "herdr integration install claude")

    def test_setup_is_currently_an_unknown_key_to_manifest_py(self):
        # PR [2/3] adds setup: handling; until it lands, manifest.py must
        # IGNORE (not reject) the key — derive still succeeds and emits no
        # setup variable. When [2/3] merges, this test flips to asserting
        # the PLUGIN_SETUP export instead.
        result = _derive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PLUGIN_SETUP", result.stdout)

    def test_volume_keeps_the_upstream_config_dir(self):
        self.assertEqual(
            _yq(".volumes | keys | .[]"), "herdr-auto-title")
        self.assertEqual(
            _yq(".volumes.\"herdr-auto-title\""),
            "/home/coder/.config/herdr-auto-title")

    def test_derive_accepts_a_manifest_enabling_the_plugin(self):
        result = _derive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLUGINS_ENABLED=herdr-auto-title", result.stdout)
        # The declared volume reaches the generated compose overlay.
        self.assertIn("herdr-auto-title:/home/coder/.config/herdr-auto-title",
                      result.stdout)


if __name__ == "__main__":
    unittest.main()
