#!/usr/bin/env python3
"""tunnel_config.py — singleton VPN/tunnel connector: paths, identity, compose.

The ROLE: one container per djinn installation, joined to djinn-net, that
gives an external device L3 access to the bridge. That is what makes the jump
container and every bottle reachable from a phone.

The role is provider-neutral — a Tailscale subnet router, Netbird, headscale
or a plain wireguard-go container would occupy the same slot. Only the
`── Provider ──` block below knows which one is actually running, so the rest
of brassbottle (and its CLI, `./djinn tunnel`) never names a vendor. That
matches the repo's existing convention: committed docs say "WireGuard/VPN"
generically, never a product name.

Why this has to be a container, not a process on the Mac: Docker Desktop runs
containers in a LinuxKit VM, and macOS has no route to 172.30.0.x. A connector
running natively on the Mac could not reach the bridge any more than anything
else on macOS can, and we would be back to publishing a port per bottle. On a
Linux host it would not matter; on this one it is decisive.

Stdlib only; host-side (mirrors jump_config / backup_config).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import djinn_net_addr  # noqa: E402

IDENTITY_PREFIX = "djinn-tunnel"
SERVICE_NAME = "tunnel"
IDENTITY_SUFFIX_LENGTH = 8

DEFAULT_SUBNET = djinn_net_addr.DEFAULT_SUBNET
NETWORK_NAME = djinn_net_addr.NETWORK_NAME
ENV_SUBNET = djinn_net_addr.ENV_SUBNET

# One below the jump (which takes offset 1). Same top-of-subnet reasoning:
# see djinn_net_addr.top_address.
TUNNEL_ADDRESS_OFFSET = 2

ENV_TUNNEL_IP = "DJINN_TUNNEL_IP"
ENV_TUNNEL_IMAGE = "DJINN_TUNNEL_IMAGE"

# ── Provider ────────────────────────────────────────────────────────────────
# Everything vendor-specific is confined below. Swapping providers means
# changing this block and the compose it feeds — nothing else in brassbottle
# refers to it. Named as a constant so log lines and errors can say which
# connector is running without the rest of the module knowing.
PROVIDER = "newt"  # Pangolin's site connector

# Newt is "a fully user space WireGuard tunnel client and TCP/UDP proxy" (its
# README, wireguard-go netstack), so it needs neither NET_ADMIN nor
# /dev/net/tun. It dials OUT to PANGOLIN_ENDPOINT over a websocket, so nothing
# is published on this Mac, there is no router forward and no dynamic DNS —
# the internet-facing surface is the Pangolin server, not this machine.
#
# It must be a PRIVATE site resource, not a public one: a public resource is
# fronted by Traefik and UDP does not traverse an HTTP reverse proxy, so mosh
# could not ride it. RFC 04 resolved this on 2026-07-16.
#
# Pinned, never :latest — same rule as the Backrest image in backup_config.
# Verified on Docker Hub 2026-08-31: 1.16.0 is the newest release (published
# 2026-08-19). The plain tag is the multi-arch manifest; per-arch variants
# (arm64-1.16.0, amd64-1.16.0, armv7-1.16.0) also exist, so do NOT pin one of
# those — it would break on a host of a different architecture.
# DJINN_TUNNEL_IMAGE overrides this without a code change.
DEFAULT_TUNNEL_IMAGE = "fosrl/newt:1.16.0"

# The provider's own credential names, kept verbatim so they match what the
# Pangolin admin UI shows. These live in secrets.env, never on the CLI.
SECRET_VARS = ("PANGOLIN_ENDPOINT", "NEWT_ID", "NEWT_SECRET")
# ── end Provider ────────────────────────────────────────────────────────────


class TunnelConfigError(Exception):
    """Invalid tunnel configuration."""


class TunnelIdentity:
    """Stable Docker identity for one djinn installation's tunnel container."""

    def __init__(
        self,
        suffix: str,
        compose_project_name: str,
        container_name: str,
        hostname: str,
    ) -> None:
        self.suffix = suffix
        self.compose_project_name = compose_project_name
        self.container_name = container_name
        self.hostname = hostname

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TunnelIdentity):
            return NotImplemented
        return (
            self.suffix == other.suffix
            and self.compose_project_name == other.compose_project_name
            and self.container_name == other.container_name
            and self.hostname == other.hostname
        )


def identity_suffix(base_path: Path) -> str:
    """Short deterministic suffix from resolved DJINN_HOME (never the full path)."""
    resolved = str(base_path.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:IDENTITY_SUFFIX_LENGTH]


def derive_identity(base_path: Path) -> TunnelIdentity:
    suffix = identity_suffix(base_path)
    stem = f"{IDENTITY_PREFIX}-{suffix}"
    return TunnelIdentity(
        suffix=suffix,
        compose_project_name=stem,
        container_name=stem,
        hostname=stem,
    )


def paths(base_path: Path) -> dict[str, Path]:
    base = base_path.expanduser().resolve()
    return {
        "base": base,
        "compose_dir": base / "compose",
        "compose_file": base / "compose" / "tunnel.yml",
        # The provider credentials live here at 0600, NOT inline in the
        # compose overlay: NEWT_SECRET grants L3 access to the whole bridge,
        # and a world-readable copy of it under DJINN_HOME is exactly what
        # backup_config avoids for the restic password.
        "env_file": base / "compose" / "tunnel.env",
    }


