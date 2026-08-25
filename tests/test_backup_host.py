#!/usr/bin/env python3
"""Unit tests for host-side backup operator argument validation."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import backup_host


class BackupHostTests(unittest.TestCase):
    def test_restore_requires_target_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                backup_host.main(["--base-path", tmp, "restore", "latest"])
            self.assertEqual(ctx.exception.code, 2)

    def test_restore_rejects_live_artifacts_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "artifacts").mkdir()
            (base / "browser-tmp").mkdir()
            (base / "compose").mkdir()
            (base / "compose" / "backup.yml").write_text("services: {}")
            with mock.patch("backup_host._run", return_value=mock.Mock(returncode=0)):
                rc = backup_host.main(
                    [
                        "--base-path",
                        str(base),
                        "restore",
                        "latest",
                        "--target",
                        str(base / "artifacts"),
                    ]
                )
        self.assertEqual(rc, 1)

    def test_status_reports_running_only_when_service_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "compose").mkdir()
            (base / "compose" / "backup.yml").write_text("services: {}")
            stdout = io.StringIO()
            with mock.patch(
                "backup_host._run",
                return_value=mock.Mock(returncode=0, stdout="backup\n"),
            ) as mocked:
                with mock.patch("sys.stdout", stdout):
                    rc = backup_host.main(["--base-path", str(base), "status"])
            args = mocked.call_args[0][0]
            self.assertIn("--status", args)
            self.assertIn("running", args)
            self.assertIn("--services", args)
            self.assertEqual(rc, 0)
            self.assertIn("backup status running", stdout.getvalue())

    def test_status_reports_stopped_when_service_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "compose").mkdir()
            (base / "compose" / "backup.yml").write_text("services: {}")
            stdout = io.StringIO()
            with mock.patch(
                "backup_host._run",
                return_value=mock.Mock(returncode=0, stdout=""),
            ):
                with mock.patch("sys.stdout", stdout):
                    rc = backup_host.main(["--base-path", str(base), "status"])
            self.assertEqual(rc, 1)
            self.assertIn("backup status stopped", stdout.getvalue())

    def test_status_reports_stopped_when_exited_container_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "compose").mkdir()
            (base / "compose" / "backup.yml").write_text("services: {}")
            stdout = io.StringIO()
            # Exited containers are excluded by --status running; empty services list.
            with mock.patch(
                "backup_host._run",
                return_value=mock.Mock(returncode=0, stdout=""),
            ):
                with mock.patch("sys.stdout", stdout):
                    rc = backup_host.main(["--base-path", str(base), "status"])
            self.assertEqual(rc, 1)
            self.assertIn("backup status stopped", stdout.getvalue())

    def test_stop_returns_compose_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "compose").mkdir()
            (base / "compose" / "backup.yml").write_text("services: {}")
            stderr = io.StringIO()
            with mock.patch(
                "backup_host._run",
                return_value=mock.Mock(returncode=1),
            ):
                with mock.patch("sys.stderr", stderr):
                    rc = backup_host.main(["--base-path", str(base), "stop"])
            self.assertEqual(rc, 1)
            self.assertIn("backup stop error", stderr.getvalue())

    def test_start_returns_compose_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stderr = io.StringIO()
            with mock.patch("backup_host.write_compose_file", return_value=base / "compose" / "backup.yml"):
                with mock.patch(
                    "backup_host._run",
                    return_value=mock.Mock(returncode=1),
                ):
                    with mock.patch("sys.stderr", stderr):
                        rc = backup_host.main(["--base-path", str(base), "start"])
            self.assertEqual(rc, 1)
            self.assertIn("backup start error", stderr.getvalue())

    def test_start_config_error_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with mock.patch(
                "backup_host.write_compose_file",
                side_effect=backup_host.BackupConfigError("bad interval"),
            ):
                with mock.patch("sys.stderr", stderr):
                    rc = backup_host.main(["--base-path", tmp, "start"])
            self.assertEqual(rc, 1)
            self.assertIn("backup start error", stderr.getvalue())
            self.assertIn("bad interval", stderr.getvalue())

    def test_run_without_capture_leaves_stdout_none(self):
        with mock.patch(
            "backup_host.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=None),
        ) as mocked:
            backup_host._run(["echo", "hello"])
        self.assertNotIn("capture_output", mocked.call_args.kwargs)

    def _compose_base(self, tmp: str) -> Path:
        base = Path(tmp)
        (base / "compose").mkdir()
        (base / "compose" / "backup.yml").write_text("services: {}")
        return base

    def test_start_missing_docker_returns_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home with spaces"
            stderr = io.StringIO()
            with mock.patch(
                "backup_host.subprocess.run",
                side_effect=FileNotFoundError(2, "No such file or directory", "docker"),
            ):
                with mock.patch("sys.stderr", stderr):
                    rc = backup_host.main(["--base-path", str(base), "start"])
            self.assertEqual(rc, backup_host.DOCKER_MISSING_EXIT)
            err = stderr.getvalue()
            self.assertIn("backup start error", err)
            self.assertIn("required command not found on PATH: docker", err)
            self.assertNotIn("Traceback", err)

    def test_all_docker_commands_use_shared_run_missing_executable_handler(self):
        """Every docker-backed command routes FileNotFoundError through _run."""
        docker_commands = (
            ["start"],
            ["stop"],
            ["status"],
            ["logs"],
            ["snapshots"],
            ["check"],
            ["restore", "latest", "--target", "restore-target"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._compose_base(tmp)
            (base / "artifacts").mkdir()
            (base / "browser-tmp").mkdir()
            for argv in docker_commands:
                with self.subTest(command=argv[0]):
                    stderr = io.StringIO()
                    with mock.patch(
                        "backup_host.subprocess.run",
                        side_effect=FileNotFoundError(
                            2, "No such file or directory", "docker"
                        ),
                    ):
                        with mock.patch("sys.stderr", stderr):
                            if argv[0] == "restore":
                                with mock.patch(
                                    "backup_host.validate_restore_target",
                                    return_value=base / "restore-target",
                                ):
                                    rc = backup_host.main(
                                        ["--base-path", str(base), *argv]
                                    )
                            elif argv[0] == "start":
                                with mock.patch(
                                    "backup_host.write_compose_file",
                                    return_value=base / "compose" / "backup.yml",
                                ):
                                    rc = backup_host.main(
                                        ["--base-path", str(base), *argv]
                                    )
                            else:
                                rc = backup_host.main(
                                    ["--base-path", str(base), *argv]
                                )
                    self.assertEqual(
                        rc,
                        backup_host.DOCKER_MISSING_EXIT,
                        msg=f"{argv[0]} should exit {backup_host.DOCKER_MISSING_EXIT}",
                    )
                    err = stderr.getvalue()
                    self.assertIn(f"backup {argv[0]} error", err)
                    self.assertIn("required command not found on PATH: docker", err)
                    self.assertNotIn("Traceback", err)

    def test_restore_mkdir_oserror_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._compose_base(tmp)
            (base / "artifacts").mkdir()
            (base / "browser-tmp").mkdir()
            stderr = io.StringIO()
            target = base / "restore-target"
            original_mkdir = Path.mkdir

            def selective_mkdir(self, *args, **kwargs):
                if self == target:
                    raise OSError(13, "Permission denied")
                return original_mkdir(self, *args, **kwargs)

            with mock.patch(
                "backup_host.validate_restore_target",
                return_value=target,
            ):
                with mock.patch.object(Path, "mkdir", selective_mkdir):
                    with mock.patch("sys.stderr", stderr):
                        rc = backup_host.main(
                            [
                                "--base-path",
                                str(base),
                                "restore",
                                "latest",
                                "--target",
                                str(target),
                            ]
                        )
            self.assertEqual(rc, 1)
            err = stderr.getvalue()
            self.assertIn("backup restore error", err)
            self.assertIn("cannot create target directory", err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
