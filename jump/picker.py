#!/usr/bin/env python3
"""Interactive selector for the host-generated djinn-jump bottle registry."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_REGISTRY = "/etc/djinn-jump/registry/bottles"
NAME_RE = re.compile(r"^djinn-[A-Za-z0-9_-]+$")


def log(message: str) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        except EOFError:
            log("cancel reason=eof")
            return None
        if choice.lower() == "q":
            log("cancel reason=operator")
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(names):
            target = names[int(choice) - 1]
            log(f"connect target={target}")
            return target
        print("Choose a listed number or q.", file=output)


def main() -> int:
    os.environ["DJINN_JUMP_PICKER_DONE"] = "1"
    names = read_names(Path(os.environ.get("DJINN_JUMP_REGISTRY", DEFAULT_REGISTRY)))
    target = select(names)
    if target:
        os.execvp("ssh", ["ssh", target])
    os.execv("/bin/bash", ["/bin/bash"])
    return 1  # pragma: no cover - exec only returns on error


if __name__ == "__main__":
    raise SystemExit(main())
