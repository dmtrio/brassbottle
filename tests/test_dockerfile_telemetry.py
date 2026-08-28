"""Guard the image-wide Claude Code telemetry opt-out.

Claude Code posts metrics to Datadog unless DISABLE_TELEMETRY is set. Behind
the egress firewall that is a blocked request re-filed by every session, so
the image sets the variable in BOTH places a process can be born from:
Docker ENV (entrypoint, `docker exec`) and /etc/environment (sshd builds
session envs via PAM and ignores container env). Losing either half
silently re-enables the traffic for that class of session.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


def _instructions(text):
    """Logical Dockerfile instructions, comments dropped, continuations joined."""
    out, cur = [], []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not cur and not stripped:
            continue
        cur.append(line)
        if not line.endswith("\\"):
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out


class DisableTelemetryTest(unittest.TestCase):
    def test_env_instruction_sets_it(self):
        envs = [i for i in _instructions(DOCKERFILE) if i.startswith("ENV ")]
        self.assertTrue(
            any(re.search(r"\bDISABLE_TELEMETRY=1\b", i) for i in envs),
            "Dockerfile must carry `ENV DISABLE_TELEMETRY=1` (docker-exec/entrypoint sessions)",
        )

    def test_etc_environment_carries_it(self):
        writers = [
            i for i in _instructions(DOCKERFILE)
            if i.startswith("RUN ") and "/etc/environment" in i
        ]
        self.assertTrue(writers, "no RUN writes /etc/environment")
        self.assertTrue(
            any("DISABLE_TELEMETRY=1" in i for i in writers),
            "/etc/environment must include DISABLE_TELEMETRY=1 (SSH/mosh sessions)",
        )

    def test_etc_environment_still_carries_path(self):
        # The telemetry line shares the write with PATH; make sure adding it
        # did not drop PATH, which every non-interactive SSH command needs.
        writers = [
            i for i in _instructions(DOCKERFILE)
            if i.startswith("RUN ") and "/etc/environment" in i
        ]
        self.assertTrue(any("PATH=" in i for i in writers))


if __name__ == "__main__":
    unittest.main()
