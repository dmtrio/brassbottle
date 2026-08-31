#!/usr/bin/env python3
"""tunnel_host.py — host-side operator commands for the singleton tunnel connector.

Thin docker-compose glue around tunnel_config. Stdlib only.

The connector has no key material and no persistent volume: its identity is
the provider secrets in secrets.env, and everything else is reconstructed from
them on each start. So there is no `pubkey`-style command and `stop` is safe.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import subprocess
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ensure_net  # noqa: E402

from tunnel_config import (  # noqa: E402
    PROVIDER,
    SERVICE_NAME,
    TunnelConfigError,
    TunnelIdentity,
    derive_identity,
    paths,
    remove_env_file,
    resolve_image,
    resolve_secrets,
    resolve_subnet,
    resolve_tunnel_ip,
    write_compose_file,
)


class TunnelHostError(Exception):
    """Operator-facing tunnel error."""


class DockerCommandMissing(Exception):
    """Docker CLI absent from PATH — boundary error already logged."""

    exit_code = 127


DOCKER_MISSING_EXIT = DockerCommandMissing.exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _compose_cmd(base_path: Path, identity: TunnelIdentity, *args: str) -> list[str]:
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
        print(f"tunnel {boundary or 'run'} error reason=docker-not-found", file=sys.stderr)
        raise DockerCommandMissing(str(exc)) from exc
    if boundary is not None and started is not None:
        print(
            f"tunnel {boundary} done duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}"
        )
    return result


def _service_running(base_path: Path, identity: TunnelIdentity, *, boundary: str) -> bool:
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


def _live_subnet() -> "ipaddress.IPv4Network | None":
    """The subnet djinn-net actually has, or None if it cannot be read.

    ensure_net only WARNS when a pre-existing bridge disagrees with
    DJINN_SUBNET and still returns 0, so the desired value is not a safe basis
    for a static address.
    """
    try:
        raw = ensure_net.network_subnet(ensure_net.NET_NAME)
    except Exception as exc:  # noqa: BLE001 — boundary: never fail start on this
        print(f"tunnel ensure-net warn reason=subnet-unreadable detail={exc}")
        return None
    if not raw:
        return None
    try:
        return ipaddress.IPv4Network(raw, strict=True)
    except ValueError:
        print(f"tunnel ensure-net warn reason=subnet-unparseable value={raw}")
        return None


def _ensure_network(base_path: Path) -> int:
    """Create/validate djinn-net before compose runs.

    The generated compose declares the bridge `external: true`, so compose
    will not create it. sys.executable, not a bare "python3": tunnel.sh resolves
    $PYTHON3 through common.sh's require_python3, and re-looking-up "python3"
    here would reintroduce that failure one step later.
    """
    subnet = str(resolve_subnet())
    return _run(
        [sys.executable, str(_repo_root() / "src" / "ensure_net.py"), subnet],
        boundary="ensure-net",
        check=False,
    ).returncode


# How long to wait for tunnel to settle after compose returns. A crash-loop
# restart takes a moment to show, so a single immediate check would pass.
SETTLE_CHECKS = 3
SETTLE_INTERVAL_SECONDS = 1.0


def _settled_running(base_path: Path, identity: TunnelIdentity) -> bool:
    """True once the service is running and stays running.

    Polls rather than checking once: a container that exits on start is
    briefly 'running' between restarts, so one sample can catch a crash-loop
    at the wrong instant.
    """
    for attempt in range(SETTLE_CHECKS):
        if attempt:
            time.sleep(SETTLE_INTERVAL_SECONDS)
        if not _service_running(base_path, identity, boundary="settle"):
            return False
    return True


def cmd_start(base_path: Path) -> int:
    identity = derive_identity(base_path)
    started = time.monotonic()
    print(f"tunnel start begin base={base_path}")
    try:
        secrets = resolve_secrets()
        image = resolve_image()
        tunnel_ip = resolve_tunnel_ip()
    except TunnelConfigError as exc:
        print(f"tunnel start error reason={exc}", file=sys.stderr)
        return 1
    try:
        rc = _ensure_network(base_path)
        if rc != 0:
            print(
                f"tunnel start error reason=djinn-net-unavailable exit_code={rc}",
                file=sys.stderr,
            )
            return rc
        live = _live_subnet()
        if live is not None and live != resolve_subnet():
            print(
                f"tunnel start warn reason=subnet-drift live={live} "
                f"desired={resolve_subnet()} — using the live bridge"
            )
        # `effective` is the subnet everything downstream must agree on: the
        # address written into compose, AND the CIDR the operator is told to
        # route. Reporting the desired one here would send them to configure
        # the VPN for a network the connector is not on.
        effective = live if live is not None else resolve_subnet()
        try:
            if live is not None:
                if live != resolve_subnet():
                    tunnel_ip = resolve_tunnel_ip(subnet=live)
                write_compose_file(base_path, secrets, subnet=live)
            else:
                write_compose_file(base_path, secrets)
        except TunnelConfigError as exc:
            print(f"tunnel start error reason={exc}", file=sys.stderr)
            remove_env_file(base_path)
            return 1
        result = _run(
            _compose_cmd(base_path, identity, "up", "-d", SERVICE_NAME),
            boundary="start",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if result.returncode != 0:
        # No usable connector exists, so its credentials must not be left on
        # disk. `down` first: a failed `up` can still have created a container,
        # and removing the env_file the compose overlay references would then
        # make every later compose call — including `stop` — fail on the
        # missing file, orphaning it.
        try:
            _run(
                _compose_cmd(base_path, identity, "down"),
                boundary="start-cleanup",
                check=False,
            )
        except DockerCommandMissing:
            pass
        remove_env_file(base_path)
        print(
            f"tunnel start error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode} credentials-removed=true "
            "— fix the cause and re-run './djinn tunnel start'.",
            file=sys.stderr,
        )
        return result.returncode
    # compose up returning 0 only means the container was CREATED. Newt exits
    # immediately on a bad NEWT_SECRET or PANGOLIN_ENDPOINT — the most likely
    # failure here — and `restart: unless-stopped` then loops it silently.
    # Printing the enrolment procedure at that point sends the operator off to
    # configure Pangolin against a connector that is not running.
    if not _settled_running(base_path, identity):
        print(
            f"tunnel start error reason=not-running-after-start provider={PROVIDER} "
            "— the container was created but is not running. Most likely bad "
            "credentials in secrets.env; check './djinn tunnel logs'. "
            "Credentials are kept so that command still works — "
            "'./djinn tunnel stop' clears them.",
            file=sys.stderr,
        )
        return 1
    # Never log the secrets themselves — identifiers and sizes only.
    print(
        f"tunnel start ok container={identity.container_name} ip={tunnel_ip} "
        f"image={image} provider={PROVIDER} subnet={effective}"
    )
    print("")
    print("  The connector dials OUT — nothing is published on this Mac.")
    print("  Point your VPN's private route at this bridge:")
    print(f"    {effective}   (or just {tunnel_ip} and the jump)")
    print("  Then enrol your phone and scope its AllowedIPs to that CIDR.")
    return 0


def cmd_stop(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    started = time.monotonic()
    if not compose_file.exists():
        print(f"tunnel stop not-configured project={identity.compose_project_name}")
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
    if result.returncode == 0:
        # The credential file must not outlive the container it was written
        # for; the next start regenerates it from secrets.env.
        remove_env_file(base_path)
        print("tunnel stop ok credentials-removed=true")
    return result.returncode


def cmd_status(base_path: Path) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print(f"tunnel status not-configured project={identity.compose_project_name}")
        return 1
    try:
        running = _service_running(base_path, identity, boundary="status")
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"tunnel status {'running' if running else 'stopped'} "
        f"container={identity.container_name} project={identity.compose_project_name}"
    )
    return 0 if running else 1


def cmd_logs(base_path: Path, follow: bool) -> int:
    identity = derive_identity(base_path)
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise TunnelHostError("tunnel container is not configured — run: ./djinn tunnel start")
    args = ["logs", SERVICE_NAME]
    if follow:
        args.append("-f")
    try:
        return _run(
            _compose_cmd(base_path, identity, *args), boundary="logs", check=False
        ).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def resolve_base_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    home = os.environ.get("DJINN_HOME")
    if home:
        return Path(home)
    return _repo_root() / ".djinn"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="djinn newt",
        description="Singleton Pangolin Newt connector for this djinn installation.",
    )
    parser.add_argument("--base-path", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="start the tunnel connector")
    sub.add_parser("stop", help="stop and remove the tunnel connector")
    sub.add_parser("status", help="report whether the tunnel container is running")
    logs = sub.add_parser("logs", help="show tunnel container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    try:
        if args.command == "start":
            return cmd_start(base_path)
        if args.command == "stop":
            return cmd_stop(base_path)
        if args.command == "status":
            return cmd_status(base_path)
        if args.command == "logs":
            return cmd_logs(base_path, args.follow)
    except (TunnelHostError, TunnelConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
