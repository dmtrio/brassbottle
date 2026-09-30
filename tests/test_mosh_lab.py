#!/usr/bin/env python3
"""Lab test: a mosh client container completes a session with a bottle.

Starts the built bottle image (real entrypoint, sshd in published mode,
firewall off — the lab network is the isolation) and a mosh client container
on one throwaway docker network, then runs `mosh coder@<bottle> -- <cmd>` from
the client. The command prints a marker plus the remote user, so a pass shows
mosh-server started as `coder` and the UDP leg carried the session.

Skips cleanly when docker is unavailable, or when the prebuilt image tags are
not given: CI builds the images first and passes them through
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

MARKER = "MOSH_LAB_MARKER"
MARKER_SCRIPT = "/tmp/mosh_lab_marker.sh"
SESSION_ATTEMPTS = 12
SESSION_RETRY_SECONDS = 2


BOTTLE_IMAGE = os.environ.get("DJINN_BOTTLE_CI_IMAGE", "")
CLIENT_IMAGE = os.environ.get("DJINN_JUMP_CI_IMAGE", "")


def lab_ready() -> bool:
    if not (BOTTLE_IMAGE and CLIENT_IMAGE):
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


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


@unittest.skipUnless(
    lab_ready(),
    "docker or DJINN_BOTTLE_CI_IMAGE / DJINN_JUMP_CI_IMAGE not available",
)
class MoshLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
            last = docker(
                "exec", "-t", self.client, "sh", "-c", mosh_command(self.bottle),
                check=False, timeout=120,
            )
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
