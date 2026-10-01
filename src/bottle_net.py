#!/usr/bin/env python3
"""bottle_net.py — per-bottle network allocation (subnet and host ports).

Owns $DJINN_HOME/run/bottle-nets.json:

    {"<bottle>": {"subnet": "172.31.0.0/28", "browser_port": 8900, "ssh_port": 2222}}

`ssh_port` is present only when the manifest sets ssh.port. This module makes
NO Docker calls. It decides and records; the caller (the bottle lifecycle)
gathers the facts to check against and creates the networks.

The rules, in one place:
  * Subnets are /28s taken first-fit, lowest address first, from
    DJINN_BOTTLE_POOL (default 172.31.0.0/16). Bottles are processed in sorted
    name order, so the same inputs always give the same answer.
  * browser_port is the lowest free port in DJINN_BROWSER_PROXY_PORTS
    (default 8900-8999). ssh_port is whatever the manifest declares — it is
    checked, never chosen.
  * An existing entry is never moved. Allocating again returns it unchanged
    and does not rewrite the file.
  * Anything the caller passes in as taken (Docker networks, fleet grants,
    plugin host ports, broker/admin ports, host listeners) is checked BEFORE
    the file is touched. A collision raises and leaves the file byte-identical.
  * A Docker network that overlaps the pool and is not one of ours
    (djinn-b-<bottle>) refuses the whole pool: the pool must be ours alone.
  * Writes are atomic (temp file + rename), and a fleet is written once.

Locking: the lifecycle holds `allocation_lock(home)` across its WHOLE
transaction (allocate, Docker operations, render). allocate_fleet and purge
require it to be held and raise otherwise. The lock is not re-entrant — a
second acquire in the same process would deadlock on flock, so it raises.

Every boundary logs one line to stderr: stage, duration, sizes. No secrets are
ever in scope here; only bottle names, subnets and port numbers are logged.

Stdlib only; host-side. Raises BottleNetError subclasses.
"""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set

ENV_HOME = "DJINN_HOME"
ENV_POOL = "DJINN_BOTTLE_POOL"
ENV_BROWSER_PORTS = "DJINN_BROWSER_PROXY_PORTS"

DEFAULT_POOL = "172.31.0.0/16"
DEFAULT_BROWSER_PORTS = "8900-8999"
BOTTLE_PREFIXLEN = 28
NETWORK_PREFIX = "djinn-b-"

ALLOC_FILENAME = "bottle-nets.json"
LOCK_FILENAME = "bottle-nets.lock"
DEFAULT_LOCK_TIMEOUT = 60.0
_LOCK_POLL = 0.05


class BottleNetError(Exception):
    """Base class for every refusal from this module."""


class ConfigError(BottleNetError):
    """Bad DJINN_BOTTLE_POOL, DJINN_BROWSER_PROXY_PORTS or DJINN_HOME."""


class CorruptFileError(BottleNetError):
    """bottle-nets.json is unreadable or malformed; it is left untouched."""


class LockError(BottleNetError):
    """Lock timeout, re-entry, or an operation attempted without the lock."""


class CollisionError(BottleNetError):
    """The request overlaps something the caller supplied or already allocated."""


class PoolConflictError(CollisionError):
    """A foreign Docker network overlaps the pool itself."""


class ExhaustedError(BottleNetError):
    """No free /28 or no free browser port is left."""


@dataclass(frozen=True)
class Taken:
    """What the caller found out about the host. Gathered by the caller.

    networks: {docker network name: CIDR} for every existing Docker network.
    grants: CIDRs (or single IPs) of fleet egress grants.
    plugin_ports, service_ports (broker/admin), listeners: host TCP ports.
    """

    networks: Mapping[str, str] = field(default_factory=dict)
    grants: Iterable[str] = ()
    plugin_ports: Iterable[int] = ()
    service_ports: Iterable[int] = ()
    listeners: Iterable[int] = ()


