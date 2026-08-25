#!/usr/bin/env python3
"""Unit tests for Backrest browse-only seeding and policy guardrails."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import backup_browser
import backup_config


def _write_restic_config_export(repo: Path, repo_id: str) -> None:
    payload = {"version": 2, "id": repo_id, "chunker_polynomial": "25b468838dcb75"}
    (repo / backup_browser.RESTIC_REPO_CONFIG_JSON).write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )


class BackupBrowserTests(unittest.TestCase):
    def test_build_seed_config_has_no_plans_and_no_lock(self):
        cfg = backup_browser.build_seed_config(instance="djinn-test", repo_guid="a" * 64)
        self.assertEqual(cfg["plans"], [])
        self.assertEqual(cfg["repos"][0]["flags"], ["--no-lock"])
        self.assertFalse(cfg["repos"][0]["autoInitialize"])
        self.assertTrue(cfg["auth"]["disabled"])
        for policy_key in ("prunePolicy", "checkPolicy", "forgetPolicy"):
            self.assertTrue(cfg["repos"][0][policy_key]["schedule"]["disabled"])

    def test_validate_seed_config_rejects_enabled_maintenance_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            payload = backup_browser.build_seed_config(instance="djinn-test", repo_guid="a" * 64)
            payload["repos"][0]["forgetPolicy"]["schedule"]["disabled"] = False
            config_path.write_text(json.dumps(payload) + "\n")
            with self.assertRaises(backup_config.BackupConfigError) as ctx:
                backup_browser.validate_seed_config(config_path)
            self.assertIn("forgetPolicy", str(ctx.exception))

    def test_seed_skips_when_config_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            p["browser_config_file"].parent.mkdir(parents=True, exist_ok=True)
            p["browser_config_file"].write_text("{}\n")
            status = backup_browser.seed_backrest_config(base)
        self.assertEqual(status, "skipped-existing-config")

    def test_seed_writes_import_doc_when_repo_uninitialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            status = backup_browser.seed_backrest_config(base)
            self.assertEqual(status, "skipped-repo-not-initialized")
            self.assertTrue(p["browser_import_doc"].is_file())
            self.assertFalse(p["browser_config_file"].exists())

    def test_seed_creates_config_when_repo_initialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            (p["repo"] / "config").write_bytes(b"\x00" * 8)
            guid = "b" * 64
            _write_restic_config_export(p["repo"], guid)
            status = backup_browser.seed_backrest_config(base)
            self.assertEqual(status, "seeded")
            data = json.loads(p["browser_config_file"].read_text())
            self.assertEqual(data["repos"][0]["guid"], guid)
            self.assertEqual(data["plans"], [])
            self.assertEqual(data["version"], backup_config.BACKREST_CONFIG_VERSION)
            backup_browser.validate_seed_config(p["browser_config_file"])

    def test_seed_fails_when_repo_initialized_but_export_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            (p["repo"] / "config").write_bytes(b"\x00" * 8)
            with self.assertRaises(backup_browser.BrowserSeedError) as ctx:
                backup_browser.seed_backrest_config(base)
            self.assertIn("./djinn backup start", str(ctx.exception))
            self.assertFalse(p["browser_config_file"].exists())

    def test_seed_fails_when_repo_initialized_but_export_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            (p["repo"] / "config").write_bytes(b"\x00" * 8)
            (p["repo"] / backup_browser.RESTIC_REPO_CONFIG_JSON).write_text('{"id":"short"}\n')
            with self.assertRaises(backup_browser.BrowserSeedError):
                backup_browser.seed_backrest_config(base)
            self.assertFalse(p["browser_config_file"].exists())

    def test_seed_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            (p["repo"] / "config").write_bytes(b"\x00" * 8)
            _write_restic_config_export(p["repo"], "c" * 64)
            p["browser_config_file"].parent.mkdir(parents=True, exist_ok=True)
            p["browser_config_file"].write_text('{"plans":[{"id":"keep"}]}\n')
            status = backup_browser.seed_backrest_config(base)
            self.assertEqual(status, "skipped-existing-config")
            self.assertIn("keep", p["browser_config_file"].read_text())

    def test_read_restic_repo_guid_parses_valid_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            guid = "d" * 64
            _write_restic_config_export(repo, guid)
            self.assertEqual(backup_browser.read_restic_repo_guid(repo, None), guid)

    def test_read_restic_repo_guid_rejects_malformed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / backup_browser.RESTIC_REPO_CONFIG_JSON).write_text('{"id":"short"}\n')
            self.assertIsNone(backup_browser.read_restic_repo_guid(repo, None))
            (repo / backup_browser.RESTIC_REPO_CONFIG_JSON).write_text("not-json\n")
            self.assertIsNone(backup_browser.read_restic_repo_guid(repo, None))

    def test_read_restic_repo_guid_missing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.assertIsNone(backup_browser.read_restic_repo_guid(repo, None))

    def test_parse_restic_config_json_validates_hex_id(self):
        guid = "e" * 64
        self.assertEqual(
            backup_browser.parse_restic_config_json(json.dumps({"id": guid})),
            guid,
        )
        self.assertIsNone(backup_browser.parse_restic_config_json(json.dumps({"id": "zz" * 32})))

    def test_browser_paths_isolated_under_backups_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = backup_config.ensure_layout(base)
            for key in ("browser_config_dir", "browser_data_dir", "browser_cache_dir"):
                self.assertTrue(str(p[key]).startswith(str(p["browser_root"])))
            self.assertNotIn("compose", str(p["browser_root"]))


if __name__ == "__main__":
    unittest.main()
