#!/usr/bin/env python3
"""Unit tests for singleton backup compose generation and path layout."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import backup_config


class BackupConfigTests(unittest.TestCase):
    def test_singleton_naming_constants(self):
        self.assertEqual(backup_config.COMPOSE_PROJECT_NAME, "djinn-backup")
        self.assertEqual(backup_config.CONTAINER_NAME, "djinn-backup")
        self.assertEqual(backup_config.SERVICE_NAME, "backup")

    def test_paths_under_djinn_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            p = backup_config.paths(base)
            self.assertEqual(p["artifacts_root"], base / "artifacts")
            self.assertEqual(p["browser_tmp_root"], base / "browser-tmp")
            self.assertEqual(p["repo"], base / "backups" / "restic-repo")
            self.assertEqual(p["password_file"], base / "backups" / "restic-password")

    def test_ensure_layout_creates_password_with_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            self.assertTrue(p["password_file"].is_file())
            self.assertEqual(p["password_file"].stat().st_mode & 0o777, 0o600)
            self.assertGreater(len(p["password_file"].read_text().strip()), 16)

    def test_render_compose_has_readonly_source_mounts_and_singleton_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            text = backup_config.render_compose_yaml(
                artifacts_root=p["artifacts_root"],
                browser_tmp_root=p["browser_tmp_root"],
                backup_repo=p["repo"],
                password_file=p["password_file"],
            )
            self.assertIn("container_name: djinn-backup", text)
            self.assertIn("hostname: djinn-backup", text)
            self.assertIn(
                backup_config._yaml_double_quoted(
                    f'{p["artifacts_root"]}:{backup_config.SOURCE_ARTIFACTS_MOUNT}:ro'
                ),
                text,
            )
            self.assertIn(
                backup_config._yaml_double_quoted(
                    f'{p["browser_tmp_root"]}:{backup_config.SOURCE_BROWSER_TMP_MOUNT}:ro'
                ),
                text,
            )
            self.assertIn(
                backup_config._yaml_double_quoted(f'{p["repo"]}:{backup_config.REPO_MOUNT}'),
                text,
            )
            self.assertIn('BACKUP_INTERVAL_SECONDS: "600"', text)
            self.assertIn('RETENTION_HOURLY: "48"', text)
            self.assertIn('RETENTION_DAILY: "30"', text)

    def test_render_compose_quotes_paths_with_spaces_and_colons(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "my home" / "djinn:1"
            p = backup_config.ensure_layout(base)
            text = backup_config.render_compose_yaml(
                artifacts_root=p["artifacts_root"],
                browser_tmp_root=p["browser_tmp_root"],
                backup_repo=p["repo"],
                password_file=p["password_file"],
            )
            quoted_artifacts = backup_config._yaml_double_quoted(
                f'{p["artifacts_root"]}:{backup_config.SOURCE_ARTIFACTS_MOUNT}:ro'
            )
            self.assertIn(quoted_artifacts, text)
            self.assertNotIn(f"- {p['artifacts_root']}:", text)

    def test_bottle_compose_must_not_reference_backup_repo(self):
        bottle = Path(__file__).parent.parent / "compose" / "docker-compose.local.yml"
        backup_config.bottle_compose_must_not_reference_backup(bottle.read_text())
        with self.assertRaises(backup_config.BackupConfigError):
            backup_config.bottle_compose_must_not_reference_backup(
                "volumes:\n  - /djinn/backups/restic-repo:/repo\n"
            )

    def test_write_compose_file_places_overlay_under_djinn_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = backup_config.write_compose_file(base)
            self.assertEqual(out, base / "compose" / "backup.yml")
            self.assertTrue(out.is_file())
            self.assertIn("djinn-backup", out.read_text())

    def test_load_host_backup_config_reads_djinn_backup_env(self):
        with mock.patch.dict(
            os.environ,
            {
                backup_config.ENV_BACKUP_INTERVAL: "120",
                backup_config.ENV_RETENTION_HOURLY: "12",
                backup_config.ENV_RETENTION_DAILY: "7",
                backup_config.ENV_PRUNE_INTERVAL: "3600",
            },
            clear=False,
        ):
            cfg = backup_config.load_host_backup_config()
        self.assertEqual(cfg["backup_interval_seconds"], 120)
        self.assertEqual(cfg["retention_hourly"], 12)
        self.assertEqual(cfg["retention_daily"], 7)
        self.assertEqual(cfg["prune_interval_seconds"], 3600)

    def test_load_host_backup_config_rejects_invalid_env(self):
        with mock.patch.dict(os.environ, {backup_config.ENV_BACKUP_INTERVAL: "0"}, clear=False):
            with self.assertRaises(backup_config.BackupConfigError):
                backup_config.load_host_backup_config()

    def test_write_compose_file_applies_djinn_backup_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.dict(os.environ, {backup_config.ENV_BACKUP_INTERVAL: "900"}, clear=False):
                out = backup_config.write_compose_file(base)
            self.assertIn('BACKUP_INTERVAL_SECONDS: "900"', out.read_text())

    def test_create_password_atomic_uses_mode_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            password = Path(tmp) / "backups" / "restic-password"
            (Path(tmp) / "backups").mkdir()
            backup_config._create_password_atomic(password)
            self.assertTrue(password.is_file())
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)

    def test_create_password_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            password = base / "backups" / "restic-password"
            (base / "backups").mkdir(parents=True)
            real = base / "backups" / "real-password"
            real.write_text("secret\n")
            password.symlink_to(real)
            with self.assertRaises(backup_config.BackupConfigError):
                backup_config._create_password_atomic(password)

    def test_create_password_rejects_non_regular_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            password = base / "backups" / "restic-password"
            (base / "backups").mkdir(parents=True)
            os.mkfifo(password)
            with self.assertRaises(backup_config.BackupConfigError):
                backup_config._create_password_atomic(password)

    def test_concurrent_password_creators_leave_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "backups").mkdir(parents=True)
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def creator() -> None:
                try:
                    barrier.wait()
                    backup_config._create_password_atomic(base / "backups" / "restic-password")
                except Exception as exc:  # noqa: BLE001 — collect thread errors for assertion
                    errors.append(exc)

            threads = [threading.Thread(target=creator), threading.Thread(target=creator)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            password = base / "backups" / "restic-password"
            self.assertFalse(errors)
            self.assertTrue(password.is_file())
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)

    def test_ensure_layout_mkdir_oserror_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(
                Path,
                "mkdir",
                side_effect=OSError(13, "Permission denied"),
            ):
                with self.assertRaises(backup_config.BackupConfigError) as ctx:
                    backup_config.ensure_layout(base)
            self.assertIn("cannot create", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_write_compose_file_write_oserror_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(
                Path,
                "write_text",
                side_effect=OSError(13, "Permission denied"),
            ):
                with self.assertRaises(backup_config.BackupConfigError) as ctx:
                    backup_config.write_compose_file(base)
            self.assertIn("cannot write compose overlay", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
