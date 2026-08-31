#!/usr/bin/env python3
"""djinn_net_addr.py — shared djinn-net subnet and static-address arithmetic.

Extracted so the singleton containers that need a stable bridge address
(jump, newt) derive it the same way instead of each keeping its own copy of
the rules. Past reviews have flagged exactly this shape of duplication
("constants drift ... without a single source of truth").

The rules, in one place:
  * The subnet comes from DJINN_SUBNET — the same variable up.sh reads.
  * Static addresses are taken from the TOP of the subnet, counting down.
    djinn-net is created with `--subnet` and no `--ip-range`, so docker's
    dynamic allocator hands out addresses ascending from .2 — the low end is
    where bottles land. Taking the top avoids collisions without carving out
    an --ip-range, which would mean recreating the bridge under every running
    bottle.
  * The network and broadcast addresses are inside the subnet but are not
    assignable; docker rejects them with an opaque IPAM error far downstream.
    Checks are constant-time — never enumerate a subnet, a /8 would
    materialise ~16.7M address objects.

Raises ValueError; callers wrap it in their own error type.
Stdlib only; host-side.
"""

from __future__ import annotations

import ipaddress
import os

NETWORK_NAME = "djinn-net"
DEFAULT_SUBNET = "172.30.0.0/24"
ENV_SUBNET = "DJINN_SUBNET"

# Smallest subnet a static address can be derived from: /29 is 8 addresses,
# 6 usable — room for the gateway, the singletons at the top, and a fleet in
# between. Below that the arithmetic still yields an address, but a /31's
# "last usable" is the network address itself.
MIN_PREFIXLEN_FOR_DERIVED = 29

# Every singleton that claims a fixed top-of-subnet slot, by offset. Used to
# reject an operator override that lands on another singleton's address —
# otherwise compose fails with an opaque "Address already in use", or worse
# the sibling is stopped, this one takes its address, and the NEXT start of
# the sibling is what breaks, far from the cause.
SINGLETON_OFFSETS = {1: "jump", 2: "newt"}


def resolve_subnet(env: dict[str, str] | None = None) -> ipaddress.IPv4Network:
    """The bridge subnet, from DJINN_SUBNET."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_SUBNET) or "").strip() or DEFAULT_SUBNET
    try:
        return ipaddress.IPv4Network(raw, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid {ENV_SUBNET} '{raw}': {exc}") from exc


def is_reserved(
    subnet: ipaddress.IPv4Network, addr: ipaddress.IPv4Address
) -> bool:
    """Network or broadcast address — in the subnet but not assignable."""
    return addr in (subnet.network_address, subnet.broadcast_address)


def is_gateway(
    subnet: ipaddress.IPv4Network, addr: ipaddress.IPv4Address
) -> bool:
    """The address docker assigns to the bridge gateway."""
    return addr == subnet.network_address + 1


def top_address(
    subnet: ipaddress.IPv4Network, offset: int, env_name: str = "the address"
) -> ipaddress.IPv4Address:
    """The offset-th assignable address counting down from the top.

    offset=1 is the last usable address, offset=2 the one below it, and so on.
    `env_name` is named in the error so an operator is told which override to
    set rather than a generic "set it explicitly".
    """
    if offset < 1:
        raise ValueError(f"offset must be >= 1, got {offset}")
    if subnet.prefixlen > MIN_PREFIXLEN_FOR_DERIVED:
        raise ValueError(
            f"{ENV_SUBNET} {subnet} is too small for a derived address — "
            f"use a /{MIN_PREFIXLEN_FOR_DERIVED} or larger, or set {env_name} "
            f"explicitly"
        )
    candidate = subnet.broadcast_address - offset
    if is_reserved(subnet, candidate) or is_gateway(subnet, candidate):
        raise ValueError(
            f"{ENV_SUBNET} {subnet} has no free address at offset {offset} "
            f"from the top — set {env_name} explicitly"
        )
    return candidate


def validate_static(
    subnet: ipaddress.IPv4Network,
    raw: str,
    env_name: str,
    own_offset: int | None = None,
) -> str:
    """Validate an operator-supplied static address inside the subnet.

    `own_offset` is the caller's own slot, so its own default is not reported
    as a collision with itself.
    """
    try:
        addr = ipaddress.IPv4Address(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {env_name} '{raw}': {exc}") from exc
    if addr not in subnet or is_reserved(subnet, addr):
        raise ValueError(
            f"{env_name} {addr} is not an assignable host address in "
            f"{ENV_SUBNET} {subnet}"
        )
    if is_gateway(subnet, addr):
        raise ValueError(
            f"{env_name} {addr} is the bridge gateway — pick another address"
        )
    for offset, owner in SINGLETON_OFFSETS.items():
        if offset == own_offset:
            continue
        try:
            taken = top_address(subnet, offset)
        except ValueError:
            continue
        if addr == taken:
            raise ValueError(
                f"{env_name} {addr} is the {owner} container's address — "
                f"pick another, or move {owner} too"
            )
    return str(addr)
