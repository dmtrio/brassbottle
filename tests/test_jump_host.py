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

    def test_start_accepts_multiple_keys_from_the_file(self):
        # The reason the file exists: more than one device, no secrets.env
        # quoting, and no key content in the compose overlay.
        keys = [KEY, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE6 phone@moshi"]
        with tempfile.TemporaryDirectory() as home:
            jump_config.write_authorized_keys(Path(home), keys)
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": ""}, clear=False
            ), mock.patch("subprocess.run", side_effect=lambda c, **k: _completed(c)):
                self.assertEqual(jump_host.cmd_start(Path(home)), 0)
            written = jump_config.paths(Path(home))["authorized_keys"].read_text()
            self.assertEqual(written, "".join(f"{k}\n" for k in keys))

    def test_start_bootstraps_the_external_bridge_first(self):
        # djinn-net is declared external, and `jump start` is documented as the
        # FIRST step of a fresh install — before any ./djinn up has created it.
        calls = []
        with tempfile.TemporaryDirectory() as home:
            def record(cmd, **kw):
                calls.append(cmd)
                return _completed(cmd)
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch("subprocess.run", side_effect=record):
                self.assertEqual(jump_host.cmd_start(Path(home)), 0)
        self.assertTrue(any("ensure_net.py" in " ".join(c) for c in calls))
        ensure_at = next(i for i, c in enumerate(calls) if "ensure_net.py" in " ".join(c))
        up_at = next(i for i, c in enumerate(calls) if "up" in c and "docker" in c)
        self.assertLess(ensure_at, up_at, "ensure_net must run before compose up")

    def test_ensure_net_uses_the_running_interpreter(self):
        # A bare "python3" would re-do the PATH lookup that jump.sh's
        # require_python3 exists to avoid — failing one step AFTER jump_host
        # already started fine under an explicit $PYTHON3.
        calls = []
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: (calls.append(cmd), _completed(cmd))[1]
            ):
                jump_host.cmd_start(Path(home))
        ensure = next(c for c in calls if "ensure_net.py" in " ".join(c))
        self.assertEqual(ensure[0], sys.executable)
        self.assertNotEqual(ensure[0], "python3")

    def test_start_aborts_when_the_bridge_cannot_be_ensured(self):
        with tempfile.TemporaryDirectory() as home:
            def fail_ensure(cmd, **kw):
                return _completed(cmd, 1 if "ensure_net.py" in " ".join(cmd) else 0)
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch("subprocess.run", side_effect=fail_ensure) as run:
                self.assertEqual(jump_host.cmd_start(Path(home)), 1)
            self.assertFalse(
                any("up" in c[0] for c in run.call_args_list if "docker" in c[0][0]),
                "compose up must not run when the bridge is unavailable",
            )

    def test_start_derives_the_address_from_the_live_bridge(self):
        # ensure_net only WARNS on subnet drift and returns 0, so the desired
        # DJINN_SUBNET is not a safe basis for a static address.
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            with mock.patch.dict(
                jump_host.os.environ,
                {"SSH_AUTHORIZED_KEY": KEY, "DJINN_SUBNET": "10.9.0.0/24"},
                clear=False,
            ), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ), mock.patch.object(
                jump_host.ensure_net, "network_subnet", return_value="172.30.0.0/24"
            ):
                self.assertEqual(jump_host.cmd_start(base), 0)
            compose = jump_config.paths(base)["compose_file"].read_text(encoding="utf-8")
            self.assertIn("ipv4_address: 172.30.0.254", compose)
            self.assertNotIn("10.9.0.254", compose)

    def test_start_survives_an_unreadable_live_subnet(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            with mock.patch.dict(
                jump_host.os.environ, {"SSH_AUTHORIZED_KEY": KEY}, clear=False
            ), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ), mock.patch.object(
                jump_host.ensure_net, "network_subnet", return_value=None
            ):
                self.assertEqual(jump_host.cmd_start(base), 0)
            self.assertTrue(jump_config.paths(base)["compose_file"].exists())

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
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "jump\n")
            ):
                self.assertEqual(jump_host.cmd_status(base), 0)

    def test_status_uses_running_filter_not_bare_ps_q(self):
        # `ps -q` yields an id for a created/restarting container too, and the
        # jump is restart: unless-stopped — a crash-looping entrypoint would
        # otherwise report "running".
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            jump_config.write_compose_file(base, KEY)
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "")
            ) as run:
                jump_host.cmd_status(base)
            argv = run.call_args[0][0]
            self.assertIn("--status", argv)
            self.assertIn("running", argv)
            self.assertNotIn("-q", argv)

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

    def test_pubkey_reports_unreadable_differently_from_missing(self):
        # Path.exists() returns False on PermissionError, which would tell the
        # operator to re-run the command that just created the key.
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            p = jump_config.ensure_layout(base)
            p["client_pubkey"].write_text("ssh-ed25519 AAAAjump djinn-jump\n", encoding="utf-8")
            with mock.patch.object(
                Path, "stat", side_effect=PermissionError(13, "Permission denied")
            ):
                with self.assertRaises(jump_host.JumpHostError) as ctx:
                    jump_host.cmd_pubkey(base)
            msg = str(ctx.exception)
            self.assertIn("cannot read", msg)
            self.assertNotIn("jump start", msg)

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
