"""Guard: every module egress_broker.py/egress_nflog.py import at runtime
INSIDE the container is actually COPYed into the image.

finding #1 (BLOCKER): egress_broker.py and egress_nflog.py (which run inside
the container from /usr/local/lib/djinn, see src/entrypoint.sh) import
egress_broker_host, which imports egress_denylist at module top — but the
Dockerfile never COPYed src/egress_denylist.py, so the image would crash on
boot with ModuleNotFoundError. This test proves the exact file set the
Dockerfile COPYs into /usr/local/lib/djinn is sufficient to import the three
broker-side entry points, the same way python3 would inside the built image
(PYTHONPATH=/usr/local/lib/djinn per entrypoint.sh).

Parses the Dockerfile's own `COPY src/<file> /usr/local/lib/djinn/<file>`
lines rather than hard-coding a file list: this fails the same way a real
image build would if a future file gets added to one side (src/ import) but
not the other (Dockerfile COPY) — the actual regression this finding fixes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
SRC_DIR = REPO_ROOT / "src"

# COPY src/<file> /usr/local/lib/djinn/<file> — the exact form every
# broker-side module is copied with (egress_request.py is ALSO copied to
# /usr/local/bin/request-egress under a different name; that second COPY line
# does not match this pattern and is correctly excluded).
_COPY_RE = re.compile(
    r"^COPY\s+src/([A-Za-z0-9_.-]+)\s+/usr/local/lib/djinn/\1\s*$",
    re.MULTILINE,
)

# The modules that actually run inside the container (src/entrypoint.sh:
# `PYTHONPATH=/usr/local/lib/djinn python3 /usr/local/lib/djinn/egress_broker.py`
# and the same for egress_nflog.py); egress_broker_host is imported by both,
# never invoked directly in-container, but must import cleanly too.
BROKER_SIDE_MODULES = ("egress_broker", "egress_nflog", "egress_broker_host")


def _copied_files() -> list[str]:
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    return _COPY_RE.findall(text)


class ImagePythonImportsTests(unittest.TestCase):
    def test_dockerfile_copies_a_nonempty_file_set(self):
        # Sanity check on the parser itself: if this ever returns [], every
        # other assertion in this file would trivially pass for the wrong
        # reason (an empty tmp dir "importing successfully" because nothing
        # was even attempted).
        files = _copied_files()
        self.assertGreater(len(files), 0)
        for name in BROKER_SIDE_MODULES:
            self.assertIn(f"{name}.py", files, f"Dockerfile never COPYs src/{name}.py")

    def test_broker_side_modules_import_from_exactly_the_copied_file_set(self):
        files = _copied_files()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for name in files:
                src = SRC_DIR / name
                if not src.is_file():
                    # Not every COPY target need be a src/*.py module (e.g. a
                    # future non-python asset) — only copy what exists.
                    continue
                shutil.copy2(src, tmp_path / name)

            import_stmt = "import " + ", ".join(BROKER_SIDE_MODULES)
            result = subprocess.run(
                [sys.executable, "-c", import_stmt],
                cwd=str(tmp_path),
                env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"import failed with exactly the Dockerfile's COPY set:\n"
                f"stdout={result.stdout}\nstderr={result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
