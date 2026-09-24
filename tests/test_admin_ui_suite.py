"""Unit tests for the helpers of the admin behaviour suite (tests/admin_ui_playwright.py)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admin_ui_playwright as suite  # noqa: E402


def _touch(path: Path, mtime: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, ns=(mtime, mtime))
    return path


class SourcesNewerThanBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bundle = _touch(self.root / "dist" / "index.html", 2_000)

    def test_a_bundle_newer_than_every_source_is_fresh(self):
        _touch(self.root / "src" / "a.vue", 1_000)
        _touch(self.root / "src" / "components" / "b.ts", 2_000)
        self.assertEqual(suite.sources_newer_than_bundle(self.root / "src", self.bundle), [])

    def test_any_source_newer_than_the_bundle_is_reported_by_path(self):
        _touch(self.root / "src" / "a.vue", 1_000)
        late = _touch(self.root / "src" / "components" / "deep" / "b.ts", 2_001)
        self.assertEqual(suite.sources_newer_than_bundle(self.root / "src", self.bundle), [late])


if __name__ == "__main__":
    unittest.main()
