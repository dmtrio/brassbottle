#!/usr/bin/env python3
"""newt_config.py — singleton Pangolin Newt connector: paths, identity, compose.

Newt is the site connector for a Pangolin **private site resource**: it dials
OUT to your Pangolin instance and gives enrolled clients (Olm on the phone)
L3 access to whatever it can reach. Joining it to djinn-net is what makes the
jump container and every bottle reachable from a phone.

Why this has to be a container, not a process on the Mac: Docker Desktop runs
containers in a LinuxKit VM, and macOS has no route to 172.30.0.x. A Newt
running natively on the Mac could not reach the bridge any more than anything
else on macOS can, and we would be back to publishing a port per bottle. On a
Linux host it would not matter; on this one it is decisive.

Why no NET_ADMIN and no /dev/net/tun: Newt is "a fully user space WireGuard
tunnel client and TCP/UDP proxy" (its README) built on wireguard-go's netstack,
so it never creates a kernel interface. Nothing here needs elevated
capabilities — a genuinely better posture than a generic WireGuard container.

Nothing is published to the host: Newt makes an OUTBOUND websocket to
Pangolin, so there is no inbound port, no router forward and no dynamic DNS.
The internet-facing surface is the Pangolin server, not this Mac.

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

IDENTITY_PREFIX = "djinn-newt"
SERVICE_NAME = "newt"
IDENTITY_SUFFIX_LENGTH = 8

DEFAULT_SUBNET = djinn_net_addr.DEFAULT_SUBNET
NETWORK_NAME = djinn_net_addr.NETWORK_NAME
ENV_SUBNET = djinn_net_addr.ENV_SUBNET

# One below the jump (which takes offset 1). Same top-of-subnet reasoning:
# see djinn_net_addr.top_address.
NEWT_ADDRESS_OFFSET = 2

ENV_NEWT_IP = "DJINN_NEWT_IP"
ENV_NEWT_IMAGE = "DJINN_NEWT_IMAGE"

# Pinned, never :latest — same rule as the Backrest image in backup_config.
# 1.16.0 is the GitHub release tag as of 2026-08-30; Docker Hub was blocked by
# the egress allowlist when this was written, so the exact tag string is
# unverified. DJINN_NEWT_IMAGE overrides it without a code change.
DEFAULT_NEWT_IMAGE = "fosrl/newt:1.16.0"

# Secrets Newt needs, from secrets.env. Created in the Pangolin admin UI when
# you add a site; NEWT_SECRET is shown once.
SECRET_VARS = ("PANGOLIN_ENDPOINT", "NEWT_ID", "NEWT_SECRET")


class NewtConfigError(Exception):
    """Invalid newt configuration."""


class NewtIdentity:
    """Stable Docker identity for one djinn installation's newt container."""

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
        if not isinstance(other, NewtIdentity):
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


def derive_identity(base_path: Path) -> NewtIdentity:
    suffix = identity_suffix(base_path)
    stem = f"{IDENTITY_PREFIX}-{suffix}"
    return NewtIdentity(
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
        "compose_file": base / "compose" / "newt.yml",
    }


def ensure_layout(base_path: Path) -> dict[str, Path]:
    p = paths(base_path)
    try:
        p["compose_dir"].mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NewtConfigError(f"cannot create compose dir {p['compose_dir']}: {exc}") from exc
    return p


def resolve_subnet(env: dict[str, str] | None = None) -> ipaddress.IPv4Network:
    try:
        return djinn_net_addr.resolve_subnet(env)
    except ValueError as exc:
        raise NewtConfigError(str(exc)) from exc


def resolve_newt_ip(
    env: dict[str, str] | None = None,
    subnet: ipaddress.IPv4Network | None = None,
) -> str:
    """Static bridge address for newt — one below the jump.

    Callers pass the LIVE djinn-net subnet; ensure_net only warns on drift and
    still returns 0, so the desired DJINN_SUBNET is not a safe basis.
    """
    env = os.environ if env is None else env
    subnet = resolve_subnet(env) if subnet is None else subnet
    override = (env.get(ENV_NEWT_IP) or "").strip()
    try:
        if override:
            return djinn_net_addr.validate_static(subnet, override, ENV_NEWT_IP)
        return str(
            djinn_net_addr.top_address(subnet, NEWT_ADDRESS_OFFSET, ENV_NEWT_IP)
        )
    except ValueError as exc:
        raise NewtConfigError(str(exc)) from exc


def resolve_image(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    image = (env.get(ENV_NEWT_IMAGE) or "").strip() or DEFAULT_NEWT_IMAGE
    if ":" not in image.rsplit("/", 1)[-1]:
        raise NewtConfigError(
            f"{ENV_NEWT_IMAGE} '{image}' has no tag — pin a version, never :latest"
        )
    if image.rsplit(":", 1)[-1] == "latest":
        raise NewtConfigError(
            f"{ENV_NEWT_IMAGE} '{image}' is pinned to :latest — pin a version"
        )
    return image


def resolve_secrets(env: dict[str, str] | None = None) -> dict[str, str]:
    """The three Pangolin credentials, all required."""
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
        raise NewtConfigError(
            f"missing from secrets.env: {', '.join(missing)} — create a site in "
            f"the Pangolin admin UI and copy its values"
        )
    return out


def _scalar(value: str) -> str:
    """YAML-safe double-quoted scalar, with compose's ${VAR} interpolation
    neutralised ($$ is its literal-dollar escape)."""
    return json.dumps(value).replace("$", "$$")


def render_compose_yaml(
    *,
    identity: NewtIdentity,
    newt_ip: str,
    image: str,
    secrets: dict[str, str],
) -> str:
    """Render the singleton newt compose overlay written under DJINN_HOME.

    No `ports:` — Newt dials out. No `cap_add`/`devices` — it is userspace.
    """
    for name in SECRET_VARS:
        if name not in secrets:
            raise NewtConfigError(f"render_compose_yaml missing secret {name}")

    env_lines = "\n".join(
        f"      {name}: {_scalar(secrets[name])}" for name in SECRET_VARS
    )
    return f"""# Generated by brassbottle newt — do not hand-edit.
services:
  {SERVICE_NAME}:
    image: {image}
    container_name: {identity.container_name}
    hostname: {identity.hostname}
    restart: unless-stopped
    environment:
{env_lines}
    networks:
      default:
        ipv4_address: {newt_ip}

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
    content = render_compose_yaml(
        identity=derive_identity(base_path),
        newt_ip=resolve_newt_ip(subnet=subnet),
        image=resolve_image(),
        secrets=secrets,
    )
    compose_path = p["compose_file"]
    try:
        compose_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise NewtConfigError(f"cannot write compose overlay {compose_path}: {exc}") from exc
    return compose_path
