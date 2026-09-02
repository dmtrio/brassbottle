"""Unit tests for src/code_workspace.py — idempotent merge of REPO_NAMES into
the VS Code multi-root workspace file.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import code_workspace  # noqa: E402


def _run(path, repo_names):
    env = {"REPO_NAMES": repo_names}
    return code_workspace.main([str(path)], env)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class FreshFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dev.code-workspace"

    def test_two_names_sorted_artifacts_last(self):
        # Unsorted input → output sorted by name; /artifacts always last.
        rc = _run(self.path, "shared-lib app")
        self.assertEqual(rc, 0)
        data = _load(self.path)
        self.assertEqual(
            data["folders"],
            [
                {"path": "repos/app", "name": "app"},
                {"path": "repos/shared-lib", "name": "shared-lib"},
                {"path": "/artifacts", "name": "artifacts"},
            ],
        )
        self.assertEqual(
            data["settings"][code_workspace.TERMINAL_CWD_KEY], "/workspace/repos"
        )

    def test_empty_repo_names_just_artifacts(self):
        rc = _run(self.path, "")
        self.assertEqual(rc, 0)
        self.assertEqual(
            _load(self.path),
            {
                "folders": [{"path": "/artifacts", "name": "artifacts"}],
                "settings": code_workspace.merge_settings({})[0],
            },
        )

    def test_zero_byte_file_treated_as_missing(self):
        self.path.write_text("", encoding="utf-8")
        rc = _run(self.path, "app")
        self.assertEqual(rc, 0)
        data = _load(self.path)
        self.assertEqual(
            [f["path"] for f in data["folders"]],
            ["repos/app", "/artifacts"],
        )
        self.assertEqual(
            data["settings"][code_workspace.TERMINAL_CWD_KEY], "/workspace/repos"
        )


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dev.code-workspace"

    def _write(self, obj):
        self.path.write_text(code_workspace._dump_json(obj), encoding="utf-8")

    def test_merge_repos_then_others_then_artifacts(self):
        self._write(
            {
                "folders": [
                    {"path": "worktrees/app/feature", "name": "feature"},
                    {"path": "notes", "name": "hand-added"},
                    {"path": "/artifacts", "name": "artifacts"},
                ],
                "settings": {},
            }
        )
        rc = _run(self.path, "shared-lib app")
        self.assertEqual(rc, 0)
        self.assertEqual(
            _load(self.path)["folders"],
            [
                {"path": "repos/app", "name": "app"},
                {"path": "repos/shared-lib", "name": "shared-lib"},
                {"path": "worktrees/app/feature", "name": "feature"},
                {"path": "notes", "name": "hand-added"},
                {"path": "/artifacts", "name": "artifacts"},
            ],
        )

    def test_rerun_is_byte_identical(self):
        self._write(
            {
                "folders": [
                    {"path": "worktrees/app/feature", "name": "feature"},
                    {"path": "/artifacts", "name": "artifacts"},
                ],
                "settings": {},
            }
        )
        self.assertEqual(_run(self.path, "app"), 0)
        first = self.path.read_bytes()
        self.assertEqual(_run(self.path, "app"), 0)
        self.assertEqual(self.path.read_bytes(), first)

    def test_adding_one_more_name_inserts_exactly_that_entry(self):
        self._write(
            {
                "folders": [
                    {"path": "repos/app", "name": "app"},
                    {"path": "worktrees/app/feature", "name": "feature"},
                    {"path": "/artifacts", "name": "artifacts"},
                ],
                "settings": {},
            }
        )
        before = _load(self.path)["folders"]
        self.assertEqual(_run(self.path, "app shared-lib"), 0)
        after = _load(self.path)["folders"]
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(
            after,
            [
                {"path": "repos/app", "name": "app"},
                {"path": "repos/shared-lib", "name": "shared-lib"},
                {"path": "worktrees/app/feature", "name": "feature"},
                {"path": "/artifacts", "name": "artifacts"},
            ],
        )

    def test_extra_keys_on_entries_and_settings_survive(self):
        self._write(
            {
                "folders": [
                    {
                        "path": "repos/app",
                        "name": "app",
                        "foo": 1,
                    },
                    {
                        "path": "worktrees/app/x",
                        "name": "x",
                        "bar": "keep",
                    },
                    {"path": "/artifacts", "name": "artifacts", "baz": True},
                ],
                "settings": {"editor.tabSize": 2, "custom": {"a": 1}},
                "extensions": {"recommendations": ["ms-python.python"]},
            }
        )
        self.assertEqual(_run(self.path, "app"), 0)
        data = _load(self.path)
        self.assertEqual(data["folders"][0]["foo"], 1)
        self.assertEqual(data["folders"][1]["bar"], "keep")
        self.assertEqual(data["folders"][2]["baz"], True)
        # The operator's own settings survive untouched; managed keys are
        # ADDED alongside, never replacing the dict wholesale.
        self.assertEqual(data["settings"]["editor.tabSize"], 2)
        self.assertEqual(data["settings"]["custom"], {"a": 1})
        self.assertIn(code_workspace.PROFILES_KEY, data["settings"])
        self.assertEqual(
            data["extensions"], {"recommendations": ["ms-python.python"]}
        )


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dev.code-workspace"

    def test_invalid_json_exits_1_and_leaves_file_untouched(self):
        raw = b"{not json\n"
        self.path.write_bytes(raw)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = _run(self.path, "app")
        self.assertEqual(rc, 1)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertIn("not valid JSON", err.getvalue())

    def test_folders_not_a_list_exits_1_and_leaves_file_untouched(self):
        payload = b'{"folders": {}, "settings": {}}\n'
        self.path.write_bytes(payload)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = _run(self.path, "app")
        self.assertEqual(rc, 1)
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertIn("folders", err.getvalue())


class ManagedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dev.code-workspace"

    def _settings(self):
        return _load(self.path)["settings"]

    def test_fresh_file_gets_a_plain_bash_profile(self):
        _run(self.path, "app")
        profiles = self._settings()[code_workspace.PROFILES_KEY]
        self.assertEqual(profiles["bash"], {"path": "bash"})

    def test_default_profile_is_plain_bash(self):
        # Load-bearing: VS Code runs tasks, debug consoles and git operations
        # through the DEFAULT profile. Non-interactive shells must stay out of
        # tmux and keep normal behavior.
        _run(self.path, "app")
        self.assertEqual(
            self._settings()[code_workspace.DEFAULT_PROFILE_KEY], "bash"
        )

    def test_is_idempotent(self):
        _run(self.path, "app")
        first = self.path.read_text(encoding="utf-8")
        _run(self.path, "app")
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)

    def test_never_overwrites_an_existing_bash_profile(self):
        # Add-if-missing only: keep any already-written profile config.
        mine = {"path": "bash", "args": ["--noprofile"]}
        self.path.write_text(json.dumps({
            "folders": [],
            "settings": {code_workspace.PROFILES_KEY: {
                "bash": mine
            }},
        }))
        _run(self.path, "app")
        self.assertEqual(
            self._settings()[code_workspace.PROFILES_KEY]["bash"],
            mine,
        )

    def test_never_overwrites_a_chosen_default_profile(self):
        self.path.write_text(json.dumps({
            "folders": [],
            "settings": {code_workspace.DEFAULT_PROFILE_KEY: "zsh"},
        }))
        _run(self.path, "app")
        self.assertEqual(
            self._settings()[code_workspace.DEFAULT_PROFILE_KEY], "zsh"
        )

    def test_other_profiles_are_left_alone(self):
        self.path.write_text(json.dumps({
            "folders": [],
            "settings": {code_workspace.PROFILES_KEY: {"zsh": {"path": "zsh"}}},
        }))
        _run(self.path, "app")
        profiles = self._settings()[code_workspace.PROFILES_KEY]
        self.assertEqual(profiles["zsh"], {"path": "zsh"})
        self.assertIn("bash", profiles)

    def test_non_dict_profiles_are_left_entirely_alone(self):
        # The operator's own invalid config: VS Code already ignores it, and
        # rewriting it would lose their work rather than fix anything.
        self.path.write_text(json.dumps({
            "folders": [],
            "settings": {code_workspace.PROFILES_KEY: "nonsense"},
        }))
        self.assertEqual(_run(self.path, "app"), 0)
        self.assertEqual(self._settings()[code_workspace.PROFILES_KEY], "nonsense")

    def test_non_dict_settings_does_not_crash_or_clobber(self):
        self.path.write_text(json.dumps({"folders": [], "settings": "nonsense"}))
        self.assertEqual(_run(self.path, "app"), 0)
        self.assertEqual(_load(self.path)["settings"], "nonsense")

if __name__ == "__main__":
    unittest.main()
