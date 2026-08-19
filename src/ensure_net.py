#!/usr/bin/env python3
"""ensure_net.py <subnet> — create or verify the shared `djinn-net` bridge
every container attaches to (single CIDR for VPN/tunnel targeting).

Extracted out of up.sh (was inline docker-network shell) so the create/verify
logic could be unit-tested; see tests/test_ensure_net.py.

Usage:
    python3 src/ensure_net.py <desired-subnet>

Behavior:
  - djinn-net exists: compare its actual subnet to the desired one; a
    mismatch is a warning (existing containers already depend on the actual
    subnet), not a failure — same as before this file existed.
  - djinn-net missing: create with the desired subnet; on failure (subnet
    overlaps something else) keep the DJINN_SUBNET hint.

Every docker/subprocess call goes through _run(), so the outbound half of
each boundary call is logged to stdout before it runs.

Stdlib only (matches wire_plugins.py / manifest.py).
"""

import subprocess
import sys

NET_NAME = "djinn-net"


class EnsureNetError(Exception):
    """Fatal ensure-net error; main() prints it as 'Error: …' and exits 1."""


def _run(cmd):
    """Single choke point for every subprocess call — logs the exact command
    about to run (the outbound half of the boundary log) so both the plan
    and any failure are reconstructable from stdout alone."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True)


def network_exists(name):
    return _run(["docker", "network", "inspect", name]).returncode == 0


def network_subnet(name):
    """The network's configured subnet, or None if it can't be read (missing
    network, no IPAM config, or an unexpected docker failure)."""
    result = _run(["docker", "network", "inspect", "-f",
                   "{{(index .IPAM.Config 0).Subnet}}", name])
    if result.returncode != 0:
        return None
    subnet = result.stdout.strip()
    return subnet or None


def create_network(subnet):
    return _run(["docker", "network", "create", "--subnet", subnet, NET_NAME]).returncode == 0


def ensure_net(subnet):
    """Ensure djinn-net exists with (best-effort) the desired subnet. Returns
    the process exit code (0/1); never raises EnsureNetError itself — this is
    the sole catcher."""
    if network_exists(NET_NAME):
        actual = network_subnet(NET_NAME)
        if actual and actual != subnet:
            print(f"  ⚠ {NET_NAME} already exists with subnet {actual} (config wants {subnet}).")
            print(f"    To change it: stop all djinn containers, 'docker network rm {NET_NAME}', rerun up.sh.")
        return 0

    print(f"Creating shared network {NET_NAME} ({subnet})")
    # A failed create tolerates losing a create race to a concurrent up.sh run
    # — only a real failure (subnet overlap) is an error.
    if not create_network(subnet) and not network_exists(NET_NAME):
        print(f"Error: could not create {NET_NAME} ({subnet}) — the subnet may overlap an "
              "existing docker network.", file=sys.stderr)
        print("Pick a free range via DJINN_SUBNET in ./.env (docker auto-allocates inside "
              "172.17-172.31).", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if not argv:
        print("Usage: ensure_net.py <subnet>", file=sys.stderr)
        return 1
    return ensure_net(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
