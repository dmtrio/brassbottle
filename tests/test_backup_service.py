#!/usr/bin/env python3
"""Unit tests for in-container backup logging and retention behavior."""

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import backup_service


class BackupServiceLoggingTests(unittest.TestCase):
    def test_log_stage_emits_boundary_metadata(self):
        buf = StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            backup_service.log_stage(
                "run",
                "ok",
                duration_sec=1.5,
                files_new=3,
            )
        line = buf.getvalue().strip()
        self.assertIn("backup run ok", line)
        self.assertIn("duration=1.50s", line)
        self.assertIn("files_new=3", line)

    def test_run_backup_logs_summary_without_file_contents(self):
        stdout = '{"message_type":"summary","files_new":2,"data_added":128}\n'
        with unittest.mock.patch.dict(
            os.environ,
            {"BACKUP_SOURCES": "/sources/artifacts /sources/browser-tmp"},
            clear=False,
        ):
            with unittest.mock.patch(
                "backup_service._run_restic",
                return_value=subprocess.CompletedProcess(
                    ["restic", "backup"], 0, stdout, "",
                ),
            ) as mocked:
                buf = StringIO()
                with unittest.mock.patch("sys.stdout", buf):
                    summary = backup_service.run_backup(["/sources/artifacts"])
        mocked.assert_called_once()
        self.assertEqual(summary.get("files_new"), 2)
        output = buf.getvalue()
        self.assertIn("backup run ok", output)
        self.assertNotIn("secret-file-contents", output)

    def test_run_forget_uses_retention_env_and_runs_check(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"RETENTION_HOURLY": "48", "RETENTION_DAILY": "30"},
            clear=False,
        ):
            with unittest.mock.patch("backup_service._run_restic") as mocked:
                with unittest.mock.patch("backup_service.run_check") as check:
                    backup_service.run_forget()
        args = mocked.call_args[0][0]
        self.assertEqual(args, ["forget", "--keep-hourly", "48", "--keep-daily", "30", "--prune"])
        check.assert_called_once()

    def test_run_forget_surfaces_check_failure(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"RETENTION_HOURLY": "48", "RETENTION_DAILY": "30"},
            clear=False,
        ):
            with unittest.mock.patch("backup_service._run_restic"):
                with unittest.mock.patch(
                    "backup_service.run_check",
                    side_effect=subprocess.CalledProcessError(1, ["restic", "check"]),
                ):
                    with self.assertRaises(subprocess.CalledProcessError):
                        backup_service.run_forget()

    def test_ensure_repo_initialized_skips_when_snapshots_succeed(self):
        with unittest.mock.patch(
            "backup_service.subprocess.run",
            return_value=subprocess.CompletedProcess(["restic", "snapshots"], 0, "", ""),
        ) as mocked:
            backup_service.ensure_repo_initialized()
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args[0][0], ["restic", "snapshots"])

    def test_ensure_repo_initialized_calls_init_on_empty_repo(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["restic", "snapshots"]:
                return subprocess.CompletedProcess(cmd, 1, "", "repo missing")
            if cmd == ["restic", "init"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"unexpected command: {cmd}")

        with unittest.mock.patch("backup_service.subprocess.run", side_effect=fake_run):
            backup_service.ensure_repo_initialized()
        self.assertEqual(calls, [["restic", "snapshots"], ["restic", "init"]])

    def test_ensure_repo_initialized_treats_already_initialized_as_success(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["restic", "snapshots"]:
                return subprocess.CompletedProcess(cmd, 1, "", "repo missing")
            if cmd == ["restic", "init"]:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "Fatal: repository location already initialized\n"
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with unittest.mock.patch("backup_service.subprocess.run", side_effect=fake_run):
            backup_service.ensure_repo_initialized()
        self.assertEqual(calls, [["restic", "snapshots"], ["restic", "init"]])

    def test_ensure_repo_initialized_skips_init_when_repo_has_data_but_snapshots_fail(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["restic", "snapshots"]:
                return subprocess.CompletedProcess(cmd, 1, "", "wrong password")
            raise AssertionError(f"unexpected command: {cmd}")

        with unittest.mock.patch.dict(
            os.environ,
            {"RESTIC_REPOSITORY": "file:/repo"},
            clear=False,
        ):
            with unittest.mock.patch("backup_service._repo_directory_has_data", return_value=True):
                with unittest.mock.patch("backup_service.subprocess.run", side_effect=fake_run):
                    with self.assertRaises(subprocess.CalledProcessError):
                        backup_service.ensure_repo_initialized()
        self.assertEqual(calls, [["restic", "snapshots"]])

    def test_repo_directory_has_data_detects_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "restic-repo"
            repo.mkdir()
            (repo / "config").write_text("restic\n")
            with unittest.mock.patch.dict(
                os.environ,
                {"RESTIC_REPOSITORY": f"file:{repo}"},
                clear=False,
            ):
                self.assertTrue(backup_service._repo_directory_has_data())

    def test_repo_directory_has_data_false_for_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "restic-repo"
            repo.mkdir()
            with unittest.mock.patch.dict(
                os.environ,
                {"RESTIC_REPOSITORY": f"file:{repo}"},
                clear=False,
            ):
                self.assertFalse(backup_service._repo_directory_has_data())

    def test_load_daemon_config_rejects_non_positive_interval(self):
        with unittest.mock.patch.dict(os.environ, {"BACKUP_INTERVAL_SECONDS": "0"}, clear=False):
            with self.assertRaises(backup_service.BackupServiceConfigError):
                backup_service.load_daemon_config()

    def test_load_daemon_config_rejects_invalid_integer(self):
        with unittest.mock.patch.dict(os.environ, {"PRUNE_INTERVAL_SECONDS": "often"}, clear=False):
            with self.assertRaises(backup_service.BackupServiceConfigError):
                backup_service.load_daemon_config()

    def test_should_run_prune_false_until_interval_elapses(self):
        self.assertFalse(backup_service.should_run_prune(100.0, 100.0, 86400))
        self.assertFalse(backup_service.should_run_prune(50000.0, 100.0, 86400))
        self.assertTrue(backup_service.should_run_prune(86501.0, 100.0, 86400))

    def test_daemon_loop_skips_prune_on_first_iteration(self):
        with unittest.mock.patch.dict(
            os.environ,
            {
                "BACKUP_SOURCES": "/sources/artifacts",
                "BACKUP_INTERVAL_SECONDS": "600",
                "PRUNE_INTERVAL_SECONDS": "86400",
            },
            clear=False,
        ):
            with unittest.mock.patch("backup_service.ensure_repo_initialized"):
                with unittest.mock.patch("backup_service.run_backup"):
                    with unittest.mock.patch("backup_service.run_forget") as forget:
                        with unittest.mock.patch("backup_service.time.sleep", side_effect=StopIteration):
                            with self.assertRaises(StopIteration):
                                backup_service.daemon_loop()
        forget.assert_not_called()

    def test_daemon_loop_exits_on_invalid_config(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"BACKUP_SOURCES": "/sources/artifacts", "BACKUP_INTERVAL_SECONDS": "-1"},
            clear=False,
        ):
            with self.assertRaises(SystemExit) as ctx:
                backup_service.daemon_loop()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
