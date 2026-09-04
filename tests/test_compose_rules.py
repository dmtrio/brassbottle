#!/usr/bin/env python3
"""Unit tests for compose_rules.py — the generator that replaced the
symlink-to-mount fan-out of the agent global rules files.

Pins the load-bearing guarantees: byte-identical output when no enabled plugin
ships a fragment, enabled-only + ordered fragment inclusion, empty/missing
fragments skipped, and atomic writes that swap a pre-existing symlink for a
regular file WITHOUT writing through it into the (read-only) base. Target files
are index-driven from agents-index.tsv, including guard behavior when the index
is absent or malformed.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import compose_rules


class ComposeTextTests(unittest.TestCase):
    def test_no_fragments_returns_base_unchanged(self):
        for base in ("a\nb\n", "no trailing newline", "trailing blanks\n\n\n", ""):
            with self.subTest(base=repr(base)):
                self.assertEqual(compose_rules.compose(base, []), base)

    def test_fragments_appended_in_order_under_section(self):
        out = compose_rules.compose(
            "BASE\n",
            [("serena", "## Serena\nuse it\n"), ("archex", "## Archex\nquery it\n")],
        )
        self.assertTrue(out.startswith("BASE\n"))
        self.assertIn(compose_rules.SECTION_HEADING, out)
        self.assertIn(compose_rules.GEN_NOTICE, out)
        # order preserved: serena before archex
        self.assertLess(out.index("## Serena"), out.index("## Archex"))
        self.assertIn("use it", out)
        self.assertIn("query it", out)
        self.assertTrue(out.endswith("\n"))

    def test_base_precedes_section(self):
        out = compose_rules.compose("BASE\n", [("p", "## P\n")])
        self.assertLess(out.index("BASE"), out.index(compose_rules.SECTION_HEADING))

    def test_workspace_alone_is_appended_after_base(self):
        out = compose_rules.compose("BASE\n", [], "# Workspace Contract\nrules here\n")
        self.assertTrue(out.startswith("BASE\n"))
        self.assertIn(compose_rules.WORKSPACE_NOTICE, out)
        self.assertIn("rules here", out)
        self.assertNotIn(compose_rules.SECTION_HEADING, out, "no plugin section without fragments")
        self.assertTrue(out.endswith("\n"))

    def test_workspace_follows_plugin_section(self):
        out = compose_rules.compose("BASE\n", [("p", "## P\n")], "# WS\n")
        self.assertLess(out.index("BASE"), out.index(compose_rules.SECTION_HEADING))
        self.assertLess(out.index("## P"), out.index(compose_rules.WORKSPACE_NOTICE))
        self.assertLess(out.index(compose_rules.WORKSPACE_NOTICE), out.index("# WS"))

    def test_absent_or_blank_workspace_keeps_base_byte_identical(self):
        for ws in (None, "", "\n\n  \n"):
            with self.subTest(ws=repr(ws)):
                self.assertEqual(compose_rules.compose("a\nb\n", [], ws), "a\nb\n")


class ReadEnabledTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_absent_file_is_none(self):
        # absent ≠ empty: None signals "unknown", so run() skips instead of
        # downgrading a good file to base-only.
        self.assertIsNone(compose_rules.read_enabled(self.d / "nope"))

    def test_whitespace_split(self):
        f = self.d / "enabled"
        f.write_text("serena archex  gateway\n")
        self.assertEqual(compose_rules.read_enabled(f), ["serena", "archex", "gateway"])

    def test_empty_file_is_empty_list(self):
        f = self.d / "enabled"
        f.write_text("\n")
        self.assertEqual(compose_rules.read_enabled(f), [])


class DefaultTargetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.index = self.d / "agents-index.tsv"

    def test_absent_file_is_none(self):
        self.assertIsNone(compose_rules.default_targets(self.d / "home", self.index))

    def test_rows_with_rules_file_become_home_targets_in_file_order(self):
        self.index.write_text(
            "claude\tclaude\t.claude/CLAUDE.md\tmcp:true\n"
            "cursor\tcursor-agent\t\tmcp:true\n"
            "codex\tcodex\t.codex/AGENTS.md\tmcp:true\n",
            encoding="utf-8",
        )
        home = self.d / "home"
        got = compose_rules.default_targets(home, self.index)
        self.assertEqual(
            got,
            [home / ".claude/CLAUDE.md", home / ".codex/AGENTS.md"],
        )

    def test_malformed_row_is_skipped_with_warning(self):
        self.index.write_text(
            "claude\tclaude\t.claude/CLAUDE.md\tmcp:true\n"
            "bad-row-without-tabs\n"
            "gemini\tgemini\t.gemini/GEMINI.md\tmcp:true\n",
            encoding="utf-8",
        )
        err = io.StringIO()
        with redirect_stderr(err):
            got = compose_rules.default_targets(self.d / "home", self.index)
        self.assertIn("malformed agents-index row", err.getvalue())
        self.assertEqual(
            got,
            [self.d / "home/.claude/CLAUDE.md", self.d / "home/.gemini/GEMINI.md"],
        )


class LoadFragmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _plugin(self, name, fragment=None):
        d = self.root / name
        d.mkdir()
        if fragment is not None:
            (d / "AGENTS.md").write_text(fragment)

    def test_only_enabled_with_fragments_and_order(self):
        self._plugin("serena", "## Serena\n")
        self._plugin("archex", "## Archex\n")
        self._plugin("gateway")  # no fragment
        # enabled order archex,serena → output must follow it, gateway skipped
        got = compose_rules.load_fragments(self.root, ["archex", "gateway", "serena"])
        self.assertEqual([n for n, _ in got], ["archex", "serena"])

    def test_missing_plugin_dir_skipped(self):
        self._plugin("serena", "## Serena\n")
        got = compose_rules.load_fragments(self.root, ["serena", "ghost"])
        self.assertEqual([n for n, _ in got], ["serena"])

    def test_empty_fragment_skipped(self):
        self._plugin("serena", "   \n")
        self.assertEqual(compose_rules.load_fragments(self.root, ["serena"]), [])

    def test_disabled_plugin_fragment_ignored(self):
        self._plugin("serena", "## Serena\n")
        # serena has a fragment but is NOT in the enabled list
        self.assertEqual(compose_rules.load_fragments(self.root, []), [])

    def test_unsafe_names_rejected(self):
        # A name that could escape the plugins root must never reach a read,
        # even if a matching file happens to exist outside the tree.
        outside = self.root.parent / "AGENTS.md"
        outside.write_text("## Evil\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        for bad in ("..", "../..", "a/b", "/etc", ".", "\\x"):
            with self.subTest(name=bad):
                self.assertEqual(compose_rules.load_fragments(self.root, [bad]), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.base = self.d / "AGENTS.md"
        self.base.write_text("BASE RULES\n@/workspace/rules.local.md\n")
        self.plugins = self.d / "plugins"
        self.plugins.mkdir()
        self.enabled = self.d / "enabled"
        self.index = self.d / "agents-index.tsv"
        self.index.write_text(
            "claude\tclaude\t.claude/CLAUDE.md\tmcp:true\n"
            "gemini\tgemini\t.gemini/GEMINI.md\tmcp:true\n",
            encoding="utf-8",
        )
        self.t1 = self.d / "home/.claude/CLAUDE.md"
        self.t2 = self.d / "home/.gemini/GEMINI.md"

    def _run(self, announce=False):
        # workspace defaults to an absent path so the host's own /workspace
        # contract never leaks into these fixtures.
        with redirect_stdout(io.StringIO()):
            return compose_rules.run(self.base, self.plugins, self.enabled,
                                     [self.t1, self.t2], index=self.index, announce=announce,
                                     workspace=self.d / "workspace-AGENTS.md")

    def test_no_plugins_targets_byte_identical_to_base(self):
        self.enabled.write_text("")
        self.assertEqual(self._run(), 0)
        base_bytes = self.base.read_bytes()
        self.assertEqual(self.t1.read_bytes(), base_bytes)
        self.assertEqual(self.t2.read_bytes(), base_bytes)

    def test_workspace_contract_reaches_every_target(self):
        (self.d / "workspace-AGENTS.md").write_text("# Workspace Contract\nworktrees only\n")
        self.enabled.write_text("")
        self.assertEqual(self._run(), 0)
        for target in (self.t1, self.t2):
            out = target.read_text()
            self.assertIn("BASE RULES", out)
            self.assertIn(compose_rules.WORKSPACE_NOTICE, out)
            self.assertIn("worktrees only", out)
        self.assertEqual(self.t1.read_bytes(), self.t2.read_bytes(), "same text for every harness")

    def test_workspace_contract_comes_after_plugin_fragments(self):
        (self.plugins / "serena").mkdir()
        (self.plugins / "serena" / "AGENTS.md").write_text("## Serena\n")
        self.enabled.write_text("serena\n")
        (self.d / "workspace-AGENTS.md").write_text("# WS\n")
        self._run()
        out = self.t1.read_text()
        self.assertLess(out.index("## Serena"), out.index("# WS"))

    def test_import_line_preserved(self):
        self.enabled.write_text("")
        self._run()
        self.assertIn("@/workspace/rules.local.md", self.t1.read_text())

    def test_enabled_fragment_included(self):
        (self.plugins / "serena").mkdir()
        (self.plugins / "serena" / "AGENTS.md").write_text("## Serena\nactivate first\n")
        self.enabled.write_text("serena\n")
        self._run()
        out = self.t1.read_text()
        self.assertIn("BASE RULES", out)
        self.assertIn("## Serena", out)
        self.assertIn("activate first", out)

    def test_replaces_symlink_without_touching_base(self):
        # Simulate the pre-change state: target is a symlink into the read-only base.
        self.t1.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(self.base, self.t1)
        self.assertTrue(self.t1.is_symlink())
        base_before = self.base.read_bytes()
        self.enabled.write_text("")
        self._run()
        self.assertFalse(self.t1.is_symlink(), "symlink should be replaced by a regular file")
        self.assertEqual(self.base.read_bytes(), base_before, "base must be untouched")

    def test_missing_base_warns_and_leaves_targets(self):
        self.base.unlink()
        self.enabled.write_text("")
        # pre-existing target content must survive a missing-base run
        self.t1.parent.mkdir(parents=True, exist_ok=True)
        self.t1.write_text("OLD\n")
        with redirect_stdout(io.StringIO()):
            rc = compose_rules.run(self.base, self.plugins, self.enabled, [self.t1])
        self.assertEqual(rc, 1)
        self.assertEqual(self.t1.read_text(), "OLD\n")

    def test_absent_enabled_file_leaves_good_file(self):
        # The vanished-enabled-file case: a shell hook must NOT strip the plugin
        # section from a previously-good composed file. enabled-file absent →
        # skip, preserving whatever the last (up-time) compose wrote.
        (self.plugins / "serena").mkdir()
        (self.plugins / "serena" / "AGENTS.md").write_text("## Serena\nactivate first\n")
        self.t1.parent.mkdir(parents=True, exist_ok=True)
        good = "BASE RULES\n\n# Plugin tools enabled in this container\n\n## Serena\nactivate first\n"
        self.t1.write_text(good)
        self.assertFalse(self.enabled.exists())  # never written
        with redirect_stdout(io.StringIO()):
            rc = compose_rules.run(self.base, self.plugins, self.enabled, [self.t1])
        self.assertEqual(rc, 1)
        self.assertEqual(self.t1.read_text(), good)

    def test_absent_index_file_leaves_good_file_when_using_default_targets(self):
        self.index.unlink()
        self.enabled.write_text("")
        self.t1.parent.mkdir(parents=True, exist_ok=True)
        self.t1.write_text("OLD\n")
        with redirect_stdout(io.StringIO()):
            rc = compose_rules.run(self.base, self.plugins, self.enabled,
                                   targets=None, index=self.index)
        self.assertEqual(rc, 1)
        self.assertEqual(self.t1.read_text(), "OLD\n")

    def test_explicit_targets_bypass_absent_index(self):
        self.index.unlink()
        self.enabled.write_text("")
        self.assertEqual(
            compose_rules.run(self.base, self.plugins, self.enabled,
                              targets=[self.t1], index=self.index),
            0,
        )
        self.assertEqual(self.t1.read_text(), self.base.read_text())

    def test_announce_lists_fragments(self):
        (self.plugins / "serena").mkdir()
        (self.plugins / "serena" / "AGENTS.md").write_text("## Serena\n")
        self.enabled.write_text("serena\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            compose_rules.run(self.base, self.plugins, self.enabled, [self.t1], announce=True,
                              workspace=self.d / "absent")
        self.assertIn("serena", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
