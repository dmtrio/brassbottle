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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import backup_config  # noqa: E402
import backup_browser  # noqa: E402


def docker_available() -> bool:
    if os.environ.get("DJINN_SKIP_BACKUP_INTEGRATION") == "1":
        return False
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
    result = subprocess.run(cmd, text=True, check=False, **kwargs)
    if result.returncode != 0:
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        raise AssertionError(
            f"command failed rc={result.returncode}: {' '.join(cmd)}\n"
            f"stdout:\n{stdout[-4000:]}\nstderr:\n{stderr[-4000:]}"
        )
    return result


class BackupIntegrationDiscoveryTests(unittest.TestCase):
    def test_explicit_skip_prevents_early_docker_integration(self):
        with mock.patch.dict(
            os.environ, {"DJINN_SKIP_BACKUP_INTEGRATION": "1"}, clear=False
        ):
            with mock.patch("subprocess.run") as run:
                self.assertFalse(docker_available())
        run.assert_not_called()

    def test_run_surfaces_captured_output_on_failure(self):
        failed = subprocess.CompletedProcess(["example"], 7, "out detail", "err detail")
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(AssertionError) as ctx:
                _run(["example"], capture_output=True)
        message = str(ctx.exception)
        self.assertIn("rc=7", message)
        self.assertIn("out detail", message)
        self.assertIn("err detail", message)


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
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "sh",
                    "-v",
                    f"{self.home}:/cleanup",
                    self.identity.image_tag,
                    "-c",
                    "chmod -R a+rwX /cleanup",
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

    def test_backrest_browser_repo_mount_is_read_only(self):
        """Backrest cannot mutate the restic repository (read-only mount)."""
        self._compose_run(backup_config.SERVICE_NAME, "backup")

        seed_status = backup_browser.seed_backrest_config(self.home)
        self.assertIn(seed_status, ("seeded", "skipped-existing-config"))

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
                "up",
                "-d",
                backup_config.BROWSER_SERVICE_NAME,
            ]
        )

        restic_env = (
            "RESTIC_REPOSITORY=/repo RESTIC_PASSWORD_FILE=/run/secrets/restic-password"
        )

        before = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.identity.compose_project_name,
                "--project-directory",
                str(self.repo_root),
                "-f",
                str(self.compose_file),
                "exec",
                "-T",
                backup_config.BROWSER_SERVICE_NAME,
                "sh",
                "-c",
                f"{restic_env} restic snapshots",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(before.returncode, 0)

        write_attempt = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.identity.compose_project_name,
                "--project-directory",
                str(self.repo_root),
                "-f",
                str(self.compose_file),
                "exec",
                "-T",
                backup_config.BROWSER_SERVICE_NAME,
                "sh",
                "-c",
                "touch /repo/.djinn-ro-test 2>&1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(write_attempt.returncode, 0)

        backup_attempt = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.identity.compose_project_name,
                "--project-directory",
                str(self.repo_root),
                "-f",
                str(self.compose_file),
                "exec",
                "-T",
                backup_config.BROWSER_SERVICE_NAME,
                "sh",
                "-c",
                "RESTIC_REPOSITORY=/repo RESTIC_PASSWORD_FILE=/run/secrets/restic-password "
                "restic backup /tmp --no-lock 2>&1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(backup_attempt.returncode, 0)

        after = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.identity.compose_project_name,
                "--project-directory",
                str(self.repo_root),
                "-f",
                str(self.compose_file),
                "exec",
                "-T",
                backup_config.BROWSER_SERVICE_NAME,
                "sh",
                "-c",
                f"{restic_env} restic snapshots",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(after.returncode, 0)
        self.assertEqual(after.stdout, before.stdout)

        compose_text = self.compose_file.read_text()
        backup_config.browser_compose_must_not_mount_sources_or_scheduler(compose_text)
        self.assertIn('"127.0.0.1:', compose_text)
        self.assertIn(backup_config.BACKREST_IMAGE, compose_text)


if __name__ == "__main__":
    unittest.main()
