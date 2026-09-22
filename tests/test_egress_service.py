#!/usr/bin/env python3
"""Unit tests for src/egress_service.py — the djinn egress compose singleton.

No real docker: subprocess.run is mocked throughout (the way
tests/test_jump_host.py does it) and the rendered compose project is pinned
as a literal dict — networks per service, mounts at the real DJINN_HOME /
BOTTLES_PATH paths, env, ports, address, restart policy, depends_on.
"""

import contextlib
import fcntl
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import djinn_net_addr  # noqa: E402
import egress_service as svc  # noqa: E402
import egress_broker_host as broker  # noqa: E402


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
    )


class DockerSocketTests(unittest.TestCase):
    def test_default_socket_when_unset(self):
        self.assertEqual(svc.resolve_docker_socket({}), "/var/run/docker.sock")

    def test_unix_scheme_renders_its_path_as_the_mount_source(self):
        env = {"DOCKER_HOST": "unix:///custom/path.sock"}
        self.assertEqual(svc.resolve_docker_socket(env), "/custom/path.sock")

    def test_non_unix_docker_host_is_refused_by_name(self):
        for value in ("tcp://127.0.0.1:2375", "ssh://box", "npipe:////./pipe/docker_engine"):
            with self.subTest(value=value):
                with self.assertRaises(svc.EgressServiceError) as ctx:
                    svc.resolve_docker_socket({"DOCKER_HOST": value})
                self.assertIn("unix", str(ctx.exception))


class ComposeSpecTests(unittest.TestCase):
    def _spec(self, base: Path, bottles: Path, *, env: dict[str, str] | None = None):
        return svc.build_compose_spec(
            base_path=base,
            bottles_path=bottles,
            egress_ip="172.30.0.252",
            docker_socket=svc.resolve_docker_socket(env or {}),
            actions_url=(env or {}).get("EGRESS_ACTIONS_URL"),
            repo_root=Path("/repo"),
        )

    def test_rendered_project_is_pinned_as_a_literal_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bottles = base / "bottles"
            spec = self._spec(base, bottles)
            self.assertEqual(
                spec,
                {
                    "networks": {
                        "djinn-net": {"name": "djinn-net", "external": True},
                        "egress-backend": {"internal": True},
                    },
                    "services": {
                        "broker": {
                            "build": {"context": "/repo", "dockerfile": "egress/Dockerfile"},
                            "image": "djinn-egress:local",
                            "container_name": "djinn-egress-broker",
                            "restart": "unless-stopped",
                            "networks": {
                                "egress-backend": None,
                                "djinn-net": {"ipv4_address": "172.30.0.252"},
                            },
                            "ports": ["127.0.0.1:8816:8816"],
                            "volumes": [
                                "/var/run/docker.sock:/var/run/docker.sock",
                                f"{base}:{base}",
                                f"{bottles}:{bottles}",
                            ],
                            "environment": {
                                "DJINN_HOME": str(base),
                                "BOTTLES_PATH": str(bottles),
                                "DJINN_CONTAINER": "1",
                                "EGRESS_ADMIN_URL": "http://127.0.0.1:8817",
                            },
                            "command": [
                                "python3",
                                "/opt/brassbottle/src/egress_broker_host.py",
                                "--base-path",
                                str(base),
                                "--bind-any",
                                "--port",
                                "8816",
                                "--advertise",
                                "127.0.0.1:8816",
                            ],
                        },
                        "admin": {
                            "build": {"context": "/repo", "dockerfile": "egress/Dockerfile"},
                            "image": "djinn-egress:local",
                            "container_name": "djinn-egress-admin",
                            "restart": "unless-stopped",
                            "networks": {"egress-backend": None},
                            "ports": ["127.0.0.1:8817:8817"],
                            "volumes": [f"{base}:{base}"],
                            "environment": {
                                "DJINN_HOME": str(base),
                                "DJINN_CONTAINER": "1",
                                "EGRESS_BROKER_URL": "http://broker:8816",
                            },
                            "command": [
                                "python3",
                                "/opt/brassbottle/src/admin_daemon.py",
                                "--bind-any",
                                "--port",
                                "8817",
                            ],
                            "depends_on": ["broker"],
                        },
                    },
                },
            )

    def test_custom_unix_socket_renders_as_mount_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            spec = self._spec(
                base, base / "bottles", env={"DOCKER_HOST": "unix:///custom/path.sock"}
            )
            self.assertIn(
                "/custom/path.sock:/var/run/docker.sock",
                spec["services"]["broker"]["volumes"],
            )

    def test_actions_url_only_in_secrets_env_reaches_broker_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "secrets.env").write_text(
                "EGRESS_ACTIONS_URL=http://10.9.9.9:8816\n", encoding="utf-8"
            )
            actions_url = svc._actions_url({}, base)
            spec = svc.build_compose_spec(
                base_path=base,
                bottles_path=base / "bottles",
                egress_ip="172.30.0.252",
                docker_socket="/var/run/docker.sock",
                actions_url=actions_url,
                repo_root=Path("/repo"),
            )
            self.assertEqual(
                spec["services"]["broker"]["environment"].get("EGRESS_ACTIONS_URL"),
                "http://10.9.9.9:8816",
            )
            self.assertNotIn(
                "EGRESS_ACTIONS_URL", spec["services"]["admin"]["environment"]
            )

    def test_actions_url_env_var_wins_over_secrets_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "secrets.env").write_text(
                "EGRESS_ACTIONS_URL=http://10.9.9.9:8816\n", encoding="utf-8"
            )
            self.assertEqual(
                svc._actions_url({"EGRESS_ACTIONS_URL": "http://10.1.1.1:8816"}, base),
                "http://10.1.1.1:8816",
            )

    def test_egress_ip_collision_with_jump_address_is_refused(self):
        subnet = djinn_net_addr.resolve_subnet({})
        jump_addr = str(djinn_net_addr.top_address(subnet, 1))
        with self.assertRaises(ValueError) as ctx:
            djinn_net_addr.resolve_egress_ip(env={"DJINN_EGRESS_IP": jump_addr})
        self.assertIn("jump", str(ctx.exception))


