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

# Subnet/address rules live in djinn_net_addr (shared with tunnel_config).
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

# In-container mount target for the operator's authorised public keys. A FILE
# bind mount, not a directory: the entrypoint copies it to ~/.ssh with a known
# owner and mode rather than pointing sshd's AuthorizedKeysFile at the mount,
# because sshd's StrictModes rejects a file owned by neither root nor the login
# user and a bind-mounted host file arrives with whatever uid the host maps.
AUTHORIZED_KEYS_MOUNT = "/etc/djinn-jump/authorized_keys"

# Public keys are not secrets, so they live in DJINN_HOME as a plain
# authorized_keys file rather than in secrets.env. That is what makes MULTIPLE
# keys practical: one per line, blanks and # comments allowed, no shell
# quoting to get wrong and no newline smuggled through a YAML scalar.
AUTHORIZED_KEYS_FILENAME = "authorized_keys"

ENV_AUTHORIZED_KEY = "SSH_AUTHORIZED_KEY"

# Key types OpenSSH will actually accept in an authorized_keys line. Checked
# host-side so a typo is a named error here instead of a silent auth failure
# on a phone with no way to see sshd's log.
KEY_TYPE_PREFIXES = (
    "ssh-rsa",
    "ssh-ed25519",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


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
        "authorized_keys": jump_root / AUTHORIZED_KEYS_FILENAME,
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


def parse_authorized_keys(text: str, *, source: str) -> list[str]:
    """Split authorized_keys content into validated key lines.

    Blank lines and # comments are dropped (standard authorized_keys), CR is
    stripped so a file edited on Windows or pasted through a phone does not
    produce a key sshd silently rejects. Every surviving line must start with
    a key type OpenSSH accepts — an unrecognised one is an error naming the
    line number, because the alternative is a phone that cannot log in and no
    visible reason why.
    """
    keys: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        # `options ssh-ed25519 AAAA...` is legal authorized_keys, but this file
        # is generated-adjacent operator input, not a general parser: accepting
        # options would mean validating them too. Require the key type first.
        if parts[0] not in KEY_TYPE_PREFIXES:
            raise JumpConfigError(
                f"{source} line {lineno}: unrecognised key type "
                f"{parts[0]!r} — expected one of {', '.join(KEY_TYPE_PREFIXES)}"
            )
        if len(parts) < 2:
            raise JumpConfigError(
                f"{source} line {lineno}: key type with no key material"
            )
        keys.append(line)
    return keys


def read_authorized_keys(base_path: Path) -> list[str]:
    """Validated keys from DJINN_HOME/jump/authorized_keys, [] if absent."""
    path = paths(base_path)["authorized_keys"]
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise JumpConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise JumpConfigError(f"{path} is not valid UTF-8: {exc}") from exc
    return parse_authorized_keys(text, source=str(path))


def resolve_authorized_keys(
    base_path: Path, env: dict[str, str] | None = None
) -> tuple[list[str], str]:
    """The keys to authorise, and where they came from ("file" | "env").

    Precedence is deliberate and one-directional: the FILE wins whenever it
    holds at least one key. SSH_AUTHORIZED_KEY is a compatibility seed for
    installations that predate the file — it populates the file once and then
    stops mattering. The alternative (env always wins, or a merge) would mean
    an operator editing the file sees no effect, or cannot remove a key that
    secrets.env keeps re-adding.
    """
    env = os.environ if env is None else env
    keys = read_authorized_keys(base_path)
    if keys:
        return keys, "file"

    legacy = (env.get(ENV_AUTHORIZED_KEY) or "").strip()
    if legacy:
        return parse_authorized_keys(legacy, source=ENV_AUTHORIZED_KEY), "env"

    path = paths(base_path)["authorized_keys"]
    raise JumpConfigError(
        f"no authorised keys — add one public key per line to {path} "
        f"(or set {ENV_AUTHORIZED_KEY} in secrets.env to seed it)"
    )


def write_authorized_keys(base_path: Path, keys: list[str]) -> Path:
    """Persist the key list, 0644, one per line.

    Always written before compose runs, even when the content is unchanged:
    docker creates a DIRECTORY at a bind-mount source that does not exist, and
    a directory mounted where the entrypoint expects a file is a crash loop
    whose cause is invisible from the phone.
    """
    p = ensure_layout(base_path)
    target = p["authorized_keys"]
    body = "".join(f"{key}\n" for key in keys)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(target)
    except OSError as exc:
        raise JumpConfigError(f"cannot write {target}: {exc}") from exc
    return target


def render_compose_yaml(
    *,
    identity: JumpIdentity,
    ssh_dir: Path,
    authorized_keys_file: Path,
    jump_ip: str,
    mosh_ports: str,
) -> str:
    """Render the singleton jump compose overlay written under DJINN_HOME.

    No `ports:` block by design — see the module docstring. The container is
    reached at `jump_ip` over the operator's tunnel, and reaches bottles by
    container name via djinn-net's embedded DNS.

    Key material is MOUNTED, never embedded: authorized_keys arrives as a
    read-only file bind mount, so multiple keys need no YAML scalar carrying
    newlines and no shell quoting in secrets.env.
    """
    if not ssh_dir.exists():
        raise JumpConfigError(f"jump ssh dir does not exist: {ssh_dir}")
    # Docker creates a DIRECTORY at a missing bind-mount source, which the
    # entrypoint would then fail to read as a file — on a restart:
    # unless-stopped service that is a silent crash loop. write_authorized_keys
    # runs first in the normal path; this catches a hand-deleted file.
    if not authorized_keys_file.is_file():
        raise JumpConfigError(
            f"jump authorized_keys is not a file: {authorized_keys_file}"
        )

    # json.dumps gives a correctly escaped double-quoted scalar (a `"` or `\\`
    # in a path would otherwise produce YAML compose cannot parse). `$` is then
    # doubled because docker compose interpolates ${VAR} and $VAR in file
    # contents — `$$` is its literal-dollar escape.
    #
    # A DJINN_HOME containing " #" would
    # truncate the scalar at a YAML comment and silently mount a SHORTER path
    # — the jump would then regenerate its host and client keys somewhere the
    # operator never sees, so every rebuild is a phone MITM warning and every
    # bottle's authorised key goes stale. ": " turns the item into a mapping.
    # Mirrors backup_config._volume_mount rather than interpolating raw.
    volume_scalar = json.dumps(f"{ssh_dir}:{SSH_MOUNT}").replace("$", "$$")
    # :ro — the container has no reason to write the operator's key list, and
    # a read-only mount means a compromised jump cannot authorise a new device.
    keys_scalar = json.dumps(
        f"{authorized_keys_file}:{AUTHORIZED_KEYS_MOUNT}:ro"
    ).replace("$", "$$")

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
      MOSH_PORTS: "{mosh_ports}"
    volumes:
      - {volume_scalar}
      - {keys_scalar}
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
    keys: list[str],
    subnet: ipaddress.IPv4Network | None = None,
) -> Path:
    """Ensure layout, persist the key list, and write the compose overlay.

    The key file is written HERE rather than by the caller so the bind-mount
    source is guaranteed to exist by the time compose reads the overlay.
    """
    p = ensure_layout(base_path)
    identity = derive_identity(base_path)
    keys_path = write_authorized_keys(base_path, keys)
    content = render_compose_yaml(
        identity=identity,
        ssh_dir=p["ssh_dir"],
        authorized_keys_file=keys_path,
        jump_ip=resolve_jump_ip(subnet=subnet),
        mosh_ports=resolve_mosh_ports(),
    )
    compose_path = p["compose_file"]
    try:
        compose_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise JumpConfigError(
            f"cannot write compose overlay {compose_path}: {exc}"
        ) from exc
    return compose_path
