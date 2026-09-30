#!/usr/bin/env python3
"""Lab test: a mosh client container completes a session with a bottle.

Starts the built bottle image (real entrypoint, sshd in published mode,
firewall off — the lab network is the isolation) and a mosh client container
on one throwaway docker network, then runs `mosh coder@<bottle> -- <cmd>` from
the client. The command prints a marker plus the remote user, so a pass shows
mosh-server started as `coder` and the UDP leg carried the session.

Skips only when the prebuilt image tags are not given; once they are set (CI),
an unavailable docker FAILS the class rather than skipping, so the gated step
cannot go green without running. CI builds the images first and passes them through
DJINN_BOTTLE_CI_IMAGE / DJINN_JUMP_CI_IMAGE (the jump image is the client: it
already carries mosh, a UTF-8 locale and ssh). Building a bottle image inside
the test would make every `unittest discover` run a multi-minute build.
Locally: docker build -t bottle . && docker build -f jump/Dockerfile -t jump .
then DJINN_BOTTLE_CI_IMAGE=bottle DJINN_JUMP_CI_IMAGE=jump python3 -m unittest
tests.test_mosh_lab -v
"""

import os
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

MARKER = "MOSH_LAB_MARKER"
MARKER_SCRIPT = "/tmp/mosh_lab_marker.sh"
SESSION_ATTEMPTS = 12
SESSION_RETRY_SECONDS = 2
SESSION_ATTEMPT_TIMEOUT = 30


BOTTLE_IMAGE = os.environ.get("DJINN_BOTTLE_CI_IMAGE", "")
CLIENT_IMAGE = os.environ.get("DJINN_JUMP_CI_IMAGE", "")


def lab_requested() -> bool:
    """True when the caller asked for the lab (either image tag is set)."""
    return bool(BOTTLE_IMAGE or CLIENT_IMAGE)


def docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def require_lab(bottle: str, client: str, probe=docker_available) -> None:
    """Gate for MoshLabTests.setUpClass: raise when the lab was requested but cannot run.

    Both image tags are needed to run; a half-set pair or a dead docker is a
    misconfigured gate, and must fail loudly instead of skipping.
    """
    if not (bottle and client):
        raise AssertionError(
            "DJINN_BOTTLE_CI_IMAGE and DJINN_JUMP_CI_IMAGE must both be set "
            f"(bottle={bottle!r}, client={client!r})"
        )
    if not probe():
        raise AssertionError(
            "docker is unavailable but the mosh lab was requested via "
            "DJINN_BOTTLE_CI_IMAGE / DJINN_JUMP_CI_IMAGE; refusing to skip"
        )


def log(msg: str) -> None:
    print(f"[mosh-lab] {msg}", file=sys.stderr, flush=True)


