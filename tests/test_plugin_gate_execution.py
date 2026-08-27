"""Execute the Dockerfile's plugin bake loop and prove the gate really gates.

The other coverage for PLUGINS_ENABLED (tests/plugins.test.sh) greps the
Dockerfile for literal strings. That proves the text is present; it does NOT
prove a disabled plugin's `install:` is skipped, which is the entire point of
the gate. This runs the REAL loop body — extracted from the shipped Dockerfile,
not retyped — against a synthetic plugin tree, and asserts on side effects.

Extracting rather than duplicating is deliberate: a copy of the loop in a test
would keep passing after the Dockerfile's own logic drifted away from it.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")

MARKER = "for f in /opt/plugins/*/plugin.yml"


def _plugin_bake_run():
    """The plugin bake RUN's shell body, continuations joined.

    Comment lines inside a continuation are dropped the way the Dockerfile
    parser drops them — the loop carries one ("Keep pipefail"), and leaving it
    in would terminate the extracted script early.
    """
    lines = DOCKERFILE.splitlines()
    start = next(i for i, l in enumerate(lines) if MARKER in l)
    while not lines[start].startswith("RUN "):
        start -= 1
    body, i = [], start
    while True:
        line = lines[i]
        if line.strip().startswith("#"):
            # Dropped, and — crucially — does NOT end the instruction. A
            # comment carries no trailing backslash, so testing it for one
            # would truncate the loop mid-`if`.
            i += 1
            continue
        body.append(line[4:] if i == start else line)
        if not line.rstrip().endswith("\\"):
            break
        i += 1
    return "\n".join(body).replace("\\\n", "\n")


class PluginGateExecutionTests(unittest.TestCase):
    """Run the extracted loop with a fake /opt/plugins and a real yq."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("yq") is None:
            raise unittest.SkipTest("yq not installed; the loop cannot run")
        if shutil.which("bash") is None:
            raise unittest.SkipTest("bash not installed")

    def _run(self, plugins_enabled):
        """Execute the loop over three synthetic plugins; return (stdout, ran).

        `ran` is the set of plugin names whose install: block actually
        executed, evidenced by the sentinel file each one touches.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root, sentinels = tmp / "plugins", tmp / "ran"
        sentinels.mkdir(parents=True)

        for name in ("alpha", "beta"):
            (root / name).mkdir(parents=True)
            (root / name / "plugin.yml").write_text(
                f"install: |\n  touch {sentinels}/{name}\n", encoding="utf-8")
        # A config-only plugin (no install:) must be skipped for its own
        # reason, not misreported as gated.
        (root / "configonly").mkdir(parents=True)
        (root / "configonly" / "plugin.yml").write_text(
            "mcp:\n  configonly:\n    url: https://example.test/mcp\n",
            encoding="utf-8")

        script = _plugin_bake_run()
        # The loop is written against the image's absolute paths; retarget both
        # so it can run in a sandbox. The gating logic itself is untouched.
        script = script.replace("/opt/plugins", str(root))
        script = script.replace("/tmp/plugin-install.sh", str(tmp / "install.sh"))
        # fnm is not on PATH here and is irrelevant to gating.
        script = script.replace('eval "$(fnm env)";', "")

        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                 "PLUGINS_ENABLED": plugins_enabled},
        )
        self.assertEqual(proc.returncode, 0, f"loop failed:\n{proc.stderr}")
        return proc.stdout, {p.name for p in sentinels.iterdir()}

    def test_disabled_plugin_install_does_not_run(self):
        """The whole point: a plugin absent from PLUGINS_ENABLED is not built."""
        out, ran = self._run("alpha")
        self.assertEqual(ran, {"alpha"})
        self.assertIn("── plugin install: alpha", out)
        self.assertIn("── plugin install (disabled): beta", out)

    def test_empty_enabled_list_is_fail_closed(self):
        """`docker build .` with no build arg must install nothing."""
        out, ran = self._run("")
        self.assertEqual(ran, set())
        for name in ("alpha", "beta", "configonly"):
            self.assertIn(f"── plugin install (disabled): {name}", out)

    def test_all_enabled_runs_every_install(self):
        out, ran = self._run("alpha beta configonly")
        self.assertEqual(ran, {"alpha", "beta"})
        # Enabled but no install: block — skipped for the other reason.
        self.assertIn("── plugin (config-only, nothing to bake): configonly", out)
        self.assertNotIn("(disabled)", out)

    def test_gate_does_not_match_on_name_prefix(self):
        """`rhinomcp` and `rhinomcp-official` both ship; one must not enable
        the other. The padded `case " $PLUGINS_ENABLED "` pattern is what
        makes this safe, so it is worth pinning."""
        out, ran = self._run("alph")
        self.assertEqual(ran, set())
        self.assertIn("── plugin install (disabled): alpha", out)


if __name__ == "__main__":
    unittest.main()
