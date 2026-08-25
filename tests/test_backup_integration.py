#!/usr/bin/env python3
"""Docker/restic integration smoke test for the singleton backup stack.

Skips automatically when docker is unavailable. CI invokes this explicitly
after building the backup image.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import backup_config  # noqa: E402


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, check=True, **kwargs)


@unittest.skipUnless(docker_available(), "docker not available")
class BackupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "djinn-home"
        self.home.mkdir()
        artifacts = self.home / "artifacts" / "sample-bottle"
        artifacts.mkdir(parents=True)
        (artifacts / "note.txt").write_text("original artifact content\n")
        browser = self.home / "browser-tmp" / "exchange"
        browser.mkdir(parents=True)
        (browser / "state.json").write_text('{"tab": "home"}\n')

        compose_path = backup_config.write_compose_file(self.home)
        self.identity = backup_config.derive_identity(self.home)
        self.compose_file = compose_path
        self.repo_root = REPO_ROOT

        ci_image = os.environ.get("DJINN_BACKUP_CI_IMAGE", "")
        if ci_image:
            _run(
                [
                    "docker",
                    "tag",
                    ci_image,
                    self.identity.image_tag,
                ]
            )
        else:
            _run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.identity.compose_project_name,
                    "--project-directory",
                    str(self.repo_root),
                    "-f",
                    str(self.compose_file),
                    "build",
                ]
            )

    def tearDown(self):
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.identity.compose_project_name,
                    "--project-directory",
                    str(self.repo_root),
                    "-f",
                    str(self.compose_file),
                    "down",
                    "-v",
                ],
                capture_output=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        self._tmp.cleanup()

    def _compose_run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "docker",
                "compose",
                "-p",
                self.identity.compose_project_name,
                "--project-directory",
                str(self.repo_root),
                "-f",
                str(self.compose_file),
                "run",
                "--rm",
                *args,
            ],
            capture_output=True,
        )

    def test_init_backup_snapshots_check_restore_roundtrip(self):
        self._compose_run(backup_config.SERVICE_NAME, "backup")

        snapshots = self._compose_run(
            "--entrypoint",
            "restic",
            backup_config.SERVICE_NAME,
            "snapshots",
        )
        self.assertIn("scheduled", snapshots.stdout)

        check = self._compose_run(
            "--entrypoint",
            "restic",
            backup_config.SERVICE_NAME,
            "check",
        )
        self.assertEqual(check.returncode, 0)

        artifact_file = self.home / "artifacts" / "sample-bottle" / "note.txt"
        artifact_file.write_text("destructively changed\n")

        restore_target = self.home / "restore-target"
        restore_target.mkdir()
        self._compose_run(
            "-v",
            f"{restore_target}:/restore",
            backup_config.SERVICE_NAME,
            "restore",
            "latest",
            "/restore",
        )

        restored = restore_target / "sources" / "artifacts" / "sample-bottle" / "note.txt"
        self.assertTrue(restored.is_file())
        self.assertEqual(restored.read_text(), "original artifact content\n")

        restored_browser = restore_target / "sources" / "browser-tmp" / "exchange" / "state.json"
        self.assertTrue(restored_browser.is_file())
        self.assertIn("home", restored_browser.read_text())


if __name__ == "__main__":
    unittest.main()
