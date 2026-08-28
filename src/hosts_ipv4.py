#!/usr/bin/env python3
"""hosts_ipv4.py — drop AAAA /etc/hosts entries for names that must resolve
to the host gateway over IPv4 only (host.docker.internal, canonically).

Why this exists: Docker Desktop writes BOTH families for host.docker.internal:

    192.168.65.254        host.docker.internal
    fdc4:f303:9324::254   host.docker.internal

The container has no IPv6 route to that ULA, but getaddrinfo() sorts the AAAA
first (RFC 6724 prefers the ULA over the RFC 1918 v4 address), so every stdlib
client that resolves the name by hand dies at connect() with
`[Errno 101] Network is unreachable`. That silently broke request-egress /
egress_nflog filings: the broker was reachable the whole time on
192.168.65.254:8816, and the agent saw a network error instead of a decision.

init-firewall.sh already sidesteps this with `getent ahostsv4`; this module
fixes it once, at the resolver, for every client in the container.

Behavior:
  - Rewrites /etc/hosts IN PLACE (truncate + write, never replace): the file
    is a Docker bind mount, so an atomic rename would fail with EBUSY or —
    worse — orphan the mount.
  - Only drops an IPv6 line when an IPv4 line for the same name survives.
    Never make a name unresolvable to fix its ordering.
  - Idempotent, and a no-op on a file that never had the AAAA line.

Usage:
    python3 src/hosts_ipv4.py [name ...]     # default: host.docker.internal

Stdlib only (matches ensure_net.py / wire_plugins.py).
"""

import sys

HOSTS_PATH = "/etc/hosts"
DEFAULT_NAMES = ("host.docker.internal",)


def _fields(line):
    """(address, [names]) for a hosts line, or None if it carries no mapping."""
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return None
    parts = stripped.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _is_ipv6(address):
    """True for an IPv6 literal. ':' is unambiguous here — a hosts address is
    never a host:port pair, so no need to drag in ipaddress for the check."""
    return ":" in address


def strip_ipv6_lines(text, names):
    """Return (new_text, dropped_lines) with AAAA lines for `names` removed.

    A line is dropped only when it maps one of `names` over IPv6 AND that name
    still has an IPv4 line elsewhere in the file — otherwise dropping it would
    turn a slow path into a broken one.
    """
    wanted = {name.lower() for name in names}
    lines = text.splitlines(keepends=True)

    has_ipv4 = set()
    for line in lines:
        parsed = _fields(line)
        if parsed is None:
            continue
        address, entry_names = parsed
        if _is_ipv6(address):
            continue
        has_ipv4.update(wanted.intersection(n.lower() for n in entry_names))

    kept, dropped = [], []
    for line in lines:
        parsed = _fields(line)
        if parsed is not None:
            address, entry_names = parsed
            hit = wanted.intersection(n.lower() for n in entry_names)
            if _is_ipv6(address) and hit and hit <= has_ipv4:
                dropped.append(line.rstrip("\n"))
                continue
        kept.append(line)

    return "".join(kept), dropped


def rewrite(path=HOSTS_PATH, names=DEFAULT_NAMES):
    """Apply strip_ipv6_lines to `path` in place; return the dropped lines."""
    with open(path, "r", encoding="utf-8") as handle:
        original = handle.read()

    updated, dropped = strip_ipv6_lines(original, names)
    if not dropped:
        return dropped

    # Truncate-and-write, not rename: see the module docstring.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)
    return dropped


def main(argv=None):
    names = tuple(argv or ()) or DEFAULT_NAMES
    try:
        dropped = rewrite(names=names)
    except OSError as exc:
        # Non-fatal by contract: the entrypoint warns and boots on. Losing the
        # rewrite costs a slow resolver path, not a broken container.
        print(f"⚠ {HOSTS_PATH}: could not drop AAAA entries ({exc})")
        return 1
    for line in dropped:
        print(f"✓ {HOSTS_PATH}: dropped unroutable AAAA entry: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