def ensure_layout(base_path: Path) -> dict[str, Path]:
    p = paths(base_path)
    try:
        p["compose_dir"].mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TunnelConfigError(f"cannot create compose dir {p['compose_dir']}: {exc}") from exc
    return p


def resolve_subnet(env: dict[str, str] | None = None) -> ipaddress.IPv4Network:
    try:
        return djinn_net_addr.resolve_subnet(env)
    except ValueError as exc:
        raise TunnelConfigError(str(exc)) from exc


def resolve_tunnel_ip(
    env: dict[str, str] | None = None,
    subnet: ipaddress.IPv4Network | None = None,
) -> str:
    """Static bridge address for the tunnel — one below the jump.

    Callers pass the LIVE djinn-net subnet; ensure_net only warns on drift and
    still returns 0, so the desired DJINN_SUBNET is not a safe basis.
    """
    env = os.environ if env is None else env
    subnet = resolve_subnet(env) if subnet is None else subnet
    override = (env.get(ENV_TUNNEL_IP) or "").strip()
    try:
        if override:
            return djinn_net_addr.validate_static(
                subnet, override, ENV_TUNNEL_IP, own_offset=TUNNEL_ADDRESS_OFFSET
            )
        return str(
            djinn_net_addr.top_address(subnet, TUNNEL_ADDRESS_OFFSET, ENV_TUNNEL_IP)
        )
    except ValueError as exc:
        raise TunnelConfigError(str(exc)) from exc


def resolve_image(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    image = (env.get(ENV_TUNNEL_IMAGE) or "").strip() or DEFAULT_TUNNEL_IMAGE
    if ":" not in image.rsplit("/", 1)[-1]:
        raise TunnelConfigError(
            f"{ENV_TUNNEL_IMAGE} '{image}' has no tag — pin a version, never :latest"
        )
    if image.rsplit(":", 1)[-1] == "latest":
        raise TunnelConfigError(
            f"{ENV_TUNNEL_IMAGE} '{image}' is pinned to :latest — pin a version"
        )
    return image


def resolve_secrets(env: dict[str, str] | None = None) -> dict[str, str]:
    """The provider's credentials, all required."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    missing: list[str] = []
    for name in SECRET_VARS:
        value = (env.get(name) or "").strip()
        if not value:
            missing.append(name)
        else:
            out[name] = value
    if missing:
        raise TunnelConfigError(
            f"missing from secrets.env: {', '.join(missing)} — create a site in "
            f"the Pangolin admin UI and copy its values"
        )
    return out


def write_env_file(base_path: Path, secrets: dict[str, str]) -> Path:
    """Write the provider credentials to a 0600 file for compose's env_file.

    Same discipline as backup_config._create_password_atomic: create with an
    explicit restrictive mode rather than relying on the process umask, and
    never leave a readable window (write to a temp path in the same directory,
    chmod, then atomically rename).
    """
    p = ensure_layout(base_path)
    target = p["env_file"]
    tmp = target.with_suffix(".env.tmp")
    body = "".join(f"{name}={secrets[name]}\n" for name in SECRET_VARS)
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        try:
            tmp.unlink()
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise TunnelConfigError(f"cannot create {tmp}: {exc}") from exc
    except OSError as exc:
        raise TunnelConfigError(f"cannot create {tmp}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise TunnelConfigError(f"cannot write {target}: {exc}") from exc
    # Boundary log: never the values, only that it was written and how much.
    print(f"tunnel env-file written path={target} vars={len(SECRET_VARS)} mode=600")
    return target


def remove_env_file(base_path: Path) -> None:
    """Drop the credential file — called on stop so it does not outlive the
    container. It is regenerated from secrets.env on the next start."""
    paths(base_path)["env_file"].unlink(missing_ok=True)


def _scalar(value: str) -> str:
    """YAML-safe double-quoted scalar, with compose's ${VAR} interpolation
    neutralised ($$ is its literal-dollar escape)."""
    return json.dumps(value).replace("$", "$$")


def render_compose_yaml(
    *,
    identity: TunnelIdentity,
    tunnel_ip: str,
    image: str,
    env_file: Path,
) -> str:
    """Render the singleton tunnel compose overlay written under DJINN_HOME.

    The provider credentials are referenced through `env_file` (0600) rather
    than inlined: the overlay itself is written at the process umask and
    carries no secret, so it can be read, diffed and pasted safely.

    No `ports:` — the connector dials out. No `cap_add`/`devices` — it is userspace.
    """
    return f"""# Generated by brassbottle tunnel — do not hand-edit.
# Credentials are NOT here: see the 0600 env_file referenced below.
services:
  {SERVICE_NAME}:
    image: {image}
    container_name: {identity.container_name}
    hostname: {identity.hostname}
    restart: unless-stopped
    env_file:
      - {_scalar(str(env_file))}
    networks:
      default:
        ipv4_address: {tunnel_ip}

networks:
  default:
    name: {NETWORK_NAME}
    external: true
"""


def write_compose_file(
    base_path: Path,
    secrets: dict[str, str],
    subnet: ipaddress.IPv4Network | None = None,
) -> Path:
    p = ensure_layout(base_path)
    env_file = write_env_file(base_path, secrets)
    content = render_compose_yaml(
        identity=derive_identity(base_path),
        tunnel_ip=resolve_tunnel_ip(subnet=subnet),
        image=resolve_image(),
        env_file=env_file,
    )
    compose_path = p["compose_file"]
    try:
        compose_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise TunnelConfigError(f"cannot write compose overlay {compose_path}: {exc}") from exc
    return compose_path
