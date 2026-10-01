#!/usr/bin/env python3
"""derive_env: the one environment `manifest.py --derive` runs under.

up.sh and the egress broker's policy engine both build it here. Tests pin the
variables, that secret values never reach a log line, and that up.sh really
goes through the module.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import derive_env  # noqa: E402
import egress_policy  # noqa: E402

SECRET = "hunter2-do-not-log"


def git(key: str) -> str:
    return {"user.name": "Ada", "user.email": "ada@example.com"}[key]


class DeriveVarsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secrets = Path(tmp.name) / "secrets.env"

    def write(self, text: str) -> None:
        self.secrets.write_text(text)

    def test_present_vars_are_the_nonempty_assignments_sorted(self):
        self.write(f"export B_KEY={SECRET}\nA_KEY='x'\nEMPTY=\n# C=1\n  D_KEY=\"\"\n")
        out = derive_env.derive_vars(self.secrets, {}, git)
        self.assertEqual(out["PRESENT_SECRET_VARS"], "A_KEY B_KEY")
        self.assertEqual(out["SECRETS_FILE"], str(self.secrets))
        self.assertEqual((out["GIT_NAME_DEFAULT"], out["GIT_EMAIL_DEFAULT"]), ("Ada", "ada@example.com"))

    def test_environment_value_makes_an_empty_assignment_present(self):
        self.write("A_KEY=\n")
        out = derive_env.derive_vars(self.secrets, {"A_KEY": "from-env"}, git)
        self.assertEqual(out["PRESENT_SECRET_VARS"], "A_KEY")

    def test_ntfy_comes_from_the_environment_first_then_the_file(self):
        self.write("NTFY_URL=https://file.example\nNTFY_TOPIC=file-topic\n")
        out = derive_env.derive_vars(self.secrets, {"NTFY_URL": "https://env.example"}, git)
        self.assertEqual((out["NTFY_URL"], out["NTFY_TOPIC"]), ("https://env.example", "file-topic"))

    def test_network_vars_come_from_the_environment_only(self):
        self.write("DJINN_SUBNET=10.0.0.0/24\n")
        out = derive_env.derive_vars(
            self.secrets, {"DJINN_EGRESS_IP": "172.30.0.5"}, git
        )
        self.assertEqual((out["DJINN_SUBNET"], out["DJINN_EGRESS_IP"]), ("", "172.30.0.5"))

    def test_missing_secrets_file_yields_no_present_vars(self):
        out = derive_env.derive_vars(self.secrets, {}, git)
        self.assertEqual(out["PRESENT_SECRET_VARS"], "")

    def test_no_secret_value_is_ever_logged(self):
        self.write(f"NTFY_URL=https://{SECRET}.example\nTOKEN_X={SECRET}\n")
        with self.assertLogs("derive_env", level="DEBUG") as logs:
            derive_env.derive_vars(self.secrets, {}, git)
        self.assertTrue(logs.output)
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_cli_execs_the_command_with_the_derive_environment(self):
        self.write(f"A_KEY={SECRET}\n")
        result = subprocess.run(
            [
                sys.executable, str(REPO_ROOT / "src" / "derive_env.py"),
                "--secrets-file", str(self.secrets), "--",
                sys.executable, "-c",
                "import os,sys;print(os.environ['PRESENT_SECRET_VARS']);print(sys.stdin.read())",
            ],
            input="piped", capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\n")[:2], ["A_KEY", "piped"])


class WiringTests(unittest.TestCase):
    def test_up_sh_derives_through_the_module(self):
        up = (REPO_ROOT / "up.sh").read_text()
        self.assertIn("src/derive_env.py", up)
        self.assertNotIn('PRESENT_SECRET_VARS="$PRESENT_SECRET_VARS"', up)

    def test_policy_container_prefix_matches_common_sh(self):
        common = (REPO_ROOT / "src" / "common.sh").read_text()
        self.assertIn(f'DJINN_CTR_PREFIX="{egress_policy.CONTAINER_PREFIX}"', common)


if __name__ == "__main__":
    unittest.main()
