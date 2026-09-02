"""Pin the bottle sshd host-key persistence contract.

Host keys must live on the `ssh-host-keys` named volume, never in the image
layer: a key regenerated on recreate is a REMOTE HOST IDENTIFICATION HAS
CHANGED refusal on the jump's next hop. Three files have to agree on the one
path — compose (the mount), the Dockerfile (sshd_config HostKey lines) and the
entrypoint (where keys are generated) — and none of them imports the others,
so drift is only visible as a scary warning on the operator's phone.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
COMPOSE = (REPO / "compose" / "docker-compose.local.yml").read_text(encoding="utf-8")
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (REPO / "src" / "entrypoint.sh").read_text(encoding="utf-8")

VOLUME = "ssh-host-keys"
KEY_DIR = "/etc/ssh/host_keys"
KEY_TYPES = ("rsa", "ecdsa", "ed25519")


class SshHostKeyPersistenceTests(unittest.TestCase):
    def test_compose_mounts_named_volume_at_key_dir(self):
        self.assertTrue(re.search(rf"^\s*-\s*{VOLUME}:{KEY_DIR}\s*$", COMPOSE, re.M))
        self.assertTrue(re.search(rf"^  {VOLUME}:\s*$", COMPOSE, re.M))

    def test_key_dir_is_not_a_bind_mount(self):
        # A host bind here would share one identity across bottles (or leak a
        # private key onto the Mac); the volume is per compose project.
        for line in COMPOSE.splitlines():
            if KEY_DIR in line and line.strip().startswith("-"):
                self.assertTrue(line.strip().startswith(f"- {VOLUME}:"), line)

    def test_dockerfile_points_sshd_at_every_key_in_key_dir(self):
        # The sshd_config is written at build time; the keys arrive at runtime.
        # printf with a %s format expands to one HostKey line per type.
        m = re.search(r"printf 'HostKey ([^']*)' ((?:\w+ ?)+)", DOCKERFILE)
        self.assertIsNotNone(m, "Dockerfile must printf HostKey lines into sshd_config")
        fmt, types = m.group(1), tuple(m.group(2).split())
        self.assertEqual(fmt, f"{KEY_DIR}/ssh_host_%s_key\\n")
        self.assertEqual(types, KEY_TYPES)
        self.assertIn(f"mkdir -p {KEY_DIR}", DOCKERFILE)

    def test_entrypoint_generates_into_key_dir_never_the_image_default(self):
        self.assertNotIn("ssh-keygen -A", ENTRYPOINT,
                         "-A writes to /etc/ssh in the image layer, not the volume")
        self.assertIn(f'key="{KEY_DIR}/ssh_host_${{type}}_key"', ENTRYPOINT)
        m = re.search(r"for type in ((?:\w+ ?)+); do", ENTRYPOINT)
        self.assertIsNotNone(m)
        self.assertEqual(tuple(m.group(1).split()), KEY_TYPES)
        self.assertIn('ssh-keygen -q -t "$type" -N "" -f "$key"', ENTRYPOINT)

    def test_entrypoint_generates_only_when_missing(self):
        self.assertIn('if [ ! -f "$key" ]; then', ENTRYPOINT)

    def test_manifest_reserves_volume_and_path(self):
        import sys
        sys.path.insert(0, str(REPO / "src"))
        import manifest
        self.assertIn(VOLUME, manifest.STATIC_COMPOSE_VOLUME_NAMES)
        self.assertIn(KEY_DIR, manifest.STATIC_COMPOSE_MOUNT_PATHS)


if __name__ == "__main__":
    unittest.main()
