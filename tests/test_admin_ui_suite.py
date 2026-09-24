"""Unit tests for the helpers of the admin behaviour suite (tests/admin_ui_playwright.py)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admin_ui_playwright as suite  # noqa: E402

OLD, BUILT, LATER = 1_000, 2_000, 3_000


def _touch(path: Path, mtime: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, ns=(mtime, mtime))
    return path


def _age(path: Path, mtime: int) -> None:
    os.utime(path, ns=(mtime, mtime))


class BundleRefusalTests(unittest.TestCase):
    """bundle_refusal(root) is what main() calls, on the real repo layout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.app = _touch(self.root / "admin/ui/src/App.vue", OLD)
        self.gone = _touch(self.root / "admin/ui/src/components/Gone.vue", OLD)
        self.schema = _touch(self.root / "admin/contract/queue_snapshot.schema.json", OLD)
        _touch(self.root / "admin/ui/index.html", OLD)
        # node_modules is not a build input the guard watches; a newer file there is ignored.
        _touch(self.root / "admin/ui/node_modules/x/index.js", LATER)
        for directory in ("admin/ui/src/components", "admin/ui/src", "admin/contract"):
            _age(self.root / directory, OLD)
        self.bundle = _touch(self.root / suite.BUNDLE, BUILT)

    def test_a_fresh_bundle_is_accepted(self):
        self.assertIsNone(suite.bundle_refusal(self.root))

    def test_a_touched_source_is_refused_and_named(self):
        _age(self.app, LATER)
        refusal = suite.bundle_refusal(self.root)
        self.assertIsNotNone(refusal)
        self.assertIn("admin/ui/src/App.vue", refusal)

    def test_a_deleted_source_is_refused_through_its_directory(self):
        self.gone.unlink()
        _age(self.root / "admin/ui/src/components", LATER)
        refusal = suite.bundle_refusal(self.root)
        self.assertIsNotNone(refusal)
        self.assertIn("admin/ui/src/components", refusal)

    def test_a_contract_edit_is_refused(self):
        _age(self.schema, LATER)
        refusal = suite.bundle_refusal(self.root)
        self.assertIsNotNone(refusal)
        self.assertIn("admin/contract/queue_snapshot.schema.json", refusal)

    def test_a_missing_bundle_is_refused(self):
        self.bundle.unlink()
        self.assertIn("not built", suite.bundle_refusal(self.root))


# The path that stands for each BUILD_INPUTS entry when the test touches it. A new
# entry without a line here fails test_every_entry_has_a_touch_case.
TOUCH = {
    "admin/ui/src": "admin/ui/src/components/Deep.vue",
    "admin/ui/public": "admin/ui/public/favicon.svg",
    "admin/ui/scripts": "admin/ui/scripts/gen-types.ts",
    "admin/ui/index.html": "admin/ui/index.html",
    "admin/ui/vite.config.ts": "admin/ui/vite.config.ts",
    "admin/ui/package.json": "admin/ui/package.json",
    "admin/ui/package-lock.json": "admin/ui/package-lock.json",
    "admin/ui/tsconfig*.json": "admin/ui/tsconfig.app.json",
    "admin/contract/": "admin/contract",
    "admin/contract/*.schema.json": "admin/contract/recent_page.schema.json",
}

# Files the build never reads, inside the watched trees.
NOT_INPUTS = (
    "admin/ui/README.md",
    "admin/ui/.gitignore",
    "admin/ui/.eslintcache",
    "admin/ui/src/.App.vue.swp",
    "admin/ui/src/App.vue~",
    "admin/ui/src/#App.vue#",
    "admin/ui/src/4913",
    "admin/ui/src/.cache/chunk.js",
    "admin/ui/scripts/README.md",
    "admin/ui/scripts/.gen-types.ts.swo",
    "admin/ui/public/.DS_Store",
    "admin/contract/README.md",
    "admin/contract/notes.txt",
    "admin/contract/queue_snapshot.schema.json.orig",
    "admin/contract/.recent_page.schema.json.swp",
)


class BuildInputsTests(unittest.TestCase):
    """Every input the build reads makes a stale bundle refuse; nothing else does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for touched in TOUCH.values():
            if touched != "admin/contract":
                _touch(self.root / touched, OLD)
        for extra in ("admin/ui/src/App.vue", "admin/ui/tsconfig.json", "admin/ui/tsconfig.node.json",
                      "admin/contract/queue_snapshot.schema.json"):
            _touch(self.root / extra, OLD)
        for noise in NOT_INPUTS:
            _touch(self.root / noise, OLD)
        _touch(self.root / "admin/ui/node_modules/x/index.js", OLD)
        self.bundle = _touch(self.root / suite.BUNDLE, BUILT)
        self.reset()

    def reset(self):
        """Every input, and every directory, back to before the build."""
        for path in [self.root, *self.root.rglob("*")]:
            if path != self.bundle:
                _age(path, OLD)

    def test_every_entry_has_a_touch_case(self):
        self.assertEqual(sorted(suite.BUILD_INPUTS), sorted(TOUCH))

    def test_the_fixture_is_fresh(self):
        self.assertIsNone(suite.bundle_refusal(self.root))

    def test_touching_any_build_input_refuses_and_names_it(self):
        for entry, touched in TOUCH.items():
            with self.subTest(entry=entry):
                self.reset()
                _age(self.root / touched, LATER)
                refusal = suite.bundle_refusal(self.root)
                self.assertIsNotNone(refusal, f"{touched} changed and the guard accepted the bundle")
                self.assertIn(touched, refusal)

    def test_a_wildcard_entry_covers_every_match(self):
        for touched in ("admin/ui/tsconfig.json", "admin/ui/tsconfig.node.json",
                        "admin/contract/queue_snapshot.schema.json"):
            with self.subTest(touched=touched):
                self.reset()
                _age(self.root / touched, LATER)
                self.assertIn(touched, suite.bundle_refusal(self.root) or "")

    def test_touching_a_non_input_is_ignored(self):
        for noise in NOT_INPUTS:
            with self.subTest(noise=noise):
                self.reset()
                _age(self.root / noise, LATER)
                self.assertIsNone(suite.bundle_refusal(self.root))

    def test_a_schema_deleted_from_the_contract_directory_is_refused(self):
        (self.root / "admin/contract/recent_page.schema.json").unlink()
        _age(self.root / "admin/contract", LATER)
        self.assertIn("admin/contract", suite.bundle_refusal(self.root))


if __name__ == "__main__":
    unittest.main()