class RenderTests(unittest.TestCase):
    def _spec(self, base: Path) -> dict:
        return svc.build_compose_spec(
            base_path=base,
            bottles_path=base / "bottles",
            egress_ip="172.30.0.252",
            docker_socket="/var/run/docker.sock",
            actions_url=None,
            repo_root=Path("/repo"),
        )

    def test_write_is_atomic_and_leaves_no_tmp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = svc.write_compose_file(base, self._spec(base))
            self.assertEqual(target, base / "egress" / "docker-compose.yml")
            self.assertEqual(
                target.read_text(encoding="utf-8"), svc.render_compose_yaml(self._spec(base))
            )
            leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
            self.assertEqual(leftovers, [])

    def test_yaml_carries_internal_backend_and_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            text = svc.render_compose_yaml(self._spec(base))
            self.assertIn("internal: true", text)
            self.assertIn("external: true", text)
            self.assertIn("ipv4_address: 172.30.0.252", text)
            self.assertIn("restart: unless-stopped", text)
            self.assertIn("depends_on:", text)


class LockRefusalTests(unittest.TestCase):
    def test_start_refuses_when_a_watcher_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)
            egress_root.mkdir(parents=True, exist_ok=True)
            lock_path = egress_root / broker.LOCK_FILENAME
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)):
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = svc.cmd_start(base)
                self.assertEqual(rc, 1)
                self.assertIn("daemon-lock-held", err.getvalue())
                self.assertIn("allow --watch", err.getvalue())
                # nothing came up: no compose file was ever rendered
                self.assertFalse((base / "egress" / "docker-compose.yml").exists())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


