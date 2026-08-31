#!/usr/bin/env python3
"""jump_config.py — singleton mosh jump container: paths, identity, compose.

One jump container per djinn installation. It terminates the operator's
inbound mosh session and hops onward to bottles over the shared bridge, so
mosh leaves the bottle images entirely (PLN - Djinn Admin Plane, PR 1).

Why a container and not a host process: the jump terminates an INBOUND remote
path, and RFC 04's trust model puts that boundary at a container, never on the
operator's Mac. Why no published host ports: the tunnel already routes the
djinn-net CIDR, so the jump is reached at its bridge IP — which also sidesteps
the host-port exclusivity that forced per-bottle mosh ranges in the first
place (one range now serves the whole fleet).

Stdlib only; host-side (matches backup_config.py / ensure_net.py).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import djinn_net_addr  # noqa: E402

IDENTITY_PREFIX = "djinn-jump"
SERVICE_NAME = "jump"
IDENTITY_SUFFIX_LENGTH = 8

# Subnet/address rules live in djinn_net_addr (shared with newt_config).
DEFAULT_SUBNET = djinn_net_addr.DEFAULT_SUBNET
NETWORK_NAME = djinn_net_addr.NETWORK_NAME

# The jump takes the LAST usable address in the bridge subnet — offset 1 from
# the top. See djinn_net_addr.top_address for why the top and not the bottom.
JUMP_ADDRESS_OFFSET = 1

# mosh's UDP range INSIDE the container. Nothing is published to the host, so
# this range is not exclusive per-container the way the per-bottle ranges were
# — one range serves every session to every bottle.
DEFAULT_MOSH_PORTS = "60000:60010"

ENV_SUBNET = djinn_net_addr.ENV_SUBNET
ENV_JUMP_IP = "DJINN_JUMP_IP"
ENV_MOSH_PORTS = "DJINN_JUMP_MOSH_PORTS"

MOSH_PORTS_RE = re.compile(r"^(\d+):(\d+)$")

# In-container mount target for the persisted ssh material (host keys + the
# jump's own client keypair). Persisted so a recreate does not invalidate the
# public key the bottles have authorised.
SSH_MOUNT = "/etc/djinn-jump/ssh"


class JumpConfigError(Exception):
    """Invalid jump configuration."""


class JumpIdentity:
    """Stable Docker identity for one djinn installation's jump container."""

    def __init__(
        self,
        suffix: str,
        compose_project_name: str,
        container_name: str,
        hostname: str,
        image_tag: str,
    ) -> None:
        self.suffix = suffix
        self.compose_project_name = compose_project_name
        self.container_name = container_name
        self.hostname = hostname
        self.image_tag = image_tag

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, JumpIdentity):
            return NotImplemented
        return (
            self.suffix == other.suffix
            and self.compose_project_name == other.compose_project_name
            and self.container_name == other.container_name
            and self.hostname == other.hostname
            and self.image_tag == other.image_tag
        )