def _log(stage: str, started: float, **kv: Any) -> None:
    ms = int((time.monotonic() - started) * 1000)
    extra = "".join(f" {k}={v}" for k, v in kv.items())
    print(f"bottle_net: stage={stage} ms={ms}{extra}", file=sys.stderr)


# --- configuration ---------------------------------------------------------


def resolve_home(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get(ENV_HOME) or "").strip()
    if not raw:
        raise ConfigError(f"{ENV_HOME} is not set")
    return Path(os.path.expanduser(raw))


def resolve_pool(env: Optional[Mapping[str, str]] = None) -> ipaddress.IPv4Network:
    env = os.environ if env is None else env
    raw = (env.get(ENV_POOL) or "").strip() or DEFAULT_POOL
    try:
        pool = ipaddress.IPv4Network(raw, strict=True)
    except ValueError as exc:
        raise ConfigError(f"{ENV_POOL}={raw!r}: {exc}") from None
    if pool.prefixlen > BOTTLE_PREFIXLEN:
        raise ConfigError(
            f"{ENV_POOL}={raw!r} is smaller than one /{BOTTLE_PREFIXLEN} bottle subnet"
        )
    return pool


def resolve_browser_ports(env: Optional[Mapping[str, str]] = None) -> range:
    env = os.environ if env is None else env
    raw = (env.get(ENV_BROWSER_PORTS) or "").strip() or DEFAULT_BROWSER_PORTS
    lo_s, sep, hi_s = raw.partition("-")
    try:
        lo = int(lo_s)
        hi = int(hi_s) if sep else lo
    except ValueError:
        raise ConfigError(f"{ENV_BROWSER_PORTS}={raw!r}: want N or LOW-HIGH") from None
    if not (1 <= lo <= hi <= 65535):
        raise ConfigError(f"{ENV_BROWSER_PORTS}={raw!r}: want 1 <= LOW <= HIGH <= 65535")
    return range(lo, hi + 1)


def _paths(home: Path) -> "tuple[Path, Path]":
    run = Path(home) / "run"
    return run / ALLOC_FILENAME, run / LOCK_FILENAME


# --- lock ------------------------------------------------------------------

_HELD: Set[str] = set()


