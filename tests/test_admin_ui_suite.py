"""Unit tests for the helpers of the admin behaviour suite (tests/admin_ui_playwright.py)."""
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admin_ui_playwright as suite  # noqa: E402

build_inputs = suite.build_inputs
REPO = Path(__file__).resolve().parent.parent
LATER = 4_000_000_000_000_000_000  # ns


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _age(path: Path) -> None:
    """Bump an mtime, as an editor, `touch` or a scratch file in the directory does."""
    os.utime(path, ns=(LATER, LATER))


# The path that stands for each INPUTS entry (and each kind of file inside a tree)
# when a test changes it. A new entry without a line here fails test_every_entry_has_a_case.
CASES = {
    "admin/ui/src": ("admin/ui/src/components/Deep.vue", "admin/ui/src/README.md"),
    "admin/ui/public": ("admin/ui/public/favicon.svg", "admin/ui/public/README.md",
                        "admin/ui/public/.well-known/security.txt", "admin/ui/public/.DS_Store"),
    "admin/ui/scripts": ("admin/ui/scripts/gen-types.ts", "admin/ui/scripts/check_tokens.py"),
    "admin/ui/index.html": ("admin/ui/index.html",),
    "admin/ui/vite.config.ts": ("admin/ui/vite.config.ts",),
    "admin/ui/package.json": ("admin/ui/package.json",),
    "admin/ui/package-lock.json": ("admin/ui/package-lock.json",),
    "admin/ui/tsconfig*.json": ("admin/ui/tsconfig.json", "admin/ui/tsconfig.app.json",
                                "admin/ui/tsconfig.node.json"),
    "admin/contract/*.schema.json": ("admin/contract/queue_snapshot.schema.json",
                                     "admin/contract/recent_page.schema.json"),
}
INPUT_PATHS = [path for paths in CASES.values() for path in paths]

# Files the build never reads.
NOT_INPUTS = (
    "admin/ui/README.md",
    "admin/ui/components.json",
    "admin/ui/eslint.config.js",
    "admin/ui/.gitignore",
    "admin/ui/.eslintcache",
    "admin/ui/src/.App.vue.swp",
    "admin/ui/src/App.vue~",
    "admin/ui/src/#App.vue#",
    "admin/ui/src/4913",
    "admin/ui/src/.cache/chunk.js",
    "admin/ui/scripts/__pycache__/check_tokens.cpython-312.pyc",
    "admin/ui/scripts/__pycache__/test_check_tokens.cpython-312.pyc",
    "admin/ui/scripts/.gen-types.ts.swo",
    "admin/contract/README.md",
    "admin/contract/notes.txt",
    "admin/contract/.recent_page.schema.json.swp",
    "admin/ui/node_modules/x/index.js",
)


def _build(root: Path) -> None:
    """The tree of a finished build: every input, a bundle, and the manifest the build writes."""
    for path in [*INPUT_PATHS, "admin/ui/src/App.vue", *NOT_INPUTS]:
        _write(root / path, path)
    _write(root / suite.BUNDLE, "<html>")
    with contextlib.redirect_stderr(io.StringIO()):
        build_inputs.write_manifest(root)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _build(self.root)


class BundleRefusalTests(FixtureCase):
    """bundle_refusal(root) is what main() calls, on the real repo layout."""

    def test_a_fresh_bundle_is_accepted(self):
        self.assertIsNone(suite.bundle_refusal(self.root))

    def test_a_changed_source_is_refused_and_named(self):
        _write(self.root / "admin/ui/src/App.vue", "changed")
        refusal = suite.bundle_refusal(self.root)
        self.assertIn("changed admin/ui/src/App.vue", refusal)

    def test_a_deleted_source_is_refused_and_named(self):
        (self.root / "admin/ui/src/components/Deep.vue").unlink()
        self.assertIn("deleted admin/ui/src/components/Deep.vue", suite.bundle_refusal(self.root))

    def test_an_added_source_is_refused_and_named(self):
        _write(self.root / "admin/ui/src/New.vue")
        self.assertIn("added admin/ui/src/New.vue", suite.bundle_refusal(self.root))

    def test_a_missing_bundle_is_refused(self):
        (self.root / suite.BUNDLE).unlink()
        self.assertIn("not built", suite.bundle_refusal(self.root))

    def test_a_missing_manifest_is_refused(self):
        (self.root / build_inputs.MANIFEST).unlink()
        self.assertIn("no readable build-input manifest", suite.bundle_refusal(self.root))

    def test_an_unreadable_manifest_is_refused(self):
        for text in ("", "{", "[]", '{"inputs": 3}', '{"version": 1}'):
            with self.subTest(text=text):
                (self.root / build_inputs.MANIFEST).write_text(text)
                self.assertIn("no readable build-input manifest", suite.bundle_refusal(self.root))

    def test_many_differences_are_counted_and_the_list_is_cut(self):
        for n in range(8):
            _write(self.root / f"admin/ui/src/Extra{n}.vue")
        refusal = suite.bundle_refusal(self.root)
        self.assertIn("(8 differ: added admin/ui/src/Extra0.vue", refusal)
        self.assertIn(", …)", refusal)


