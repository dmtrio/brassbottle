#!/usr/bin/env python3
"""Tests for herdr_integrations.py — a fake `herdr` on disk records what it
was asked to install and returns the exit code the test chooses, so every
branch (install, skip, failure, missing binary, missing index) is pinned
without a real herdr."""

import io
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import herdr_integrations as hi

FAKE_HERDR = """#!/bin/bash
# $1 $2 = integration install ; $3 = target
echo "$3" >> "$FAKE_HERDR_CALLS"
case "$3" in
  *fail*) echo "boom: $3" >&2; exit 3 ;;
  *hang*) sleep 30 ;;
  *) echo "installed $3 integration hook"; exit 0 ;;
esac
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.herdr = root / "herdr"
        self.herdr.write_text(FAKE_HERDR, encoding="utf-8")
        self.herdr.chmod(self.herdr.stat().st_mode | stat.S_IXUSR)
        self.calls = root / "calls"
        os.environ["FAKE_HERDR_CALLS"] = str(self.calls)
        self.addCleanup(os.environ.pop, "FAKE_HERDR_CALLS", None)
        self.index = root / "agents-index.tsv"

    def run_main(self, index_lines, herdr=None):
        if index_lines is not None:
            self.index.write_text("".join(f"{l}\n" for l in index_lines), encoding="utf-8")
        os.environ["HERDR_INTEGRATIONS_BIN"] = herdr or str(self.herdr)
        self.addCleanup(os.environ.pop, "HERDR_INTEGRATIONS_BIN", None)
        err = io.StringIO()
        with redirect_stderr(err):
            code = hi.main([str(self.index)])
        return code, err.getvalue()

    def recorded_calls(self):
        return self.calls.read_text(encoding="utf-8").split() if self.calls.exists() else []


class TestInstall(Fixture):
    def test_installs_every_enabled_agent_herdr_supports(self):
        code, log = self.run_main([
            "claude\tclaude\t.claude/CLAUDE.md\tmcp:true",
            "codex\tcodex\t.codex/AGENTS.md\tmcp:true",
            "pi\tpi\t\tmcp:true",
        ])
        self.assertEqual(0, code)
        self.assertEqual(["claude", "codex", "pi"], self.recorded_calls())
        self.assertIn("stage=summary installed=claude,codex,pi skipped=- failed=-", log)

    def test_agent_without_herdr_target_is_skipped_not_failed(self):
        code, log = self.run_main(["aider\taider\t\tmcp:false", "claude\tclaude\t\tmcp:true"])
        self.assertEqual(0, code)
        self.assertEqual(["claude"], self.recorded_calls())
        self.assertIn("stage=skip agent=aider reason=no herdr integration target", log)

    def test_one_failure_does_not_stop_the_others_but_fails_the_run(self):
        # "fail" is not a real target; the fake herdr uses the name to choose
        # its exit code, so smuggle it in through the target set for the test.
        with unittest.mock.patch.object(hi, "HERDR_TARGETS", hi.HERDR_TARGETS | {"failme"}):
            code, log = self.run_main(["claude\tclaude", "failme\tx", "codex\tcodex"])
        self.assertEqual(1, code)
        self.assertEqual(["claude", "failme", "codex"], self.recorded_calls())
        self.assertIn("target=failme status=failed code=3", log)
        self.assertIn("boom: failme", log)          # herdr's stderr reaches the log
        self.assertIn("failed=failme", log)

    def test_duplicate_index_lines_install_once(self):
        code, _ = self.run_main(["claude\tclaude", "claude\tclaude"])
        self.assertEqual(0, code)
        self.assertEqual(["claude"], self.recorded_calls())

    def test_every_install_is_logged_with_code_and_duration(self):
        _, log = self.run_main(["kimi\tkimi"])
        self.assertRegex(log, r"stage=install target=kimi status=ok code=0 duration_ms=\d+ "
                              r"stdout_bytes=\d+ stderr_bytes=0")


class TestBudget(Fixture):
    """The setup: wrapper kills the whole process at plugin_setup.TIMEOUT_SECS;
    the helper must finish — every agent lined, summary printed — inside its
    own smaller budget however many installs hang."""

    def test_total_budget_sits_below_the_setup_wrapper_ceiling(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        import plugin_setup
        self.assertLess(hi.TOTAL_BUDGET_SECS, plugin_setup.TIMEOUT_SECS)
        # Even with every supported agent enabled, the per-agent cap alone
        # cannot blow the budget — the share rule below is the backstop for
        # whatever the caps do not cover.
        self.assertGreater(hi.TOTAL_BUDGET_SECS / len(hi.HERDR_TARGETS),
                           hi.MIN_INSTALL_TIMEOUT_SECS)

    def test_per_install_timeout_is_min_of_cap_and_even_share(self):
        self.assertEqual(30, hi.per_install_timeout(240, 6))    # cap wins
        self.assertEqual(10, hi.per_install_timeout(30, 3))     # share wins
        self.assertEqual(1, hi.per_install_timeout(0.2, 5))     # floor
        self.assertEqual(30, hi.per_install_timeout(240, 0))    # no div-by-zero

    def test_hanging_installs_all_get_a_line_and_the_summary_prints_in_budget(self):
        # Three agents that hang forever against a 3 s total budget: each gets
        # ~1 s, the run ends near the budget (not 3 × 30 s), and every agent is
        # accounted for in the summary.
        os.environ["HERDR_INTEGRATIONS_BUDGET_SECS"] = "3"
        self.addCleanup(os.environ.pop, "HERDR_INTEGRATIONS_BUDGET_SECS", None)
        hangers = {"hang-a", "hang-b", "hang-c"}
        start = time.monotonic()
        with unittest.mock.patch.object(hi, "HERDR_TARGETS", hi.HERDR_TARGETS | hangers):
            code, log = self.run_main([f"{h}\tx" for h in sorted(hangers)])
        elapsed = time.monotonic() - start
        self.assertEqual(1, code)
        self.assertLess(elapsed, 8, f"run took {elapsed:.1f}s; budget was 3 s")
        for h in sorted(hangers):
            self.assertRegex(log, rf"target={h} status=(timeout|skipped)")
        self.assertIn("stage=summary installed=- skipped=- failed=hang-a,hang-b,hang-c", log)

    def test_a_hang_does_not_starve_the_healthy_agents_after_it(self):
        os.environ["HERDR_INTEGRATIONS_BUDGET_SECS"] = "4"
        self.addCleanup(os.environ.pop, "HERDR_INTEGRATIONS_BUDGET_SECS", None)
        with unittest.mock.patch.object(hi, "HERDR_TARGETS", hi.HERDR_TARGETS | {"hang-1"}):
            code, log = self.run_main(["hang-1\tx", "claude\tclaude", "codex\tcodex"])
        self.assertEqual(1, code)
        self.assertIn("target=hang-1 status=timeout", log)
        self.assertEqual(["hang-1", "claude", "codex"], self.recorded_calls())
        self.assertIn("installed=claude,codex", log)
        self.assertIn("failed=hang-1", log)


class TestEdges(Fixture):
    def test_missing_index_is_reported_and_installs_nothing(self):
        code, log = self.run_main(None)
        self.assertEqual(0, code)
        self.assertEqual([], self.recorded_calls())
        self.assertIn("stage=index_read status=missing", log)

    def test_empty_index_installs_nothing(self):
        code, log = self.run_main([])
        self.assertEqual(0, code)
        self.assertIn("agents=0", log)

    def test_missing_herdr_binary_is_a_logged_failure(self):
        code, log = self.run_main(["claude\tclaude"], herdr=str(Path(self.td.name) / "nope"))
        self.assertEqual(1, code)
        self.assertIn("target=claude status=error", log)
        self.assertIn("No such file or directory", log)
        self.assertIn("failed=claude", log)

    def test_non_executable_herdr_is_a_per_agent_failure_not_a_traceback(self):
        # Review finding: only FileNotFoundError was caught, so an existing but
        # non-executable binary escaped as PermissionError and aborted the run
        # before the summary. Every agent must still get its line.
        self.herdr.chmod(0o644)
        code, log = self.run_main(["claude\tclaude", "codex\tcodex"])
        self.assertEqual(1, code)
        self.assertIn("target=claude status=error", log)
        self.assertIn("target=codex status=error", log)
        self.assertIn("Permission denied", log)
        self.assertIn("stage=summary installed=- skipped=- failed=claude,codex", log)

    def test_targets_match_herdr_0_8_2_help_text(self):
        # The list herdr 0.8.2 prints for `integration install --help`; a
        # herdr bump that adds or renames a target should update both.
        expected = {"pi", "omp", "claude", "codex", "copilot", "devin", "droid", "kimi",
                    "opencode", "kilo", "hermes", "qodercli", "qwen", "cursor",
                    "mastracode", "antigravity-cli", "grok"}
        self.assertEqual(expected, set(hi.HERDR_TARGETS))

    def test_every_brassbottle_agent_is_either_a_target_or_known_unsupported(self):
        # A new agents/<name>/ that herdr supports should not be silently
        # skipped; a new one herdr lacks goes into UNSUPPORTED deliberately.
        agents_dir = Path(__file__).resolve().parents[2] / "agents"
        names = {p.name for p in agents_dir.iterdir() if (p / "agent.yml").exists()}
        unsupported = {"aider"}
        self.assertEqual(set(), names - hi.HERDR_TARGETS - unsupported)


import unittest.mock  # noqa: E402  (used by TestInstall)

if __name__ == "__main__":
    unittest.main()
