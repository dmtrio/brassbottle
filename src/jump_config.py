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


def _split_leading_options(line: str) -> tuple[str, str]:
    """Split an authorized_keys line into (options, remainder).

    OpenSSH's format is `[options] keytype base64 [comment]`, where the
    options field contains no UNQUOTED whitespace but may contain quoted
    values that do — `command="ssh-ed25519 nope",restrict ssh-ed25519 AAAA...`
    is a legal line whose quoted value looks exactly like a key type. So the
    split has to be quote-aware; a plain .split() would find the decoy and
    validate the wrong token.

    Returns ("", line) when the line has no options — decided by looking at
    the first token, since an options field is only distinguishable from a
    key type by not being one.
    """
    head = line.split(None, 1)[0] if line.split() else ""
    if head in KEY_TYPE_PREFIXES:
        return "", line

    in_quotes = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch.isspace() and not in_quotes:
            return line[:i], line[i:].lstrip()
    return "", line


def parse_authorized_keys(text: str, *, source: str) -> list[str]:
    """Split authorized_keys content into validated key lines.

    Blank lines and # comments are dropped (standard authorized_keys), CR is
    stripped so a file edited on Windows or pasted through a phone does not
    produce a key sshd silently rejects.

    OpenSSH options (`restrict`, `from="..."`, `command="..."`) are accepted
    and passed through untouched — the docs promise standard authorized_keys
    input, and refusing them would break migrating an already-hardened key
    list. They are deliberately NOT validated here: the file is copied to the
    container verbatim, so sshd remains the authority on what the options
    mean. This parser is a pre-flight check and the source of the change
    digest, not the thing that decides who gets in.

    What IS checked is the key type, because an unrecognised one is silently
    ignored by sshd — a phone that cannot log in with nothing to read. Better
    a named error with a line number.
    """
    keys: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue

        options, remainder = _split_leading_options(line)
        parts = remainder.split()
        if not parts or parts[0] not in KEY_TYPE_PREFIXES:
            found = parts[0] if parts else options
            # Name the options we skipped: a typo'd key type is parsed AS an
            # options field, so "unrecognised key type 'AAAA'" alone would
            # point at the wrong token on the line the operator has to fix.
            hint = f" (after options {options!r})" if options else ""
            raise JumpConfigError(
                f"{source} line {lineno}: unrecognised key type "
                f"{found!r}{hint} — expected one of "
                f"{', '.join(KEY_TYPE_PREFIXES)}"
            )
        if len(parts) < 2:
            raise JumpConfigError(
                f"{source} line {lineno}: key type with no key material"
            )
        keys.append(line)
    return keys


def read_authorized_keys(base_path: Path) -> list[str] | None:
    """Validated keys from DJINN_HOME/jump/authorized_keys.

    None means the file does not exist (never configured); [] means it exists
    but holds no keys. The distinction is load-bearing for revocation — see
    resolve_authorized_keys.
    """
    path = paths(base_path)["authorized_keys"]
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JumpConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise JumpConfigError(f"{path} is not valid UTF-8: {exc}") from exc
    return parse_authorized_keys(text, source=str(path))


def resolve_authorized_keys(
    base_path: Path, env: dict[str, str] | None = None
) -> tuple[list[str], str]:
    """The keys to authorise, and where they came from ("file" | "env").

    Precedence is one-directional and keyed on the file's EXISTENCE, not on
    whether it holds keys. Once the file exists it is the only authority;
    SSH_AUTHORIZED_KEY is a compatibility seed for installations that predate
    it and stops mattering thereafter.

    Emptying the file therefore authorises NOBODY and is an error, never a
    fallback. Falling back on an empty file would make clearing the file —
    the obvious way to revoke every device — silently restore the legacy key
    from secrets.env and re-authorise it, which is the opposite of what the
    operator just asked for.
    """
    env = os.environ if env is None else env
    path = paths(base_path)["authorized_keys"]
    keys = read_authorized_keys(base_path)

    if keys is not None:
        if keys:
            return keys, "file"
        raise JumpConfigError(
            f"{path} exists but contains no keys — sshd would accept nobody. "
            f"Add a key, or delete the file to fall back to "
            f"{ENV_AUTHORIZED_KEY}."
        )

    legacy = (env.get(ENV_AUTHORIZED_KEY) or "").strip()
    if legacy:
        return parse_authorized_keys(legacy, source=ENV_AUTHORIZED_KEY), "env"

    raise JumpConfigError(
        f"no authorised keys — add one public key per line to {path} "
        f"(or set {ENV_AUTHORIZED_KEY} in secrets.env to seed it)"
    )


def authorized_keys_digest(keys: list[str]) -> str:
    """Stable digest of the key SET, for compose change detection.

    Sorted, so reordering the file does not recreate the container; the digest
    changes only when the authorised set actually changes.
    """
    body = "\n".join(sorted(keys))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def seed_authorized_keys(base_path: Path, keys: list[str]) -> Path:
    """Create the key file from the legacy env value. SEEDING ONLY.

    Never called when the file is already the source: it writes the PARSED
    keys, which would strip the comments and blank lines an operator put there
    (docs/remote.md advertises `# mac` / `# phone` labels, and those labels are
    the only thing distinguishing which device to revoke later).

    Docker creates a DIRECTORY at a bind-mount source that does not exist, so
    the file must be on disk before compose runs either way — but for an
    operator-owned file that is guaranteed by its existence, not by rewriting.
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
    keys_digest: str,
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
    # unless-stopped service that is a silent crash loop. This also catches a
    # hand-deleted file, which resolve_authorized_keys would not see.
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
    labels:
      # The authorised key SET, hashed. Load-bearing, not decoration: the key
      # list is mounted rather than embedded, so without this the overlay is
      # byte-identical after an operator adds a key, `compose up -d` sees an
      # unchanged config and leaves the old container running, and the new
      # device cannot log in while the command reports success. Changing a
      # label changes the config hash, so compose recreates — and the
      # entrypoint re-copies the file — on the very next `./djinn jump start`.
      djinn.authorized_keys_sha256: "{keys_digest}"
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
    *,
    seed: bool = False,
) -> Path:
    """Ensure layout, optionally seed the key file, write the compose overlay.

    `seed` is True only when the keys came from the legacy SSH_AUTHORIZED_KEY
    and the file does not exist yet. When the file IS the source it is left
    exactly as the operator wrote it — comments and all.
    """
    # A bare str is the OLD signature (one key, not a list) and would be
    # accepted silently everywhere below — sorted() over its characters, and a
    # key file written one character per line. Cheap guard, unbounded damage.
    if isinstance(keys, str):
        raise JumpConfigError(
            "write_compose_file takes a list of keys, not a single string"
        )
    p = ensure_layout(base_path)
    identity = derive_identity(base_path)
    if seed:
        seed_authorized_keys(base_path, keys)
    content = render_compose_yaml(
        identity=identity,
        ssh_dir=p["ssh_dir"],
        authorized_keys_file=p["authorized_keys"],
        keys_digest=authorized_keys_digest(keys),
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
