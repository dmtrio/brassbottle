#!/usr/bin/env python3
"""egress_request.py — shared in-container egress filing for CLI/MCP tools.

Wraps the transparent broker's filing (POST /egress answered at once) with
polling for the per-request decision, multi-host batching, and check-only
ipset probes. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_DJINN_LIB = Path("/usr/local/lib/djinn")

# The baked image copy of this script lives in /usr/local/bin with its
# sibling modules in /usr/local/lib/djinn — add that directory only when
# the imports below cannot resolve from the current path (a repo checkout
# must never be shadowed by a stale baked copy).
try:
    from egress_broker import file_egress, generate_request_id, poll_decision
    from egress_broker_host import DEFAULT_HOLD_SECONDS, normalize_destination
    from egress_nflog import default_broker_url, load_broker_token
except ImportError:  # pragma: no cover - baked-image layout
    if _DJINN_LIB.is_dir() and str(_DJINN_LIB) not in sys.path:
        sys.path.insert(0, str(_DJINN_LIB))
    from egress_broker import file_egress, generate_request_id, poll_decision
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
    """Outcome of one filing-and-poll."""

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


def split_hosts_and_reason(tokens: list[str]) -> tuple[list[str], str | None]:
    """Split positional arguments into hosts and an optional trailing reason.

    Every leading token that parses as host[:port] is a host; the first
    token that does not is the reason and must be the LAST token — two
    non-host tokens raise ValueError naming them. A single-word reason
    that happens to be a valid domain is indistinguishable from a host and
    is treated as one; quote multi-word reasons.
    """
    hosts: list[str] = []
    rest: list[str] | None = None
    for index, token in enumerate(tokens):
        try:
            parse_host_target(token)
        except ValueError:
            rest = tokens[index:]
            break
        hosts.append(token)
    if not rest:
        return hosts, None
    if len(rest) > 1:
        raise ValueError(
            "expected one reason token after the hosts; got extra argument(s): "
            + ", ".join(repr(token) for token in rest[1:])
        )
    return hosts, rest[0]


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


def _map_decision_body(
    target: HostTarget,
    body: dict[str, Any],
) -> HostRequestResult:
    """Map a terminal per-request decision body to a result."""
    decision = body.get("decision")
    if decision == "allow":
        return HostRequestResult(
            target.host, target.port, "allowed", body.get("scope", "live")
        )
    if decision == "deny":
        detail = ""
        if body.get("reason") == "denylist":
            zone = body.get("zone", "")
            scope = body.get("scope", "")
            detail = f"denylist: zone={zone} scope={scope}"
        return HostRequestResult(target.host, target.port, "denied", detail)
    if decision == "error":
        return HostRequestResult(
            target.host, target.port, "error", str(body.get("reason") or "error")
        )
    return HostRequestResult(target.host, target.port, "error", f"unexpected body: {body!r}")


def request_host(
    target: HostTarget,
    *,
    reason: str | None,
    container: str,
    broker_url: str,
    broker_token: str,
    hold_seconds: int,
    file_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] = file_egress,
    poll_fn: Callable[..., dict[str, Any] | None] | None = None,
) -> HostRequestResult:
    """File one destination and poll until decided or the hold deadline.

    `hold_seconds` is this client's own deadline: past it the filing's
    pending result is returned as today (the row stays open, so a late
    allow still installs the rule for the next attempt).
    """
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
        return HostRequestResult(target.host, target.port, "error", err)
    if not isinstance(body, dict):
        return HostRequestResult(target.host, target.port, "error", "invalid response")

    decision = body.get("decision")
    if decision in ("allow", "deny"):
        # Short-circuits (denylist, a decided row) answer terminally at once.
        return _map_decision_body(target, body)

    if decision != "pending":
        return HostRequestResult(target.host, target.port, "error", f"unexpected body: {body!r}")

    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return HostRequestResult(target.host, target.port, "error", "pending body without request_id")
    attempt = body.get("attempt")
    baseline = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
    deadline = time.monotonic() + max(0.0, float(hold_seconds))
    if poll_fn is None:
        poll_fn = lambda **kwargs: poll_decision(  # noqa: E731  # bound to this filing's URL/token
            url=broker_url, token=broker_token, **kwargs
        )
    final = poll_fn(
        request_id=request_id,
        baseline_attempt=baseline,
        deadline=deadline,
    )
    if final is None:
        return HostRequestResult(target.host, target.port, "pending", "")
    if not isinstance(final, dict):
        return HostRequestResult(target.host, target.port, "error", "invalid response")
    return _map_decision_body(target, final)


def request_hosts(
    hosts: list[str],
    *,
    reason: str | None = None,
    container: str | None = None,
    broker_url: str | None = None,
    broker_token: str | None = None,
    hold_seconds: int | None = None,
    default_port: int = 443,
    file_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] = file_egress,
    poll_fn: Callable[..., dict[str, Any] | None] | None = None,
) -> tuple[list[HostRequestResult], int]:
    """File each host in order, polling each to its decision; return results
    and a process exit code. A host still pending at its deadline reports
    `pending` and exits EXIT_PENDING."""
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
                poll_fn=poll_fn,
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
        "arguments",
        nargs="+",
        metavar="HOST",
        help=(
            "one or more hostnames (optionally host:port), optionally followed"
            " by ONE quoted reason token, e.g.: request-egress example.com"
            ' "installing deps"'
        ),
    )
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=None,
        help=f"poll deadline per host, in seconds (default {DEFAULT_HOLD_SECONDS})",
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
    try:
        hosts, reason = split_hosts_and_reason(args.arguments)
    except ValueError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_DENIED
    if not hosts:
        print("at least one host is required", file=sys.stderr)
        return EXIT_DENIED

    if args.check:
        results = check_hosts(hosts)
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
            hosts,
            reason=reason,
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
    sys.exit(main())