@contextlib.contextmanager
def allocation_lock(
    home: Path, timeout: float = DEFAULT_LOCK_TIMEOUT
) -> Iterator[None]:
    """Exclusive flock on <home>/run/bottle-nets.lock, held by the caller for a
    whole transaction. Not re-entrant."""
    _, lock_path = _paths(home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    if key in _HELD:
        raise LockError(f"allocation lock already held in this process: {lock_path}")
    started = time.monotonic()
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    _log("lock", started, status="timeout")
                    raise LockError(
                        f"timed out after {timeout}s waiting for {lock_path}"
                    ) from None
                time.sleep(_LOCK_POLL)
        _HELD.add(key)
        _log("lock", started, status="acquired")
        held_at = time.monotonic()
        try:
            yield
        finally:
            _HELD.discard(key)
            _log("unlock", held_at, status="released")
    finally:
        os.close(fd)  # closing drops the flock


def _require_lock(home: Path) -> None:
    _, lock_path = _paths(home)
    if str(lock_path.resolve()) not in _HELD:
        raise LockError("allocation_lock(home) must be held for this operation")


# --- file ------------------------------------------------------------------

Alloc = Dict[str, Dict[str, Any]]


def load(home: Path) -> Alloc:
    """Read and validate the allocation file; missing means empty."""
    started = time.monotonic()
    path, _ = _paths(home)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        _log("load", started, status="absent", bytes_in=0, entries=0)
        return {}
    try:
        data = json.loads(raw)
        _validate(data)
    except (ValueError, TypeError) as exc:
        _log("load", started, status="corrupt", bytes_in=len(raw))
        raise CorruptFileError(f"{path}: {exc}") from None
    _log("load", started, status="ok", bytes_in=len(raw), entries=len(data))
    return data


def _validate(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    for name, ent in data.items():
        if not isinstance(ent, dict):
            raise ValueError(f"{name}: entry must be an object")
        ipaddress.IPv4Network(ent.get("subnet"), strict=True)
        for key in ("browser_port", "ssh_port"):
            if key in ent and not (
                isinstance(ent[key], int)
                and not isinstance(ent[key], bool)
                and 1 <= ent[key] <= 65535
            ):
                raise ValueError(f"{name}: {key} must be a port number")
        if "browser_port" not in ent:
            raise ValueError(f"{name}: browser_port missing")
        extra = set(ent) - {"subnet", "browser_port", "ssh_port"}
        if extra:
            raise ValueError(f"{name}: unknown keys {sorted(extra)}")


def _encode(data: Alloc) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def _write_atomic(home: Path, data: Alloc) -> int:
    started = time.monotonic()
    path, _ = _paths(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _encode(data)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    _log("write", started, status="ok", bytes_out=len(payload), entries=len(data))
    return len(payload)


# --- collision inputs ------------------------------------------------------


def _net(value: str, what: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(value, strict=False)
    except ValueError as exc:
        raise ConfigError(f"{what} {value!r}: {exc}") from None


def _ports(values: Iterable[int], what: str) -> Set[int]:
    out: Set[int] = set()
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
            raise ConfigError(f"{what}: {v!r} is not a port number")
        out.add(v)
    return out


@dataclass
class _Checks:
    """Normalised Taken: foreign/own networks, grants, and ports by class."""

    nets: List["tuple[str, ipaddress.IPv4Network]"]
    grants: List[ipaddress.IPv4Network]
    port_classes: Dict[str, Set[int]]

    @classmethod
    def build(cls, taken: Taken) -> "_Checks":
        return cls(
            nets=[(n, _net(c, f"network {n}")) for n, c in sorted(taken.networks.items())],
            grants=[_net(g, "grant") for g in taken.grants],
            port_classes={
                "plugin port": _ports(taken.plugin_ports, "plugin_ports"),
                "broker/admin port": _ports(taken.service_ports, "service_ports"),
                "host listener": _ports(taken.listeners, "listeners"),
            },
        )

    def blocked_ports(self) -> Set[int]:
        out: Set[int] = set()
        for s in self.port_classes.values():
            out |= s
        return out

    def port_clash(self, port: int) -> Optional[str]:
        for label, s in self.port_classes.items():
            if port in s:
                return label
        return None

    def subnet_clash(
        self, subnet: ipaddress.IPv4Network, own_network: Optional[str] = None
    ) -> Optional[str]:
        for name, net in self.nets:
            if name != own_network and net.overlaps(subnet):
                return f"Docker network {name} ({net})"
        for g in self.grants:
            if g.overlaps(subnet):
                return f"fleet grant {g}"
        return None


def _check_pool(pool: ipaddress.IPv4Network, checks: _Checks) -> None:
    for name, net in checks.nets:
        if not name.startswith(NETWORK_PREFIX) and net.overlaps(pool):
            raise PoolConflictError(
                f"{ENV_POOL} {pool} overlaps Docker network {name} ({net}); "
                f"choose a pool that nothing else uses"
            )


# --- allocation ------------------------------------------------------------


def _first_free_subnet(
    pool: ipaddress.IPv4Network, used: List[ipaddress.IPv4Network], checks: _Checks
) -> ipaddress.IPv4Network:
    for cand in pool.subnets(new_prefix=BOTTLE_PREFIXLEN):
        if any(u.overlaps(cand) for u in used):
            continue
        if checks.subnet_clash(cand):
            continue
        return cand
    raise ExhaustedError(f"no free /{BOTTLE_PREFIXLEN} left in {pool}")


def allocate_fleet(
    home: Path,
    bottles: Mapping[str, Optional[int]],
    taken: Optional[Taken] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Alloc:
    """Ensure every bottle in `bottles` ({name: manifest ssh.port or None}) has
    an entry, and return the full allocation. Requires allocation_lock(home).

    All checks run on an in-memory copy; the file is written once, only if the
    result differs, and never when anything raises.
    """
    started = time.monotonic()
    _require_lock(home)
    pool = resolve_pool(env)
    browser_range = resolve_browser_ports(env)
    checks = _Checks.build(taken or Taken())
    _check_pool(pool, checks)

    current = load(home)
    work: Alloc = {k: dict(v) for k, v in current.items()}
    names = sorted(bottles)
    wanted_ssh = {p for p in bottles.values() if p is not None}

    # Phase 1: existing entries are kept as they are, but must still be clean.
    for name in names:
        ent = work.get(name)
        if ent is None:
            continue
        sub = ipaddress.IPv4Network(ent["subnet"])
        if not sub.subnet_of(pool) or sub.prefixlen != BOTTLE_PREFIXLEN:
            raise CollisionError(
                f"{name}: allocated {sub} is not a /{BOTTLE_PREFIXLEN} inside {pool}"
            )
        clash = checks.subnet_clash(sub, own_network=NETWORK_PREFIX + name)
        if clash:
            raise CollisionError(f"{name}: subnet {sub} overlaps {clash}")
        if not browser_range.start <= ent["browser_port"] < browser_range.stop:
            raise CollisionError(
                f"{name}: browser_port {ent['browser_port']} is outside "
                f"{ENV_BROWSER_PORTS}"
            )
        label = checks.port_clash(ent["browser_port"])
        if label:
            raise CollisionError(
                f"{name}: browser_port {ent['browser_port']} collides with a {label}"
            )

    # Phase 2: manifest ssh ports onto every requested entry (new or old).
    for name in names:
        ssh = bottles[name]
        if ssh is not None:
            _ports([ssh], f"{name} ssh.port")
            label = checks.port_clash(ssh)
            if label:
                raise CollisionError(f"{name}: ssh_port {ssh} collides with a {label}")
        if name in work:
            if ssh is None:
                work[name].pop("ssh_port", None)
            else:
                work[name]["ssh_port"] = ssh

    # Phase 3: new entries, first fit, in sorted order.
    for name in names:
        if name in work:
            continue
        used_subnets = [ipaddress.IPv4Network(e["subnet"]) for e in work.values()]
        used_ports = _claimed_ports(work)
        sub = _first_free_subnet(pool, used_subnets, checks)
        blocked = checks.blocked_ports() | used_ports | wanted_ssh
        ssh = bottles[name]
        port = next((p for p in browser_range if p not in blocked and p != ssh), None)
        if port is None:
            raise ExhaustedError(f"no free browser port left in {ENV_BROWSER_PORTS}")
        ent: Dict[str, Any] = {"subnet": str(sub), "browser_port": port}
        if ssh is not None:
            ent["ssh_port"] = ssh
        work[name] = ent

    _check_internal_ports(work)

    if _encode(work) == _encode(current):
        _log("allocate", started, status="unchanged", bottles=len(names), entries=len(work))
        return work
    _write_atomic(home, work)
    _log("allocate", started, status="written", bottles=len(names), entries=len(work))
    return work


def _claimed_ports(work: Alloc) -> Set[int]:
    out: Set[int] = set()
    for e in work.values():
        out.add(e["browser_port"])
        if "ssh_port" in e:
            out.add(e["ssh_port"])
    return out


def _check_internal_ports(work: Alloc) -> None:
    seen: Dict[int, str] = {}
    for name in sorted(work):
        e = work[name]
        for key in ("browser_port", "ssh_port"):
            if key not in e:
                continue
            port = e[key]
            if port in seen:
                raise CollisionError(
                    f"{name}: {key} {port} is already used by {seen[port]}"
                )
            seen[port] = f"{name}.{key}"


def purge(home: Path, bottle: str) -> bool:
    """Free `bottle`'s entry. Returns whether one existed. Requires the lock."""
    started = time.monotonic()
    _require_lock(home)
    current = load(home)
    if bottle not in current:
        _log("purge", started, status="absent", entries=len(current))
        return False
    work = {k: v for k, v in current.items() if k != bottle}
    _write_atomic(home, work)
    _log("purge", started, status="freed", entries=len(work))
    return True
