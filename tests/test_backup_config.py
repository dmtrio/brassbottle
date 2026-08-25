#!/usr/bin/env python3
"""Unit tests for singleton backup compose generation and path layout."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import backup_config


def _load_compose_dict(compose_text: str) -> dict:
    result = subprocess.run(
        ["yq", "-o=json", "-"],
        input=compose_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class BackupConfigTests(unittest.TestCase):
    def test_derive_identity_is_stable_per_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            first = backup_config.derive_identity(base)
            second = backup_config.derive_identity(base)
            self.assertEqual(first, second)
            self.assertEqual(len(first.suffix), backup_config.IDENTITY_SUFFIX_LENGTH)
            self.assertTrue(first.compose_project_name.startswith("djinn-backup-"))

    def test_derive_identity_differs_for_different_homes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_a = backup_config.derive_identity(root / "a")
            home_b = backup_config.derive_identity(root / "b")
            self.assertNotEqual(home_a.compose_project_name, home_b.compose_project_name)

    def test_paths_under_djinn_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            p = backup_config.paths(base)
            self.assertEqual(p["artifacts_root"], base / "artifacts")
            self.assertEqual(p["browser_tmp_root"], base / "browser-tmp")
            self.assertEqual(p["repo"], base / "backups" / "restic-repo")
            self.assertEqual(p["password_file"], base / "backups" / "restic-password")
            self.assertEqual(p["browser_root"], base / "backups" / "browser")
            self.assertEqual(p["browser_config_file"], base / "backups" / "browser" / "config" / "config.json")

    def test_ensure_layout_creates_password_with_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            self.assertTrue(p["password_file"].is_file())
            self.assertEqual(p["password_file"].stat().st_mode & 0o777, 0o600)
            self.assertGreater(len(p["password_file"].read_text().strip()), 16)

    def _render_compose(self, base: Path, p: dict) -> str:
        identity = backup_config.derive_identity(base)
        bind = backup_config.load_browser_bind_config()
        return backup_config.render_compose_yaml(
            identity=identity,
            artifacts_root=p["artifacts_root"],
            browser_tmp_root=p["browser_tmp_root"],
            backup_repo=p["repo"],
            password_file=p["password_file"],
            browser_config_dir=p["browser_config_dir"],
            browser_data_dir=p["browser_data_dir"],
            browser_cache_dir=p["browser_cache_dir"],
            browser_bind_host=str(bind["host"]),
            browser_bind_port=int(bind["port"]),
        )

    def test_render_compose_has_readonly_source_mounts_and_singleton_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            identity = backup_config.derive_identity(base)
            text = self._render_compose(base, p)
            self.assertIn(f"container_name: {identity.container_name}", text)
            self.assertIn(f"hostname: {identity.hostname}", text)
            self.assertIn(f"image: {identity.image_tag}", text)
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
            self.assertIn("RESTIC_REPOSITORY: /repo", text)
            self.assertIn('RETENTION_HOURLY: "48"', text)
            self.assertIn('RETENTION_DAILY: "30"', text)
            self.assertIn('PRUNE_INTERVAL_SECONDS: "86400"', text)
            self.assertIn(backup_config.BACKREST_IMAGE, text)
            self.assertIn(f"container_name: {identity.container_name}-browser", text)
            backup_config.browser_compose_must_not_mount_sources_or_scheduler(text)

    def test_render_compose_parses_with_yq_and_structural_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            text = self._render_compose(base, p)
            data = _load_compose_dict(text)
            backup_config.validate_generated_compose_structure(data)
            services = data["services"]
            self.assertIn(backup_config.SERVICE_NAME, services)
            self.assertIn(backup_config.BROWSER_SERVICE_NAME, services)
            self.assertEqual(
                services[backup_config.BROWSER_SERVICE_NAME]["image"],
                backup_config.BACKREST_IMAGE,
            )
            browser_ports = services[backup_config.BROWSER_SERVICE_NAME]["ports"]
            self.assertTrue(str(browser_ports[0]).startswith("127.0.0.1:"))

    def test_render_compose_quotes_paths_with_spaces_and_colons(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "my home" / "djinn:1"
            p = backup_config.ensure_layout(base)
            identity = backup_config.derive_identity(base)
            text = self._render_compose(base, p)
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
            identity = backup_config.derive_identity(base)
            self.assertEqual(out, base / "compose" / "backup.yml")
            self.assertTrue(out.is_file())
            self.assertIn(identity.container_name, out.read_text())

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

    def test_load_browser_bind_config_defaults_to_localhost(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = backup_config.load_browser_bind_config()
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["port"], 9898)

    def test_load_browser_bind_config_rejects_non_loopback_host(self):
        with mock.patch.dict(os.environ, {backup_config.ENV_BROWSER_HOST: "0.0.0.0"}, clear=True):
            with self.assertRaises(backup_config.BackupConfigError):
                backup_config.load_browser_bind_config()

    def test_load_browser_bind_config_validates_port(self):
        with mock.patch.dict(os.environ, {backup_config.ENV_BROWSER_PORT: "70000"}, clear=True):
            with self.assertRaises(backup_config.BackupConfigError):
                backup_config.load_browser_bind_config()

    def test_browser_url_formats_ipv6(self):
        self.assertEqual(backup_config.browser_url("::1", 9898), "http://[::1]:9898/")

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
