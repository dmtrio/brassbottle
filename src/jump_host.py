#!/usr/bin/env python3
"""jump_host.py — host-side operator commands for the singleton jump container.

Thin docker-compose glue around jump_config. Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ensure_net  # noqa: E402
import jump_registry  # noqa: E402
from jump_config import (  # noqa: E402
    SERVICE_NAME,
    JumpConfigError,
    JumpIdentity,
    derive_identity,
    paths,
    resolve_authorized_keys,
    resolve_jump_ip,
    resolve_mosh_ports,
    resolve_subnet,
    write_compose_file,
)


class JumpHostError(Exception):
    """Operator-facing jump error."""


class DockerCommandMissing(Exception):
    """Docker CLI absent from PATH — boundary error already logged."""

    exit_code = 127


# How long `jump start` waits for the container entrypoint to write the
# client key before returning. Every `./djinn up` reads that key, and the old
# paste-into-secrets.env step is gone, so nothing else serialises "jump start"
# and the first "up" — a fleet script running them back to back would
# otherwise see "no jump key yet" and come up non-jump-reachable.
KEY_WAIT_SECONDS = 15.0
KEY_WAIT_POLL_SECONDS = 0.5

DOCKER_MISSING_EXIT = DockerCommandMissing.exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _compose_cmd(base_path: Path, identity: JumpIdentity, *args: str) -> list[str]:
    p = paths(base_path)
    return [
        "docker",
        "compose",
        "-p",
        identity.compose_project_name,
        "--project-directory",
        str(_repo_root()),
        "-f",
        str(p["compose_file"]),
        *args,
    ]


def _run(
    cmd: list[str],
    *,
    boundary: str | None = None,
    started: float | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
    except FileNotFoundError as exc:
        print(f"jump {boundary or 'run'} error reason=docker-not-found", file=sys.stderr)
        raise DockerCommandMissing(str(exc)) from exc
    if boundary is not None and started is not None:
        print(
            f"jump {boundary} done duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}"
        )
    return result


def _service_running(base_path: Path, identity: JumpIdentity, *, boundary: str) -> bool:
    """True only when the service is actually RUNNING.

    `ps -q` returns an id for a created/restarting container too, and the jump
    is `restart: unless-stopped` — so an entrypoint that dies on every start
    would report "running" while nothing is reachable, which is precisely the
    silent failure this command exists to catch. Same shape as
    backup_host._service_running.
    """
    result = _run(
        _compose_cmd(base_path, identity, "ps", "--status", "running", "--services"),
        boundary=boundary,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    return SERVICE_NAME in (result.stdout or "").split()


def _authorized_keys(base_path: Path) -> tuple[list[str], bool]:
    """Operator keys, and whether they must be seeded into the key file.

    See jump_config.resolve_authorized_keys for the precedence rule.
    """
    keys, source = resolve_authorized_keys(base_path)
    print(f"jump authorized_keys resolved source={source} count={len(keys)}")
    return keys, source == "env"


def _live_subnet() -> ipaddress.IPv4Network | None:
    """The subnet djinn-net actually has, or None if it cannot be read.

    ensure_net only WARNS when a pre-existing bridge disagrees with
    DJINN_SUBNET and still returns 0, so the desired value is not a safe basis
    for a static address. Reading the live network is what makes
    resolve_jump_ip's contract true.
    """
    try:
        raw = ensure_net.network_subnet(ensure_net.NET_NAME)
    except Exception as exc:  # noqa: BLE001 — boundary: never fail start on this
        print(f"jump ensure-net warn reason=subnet-unreadable detail={exc}")
        return None
    if not raw:
        return None
    try:
        return ipaddress.IPv4Network(raw, strict=True)
    except ValueError:
        print(f"jump ensure-net warn reason=subnet-unparseable value={raw}")
        return None


def _ensure_network(base_path: Path) -> int:
    """Create/validate djinn-net before compose runs.

    The generated compose declares the bridge `external: true`, so compose
    will not create it — and `./djinn jump start` is documented as the FIRST
    step of a fresh install, before any `./djinn up` has run ensure_net. Same
    call up.sh makes (src/ensure_net.py owns create/verify and is unit-tested);
    the subnet is passed as an argument, not inherited from the environment.

    sys.executable, not a bare "python3": jump.sh resolves $PYTHON3 through
    common.sh's require_python3 precisely because a broken PATH shim is the
    common Mac failure. Re-looking-up "python3" here would reintroduce that
    failure one step later, after jump_host had already started fine.
    """
    subnet = str(resolve_subnet())
    try:
        return _run(
            [sys.executable, str(_repo_root() / "src" / "ensure_net.py"), subnet],
            boundary="ensure-net",
            check=False,
        ).returncode
    except DockerCommandMissing:
        raise


def cmd_start(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print(f"jump start begin base={base_path}")
    try:
        keys, seed = _authorized_keys(base_path)
        jump_ip = resolve_jump_ip()
        mosh_ports = resolve_mosh_ports()
    except (JumpConfigError, JumpHostError) as exc:
        print(f"jump start error reason={exc}", file=sys.stderr)
        return 1
    try:
        rc = _ensure_network(base_path)
        if rc != 0:
            print(
                f"jump start error reason=djinn-net-unavailable exit_code={rc}",
                file=sys.stderr,
            )
            return rc
        # Re-derive against the bridge that actually exists (see _live_subnet).
        live = _live_subnet()
        if live is not None and live != resolve_subnet():
            print(
                f"jump start warn reason=subnet-drift live={live} "
                f"desired={resolve_subnet()} — using the live bridge"
            )
        try:
            jump_registry.refresh(base_path)
            if live is not None:
                if live != resolve_subnet():
                    jump_ip = resolve_jump_ip(subnet=live)
                write_compose_file(base_path, keys, subnet=live, seed=seed)
            else:
                write_compose_file(base_path, keys, seed=seed)
        except (JumpConfigError, jump_registry.JumpRegistryError) as exc:
            print(f"jump start error reason={exc}", file=sys.stderr)
            return 1
        result = _run(
            _compose_cmd(base_path, identity, "up", "-d", "--build", SERVICE_NAME),
            boundary="start",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if result.returncode != 0:
        print(
            f"jump start error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode
    key_ready = _wait_for_client_key(base_path)
    print(
        f"jump start ok container={identity.container_name} ip={jump_ip} "
        f"mosh_ports={mosh_ports} client_key={'ready' if key_ready else 'pending'}"
    )
    print("")
    print(f"  Reach it over your tunnel:  mosh coder@{jump_ip}")
    print("  Pick a bottle after login (q keeps the manual SSH shell).")
    print("")
    print("  Bottles authorise this jump on their next ./djinn up")
    print("  (./djinn jump pubkey prints the key).")
    return 0


def _wait_for_client_key(base_path: Path) -> bool:
    """Poll for the entrypoint-generated client key; never fatal.

    Returns True once the file exists, False after KEY_WAIT_SECONDS with a
    stderr warning — the container is up either way, only the first
    `./djinn up` would need re-running.
    """
    pub = paths(base_path)["client_pubkey"]
    deadline = time.monotonic() + KEY_WAIT_SECONDS
    while True:
        if pub.exists():
            return True
        if time.monotonic() >= deadline:
            print(
                f"jump start warn reason=client-key-pending path={pub} "
                f"waited={KEY_WAIT_SECONDS:.0f}s — bottles started before it "
                f"appears need another ./djinn up",
                file=sys.stderr,
            )
            return False
        time.sleep(KEY_WAIT_POLL_SECONDS)


def cmd_refresh(base_path: Path) -> int:
    """Refresh the picker registry without recreating the jump container."""
    print(f"jump refresh begin base={base_path}")
    try:
        jump_registry.refresh(base_path)
    except jump_registry.JumpRegistryError as exc:
        print(f"jump refresh error reason={exc}", file=sys.stderr)
        return 1
    return 0


def cmd_scope(base_path: Path) -> int:
    """Print the opaque installation scope used by bottle registry labels."""
    print(derive_identity(base_path).suffix)
    return 0


def cmd_stop(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    started = time.monotonic()
    if not compose_file.exists():
        print(f"jump stop not-configured project={identity.compose_project_name}")
        return 0
    try:
        result = _run(
            _compose_cmd(base_path, identity, "down"),
            boundary="stop",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    return result.returncode


def cmd_status(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print(f"jump status not-configured project={identity.compose_project_name}")
        return 1
    try:
        running = _service_running(base_path, identity, boundary="status")
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"jump status {'running' if running else 'stopped'} "
        f"container={identity.container_name} project={identity.compose_project_name}"
    )
    return 0 if running else 1


def cmd_logs(base_path: Path, follow: bool) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise JumpHostError("jump container is not configured — run: ./djinn jump start")
    args = ["logs", SERVICE_NAME]
    if follow:
        args.append("-f")
    try:
        return _run(
            _compose_cmd(base_path, identity, *args), boundary="logs", check=False
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def read_client_pubkey(base_path: Path) -> str:
    """Read the jump's client public key from the host side of the mount.

    Works whether or not the container is running (the key is generated on
    first start). Raises JumpHostError when the key is missing or unreadable.
    """
    pub = paths(base_path)["client_pubkey"]
    # Path.exists() swallows PermissionError and returns False, which would
    # tell an operator who cannot traverse the directory to re-run the very
    # command that just created the key — an infinite loop. stat() separately
    # so "missing" and "unreadable" get different advice.
    try:
        pub.stat()
    except FileNotFoundError:
        raise JumpHostError(
            f"no jump key yet at {pub} — run: ./djinn jump start (it is generated on first start)"
        ) from None
    except OSError as exc:
        raise JumpHostError(
            f"cannot read {pub}: {exc} — check ownership of {pub.parent} "
            f"(the container writes it as uid 1000)"
        ) from exc
    try:
        return pub.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise JumpHostError(f"cannot read {pub}: {exc}") from exc


def cmd_pubkey(base_path: Path) -> int:
    """Print the jump's client public key — what bottles authorise."""
    print(read_client_pubkey(base_path))
    return 0