def docker(*args: str, check: bool = True, timeout: int = 600, **kwargs) -> subprocess.CompletedProcess:
    started = time.monotonic()
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, timeout=timeout, **kwargs
    )
    log(
        f"docker {args[0]} rc={result.returncode} "
        f"secs={time.monotonic() - started:.1f} out={len(result.stdout)}B err={len(result.stderr)}B"
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed rc={result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def mosh_command(bottle: str) -> str:
    """Client-side command: ssh with the lab key, then the remote marker script.

    The script lives in the bottle (see setUpClass) so no quoting has to
    survive the client shell, ssh and the remote shell.
    """
    ssh = (
        "ssh -i /tmp/lab_key -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
    )
    return f"mosh --ssh='{ssh}' coder@{bottle} -- {MARKER_SCRIPT}"


class BottleImageSourceTests(unittest.TestCase):
    """Docker-free guard: the Dockerfile still asks for mosh and the locale."""

    def test_mosh_and_locales_install_in_one_run(self):
        installs = [
            line for line in DOCKERFILE.splitlines() if line.strip().startswith("tmux locales")
        ]
        self.assertEqual(len(installs), 1)
        self.assertIn("mosh", installs[0].split())

    def test_utf8_locale_is_generated_and_default(self):
        self.assertIn("locale-gen en_US.UTF-8", DOCKERFILE)
        self.assertIn("ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8", DOCKERFILE)


class LabGateTests(unittest.TestCase):
    """Docker-free: the gating logic, with the docker probe stubbed."""

    def test_unavailable_docker_fails_when_images_are_set(self):
        with self.assertRaises(AssertionError) as ctx:
            require_lab("bottle", "jump", probe=lambda: False)
        self.assertIn("refusing to skip", str(ctx.exception))

    def test_half_set_images_fail(self):
        with self.assertRaises(AssertionError):
            require_lab("bottle", "", probe=lambda: True)

    def test_available_docker_passes(self):
        require_lab("bottle", "jump", probe=lambda: True)

    def test_lab_requested_follows_either_variable(self):
        for bottle, client, want in (("", "", False), ("b", "", True), ("", "c", True), ("b", "c", True)):
            with mock.patch.multiple(sys.modules[__name__], BOTTLE_IMAGE=bottle, CLIENT_IMAGE=client):
                self.assertEqual(lab_requested(), want)


@unittest.skipUnless(
    lab_requested(),
    "DJINN_BOTTLE_CI_IMAGE / DJINN_JUMP_CI_IMAGE not set",
)
class MoshLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_lab(BOTTLE_IMAGE, CLIENT_IMAGE)
        suffix = uuid.uuid4().hex[:8]
        cls.network = f"djinn-mosh-lab-{suffix}"
        cls.bottle = f"djinn-mosh-lab-bottle-{suffix}"
        cls.client = f"djinn-mosh-lab-client-{suffix}"
        cls.addClassCleanup(cls._cleanup)

        docker("network", "create", cls.network)
        docker(
            "run", "-d", "--name", cls.client, "--network", cls.network,
            "--entrypoint", "sleep", CLIENT_IMAGE, "infinity",
        )
        docker(
            "exec", cls.client, "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
            "-f", "/tmp/lab_key",
        )
        pubkey = docker("exec", cls.client, "cat", "/tmp/lab_key.pub").stdout.strip()
        docker(
            "run", "-d", "--name", cls.bottle, "--network", cls.network,
            "-e", "SSH_ENABLED=true",
            "-e", f"SSH_AUTHORIZED_KEY={pubkey}",
            "-e", "ENABLE_FIREWALL=false",
            "-e", "GIT_CREDENTIAL_HOSTS=github.com",
            "-e", f"CONTAINER_NAME=mosh-lab-{suffix}",
            BOTTLE_IMAGE,
        )
        # Printed by the remote command: the marker plus the user mosh-server
        # ran it as. The sleep lets mosh's final screen state reach the client.
        docker(
            "exec", "-u", "coder", cls.bottle, "sh", "-c",
            f"printf '#!/bin/sh\\necho {MARKER}_$(id -un)\\nsleep 1\\n' > {MARKER_SCRIPT}"
            f" && chmod +x {MARKER_SCRIPT}",
        )

    @classmethod
    def _cleanup(cls):
        for name in (cls.client, cls.bottle):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
        subprocess.run(["docker", "network", "rm", cls.network], capture_output=True, timeout=120)

    def _bottle_logs(self) -> str:
        return docker("logs", self.bottle, check=False).stdout[-4000:]

    def test_mosh_server_is_installed_as_stock_binary(self):
        out = docker("exec", "-u", "coder", self.bottle, "sh", "-c", "command -v mosh-server").stdout
        self.assertEqual(out.strip(), "/usr/bin/mosh-server")

    def test_bottle_has_a_utf8_locale(self):
        out = docker("exec", "-u", "coder", self.bottle, "locale", "charmap").stdout
        self.assertEqual(out.strip(), "UTF-8")

    def test_client_completes_a_mosh_session_with_the_bottle(self):
        last = None
        for attempt in range(1, SESSION_ATTEMPTS + 1):
            # -t: mosh-client insists on a terminal; docker allocates a pty.
            try:
                last = docker(
                    "exec", "-t", self.client, "sh", "-c", mosh_command(self.bottle),
                    check=False, timeout=SESSION_ATTEMPT_TIMEOUT,
                )
            except subprocess.TimeoutExpired as exc:
                # mosh-client waits forever when the UDP leg never comes up.
                log(f"session attempt={attempt} timeout={SESSION_ATTEMPT_TIMEOUT}s")
                last = subprocess.CompletedProcess(exc.cmd, -1, "", "")
                time.sleep(SESSION_RETRY_SECONDS)
                continue
            log(f"session attempt={attempt} rc={last.returncode}")
            if f"{MARKER}_coder" in last.stdout:
                break
            time.sleep(SESSION_RETRY_SECONDS)  # sshd may still be starting
        self.assertIn(
            f"{MARKER}_coder",
            last.stdout,
            f"no marker from a coder session\nclient out:\n{last.stdout[-2000:]}\n"
            f"client err:\n{last.stderr[-2000:]}\nbottle logs:\n{self._bottle_logs()}",
        )


if __name__ == "__main__":
    unittest.main()
