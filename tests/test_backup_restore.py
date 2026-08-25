#!/usr/bin/env python3
"""Unit tests for restore-target safety validation."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from backup_restore import RestoreTargetError, validate_restore_target


class RestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.artifacts = self.base / "artifacts"
        self.browser_tmp = self.base / "browser-tmp"
        self.artifacts.mkdir()
        self.browser_tmp.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_separate_target(self):
        target = self.base / "restore-out"
        resolved = validate_restore_target(
            target,
            artifacts_root=self.artifacts,
            browser_tmp_root=self.browser_tmp,
            backup_root=self.base / "backups",
            repo=self.base / "backups" / "restic-repo",
            password_file=self.base / "backups" / "restic-password",
            compose_dir=self.base / "compose",
            compose_file=self.base / "compose" / "backup.yml",
        )
        self.assertEqual(resolved, target.resolve())

    def test_rejects_artifacts_root(self):
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                self.artifacts,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_subdirectory_of_artifacts(self):
        nested = self.artifacts / "my-bottle" / "file.txt"
        nested.parent.mkdir(parents=True)
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                nested.parent,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_parent_of_artifacts(self):
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                self.base,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_browser_tmp_tree(self):
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                self.browser_tmp / "job",
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_backup_root(self):
        backups = self.base / "backups"
        backups.mkdir()
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                backups,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=backups,
                repo=backups / "restic-repo",
                password_file=backups / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_restic_repo_path(self):
        backups = self.base / "backups"
        repo = backups / "restic-repo"
        repo.mkdir(parents=True)
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                repo,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=backups,
                repo=repo,
                password_file=backups / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_password_file_path(self):
        backups = self.base / "backups"
        backups.mkdir()
        password = backups / "restic-password"
        password.write_text("secret\n")
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                password,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=backups,
                repo=backups / "restic-repo",
                password_file=password,
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_compose_overlay_paths(self):
        compose_dir = self.base / "compose"
        compose_dir.mkdir()
        compose_file = compose_dir / "backup.yml"
        compose_file.write_text("services: {}")
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                compose_file,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=compose_dir,
                compose_file=compose_file,
            )

    def test_rejects_existing_file_target(self):
        file_target = self.base / "restore-file"
        file_target.write_text("not a directory")
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                file_target,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_rejects_non_empty_existing_directory(self):
        target = self.base / "restore-out"
        target.mkdir()
        (target / "leftover.txt").write_text("stale")
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                target,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_accepts_empty_existing_directory(self):
        target = self.base / "restore-empty"
        target.mkdir()
        resolved = validate_restore_target(
            target,
            artifacts_root=self.artifacts,
            browser_tmp_root=self.browser_tmp,
            backup_root=self.base / "backups",
            repo=self.base / "backups" / "restic-repo",
            password_file=self.base / "backups" / "restic-password",
            compose_dir=self.base / "compose",
            compose_file=self.base / "compose" / "backup.yml",
        )
        self.assertEqual(resolved, target.resolve())

    def test_rejects_symlink_to_non_empty_directory(self):
        real = self.base / "real-target"
        real.mkdir()
        (real / "data.txt").write_text("content")
        link = self.base / "restore-link"
        link.symlink_to(real)
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                link,
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )

    def test_accepts_symlink_to_empty_directory(self):
        real = self.base / "empty-target"
        real.mkdir()
        link = self.base / "restore-link-empty"
        link.symlink_to(real)
        resolved = validate_restore_target(
            link,
            artifacts_root=self.artifacts,
            browser_tmp_root=self.browser_tmp,
            backup_root=self.base / "backups",
            repo=self.base / "backups" / "restic-repo",
            password_file=self.base / "backups" / "restic-password",
            compose_dir=self.base / "compose",
            compose_file=self.base / "compose" / "backup.yml",
        )
        self.assertEqual(resolved, real.resolve())

    def test_requires_explicit_target(self):
        with self.assertRaises(RestoreTargetError):
            validate_restore_target(
                "",
                artifacts_root=self.artifacts,
                browser_tmp_root=self.browser_tmp,
                backup_root=self.base / "backups",
                repo=self.base / "backups" / "restic-repo",
                password_file=self.base / "backups" / "restic-password",
                compose_dir=self.base / "compose",
                compose_file=self.base / "compose" / "backup.yml",
            )


if __name__ == "__main__":
    unittest.main()
