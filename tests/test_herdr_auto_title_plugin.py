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
- the setup: command exists verbatim, and whenever manifest.py consumes
  setup: (PR [2/3]) it must export exactly that command — before [2/3] the
  key is unknown to manifest.py and must be ignored, not rejected;
- the config volume points at the directory upstream actually reads;
- a manifest enabling the plugin passes the real src/manifest.py --derive.

yq (not PyYAML) does the YAML→JSON conversion — the same tool up.sh and
tests/plugins.test.sh use, so the bytes this suite validates are the bytes
the pipeline reads.
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
PLUGIN_DIR = REPO / "plugins" / "herdr-auto-title"
sys.path.insert(0, str(REPO / "src"))
import manifest  # noqa: E402

PINNED_COMMIT = "4d4554fb9706a29032cb2082be32f2061b96f2c6"
# Upstream herdr-plugin.toml at PINNED_COMMIT: min_herdr_version = "0.8.2".
# Kept beside the commit pin because the repo is only cloned at image build;
# bump both together.
UPSTREAM_MIN_HERDR_VERSION = (0, 8, 2)
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


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

    def test_setup_runs_the_baked_integrations_script(self):
        # The script ships in the plugin dir, which the Dockerfile bakes at
        # /opt/plugins/<name>/ — so the path in setup: must match that layout
        # and the file must exist beside plugin.yml.
        self.assertEqual(_yq(".setup"),
                         "python3 /opt/plugins/herdr-auto-title/herdr_integrations.py")
        self.assertTrue((PLUGIN_DIR / "herdr_integrations.py").is_file())

    def test_setup_reaches_derive_verbatim_once_manifest_knows_the_key(self):
        # Valid on both sides of PR [2/3]: before it, setup: is an unknown key
        # manifest.py ignores (derive still succeeds); after it, the
        # PLUGIN_SETUP export must carry this plugin's command verbatim.
        result = _derive()
        self.assertEqual(result.returncode, 0, result.stderr)
        if "PLUGIN_SETUP=" in result.stdout:
            self.assertIn("herdr-auto-title\tpython3 /opt/plugins/herdr-auto-title/"
                          "herdr_integrations.py", result.stdout)

    def test_volume_keeps_the_upstream_config_dir(self):
        self.assertEqual(
            _yq(".volumes | keys | .[]"), "herdr-auto-title")
        self.assertEqual(
            _yq(".volumes.\"herdr-auto-title\""),
            "/home/coder/.config/herdr-auto-title")

    def test_herdr_is_installed_before_the_plugin_bake_loop(self):
        # `herdr plugin link` runs inside the bake loop, so the herdr layer
        # must come first (PR [1/3], brassbottle #114). On a main without it
        # this FAILS on purpose: merging this plugin first would break the
        # image build of every bottle enabling it with "herdr: command not
        # found", and a skipped test would not guard that.
        herdr_marker = "releases/download/v${HERDR_VERSION}"
        loop_marker = "for f in /opt/plugins/*/plugin.yml"
        self.assertIn(herdr_marker, DOCKERFILE)
        self.assertIn(loop_marker, DOCKERFILE)
        herdr_run = DOCKERFILE.index(herdr_marker)
        bake_loop = DOCKERFILE.index(loop_marker)
        self.assertLess(herdr_run, bake_loop,
                        "Dockerfile installs herdr AFTER the plugin bake loop; "
                        "merge #114 first")

    def test_image_herdr_meets_upstream_min_version(self):
        # herdr silently declines to load a plugin whose min_herdr_version
        # exceeds its own — no build error, no runtime error, tabs just never
        # rename. Pin the floor against the Dockerfile's ARG.
        m = re.search(r"^ARG HERDR_VERSION=(\d+)\.(\d+)\.(\d+)\s*$", DOCKERFILE, re.M)
        self.assertIsNotNone(m, "ARG HERDR_VERSION=x.y.z not found in Dockerfile")
        self.assertGreaterEqual(tuple(int(x) for x in m.groups()),
                                UPSTREAM_MIN_HERDR_VERSION)

    def test_cleanup_survives_read_only_go_module_cache(self):
        # go build leaves 0555 dirs under GOPATH/pkg/mod; an unprivileged
        # rm -rf exits non-zero there and `bash -e` fails the image build.
        self.assertRegex(_yq(".install"), r"sudo rm -rf [^\n]*/tmp/gopath")

    def test_derive_accepts_a_manifest_enabling_the_plugin(self):
        result = _derive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLUGINS_ENABLED=herdr-auto-title", result.stdout)
        # The declared volume reaches the generated compose overlay.
        self.assertIn("herdr-auto-title:/home/coder/.config/herdr-auto-title",
                      result.stdout)


if __name__ == "__main__":
    unittest.main()
