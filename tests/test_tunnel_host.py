#!/usr/bin/env python3
"""Unit tests for src/tunnel_host.py — operator commands for the Newt connector.

No real docker: subprocess.run is mocked throughout.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import tunnel_config  # noqa: E402
import tunnel_host  # noqa: E402

SECRETS = {
    "PANGOLIN_ENDPOINT": "https://pangolin.example.com",
    "NEWT_ID": "abcd1234",
    "NEWT_SECRET": "s3cr3t",
}

# clear=False leaves the ambient environment in play, which for THIS module
# means a developer's exported DJINN_TUNNEL_IP or DJINN_SUBNET silently changes
# the address under test. Blank them alongside the secrets.
SCRUBBED = {"DJINN_TUNNEL_IP": "", "DJINN_SUBNET": "", "DJINN_TUNNEL_IMAGE": ""}
ENV = dict(SECRETS, **SCRUBBED)


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_docker(calls=None):
    """A subprocess.run stand-in where the service reports RUNNING.

    cmd_start polls after compose up, so a fake that returns empty stdout for
    `ps` makes every start look like a crash-loop.
    """

    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if "--status" in cmd:
            return _completed(cmd, 0, "tunnel\n")
        return _completed(cmd)

    return run


def _no_settle_delay():
    # The settle poll sleeps between checks; irrelevant to the assertions.
    return mock.patch.object(tunnel_host, "SETTLE_INTERVAL_SECONDS", 0)


class StartTests(unittest.TestCase):
    def test_start_requires_the_pangolin_secrets(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, {v: "" for v in tunnel_config.SECRET_VARS}, clear=False):
                self.assertEqual(tunnel_host.cmd_start(Path(home)), 1)

    def test_start_bootstraps_the_bridge_before_compose(self):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=_fake_docker(calls)
            ):
                self.assertEqual(tunnel_host.cmd_start(Path(home)), 0)
        ensure_at = next(i for i, c in enumerate(calls) if "ensure_net.py" in " ".join(c))
        up_at = next(i for i, c in enumerate(calls) if "up" in c and "docker" in c)
        self.assertLess(ensure_at, up_at)

    def test_ensure_net_uses_the_running_interpreter(self):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=_fake_docker(calls)
            ):
                tunnel_host.cmd_start(Path(home))
        ensure = next(c for c in calls if "ensure_net.py" in " ".join(c))
        self.assertEqual(ensure[0], sys.executable)

    def test_start_does_not_build(self):
        # Newt ships an official pinned image; there is no Dockerfile to build.
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=_fake_docker()
            ) as run:
                tunnel_host.cmd_start(Path(home))
            self.assertFalse(any("--build" in c[0][0] for c in run.call_args_list))

    def test_start_derives_from_the_live_bridge(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            with mock.patch.dict(
                tunnel_host.os.environ, dict(ENV, DJINN_SUBNET="10.9.0.0/24"), clear=False
            ), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=_fake_docker()
            ), mock.patch.object(
                tunnel_host.ensure_net, "network_subnet", return_value="172.30.0.0/24"
            ):
                self.assertEqual(tunnel_host.cmd_start(base), 0)
            compose = tunnel_config.paths(base)["compose_file"].read_text(encoding="utf-8")
            self.assertIn("ipv4_address: 172.30.0.253", compose)
            self.assertNotIn("10.9.0.253", compose)

    def test_start_aborts_when_the_bridge_is_unavailable(self):
        with tempfile.TemporaryDirectory() as home:
            def fail_ensure(cmd, **kw):
                return _completed(cmd, 1 if "ensure_net.py" in " ".join(cmd) else 0)

            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), mock.patch(
                "subprocess.run", side_effect=fail_ensure
            ) as run:
                self.assertEqual(tunnel_host.cmd_start(Path(home)), 1)
            self.assertFalse(
                any("up" in c[0] for c in run.call_args_list if "docker" in c[0][0])
            )

    def test_start_never_prints_the_secret(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=_fake_docker()
            ), mock.patch("sys.stdout") as out:
                tunnel_host.cmd_start(Path(home))
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertNotIn("s3cr3t", printed)

    def test_start_fails_when_the_container_does_not_stay_up(self):
        # compose up returns 0 for a container that immediately exits; with
        # restart: unless-stopped that is a silent crash-loop. The most likely
        # cause is a bad NEWT_SECRET, so it must not report success.
        with tempfile.TemporaryDirectory() as home:
            def crashloop(cmd, **kw):
                if "--status" in cmd:
                    return _completed(cmd, 0, "")
                return _completed(cmd)

            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=crashloop
            ):
                self.assertEqual(tunnel_host.cmd_start(Path(home)), 1)

    def test_start_does_not_print_enrolment_when_not_running(self):
        with tempfile.TemporaryDirectory() as home:
            def crashloop(cmd, **kw):
                if "--status" in cmd:
                    return _completed(cmd, 0, "")
                return _completed(cmd)

            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), _no_settle_delay(), mock.patch(
                "subprocess.run", side_effect=crashloop
            ), mock.patch("sys.stdout") as out:
                tunnel_host.cmd_start(Path(home))
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertNotIn("Olm", printed)

    def test_start_without_docker_returns_127(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(tunnel_host.os.environ, ENV, clear=False), mock.patch(
                "subprocess.run", side_effect=FileNotFoundError("docker")
            ):
                self.assertEqual(tunnel_host.cmd_start(Path(home)), tunnel_host.DOCKER_MISSING_EXIT)


class StopStatusTests(unittest.TestCase):
    def test_stop_noop_when_not_configured(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("subprocess.run") as run:
                self.assertEqual(tunnel_host.cmd_stop(Path(home)), 0)
            run.assert_not_called()

    def test_stop_removes_the_credential_file(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            tunnel_config.write_compose_file(base, dict(SECRETS))
            env_file = tunnel_config.paths(base)["env_file"]
            self.assertTrue(env_file.exists())
            with mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)):
                self.assertEqual(tunnel_host.cmd_stop(base), 0)
            self.assertFalse(env_file.exists())

    def test_stop_keeps_credentials_when_down_fails(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            tunnel_config.write_compose_file(base, dict(SECRETS))
            with mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 1)):
                tunnel_host.cmd_stop(base)
            self.assertTrue(tunnel_config.paths(base)["env_file"].exists())

    def test_status_not_configured(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(tunnel_host.cmd_status(Path(home)), 1)

    def test_status_uses_the_running_filter(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            tunnel_config.write_compose_file(base, dict(SECRETS))
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "tunnel\n")
            ) as run:
                self.assertEqual(tunnel_host.cmd_status(base), 0)
            argv = run.call_args[0][0]
            self.assertIn("--status", argv)
            self.assertIn("running", argv)
            self.assertNotIn("-q", argv)

    def test_logs_not_configured_raises(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(tunnel_host.TunnelHostError):
                tunnel_host.cmd_logs(Path(home), follow=False)


class ParserTests(unittest.TestCase):
    def test_subcommands(self):
        parser = tunnel_host.build_parser()
        for argv in (["start"], ["stop"], ["status"], ["logs"], ["logs", "-f"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_no_pubkey_command(self):
        # Newt holds no key material — a pubkey command would be meaningless.
        with self.assertRaises(SystemExit):
            tunnel_host.build_parser().parse_args(["pubkey"])


if __name__ == "__main__":
    unittest.main()