class StartFlowTests(unittest.TestCase):
    def setUp(self):
        # health short-circuit: the probe answers green immediately
        self.patchers = [
            mock.patch.object(svc, "_probe_health", return_value=True),
            mock.patch.object(svc, "HEALTH_WAIT_SECONDS", 0.0),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_cmd_start(self, base: Path, bottles: Path, env_extra: dict[str, str]):
        out, err = io.StringIO(), io.StringIO()
        env = {"DJINN_HOME": str(base), "BOTTLES_PATH": str(bottles)}
        env.update(env_extra)
        with mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)):
            with mock.patch.dict(os.environ, env, clear=False):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = svc.cmd_start(base)
        return rc, out.getvalue(), err.getvalue()

    def test_start_renders_compose_brings_up_and_prints_the_session_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bottles = base / "bottles"
            rc, out, err = self._run_cmd_start(base, bottles, {})
            self.assertEqual(rc, 0)
            compose_file = base / "egress" / "docker-compose.yml"
            self.assertTrue(compose_file.is_file())
            text = compose_file.read_text(encoding="utf-8")
            self.assertIn("container_name: djinn-egress-broker", text)
            self.assertIn("container_name: djinn-egress-admin", text)
            self.assertIn("ipv4_address: 172.30.0.252", text)
            # host-side secrets exist BEFORE the containers, operator-owned
            self.assertTrue((base / "run" / "egress" / broker.OPERATOR_TOKEN_FILENAME).is_file())
            self.assertTrue((base / "run" / "egress" / admin_key_filename()).is_file())
            # the session URL is printed, exactly as ./djinn egress url prints it
            key = (base / "run" / "egress" / admin_key_filename()).read_text().strip()
            self.assertIn(
                f"http://127.0.0.1:8817/session?key={key}", out
            )
            self.assertIn("ip=172.30.0.252", out)

    def test_start_uses_the_live_bridge_on_subnet_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bottles = base / "bottles"
            rc, out, err = self._drift_cmd_start(base, bottles)
            self.assertEqual(rc, 0)
            compose_file = base / "egress" / "docker-compose.yml"
            self.assertIn("ipv4_address: 10.9.0.252", compose_file.read_text(encoding="utf-8"))
            self.assertIn("subnet-drift", out)

    def _drift_cmd_start(self, base: Path, bottles: Path):
        out, err = io.StringIO(), io.StringIO()
        env = {"DJINN_HOME": str(base), "BOTTLES_PATH": str(bottles)}
        with mock.patch("subprocess.run", side_effect=lambda cmd, **kw: _completed(cmd)):
            with mock.patch.object(
                svc.ensure_net, "network_subnet", return_value="10.9.0.0/24"
            ):
                with mock.patch.dict(os.environ, env, clear=False):
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = svc.cmd_start(base)
        return rc, out.getvalue(), err.getvalue()


class UrlTests(unittest.TestCase):
    def test_url_prints_exactly_the_session_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            egress_root = broker.resolve_egress_root(base)
            egress_root.mkdir(parents=True, exist_ok=True)
            key = admin_ensure_key(egress_root)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = svc.cmd_url(base)
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), f"http://127.0.0.1:8817/session?key={key}")

    def test_url_errors_naming_start_when_admin_key_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = svc.cmd_url(Path(tmp))
            self.assertEqual(rc, 1)
            self.assertIn("./djinn egress start", err.getvalue())
            self.assertNotIn("session?key", out.getvalue())


class StatusTests(unittest.TestCase):
    def test_status_without_compose_file_is_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = svc.cmd_status(Path(tmp))
            self.assertEqual(rc, 1)
            self.assertIn("not-configured", out.getvalue())


def admin_key_filename() -> str:
    from admin_daemon import ADMIN_KEY_FILENAME

    return ADMIN_KEY_FILENAME


def admin_ensure_key(egress_root: Path) -> str:
    from admin_daemon import ensure_admin_key

    return ensure_admin_key(egress_root)


if __name__ == "__main__":
    unittest.main()
