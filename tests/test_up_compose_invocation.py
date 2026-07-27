"""Guards the `docker compose up` invocation in up.sh.

The invocation is a chain of env-var prefixes joined by trailing backslashes,
ending in the `docker compose` call. That shape has a silent failure mode:
a backslash-newline splices the following line in, so a comment or blank line
inserted mid-chain turns every prefix above it into a commented-out no-op and
compose runs with all of those variables unset. `bash -n` still passes, and so
does any grep that only looks for the flag — the damage is only visible at
runtime as `invalid spec: :/artifacts: empty section between colons`.

These tests splice the continuations into logical lines and assert the env
prefixes and the compose call really are one command.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UP_SH = REPO_ROOT / "up.sh"

# Variables the compose files interpolate; if the prefix chain is broken these
# reach compose as blank strings and produce malformed mounts/ports.
REQUIRED_PREFIXES = [
    "CONTAINER_NAME",
    "ARTIFACTS_PATH",
    "BROWSER_TMP_PATH",
    "KEYS_PATH",
    "RULES_PATH",
    "IMAGE_TAG",
]


def logical_lines(text):
    """Splice backslash-continued physical lines into logical lines."""
    lines, current = [], ""
    for physical in text.splitlines():
        current += physical
        if current.endswith("\\"):
            current = current[:-1]  # keep splicing
        else:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


class TestComposeInvocation(unittest.TestCase):
    def setUp(self):
        self.lines = logical_lines(UP_SH.read_text())
        matches = [l for l in self.lines if re.search(r"\bdocker compose\b.*\bup\b", l)]
        self.assertEqual(
            len(matches), 1, "expected exactly one `docker compose ... up` invocation"
        )
        self.invocation = matches[0]

    def test_project_directory_is_pinned(self):
        """Without this, `context: .` resolves to compose/ and the build fails."""
        self.assertIn(
            '--project-directory "$SCRIPT_DIR"',
            self.invocation,
            "compose must be pinned to the repo root, else the build context "
            "resolves to compose/ where there is no Dockerfile",
        )

    def test_env_prefixes_reach_the_compose_call(self):
        """The prefix chain and the compose call must be ONE logical line."""
        for var in REQUIRED_PREFIXES:
            with self.subTest(var=var):
                self.assertRegex(
                    self.invocation,
                    rf"\b{var}=",
                    f"{var} is not part of the compose command's logical line — "
                    "a comment or blank line has broken the backslash chain, so "
                    "compose will run with this variable unset",
                )

    def test_no_comment_swallowed_by_a_continuation(self):
        """A commented-out fragment inside the chain means the splice broke."""
        self.assertNotRegex(
            self.invocation,
            r"#",
            "the compose invocation's logical line contains a '#' — a comment "
            "was spliced in by a trailing backslash, commenting out the rest "
            "of the command",
        )


if __name__ == "__main__":
    unittest.main()