def identity_suffix(base_path: Path) -> str:
    """Short deterministic suffix from resolved DJINN_HOME (never the full path)."""
    resolved = str(base_path.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return digest[:IDENTITY_SUFFIX_LENGTH]


def derive_identity(base_path: Path) -> JumpIdentity:
    """Compose project/container/hostname/image names scoped to one djinn home."""
    suffix = identity_suffix(base_path)
    stem = f"{IDENTITY_PREFIX}-{suffix}"
    return JumpIdentity(
        suffix=suffix,
        compose_project_name=stem,
        container_name=stem,
        hostname=stem,
        image_tag=f"{IDENTITY_PREFIX}:{suffix}",
    )


def paths(base_path: Path) -> dict[str, Path]:
    """Resolve every host path the jump container uses."""
    base = base_path.expanduser().resolve()
    jump_root = base / "jump"
    return {
        "base": base,
        "jump_root": jump_root,
        "ssh_dir": jump_root / "ssh",
        "client_key": jump_root / "ssh" / "id_ed25519",
        "client_pubkey": jump_root / "ssh" / "id_ed25519.pub",
        "compose_dir": base / "compose",
        "compose_file": base / "compose" / "jump.yml",
    }


def ensure_layout(base_path: Path) -> dict[str, Path]:
    """Create the jump directories if missing. Key material is generated in
    the container's entrypoint, not here — the host never needs to hold it."""
    p = paths(base_path)
    for key in ("jump_root", "ssh_dir", "compose_dir"):
        try:
            p[key].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JumpConfigError(
                f"cannot create {key.replace('_', ' ')} {p[key]}: {exc}"
            ) from exc
    return p


def resolve_subnet(env: dict[str, str] | None = None) -> ipaddress.IPv4Network:
    """The bridge subnet, from DJINN_SUBNET — same source up.sh reads."""
    try:
        return djinn_net_addr.resolve_subnet(env)
    except ValueError as exc:
        raise JumpConfigError(str(exc)) from exc


def resolve_jump_ip(
    env: dict[str, str] | None = None,
    subnet: ipaddress.IPv4Network | None = None,
) -> str:
    """Static bridge address for the jump container.

    `subnet` is the network the address must actually belong to. Callers pass
    the LIVE djinn-net subnet (ensure_net.network_subnet) rather than letting
    this default to DJINN_SUBNET: ensure_net deliberately only *warns* when an
    existing bridge has a different subnet and still returns 0, so deriving
    from the desired value would compute an address on a network that does not
    exist and fail with an opaque IPAM error inside compose.
    """
    env = os.environ if env is None else env
    subnet = resolve_subnet(env) if subnet is None else subnet
    override = (env.get(ENV_JUMP_IP) or "").strip()
    try:
        if override:
            return djinn_net_addr.validate_static(
                subnet, override, ENV_JUMP_IP, own_offset=JUMP_ADDRESS_OFFSET
            )
        return str(
                djinn_net_addr.top_address(subnet, JUMP_ADDRESS_OFFSET, ENV_JUMP_IP)
            )
    except ValueError as exc:
        raise JumpConfigError(str(exc)) from exc


def resolve_mosh_ports(env: dict[str, str] | None = None) -> str:
    """In-container mosh UDP range, START:END."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_MOSH_PORTS) or "").strip() or DEFAULT_MOSH_PORTS
    m = MOSH_PORTS_RE.match(raw)
    if not m:
        raise JumpConfigError(f"invalid {ENV_MOSH_PORTS} '{raw}' (want START:END)")
    start, end = int(m.group(1)), int(m.group(2))
    if not (1 <= start <= 65535 and 1 <= end <= 65535):
        raise JumpConfigError(f"{ENV_MOSH_PORTS} '{raw}' out of range 1-65535")
    if start > end:
        raise JumpConfigError(f"{ENV_MOSH_PORTS} '{raw}': START must not exceed END")
    return raw


def render_compose_yaml(
    *,
    identity: JumpIdentity,
    ssh_dir: Path,
    jump_ip: str,
    mosh_ports: str,
    authorized_key: str,
) -> str:
    """Render the singleton jump compose overlay written under DJINN_HOME.

    No `ports:` block by design — see the module docstring. The container is
    reached at `jump_ip` over the operator's tunnel, and reaches bottles by
    container name via djinn-net's embedded DNS.
    """
    if not ssh_dir.exists():
        raise JumpConfigError(f"jump ssh dir does not exist: {ssh_dir}")
    if not authorized_key.strip():
        raise JumpConfigError(
            "SSH_AUTHORIZED_KEY is empty — set your public key in secrets.env"
        )
    if "\n" in authorized_key.strip():
        raise JumpConfigError("SSH_AUTHORIZED_KEY must be a single public key line")

    # json.dumps gives a correctly escaped double-quoted scalar (a `"` or `\\`
    # in the key's comment field would otherwise produce YAML compose cannot
    # parse). `$` is then doubled because docker compose interpolates ${VAR}
    # and $VAR in file contents — `$$` is its literal-dollar escape.
    key_scalar = json.dumps(authorized_key.strip()).replace("$", "$$")
    # Same treatment for the bind mount: a DJINN_HOME containing " #" would
    # truncate the scalar at a YAML comment and silently mount a SHORTER path
    # — the jump would then regenerate its host and client keys somewhere the
    # operator never sees, so every rebuild is a phone MITM warning and every
    # bottle's authorised key goes stale. ": " turns the item into a mapping.
    # Mirrors backup_config._volume_mount rather than interpolating raw.
    volume_scalar = json.dumps(f"{ssh_dir}:{SSH_MOUNT}").replace("$", "$$")

    return f"""# Generated by brassbottle jump — do not hand-edit.
services:
  {SERVICE_NAME}:
    build:
      context: .
      dockerfile: jump/Dockerfile
    image: {identity.image_tag}
    container_name: {identity.container_name}
    hostname: {identity.hostname}
    restart: unless-stopped
    environment:
      SSH_AUTHORIZED_KEY: {key_scalar}
      MOSH_PORTS: "{mosh_ports}"
    volumes:
      - {volume_scalar}
    networks:
      default:
        ipv4_address: {jump_ip}

networks:
  default:
    name: {NETWORK_NAME}
    external: true
"""


def write_compose_file(
    base_path: Path,
    authorized_key: str,
    subnet: ipaddress.IPv4Network | None = None,
) -> Path:
    """Ensure layout and write the generated compose overlay."""
    p = ensure_layout(base_path)
    identity = derive_identity(base_path)
    content = render_compose_yaml(
        identity=identity,
        ssh_dir=p["ssh_dir"],
        jump_ip=resolve_jump_ip(subnet=subnet),
        mosh_ports=resolve_mosh_ports(),
        authorized_key=authorized_key,
    )
    compose_path = p["compose_file"]
    try:
        compose_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise JumpConfigError(
            f"cannot write compose overlay {compose_path}: {exc}"
        ) from exc
    return compose_path
