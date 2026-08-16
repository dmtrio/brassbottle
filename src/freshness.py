#!/usr/bin/env python3
"""Container freshness readout — format the one-line landing readout.

`up.sh` writes two host-truth timestamps into /etc/environment after boot:

  DJINN_UP_AT       ISO8601 UTC of the last `./up.sh` (governs the freshness
                    of external rules — pulled each `up` — and MCP wiring).
  DJINN_IMAGE_BUILT the image's real `.Created` (governs the baked half:
                    bundled rules, plugin AGENTS.md fragments, install:
                    blocks). `up.sh` uses `--build`, which is cache-gated,
                    so a full cache hit leaves this old — exactly the honest
                    signal for how stale the image is.

Two stamps because layer caching makes them diverge: you can `up` daily for two
weeks while the image build stays two weeks old, so a single "created on" would
mislead about whichever half it is not measuring.

`freshness-landing.bashrc` invokes this from interactive shells. It prints ONE
dim line — or nothing, when no stamp is present (a readout, never an alarm):

  container: up'd 2026-07-21 (1d ago) · image built 2026-07-08 (14d ago)

No network, no side effects; the relative age uses the container's own clock.
"""

import os
import sys
from datetime import datetime, timezone

ENV_FILE = "/etc/environment"
UP_AT_VAR = "DJINN_UP_AT"
IMAGE_BUILT_VAR = "DJINN_IMAGE_BUILT"


def parse_iso(value):
    """Parse an ISO8601 stamp (trailing 'Z' or an explicit offset) to an aware
    UTC datetime. Returns None on anything unparseable so a malformed or missing
    stamp degrades to 'unknown' rather than crashing the shell hook."""
    if not value:
        return None
    text = value.strip()
    # `docker inspect` emits fractional seconds ('...789Z'); fromisoformat
    # accepts the fraction but (before 3.11) not a bare 'Z' — normalise it.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_age(then, now):
    """Whole-day relative age: 'today', '1d ago', or 'Nd ago'. A negative delta
    (clock skew — a stamp in the future) clamps to 'today' so the readout never
    shows a nonsensical negative age."""
    days = (now - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def format_stamp(iso, now):
    """'<YYYY-MM-DD> (<age>)' for one stamp, or 'unknown' if missing/malformed."""
    dt = parse_iso(iso)
    if dt is None:
        return "unknown"
    return f"{dt.date().isoformat()} ({format_age(dt, now)})"


def render(up_at, image_built, now):
    """The full readout line, or '' when NEITHER stamp is present (nothing to
    say → print nothing; one present → show it and mark the other unknown)."""
    if parse_iso(up_at) is None and parse_iso(image_built) is None:
        return ""
    return (
        f"container: up'd {format_stamp(up_at, now)}"
        f" · image built {format_stamp(image_built, now)}"
    )


def read_env_file(path):
    """Parse KEY=value lines from an /etc/environment-style file. Absent/
    unreadable file → empty dict (the readout then simply prints nothing)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"')
    except OSError:
        pass
    return values


def get_stamp(name, env_file):
    """Prefer a real env var — SSH login shells receive it via PAM/pam_env —
    and fall back to the parsed file, because attach-mode `docker exec` shells
    never go through PAM and so never inherit /etc/environment as env."""
    val = os.environ.get(name)
    if val:
        return val
    return env_file.get(name)


def main():
    env_file = read_env_file(ENV_FILE)
    up_at = get_stamp(UP_AT_VAR, env_file)
    image_built = get_stamp(IMAGE_BUILT_VAR, env_file)
    line = render(up_at, image_built, datetime.now(timezone.utc))
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
