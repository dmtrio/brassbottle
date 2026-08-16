#!/usr/bin/env python3
"""ensure_net.py <subnet> — create or verify the shared `djinn-net` bridge
every container attaches to (single CIDR for VPN/tunnel targeting).

Extracted out of up.sh (was inline docker-network shell) because a
pre-existing docker-dev/dev-agent install still has `dev-agent-net` holding
the default subnet, and the old inline logic left it alone forever — so the
first `up.sh` on a not-yet-migrated host failed to create `djinn-net` with a
"pool overlaps" error on every run. This module reconciles that handoff
(rebrand-transitional) before falling back to plain create-if-missing.

Usage:
    python3 src/ensure_net.py <desired-subnet>

Behavior:
  - djinn-net exists: compare its actual subnet to the desired one; a
    mismatch is a warning (existing containers already depend on the actual
    subnet), not a failure — same as before this file existed.
  - djinn-net missing, dev-agent-net exists (rebrand-transitional):
      - no attached containers -> log, remove dev-agent-net, then create
        djinn-net (the subnet is now free).
      - attached containers -> print their names and how to resolve
        (finish `./djinn migrate <name>` for each, or pick a free
        DJINN_SUBNET), exit 1. up.sh must abort in this case.
  - djinn-net missing, no old net: create with the desired subnet; on
    failure (subnet overlaps something else) keep the DJINN_SUBNET hint.

Every docker/subprocess call goes through _run(), so the outbound half of
each boundary call is logged to stdout before it runs — mirrors src/migrate.py.

Stdlib only (matches migrate.py / wire_plugins.py / manifest.py).
"""

import subprocess
import sys

NET_NAME = "djinn-net"
# rebrand-transitional: the pre-rebrand network name (docker-dev / dev-agent).
# Delete this constant and the branch that reads it once every container has
# been re-upped onto djinn-net and the old bridge is gone.
OLD_NET_NAME = "dev-agent-net"


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


def container_count(name):
    """Number of containers attached to network `name`, or None if it
    couldn't be determined (treated as "don't know" — never as zero)."""
    result = _run(["docker", "network", "inspect", "-f",
                   "{{len .Containers}}", name])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def container_names(name):
    result = _run(["docker", "network", "inspect", "-f",
                   "{{range .Containers}}{{.Name}} {{end}}", name])
    if result.returncode != 0:
        return []
    return result.stdout.split()


def remove_network(name):
    return _run(["docker", "network", "rm", name]).returncode == 0


def create_network(subnet):
    return _run(["docker", "network", "create", "--subnet", subnet, NET_NAME]).returncode == 0


def ensure_net(subnet):
    """Ensure djinn-net exists with (best-effort) the desired subnet. Returns
    the process exit code (0/1); never raises EnsureNetError itself — this is
    the sole catcher, mirroring migrate.migrate()."""
    if network_exists(NET_NAME):
        actual = network_subnet(NET_NAME)
        if actual and actual != subnet:
            print(f"  ⚠ {NET_NAME} already exists with subnet {actual} (config wants {subnet}).")
            print(f"    To change it: stop all djinn containers, 'docker network rm {NET_NAME}', rerun up.sh.")
        return 0

    # rebrand-transitional: djinn-net is missing — the old dev-agent-net
    # bridge may still be sitting on the subnet we want.
    if network_exists(OLD_NET_NAME):
        count = container_count(OLD_NET_NAME)
        if count == 0:
            print(f"  old network {OLD_NET_NAME} has no attached containers — removing it "
                  "to free its subnet.")
            if not remove_network(OLD_NET_NAME):
                print(f"Error: could not remove old network {OLD_NET_NAME}.", file=sys.stderr)
                return 1
        else:
            names = container_names(OLD_NET_NAME)
            label = f"{count} attached container(s)" if count is not None else "attached containers"
            print(f"Error: old network {OLD_NET_NAME} still has {label}"
                  + (f": {' '.join(names)}" if names else "") + ".", file=sys.stderr)
            print(f"  Finish './djinn migrate <name>' for each (migrate stops them), or set "
                  "DJINN_SUBNET to a free range in ./.env.", file=sys.stderr)
            return 1

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
