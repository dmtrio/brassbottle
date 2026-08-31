#!/usr/bin/env python3
"""Unit tests for src/newt_host.py — operator commands for the Newt connector.

No real docker: subprocess.run is mocked throughout.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import newt_config  # noqa: E402
import newt_host  # noqa: E402

SECRETS = {
    "PANGOLIN_ENDPOINT": "https://pangolin.example.com",
    "NEWT_ID": "abcd1234",
    "NEWT_SECRET": "s3cr3t",
}


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


class StartTests(unittest.TestCase):
    def test_start_requires_the_pangolin_secrets(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, {v: "" for v in newt_config.SECRET_VARS}, clear=False):
                self.assertEqual(newt_host.cmd_start(Path(home)), 1)

    def test_start_bootstraps_the_bridge_before_compose(self):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: (calls.append(cmd), _completed(cmd))[1]
            ):
                self.assertEqual(newt_host.cmd_start(Path(home)), 0)
        ensure_at = next(i for i, c in enumerate(calls) if "ensure_net.py" in " ".join(c))
        up_at = next(i for i, c in enumerate(calls) if "up" in c and "docker" in c)
        self.assertLess(ensure_at, up_at)

    def test_ensure_net_uses_the_running_interpreter(self):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: (calls.append(cmd), _completed(cmd))[1]
            ):
                newt_host.cmd_start(Path(home))
        ensure = next(c for c in calls if "ensure_net.py" in " ".join(c))
        self.assertEqual(ensure[0], sys.executable)

    def test_start_does_not_build(self):
        # Newt ships an official pinned image; there is no Dockerfile to build.
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ) as run:
                newt_host.cmd_start(Path(home))
            self.assertNotIn("--build", run.call_args[0][0])

    def test_start_derives_from_the_live_bridge(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            with mock.patch.dict(
                newt_host.os.environ, dict(SECRETS, DJINN_SUBNET="10.9.0.0/24"), clear=False
            ), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ), mock.patch.object(
                newt_host.ensure_net, "network_subnet", return_value="172.30.0.0/24"
            ):
                self.assertEqual(newt_host.cmd_start(base), 0)
            compose = newt_config.paths(base)["compose_file"].read_text(encoding="utf-8")
            self.assertIn("ipv4_address: 172.30.0.253", compose)
            self.assertNotIn("10.9.0.253", compose)

    def test_start_aborts_when_the_bridge_is_unavailable(self):
        with tempfile.TemporaryDirectory() as home:
            def fail_ensure(cmd, **kw):
                return _completed(cmd, 1 if "ensure_net.py" in " ".join(cmd) else 0)

            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=fail_ensure
            ) as run:
                self.assertEqual(newt_host.cmd_start(Path(home)), 1)
            self.assertFalse(
                any("up" in c[0] for c in run.call_args_list if "docker" in c[0][0])
            )

    def test_start_never_prints_the_secret(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)
            ), mock.patch("sys.stdout") as out:
                newt_host.cmd_start(Path(home))
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertNotIn("s3cr3t", printed)

    def test_start_without_docker_returns_127(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(newt_host.os.environ, SECRETS, clear=False), mock.patch(
                "subprocess.run", side_effect=FileNotFoundError("docker")
            ):
                self.assertEqual(newt_host.cmd_start(Path(home)), newt_host.DOCKER_MISSING_EXIT)


class StopStatusTests(unittest.TestCase):
    def test_stop_noop_when_not_configured(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("subprocess.run") as run:
                self.assertEqual(newt_host.cmd_stop(Path(home)), 0)
            run.assert_not_called()

    def test_status_not_configured(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(newt_host.cmd_status(Path(home)), 1)

    def test_status_uses_the_running_filter(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            newt_config.write_compose_file(base, dict(SECRETS))
            with mock.patch(
                "subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd, 0, "newt\n")
            ) as run:
                self.assertEqual(newt_host.cmd_status(base), 0)
            argv = run.call_args[0][0]
            self.assertIn("--status", argv)
            self.assertIn("running", argv)
            self.assertNotIn("-q", argv)

    def test_logs_not_configured_raises(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(newt_host.NewtHostError):
                newt_host.cmd_logs(Path(home), follow=False)


class ParserTests(unittest.TestCase):
    def test_subcommands(self):
        parser = newt_host.build_parser()
        for argv in (["start"], ["stop"], ["status"], ["logs"], ["logs", "-f"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_no_pubkey_command(self):
        # Newt holds no key material — a pubkey command would be meaningless.
        with self.assertRaises(SystemExit):
            newt_host.build_parser().parse_args(["pubkey"])


if __name__ == "__main__":
    unittest.main()