def cmd_authorized_key(
    base_path: Path, env: Mapping[str, str] | None = None
) -> int:
    """Print the key up.sh should pass to bottles as JUMP_AUTHORIZED_KEY.

    Resolution order: an explicit JUMP_AUTHORIZED_KEY in the environment wins
    (override for a jump that runs elsewhere — deprecated, warned on stderr);
    else the key file under $DJINN_HOME/jump/ssh/; else JumpHostError (up.sh
    treats that as "not jump-reachable this run").
    """
    env = os.environ if env is None else env
    override = (env.get("JUMP_AUTHORIZED_KEY") or "").strip()
    if override:
        pub = paths(base_path)["client_pubkey"]
        print(
            f"⚠ jump: JUMP_AUTHORIZED_KEY in secrets.env overrides {pub} "
            f"— drop it unless the jump runs elsewhere",
            file=sys.stderr,
        )
        print(override)
        return 0
    print(read_client_pubkey(base_path))
    return 0


def cmd_ip(base_path: Path) -> int:
    """Print the jump's static bridge address — one line, nothing else.

    up.sh calls this (host-side, after ensure_net) to resolve DJINN_JUMP_IP,
    the address init-firewall.sh scopes a bottle's inbound :22 ACCEPT to.
    Mirrors cmd_start's own derivation: prefer the LIVE djinn-net subnet when
    readable (ensure_net only WARNS on drift and still returns 0, so the
    desired subnet alone is not a safe basis for a static address), falling
    back to the desired subnet (resolve_jump_ip's default) when the bridge
    cannot be read yet — e.g. before the first ./djinn up or
    ./djinn jump start on a fresh install. JumpConfigError propagates to
    main(), which prints "Error: …" and exits 1.

    up.sh consumes this command's stdout as a bare `JUMP_IP=$(...)` value, so
    it must be exactly one IP line — _live_subnet's own warn prints (an
    unreadable or unparseable live subnet) go to stdout by default, which
    would otherwise land INSIDE that value. Redirect them to stderr for the
    duration of that one call only.
    """
    with contextlib.redirect_stdout(sys.stderr):
        subnet = _live_subnet()
    print(resolve_jump_ip(subnet=subnet))
    return 0


