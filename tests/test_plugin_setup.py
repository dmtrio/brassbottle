#!/usr/bin/env python3
"""Unit tests for src/plugin_setup.py — the generated per-plugin one-shot
setup script (PLN "plugin setup hook" [2/3], setup: schema)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import plugin_setup as psetup


class TestSetupScript(unittest.TestCase):
    def test_contains_log_and_cmd_paths(self):
        script = psetup.setup_script("herdr-auto-title", "herdr integration install claude")
        self.assertIn(">> /tmp/djinn-setup/herdr-auto-title.log", script)
        self.assertIn("/tmp/djinn-setup/herdr-auto-title.cmd.sh", script)
        # No tmux, no restart loop: setup runs once, synchronously.
        self.assertNotIn("tmux", script)
        self.assertNotIn("while true", script)

    def test_contains_the_command_verbatim(self):
        script = psetup.setup_script(
            "p", "herdr integration install claude && herdr integration status claude")
        self.assertIn("herdr integration install claude && herdr integration status claude",
                      script)

    def test_heredoc_open_and_close_tags_match(self):
        script = psetup.setup_script("my-plugin", "true")
        lines = script.splitlines()
        open_lines = [l for l in lines if "<<'" in l]
        # Exactly one heredoc (the command file).
        self.assertEqual(len(open_lines), 1)
        tag = open_lines[0].split("<<'")[1].rstrip("'")
        self.assertIn(tag, lines)
        # Closing tag line must be unindented (heredoc requirement).
        close_line = next(l for l in lines if l == tag)
        self.assertEqual(close_line, close_line.strip())
        # Open line contains the tag inline; the close line IS the tag.
        self.assertEqual(len([l for l in lines if l == tag]), 1)

    def test_heredoc_tag_is_unique_per_plugin(self):
        a = psetup.setup_script("alpha", "true")
        b = psetup.setup_script("beta", "true")
        a_tags = [l.split("<<'")[1].rstrip("'") for l in a.splitlines() if "<<'" in l]
        b_tags = [l.split("<<'")[1].rstrip("'") for l in b.splitlines() if "<<'" in l]
        self.assertEqual(a_tags, ["DJINN_SETUP_ALPHA_EOF"])
        self.assertEqual(b_tags, ["DJINN_SETUP_BETA_EOF"])

    def test_summary_and_exit_code_lines(self):
        script = psetup.setup_script("p", "true")
        self.assertIn('echo "  + setup p ok (${dur}s)"', script)
        self.assertIn('echo "  ! setup p FAILED code=$code — see '
                      '/tmp/djinn-setup/p.log" >&2', script)
        # The script's exit status IS the command's exit code — up.sh's `||`
        # echo and any future caller rely on it.
        self.assertIn('exit "$code"', script)

    def test_rejects_bad_names(self):
        for name in ["", "bad name", "bad/name", "bad.name", "bad\nname", 5, None]:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    psetup.setup_script(name, "true")

    def test_accepts_plugin_dir_charset(self):
        # [A-Za-z0-9_-] — the plugins/<name>/ directory charset.
        for name in ["herdr-auto-title", "obsidian_annotated", "Plugin2"]:
            with self.subTest(name):
                psetup.setup_script(name, "true")  # must not raise

    def test_rejects_empty_command(self):
        with self.assertRaises(ValueError):
            psetup.setup_script("p", "")

    def test_rejects_whitespace_only_command(self):
        with self.assertRaises(ValueError):
            psetup.setup_script("p", "   ")

    def test_rejects_non_string_command(self):
        with self.assertRaises(ValueError):
            psetup.setup_script("p", ["herdr", "install"])


class TestMain(unittest.TestCase):
    def test_wrong_argc_errors(self):
        self.assertEqual(psetup.main(["only-one"]), 2)

    def test_bad_name_errors(self):
        self.assertEqual(psetup.main(["bad name", "true"]), 1)

    def test_good_args_render_script_to_stdout(self):
        out = io_StringIO()
        stdout = sys.stdout
        sys.stdout = out
        try:
            code = psetup.main(["p", "true"])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 0)
        self.assertIn("DJINN_SETUP_P_EOF", out.getvalue())


class io_StringIO:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def getvalue(self):
        return "".join(self.parts)


class TestRuntime(unittest.TestCase):
    """Execute the generated script with bash in a temp dir (LOG_DIR
    substituted, like test_plugin_services.py does) and a fake command: the
    exit code must land in the log AND propagate as the script's status."""

    def _run(self, command):
        with tempfile.TemporaryDirectory() as td:
            script = psetup.setup_script("demo", command).replace(
                psetup.LOG_DIR, td)
            proc = subprocess.run(["/bin/bash", "-c", script],
                                  capture_output=True, text=True)
            log_path = Path(td) / "demo.log"
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            cmd_path = Path(td) / "demo.cmd.sh"
            # Read inside the with — the temp dir is removed on return.
            cmd_text = cmd_path.read_text(encoding="utf-8") if cmd_path.exists() else None
            return proc, log, cmd_text

    def test_ok_command_exit_zero_lands_in_log(self):
        proc, log, _ = self._run("true")
        self.assertEqual(0, proc.returncode)
        self.assertIn("  + setup demo ok (0s)", proc.stdout)
        # start + exit stamps with duration and code, both timestamped.
        self.assertIn("start", log)
        self.assertIn("exit code=0 after 0s", log)
        for line in log.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_failing_command_exit_code_propagates_and_logs(self):
        proc, log, _ = self._run("exit 7")
        self.assertEqual(7, proc.returncode)
        self.assertIn("  ! setup demo FAILED code=7", proc.stderr)
        self.assertIn("exit code=7 after", log)

    def test_command_output_goes_to_log_not_console(self):
        proc, log, _ = self._run("echo hello-from-setup")
        self.assertEqual(0, proc.returncode)
        self.assertIn("hello-from-setup", log)
        self.assertNotIn("hello-from-setup", proc.stdout)

    def test_command_file_is_written_verbatim(self):
        proc, log, cmd_text = self._run("echo marker-42")
        self.assertEqual(0, proc.returncode)
        self.assertIsNotNone(cmd_text)
        self.assertIn("echo marker-42", cmd_text)

    def test_stderr_of_command_also_lands_in_log(self):
        proc, log, _ = self._run("echo oops >&2")
        self.assertEqual(0, proc.returncode)
        self.assertIn("oops", log)
        self.assertNotIn("oops", proc.stderr)

    def test_re_run_appends_second_run_to_log(self):
        # Idempotency posture: setup re-runs every up; the log accumulates,
        # it never truncates.
        with tempfile.TemporaryDirectory() as td:
            script = psetup.setup_script("demo", "true").replace(psetup.LOG_DIR, td)
            subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
            subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
            log = (Path(td) / "demo.log").read_text(encoding="utf-8")
        self.assertEqual(2, log.count("exit code=0"))


if __name__ == "__main__":
    unittest.main()
