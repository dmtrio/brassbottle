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

from tests.dockerfile_lib import instructions as _instructions

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")




def _etc_environment_final_state():
    """The /etc/environment content-bearing RUNs, in order, with the last
    overwriting `tee` (no -a) identified. Later appends (`tee -a`, `>>`) keep
    what that write put there; an earlier one is clobbered. The entrypoint's
    runtime appends only ADD vars, so the image-time final writer decides."""
    writers = [
        i for i in _instructions(DOCKERFILE)
        if i.startswith("RUN ") and "/etc/environment" in i
    ]
    overwrite = [
        i for i in writers
        if re.search(r"tee\s+/etc/environment", i) and not re.search(r"tee\s+-a\s+/etc/environment", i)
        or re.search(r"[^>]>\s*/etc/environment", i)
    ]
    return writers, overwrite


class DisableTelemetryTest(unittest.TestCase):
    def test_env_instruction_sets_it(self):
        envs = [i for i in _instructions(DOCKERFILE) if i.startswith("ENV ")]
        self.assertTrue(
            any(re.search(r"\bDISABLE_TELEMETRY=1\b", i) for i in envs),
            "Dockerfile must carry `ENV DISABLE_TELEMETRY=1` (docker-exec/entrypoint sessions)",
        )

    def test_last_overwrite_of_etc_environment_carries_it(self):
        # `tee` without -a REPLACES the file, so only the last overwriting
        # writer (plus any later appends) survives into the image. An `any()`
        # over all writers would pass while a later PATH-only rewrite dropped
        # the variable for every SSH/mosh session.
        writers, overwrite = _etc_environment_final_state()
        self.assertTrue(writers, "no RUN writes /etc/environment")
        self.assertTrue(overwrite, "no overwriting write of /etc/environment found")
        final = writers[writers.index(overwrite[-1]):]
        self.assertTrue(
            any("DISABLE_TELEMETRY=1" in i for i in final),
            "DISABLE_TELEMETRY=1 must be in the last overwrite of /etc/environment "
            "or a later append (SSH/mosh sessions)",
        )

    def test_etc_environment_still_carries_path(self):
        # The telemetry line shares the write with PATH; make sure adding it
        # did not drop PATH, which every non-interactive SSH command needs.
        writers, overwrite = _etc_environment_final_state()
        final = writers[writers.index(overwrite[-1]):]
        self.assertTrue(any("PATH=" in i for i in final))


if __name__ == "__main__":
    unittest.main()
