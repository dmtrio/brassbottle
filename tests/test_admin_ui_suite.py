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


if __name__ == "__main__":
    unittest.main()
