#!/usr/bin/env python3
"""egress_request.py — shared in-container egress filing for CLI and MCP tools.

Wraps the host broker long-poll (/egress) with normalization, multi-host
batching, and check-only ipset probes. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_DJINN_LIB = Path("/usr/local/lib/djinn")
if _DJINN_LIB.is_dir() and str(_DJINN_LIB) not in sys.path:
    sys.path.insert(0, str(_DJINN_LIB))

from typing import Any, Callable

from egress_broker import file_egress_with_hold, generate_request_id
from egress_broker_host import DEFAULT_HOLD_SECONDS, normalize_destination
from egress_nflog import default_broker_url, load_broker_token

EXIT_ALLOWED = 0
EXIT_DENIED = 1
EXIT_PENDING = 2

IPSET_NAME = "allowed-domains"
HOST_PORT_RE = re.compile(r"^(?P<host>.+):(?P<port>\d+)$")


@dataclass(frozen=True)
class HostTarget:
    """One normalized filing destination."""

    host: str
    port: int
    host_is_ip: bool
    raw: str


@dataclass(frozen=True)
class HostCheckResult:
    """Outcome of a non-blocking allowlist probe."""

    host: str
    port: int
    status: str
    detail: str = ""


@dataclass(frozen=True)
class HostRequestResult:
    """Outcome of one long-poll filing."""

    host: str
    port: int
    decision: str
    detail: str = ""


def parse_host_target(raw: str, *, default_port: int = 443) -> HostTarget:
    """Parse a host or host:port string into a filing target."""
    value = raw.strip()
    if not value:
        raise ValueError("host must not be empty")
    port = default_port
    host_part = value
    match = HOST_PORT_RE.fullmatch(value)
    if match:
        host_part = match.group("host")
        port = int(match.group("port"))
    host, host_is_ip = normalize_destination(host_part)
    return HostTarget(host=host, port=port, host_is_ip=host_is_ip, raw=value)


def container_name() -> str:
    return os.environ.get("CONTAINER_NAME", "unnamed")


def ipset_contains(ip: str, *, runner: Callable[..., Any] | None = None) -> bool:
    """Return True when ip is already in the allowed-domains ipset."""
    run = runner or subprocess.run
    try:
        result = run(
            ["ipset", "test", IPSET_NAME, ip],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_ipv4(host: str) -> list[str]:
    """Resolve host to IPv4 addresses (empty when lookup fails)."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in ips:
            ips.append(sockaddr[0])
    return ips


def check_host(
    raw: str,
    *,
    default_port: int = 443,
    runner: Callable[..., Any] | None = None,
) -> HostCheckResult:
    """Return allowed when any resolved IP is already in the ipset."""
    try:
        target = parse_host_target(raw, default_port=default_port)
    except ValueError as exc:
        return HostCheckResult(raw, default_port, "invalid", str(exc))

    if target.host_is_ip:
        if ipset_contains(target.host, runner=runner):
            return HostCheckResult(target.host, target.port, "allowed", "ip in ipset")
        return HostCheckResult(target.host, target.port, "blocked", "ip not in ipset")

    ips = resolve_ipv4(target.host)
    if not ips:
        return HostCheckResult(target.host, target.port, "unknown", "dns lookup failed")
    for ip in ips:
        if ipset_contains(ip, runner=runner):
            return HostCheckResult(target.host, target.port, "allowed", f"{ip} in ipset")
    return HostCheckResult(
        target.host,
        target.port,
        "blocked",
        f"resolved {len(ips)} ip(s), none in ipset",
    )


def check_hosts(
    hosts: list[str],
    *,
    default_port: int = 443,
    runner: Callable[..., Any] | None = None,
) -> list[HostCheckResult]:
    return [check_host(host, default_port=default_port, runner=runner) for host in hosts]


