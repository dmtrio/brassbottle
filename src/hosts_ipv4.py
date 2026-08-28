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
  - Removes a NAME, not a line: an IPv6 line mapping several names keeps the
    ones that are not targets, so an alias with no IPv4 entry of its own does
    not become unresolvable. A name is removed only when an IPv4 line for it
    survives.
  - Idempotent, and a no-op on a file that never had the AAAA line.

Usage:
    python3 src/hosts_ipv4.py [name ...]     # default: host.docker.internal

Stdlib only (matches ensure_net.py / wire_plugins.py).
"""

import sys

HOSTS_PATH = "/etc/hosts"
DEFAULT_NAMES = ("host.docker.internal",)


def _split_comment(line):
    """(payload, comment) — comment keeps its leading '#', or is empty."""
    idx = line.find("#")
    if idx == -1:
        return line, ""
    return line[:idx], line[idx:]


def _fields(line):
    """(address, [names]) for a hosts line, or None if it carries no mapping."""
    payload, _ = _split_comment(line)
    parts = payload.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _is_ipv6(address):
    """True for an IPv6 literal. ':' is unambiguous here — a hosts address is
    never a host:port pair, so no need to drag in ipaddress for the check."""
    return ":" in address


def strip_ipv6_lines(text, names):
    """Return (new_text, changed_lines) with AAAA entries for `names` removed.

    Removal is per NAME, not per line. A hosts line can map several names to
    one address, so dropping the whole line would take unrelated aliases with
    it: given

        192.168.65.254   host.docker.internal
        fdc4::254        host.docker.internal gateway.internal

    the second line loses only `host.docker.internal`; `gateway.internal` has
    no IPv4 entry of its own and would become unresolvable. It is rewritten as
    `fdc4::254 gateway.internal`, and only a line left with no names at all is
    dropped outright.

    A name is removed only when it still has an IPv4 line elsewhere in the
    file — otherwise removing it would turn a slow path into a broken one.

    changed_lines holds the ORIGINAL text of every line removed or rewritten,
    for the caller to report.
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

    removable = wanted & has_ipv4

    kept, changed = [], []
    for line in lines:
        parsed = _fields(line)
        if parsed is None:
            kept.append(line)
            continue
        address, entry_names = parsed
        if not _is_ipv6(address) or not removable.intersection(
            n.lower() for n in entry_names
        ):
            kept.append(line)
            continue

        survivors = [n for n in entry_names if n.lower() not in removable]
        changed.append(line.rstrip("\n"))
        if not survivors:
            continue

        # Only a line we actually edit is reformatted; untouched lines stay
        # byte-identical. The comment and line ending ride along.
        _, comment = _split_comment(line)
        ending = "\n" if line.endswith("\n") else ""
        rebuilt = address + "\t" + " ".join(survivors)
        if comment:
            rebuilt += " " + comment.rstrip("\n")
        kept.append(rebuilt + ending)

    return "".join(kept), changed


def rewrite(path=HOSTS_PATH, names=DEFAULT_NAMES):
    """Apply strip_ipv6_lines to `path` in place; return the changed lines."""
    with open(path, "r", encoding="utf-8") as handle:
        original = handle.read()

    updated, changed = strip_ipv6_lines(original, names)
    if not changed:
        return changed

    # Truncate-and-write, not rename: see the module docstring.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)
    return changed


def main(argv=None):
    names = tuple(argv or ()) or DEFAULT_NAMES
    try:
        changed = rewrite(names=names)
    except OSError as exc:
        # Non-fatal by contract: the entrypoint warns and boots on. Losing the
        # rewrite costs a slow resolver path, not a broken container.
        print(f"⚠ {HOSTS_PATH}: could not drop AAAA entries ({exc})")
        return 1
    for line in changed:
        print(f"✓ {HOSTS_PATH}: dropped unroutable AAAA entry: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
