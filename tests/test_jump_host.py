#!/usr/bin/env python3
"""Unit tests for src/jump_host.py — the operator commands for the jump container.

No real docker: subprocess.run is mocked throughout. Covers the compose argv
shape, the not-configured branches, docker-missing handling, and the pubkey
command's read-from-the-host-side-of-the-mount behaviour.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import jump_config  # noqa: E402
import jump_host  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 operator@mac"


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


class ComposeArgvTests(unittest.TestCase):
    def test_compose_cmd_pins_project_and_file(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            ident = jump_config.derive_identity(base)
            cmd = jump_host._compose_cmd(base, ident, "up", "-d")
            self.assertEqual(cmd[:3], ["docker", "compose", "-p"])
            self.assertEqual(cmd[3], ident.compose_project_name)
            self.assertIn("--project-directory", cmd)
            self.assertIn(str(jump_config.paths(base)["compose_file"]), cmd)
            self.assertEqual(cmd[-2:], ["up", "-d"])


class StartTests(unittest.TestCase):
    def test_start_requires_authorized_key(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(jump_host.os.environ, {"SSH_AUTHORIZED_KEY": ""}, clear=False):
                self.assertEqual(jump_host.cmd_start(Path(home)), 1)

    def test_start_writes_compose_and_calls_up(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ) as run:
                self.assertEqual(jump_host.cmd_start(base), 0)
            self.assertTrue(jump_config.paths(base)["compose_file"].exists())
            argv = run.call_args[0][0]
            self.assertIn("up", argv)
            self.assertIn("--build", argv)

    def test_start_propagates_compose_failure(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 3)):
                self.assertEqual(jump_host.cmd_start(Path(home)), 3)

    def test_start_without_docker_returns_127(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch("subprocess.run", side_effect=FileNotFoundError("docker")):
                self.assertEqual(jump_host.cmd_start(Path(home)), jump_host.DOCKER_MISSING_EXIT)


class StopStatusTests(unittest.TestCase):
    def test_stop_is_a_noop_when_not_configured(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("subprocess.run") as run:
                self.assertEqual(jump_host.cmd_stop(Path(home)), 0)
            run.assert_not_called()

    def test_status_not_configured_returns_1(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(jump_host.cmd_status(Path(home)), 1)

    def test_status_running_returns_0(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            jump_config.write_compose_file(base, KEY)
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "abc123\n")
            ):
                self.assertEqual(jump_host.cmd_status(base), 0)

    def test_status_stopped_returns_1(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            jump_config.write_compose_file(base, KEY)
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "")
            ):
                self.assertEqual(jump_host.cmd_status(base), 1)

    def test_logs_not_configured_raises(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_host.JumpHostError):
                jump_host.cmd_logs(Path(home), follow=False)

    def test_logs_follow_appends_flag(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            jump_config.write_compose_file(base, KEY)
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ) as run:
                jump_host.cmd_logs(base, follow=True)
            self.assertIn("-f", run.call_args[0][0])


class PubkeyTests(unittest.TestCase):
    def test_pubkey_before_first_start_explains_itself(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_host.JumpHostError) as ctx:
                jump_host.cmd_pubkey(Path(home))
            self.assertIn("jump start", str(ctx.exception))

    def test_pubkey_prints_the_generated_key(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            p = jump_config.ensure_layout(base)
            p["client_pubkey"].write_text("ssh-ed25519 AAAAjump djinn-jump\n", encoding="utf-8")
            with mock.patch("sys.stdout") as out:
                self.assertEqual(jump_host.cmd_pubkey(base), 0)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertIn("ssh-ed25519 AAAAjump djinn-jump", printed)

    def test_pubkey_does_not_need_a_running_container(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            p = jump_config.ensure_layout(base)
            p["client_pubkey"].write_text("ssh-ed25519 AAAAjump djinn-jump\n", encoding="utf-8")
            with mock.patch("subprocess.run") as run:
                jump_host.cmd_pubkey(base)
            run.assert_not_called()


class ParserTests(unittest.TestCase):
    def test_every_subcommand_parses(self):
        parser = jump_host.build_parser()
        for argv in (["start"], ["stop"], ["status"], ["logs"], ["logs", "-f"], ["pubkey"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_missing_subcommand_exits(self):
        with self.assertRaises(SystemExit):
            jump_host.build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