def request_host(
    target: HostTarget,
    *,
    reason: str | None,
    container: str,
    broker_url: str,
    broker_token: str,
    hold_seconds: int,
    file_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] = file_egress_with_hold,
) -> HostRequestResult:
    """File one destination and block until allow/deny/pending."""
    body, err = file_fn(
        url=broker_url,
        token=broker_token,
        container=container,
        host=target.host,
        port=target.port,
        request_id=generate_request_id(),
        host_is_ip=target.host_is_ip,
        hold_seconds=hold_seconds,
        reason=reason,
    )
    if err:
        return HostRequestResult(
            target.host,
            target.port,
            "error",
            err,
        )
    if not isinstance(body, dict):
        return HostRequestResult(target.host, target.port, "error", "invalid response")
    decision = body.get("decision")
    if decision == "allow":
        return HostRequestResult(target.host, target.port, "allowed", body.get("scope", "live"))
    if decision == "deny":
        return HostRequestResult(target.host, target.port, "denied", "")
    if decision == "pending":
        return HostRequestResult(target.host, target.port, "pending", "")
    return HostRequestResult(target.host, target.port, "error", f"unexpected body: {body!r}")


def request_hosts(
    hosts: list[str],
    *,
    reason: str | None = None,
    container: str | None = None,
    broker_url: str | None = None,
    broker_token: str | None = None,
    hold_seconds: int | None = None,
    default_port: int = 443,
    file_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] = file_egress_with_hold,
) -> tuple[list[HostRequestResult], int]:
    """File each host in order; return results and a process exit code."""
    if not hosts:
        raise ValueError("at least one host is required")

    container_name_value = container or container_name()
    token = broker_token if broker_token is not None else load_broker_token()
    if not token:
        raise RuntimeError("EGRESS_BROKER_TOKEN is not set")

    url = broker_url if broker_url is not None else default_broker_url()
    hold = hold_seconds if hold_seconds is not None else DEFAULT_HOLD_SECONDS

    results: list[HostRequestResult] = []
    for raw in hosts:
        target = parse_host_target(raw, default_port=default_port)
        results.append(
            request_host(
                target,
                reason=reason,
                container=container_name_value,
                broker_url=url,
                broker_token=token,
                hold_seconds=hold,
                file_fn=file_fn,
            )
        )

    if any(item.decision == "error" for item in results):
        return results, EXIT_DENIED
    if any(item.decision == "denied" for item in results):
        return results, EXIT_DENIED
    if any(item.decision == "pending" for item in results):
        return results, EXIT_PENDING
    return results, EXIT_ALLOWED


def format_request_results(results: list[HostRequestResult]) -> str:
    lines: list[str] = []
    for item in results:
        suffix = f" ({item.detail})" if item.detail else ""
        lines.append(f"{item.host}:{item.port} {item.decision}{suffix}")
    return "\n".join(lines)


def format_check_results(results: list[HostCheckResult]) -> str:
    lines: list[str] = []
    for item in results:
        suffix = f" ({item.detail})" if item.detail else ""
        lines.append(f"{item.host}:{item.port} {item.status}{suffix}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="File egress approval requests with the host broker and wait for a decision",
    )
    parser.add_argument(
        "hosts",
        nargs="+",
        metavar="HOST",
        help="one or more hostnames (optionally host:port)",
    )
    parser.add_argument(
        "reason",
        nargs="?",
        default="",
        help="reason attached to the filing (quoted if multiple words)",
    )
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=None,
        help=f"broker hold window (default {DEFAULT_HOLD_SECONDS})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="probe ipset only; do not file with the host broker",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON on stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        results = check_hosts(args.hosts)
        if args.json:
            payload = [
                {
                    "host": item.host,
                    "port": item.port,
                    "status": item.status,
                    "detail": item.detail,
                }
                for item in results
            ]
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print(format_check_results(results))
        return EXIT_ALLOWED

    try:
        results, code = request_hosts(
            args.hosts,
            reason=args.reason or None,
            hold_seconds=args.hold_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DENIED

    if args.json:
        payload = [
            {
                "host": item.host,
                "port": item.port,
                "decision": item.decision,
                "detail": item.detail,
            }
            for item in results
        ]
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(format_request_results(results))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