def resolve_base_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    home = os.environ.get("DJINN_HOME")
    if home:
        return Path(home)
    return _repo_root() / ".djinn"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="djinn jump",
        description="Singleton mosh jump container for this djinn installation.",
    )
    parser.add_argument("--base-path", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="build and start the jump container")
    sub.add_parser("refresh", help="refresh the jump bottle picker registry")
    sub.add_parser("scope", help=argparse.SUPPRESS)
    sub.add_parser("stop", help="stop and remove the jump container")
    sub.add_parser("status", help="report whether the jump container is running")
    logs = sub.add_parser("logs", help="show jump container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("pubkey", help="print the key bottles must authorise")
    sub.add_parser("authorized-key", help=argparse.SUPPRESS)
    sub.add_parser("ip", help="print the jump's static bridge address")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    try:
        if args.command == "start":
            return cmd_start(base_path)
        if args.command == "refresh":
            return cmd_refresh(base_path)
        if args.command == "scope":
            return cmd_scope(base_path)
        if args.command == "stop":
            return cmd_stop(base_path)
        if args.command == "status":
            return cmd_status(base_path)
        if args.command == "logs":
            return cmd_logs(base_path, args.follow)
        if args.command == "pubkey":
            return cmd_pubkey(base_path)
        if args.command == "authorized-key":
            return cmd_authorized_key(base_path)
        if args.command == "ip":
            return cmd_ip(base_path)
    except (JumpHostError, JumpConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
