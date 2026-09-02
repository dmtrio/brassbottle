#!/usr/bin/env python3
"""Interactive selector for the host-generated djinn-jump bottle registry."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY = "/etc/djinn-jump/registry/bottles"
NAME_RE = re.compile(r"^djinn-[A-Za-z0-9_-]+$")


def log(message: str) -> None:
    # timezone.utc works on the documented Python 3.9 floor; datetime.UTC does not.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017
    print(f"{now} jump picker {message}", file=sys.stderr)


def read_names(path: Path) -> list[str]:
    """Read the untrusted registry defensively; only valid container names pass."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        log("skip reason=registry-unreadable")
        return []
    return [name for name in lines if NAME_RE.fullmatch(name)]


def select(names: list[str], input_fn=input, output=sys.stdout) -> str | None:
    """Present a menu and return a selected Docker DNS name, or None to stay."""
    if not names:
        print(
            "No running jump-reachable bottles. Use ./djinn up <bottle> on the host, "
            "then reconnect.",
            file=output,
        )
        return None
    log(f"show entries={len(names)}")
    print("\nAvailable bottles:", file=output)
    for index, name in enumerate(names, start=1):
        print(f"  {index}. {name.removeprefix('djinn-')}", file=output)
    print("  q. Stay on the jump shell", file=output)
    while True:
        try:
            choice = input_fn("Select a bottle: ").strip()
        except (EOFError, KeyboardInterrupt):
            log("cancel reason=input-interrupted")
            return None
        if choice.lower() == "q":
            log("cancel reason=operator")
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(names):
            target = names[int(choice) - 1]
            log(f"connect target={target}")
            return target
        print("Choose a listed number or q.", file=output)


def hop(target: str) -> int | None:
    """Run one bottle SSH session without replacing the Mosh-side picker."""
    try:
        return subprocess.call(["ssh", target])
    except OSError:
        # Keep the Mosh session useful even if ssh itself cannot be started.
        log(f"disconnect target={target} reason=ssh-exec-failed")
        return None


def main() -> int:
    os.environ["DJINN_JUMP_PICKER_DONE"] = "1"
    names = read_names(Path(os.environ.get("DJINN_JUMP_REGISTRY", DEFAULT_REGISTRY)))
    while True:
        target = select(names)
        if target is None:
            os.execv("/bin/bash", ["/bin/bash"])
            return 1  # pragma: no cover - exec only returns on error
        returncode = hop(target)
        if returncode is None:
            print("Could not start SSH; choose another bottle.")
        elif returncode == 0:
            print("Disconnected from bottle; choose another bottle.")
        else:
            print(f"SSH exited with status {returncode}; choose another bottle.")
        log(f"disconnect target={target} exit_code={returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