class BuildInputsTests(FixtureCase):
    """Every input the build reads makes a stale bundle refuse; nothing else does."""

    def test_every_entry_has_a_case(self):
        self.assertEqual(sorted(suite.build_inputs.INPUTS), sorted(CASES))

    def test_changing_an_input_refuses_and_names_it(self):
        for path in INPUT_PATHS:
            with self.subTest(input=path):
                _write(self.root / path, "edited")
                self.assertIn(f"changed {path}", suite.bundle_refusal(self.root) or "")
                _write(self.root / path, path)
                self.assertIsNone(suite.bundle_refusal(self.root))

    def test_a_change_of_the_same_length_refuses_and_names_it(self):
        """Same size, different content: only the hash tells it."""
        for path in INPUT_PATHS:
            with self.subTest(input=path):
                same_length = "X" + path[1:]
                self.assertEqual(len(same_length), len(path))
                _write(self.root / path, same_length)
                self.assertIn(f"changed {path}", suite.bundle_refusal(self.root) or "")
                _write(self.root / path, path)
                self.assertIsNone(suite.bundle_refusal(self.root))

    def test_deleting_an_input_refuses_and_names_it(self):
        for path in INPUT_PATHS:
            with self.subTest(input=path):
                (self.root / path).unlink()
                self.assertIn(f"deleted {path}", suite.bundle_refusal(self.root) or "")
                _write(self.root / path, path)

    def test_adding_an_input_refuses_and_names_it(self):
        for tree in ("admin/ui/src/New.vue", "admin/ui/public/robots.txt", "admin/ui/public/.well-known/new.txt",
                     "admin/ui/scripts/new.ts", "admin/ui/tsconfig.extra.json", "admin/contract/new.schema.json"):
            with self.subTest(input=tree):
                _write(self.root / tree)
                self.assertIn(f"added {tree}", suite.bundle_refusal(self.root) or "")
                (self.root / tree).unlink()

    def test_a_file_that_looks_like_noise_is_an_input_inside_public(self):
        """Vite ships public/ whole, README and dotfiles included; src/README.md is scanned by Tailwind."""
        for path in ("admin/ui/public/README.md", "admin/ui/public/.well-known/security.txt",
                     "admin/ui/public/.DS_Store", "admin/ui/src/README.md"):
            with self.subTest(input=path):
                _write(self.root / path, "edited")
                self.assertIn(f"changed {path}", suite.bundle_refusal(self.root) or "")
                _write(self.root / path, path)

    def test_creating_a_non_input_is_ignored(self):
        """A new scratch file also bumps its directory's mtime; nothing depends on that."""
        for noise in NOT_INPUTS:
            with self.subTest(noise=noise):
                path = self.root / noise
                path.unlink(missing_ok=True)
                _write(path, "new")
                for directory in path.relative_to(self.root).parents:
                    _age(self.root / directory)
                self.assertIsNone(suite.bundle_refusal(self.root))

    def test_editing_or_deleting_a_non_input_is_ignored(self):
        for noise in NOT_INPUTS:
            with self.subTest(noise=noise):
                path = self.root / noise
                _write(path, "edited")
                self.assertIsNone(suite.bundle_refusal(self.root))
                path.unlink()
                self.assertIsNone(suite.bundle_refusal(self.root))
                _write(path, noise)

    def test_touching_files_and_directories_without_changing_them_is_ignored(self):
        for path in [self.root, *self.root.rglob("*")]:
            if path != self.root / suite.BUNDLE:
                _age(path)
        self.assertIsNone(suite.bundle_refusal(self.root))

    def test_a_removed_scratch_file_leaves_no_trace(self):
        """Vim's swap file, and its 4913 probe, come and go in a watched directory."""
        for name in (".App.vue.swp", "4913"):
            _write(self.root / "admin/ui/src" / name)
            (self.root / "admin/ui/src" / name).unlink()
        _age(self.root / "admin/ui/src")
        self.assertIsNone(suite.bundle_refusal(self.root))


class TailwindScopeTests(unittest.TestCase):
    """Tailwind reads every file it can find, so src/style.css scopes it to what INPUTS lists.

    Bare `@import "tailwindcss"` scans all of admin/ui/ that .gitignore does not
    exclude, README.md included: a class added there changes the CSS while the
    guard, which does not list the README, accepts the stale bundle.
    """
    css = (REPO / "admin/ui/src/style.css").read_text()

    def test_the_scan_is_scoped_to_src(self):
        self.assertIn('@import "tailwindcss" source("./");', self.css)
        self.assertNotRegex(self.css, r'@import "tailwindcss";')

    def test_every_extra_source_is_an_input(self):
        sources = re.findall(r'^@source "([^"]+)";', self.css, re.M)
        self.assertEqual(sources, ["../scripts"])
        for source in sources:
            resolved = os.path.normpath(REPO / "admin/ui/src" / source)
            self.assertIn(Path(resolved).relative_to(REPO).as_posix(), build_inputs.INPUTS)

    def test_the_names_the_guard_ignores_are_excluded_from_the_scan(self):
        excluded = re.findall(r'^@source not "([^"]+)";', self.css, re.M)
        self.assertEqual(excluded, ["../**/.*", "../**/.*/**", "../**/*~", "../**/#*#", "../**/4913"])


class ManifestTests(FixtureCase):
    def test_the_manifest_lists_exactly_the_inputs_with_size_and_hash(self):
        recorded = json.loads((self.root / build_inputs.MANIFEST).read_text())
        self.assertEqual(recorded["version"], 1)
        self.assertEqual(sorted(recorded["inputs"]), sorted([*INPUT_PATHS, "admin/ui/src/App.vue"]))
        self.assertEqual(recorded["inputs"]["admin/ui/public/README.md"],
                         {"size": len("admin/ui/public/README.md"),
                          "sha256": hashlib.sha256(b"admin/ui/public/README.md").hexdigest()})

    def test_writing_needs_a_dist_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "admin/ui/index.html")
            with self.assertRaises(FileNotFoundError):
                build_inputs.write_manifest(Path(tmp))

    def test_the_build_script_writes_the_manifest_after_vite(self):
        script = json.loads((REPO / "admin/ui/package.json").read_text())["scripts"]["build"]
        self.assertTrue(script.endswith("vite build && python3 scripts/build_inputs.py write"), script)


if __name__ == "__main__":
    unittest.main()
