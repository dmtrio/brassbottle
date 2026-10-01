#!/usr/bin/env python3
"""egress_policy.py — the versioned policy document the egress gateway pulls.

Owner of one thing: the policy document and its revision. The broker owns the
decisions; this module turns "what is allowed to leave, per bottle" into one
document (egress/contract/policy.schema.json) and a monotonic revision counter.

Inputs, all read here and nowhere else:
  * bottle manifests (bottles/<name>.yml) through the same yq -> manifest.py
    --derive chain up.sh uses: zones, CIDR grants, host ports, open relays;
    plus `remote.direct` (default true) read straight from the manifest;
  * $DJINN_HOME/run/bottle-nets.json (src/bottle_net.py): network, subnet,
    browser_port, ssh_port — empty (null) for a bottle with no allocation;
  * the request store: operator allows, as extra zones or `name:port` /
    `cidr:port` grants;
  * the denylist: persisted denies.

The revision is persisted in <egress root>/policy.json beside the last
document, so a restart never moves it backwards. It moves ONLY when the
document changes, whatever the cause; a recompute that yields an identical
document leaves it alone. `recompute` is the single place that decides, and it
holds one lock across build, compare, persist and bump, so two concurrent
causes can never lose each other's change or skip a revision.

A source that cannot be read (broken manifest, corrupt allocation file) keeps
that part of the previous document instead of dropping bottles from policy.

Every boundary logs one line: stage, duration, sizes. Never a secret.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import bottle_net

LOG = logging.getLogger(__name__)

POLICY_FILENAME = "policy.json"
POLICY_WAIT_SECONDS = 25.0
DERIVE_TIMEOUT_SECONDS = 60
WEB_PORTS = (80, 443)
TEMPLATE_STEM = "TEMPLATE"

Document = dict[str, Any]
# (bottle name, manifest path) -> {"derived": {...}, "remote_direct": bool}
DeriveFn = Callable[[str, Path], "Mapping[str, Any]"]


class PolicyError(Exception):
    """The persisted policy state is unusable."""


def _log(stage: str, started: float, **kv: Any) -> None:
    extra = "".join(f" {k}={v}" for k, v in kv.items())
    LOG.info(
        "egress policy %s duration_ms=%.1f%s",
        stage,
        (time.monotonic() - started) * 1000,
        extra,
    )


def _split_words(raw: str) -> list[str]:
    return [w for w in raw.replace(",", " ").split() if w]


def _split_target(item: str) -> tuple[str, list[int]]:
    """`name`, `name:port`, `cidr` or `cidr:port` -> (target, ports)."""
    target, sep, port = item.rpartition(":")
    if sep and port.isdigit() and 1 <= int(port) <= 65535:
        return target, [int(port)]
    return item, []


def _is_cidr_or_ip(value: str) -> bool:
    head = value.split("/", 1)[0]
    parts = head.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def default_derive(repo_root: Path) -> DeriveFn:
    """Derive one manifest the way up.sh does: yq -> manifest.py --derive."""

    def derive(name: str, manifest: Path) -> Mapping[str, Any]:
        def run(argv: list[str], stdin: str | None = None) -> str:
            result = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=DERIVE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
                raise PolicyError(f"{argv[0]} exit={result.returncode} {tail[0][:200]}")
            return result.stdout

        manifest_json = run(["yq", "-o=json", "-I=0", str(manifest)]).strip()
        lines = [manifest_json]
        for group, pattern in (("plugins", "plugin.yml"), ("agents", "agent.yml")):
            if group == "agents":
                lines.append("---agents---")
            for f in sorted((repo_root / group).glob(f"*/{pattern}")):
                try:
                    doc = run(["yq", "-o=json", "-I=0", str(f)]).strip()
                    if "\n" in doc:
                        doc = "!"
                except (PolicyError, OSError, subprocess.SubprocessError):
                    doc = "!"
                lines.append(f"{f.parent.name}\t{doc}")
        out = run(
            [sys.executable, str(repo_root / "src" / "manifest.py"), "--derive"],
            stdin="\n".join(lines) + "\n",
        )
        derived: dict[str, str] = {}
        # Values are shlex-quoted and may span lines, so tokenise the whole
        # output rather than splitting on newlines.
        for token in shlex.split(out):
            key, sep, value = token.partition("=")
            if sep and key.isidentifier():
                derived[key] = value
        parsed = json.loads(manifest_json) or {}
        remote = parsed.get("remote") if isinstance(parsed, dict) else None
        direct = remote.get("direct", True) if isinstance(remote, dict) else True
        return {"derived": derived, "remote_direct": direct is not False}

    return derive


class PolicyEngine:
    """Builds the policy document, owns its persisted revision, wakes waiters."""

    def __init__(
        self,
        root: Path,
        *,
        bottles_path: Path | None = None,
        home: Path | None = None,
        repo_root: Path | None = None,
        derive_fn: DeriveFn | None = None,
        store: Any = None,
        denylist: Any = None,
        wait_seconds: float = POLICY_WAIT_SECONDS,
    ) -> None:
        self._root = root
        self._path = root / POLICY_FILENAME
        self._bottles_path = bottles_path
        self._home = home
        self._derive = derive_fn or default_derive(
            repo_root or Path(__file__).resolve().parent.parent
        )
        self._store = store
        self._denylist = denylist
        self._wait_seconds = wait_seconds
        self._cond = threading.Condition()
        # Serialises recompute() so a slow derive never blocks readers, which
        # only need _cond for the instant the state is swapped.
        self._build_lock = threading.Lock()
        self._revision = 0
        self._bottles: dict[str, Any] = {}
        self._denies: list[dict[str, str]] = []
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        started = time.monotonic()
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            _log("load", started, status="absent", bytes_in=0)
            return
        try:
            data = json.loads(raw)
            revision = data["revision"]
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                raise ValueError("revision must be a non-negative integer")
            bottles = data["bottles"]
            denies = data["denylist"]
            if not isinstance(bottles, dict) or not isinstance(denies, list):
                raise ValueError("bottles/denylist have the wrong type")
        except (ValueError, KeyError, TypeError) as exc:
            _log("load", started, status="corrupt", bytes_in=len(raw))
            # Resetting would move the revision backwards; refuse to start.
            raise PolicyError(f"{self._path}: {exc}") from None
        self._revision, self._bottles, self._denies = revision, bottles, denies
        _log("load", started, status="ok", bytes_in=len(raw), revision=revision)

    def _persist(self) -> None:
        started = time.monotonic()
        payload = (json.dumps(self._document(), indent=2, sort_keys=True) + "\n").encode()
        tmp = self._path.with_name(self._path.name + f".{os.getpid()}.tmp")
        self._root.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
        _log("persist", started, bytes_out=len(payload), revision=self._revision)

    # -- the document ------------------------------------------------------

    def _document(self) -> Document:
        return {
            "revision": self._revision,
            "bottles": self._bottles,
            "denylist": self._denies,
        }

    def document(self) -> Document:
        with self._cond:
            return json.loads(json.dumps(self._document()))

    @property
    def revision(self) -> int:
        with self._cond:
            return self._revision

    # -- building ----------------------------------------------------------

    def _manifest_bottles(self) -> dict[str, Mapping[str, Any] | None]:
        """name -> derive result, or None when that manifest could not be read."""
        found: dict[str, Mapping[str, Any] | None] = {}
        if self._bottles_path is None or not self._bottles_path.is_dir():
            return found
        for manifest in sorted(self._bottles_path.glob("*.yml")):
            name = manifest.stem
            if name == TEMPLATE_STEM:
                continue
            started = time.monotonic()
            try:
                found[name] = self._derive(name, manifest)
                _log("derive", started, bottle=name, status="ok")
            except (PolicyError, OSError, ValueError, subprocess.SubprocessError) as exc:
                _log("derive", started, bottle=name, status="error", error=str(exc)[:200])
                found[name] = None
        return found

    def _allocations(self) -> dict[str, Any] | None:
        if self._home is None:
            return {}
        try:
            return dict(bottle_net.load(self._home))
        except (bottle_net.BottleNetError, OSError) as exc:
            LOG.info("egress policy allocations unreadable error=%s", str(exc)[:200])
            return None

    def _live_allows(self) -> dict[str, list[tuple[str, int]]]:
        allows: dict[str, list[tuple[str, int]]] = {}
        if self._store is None:
            return allows
        for row in self._store.list_allowed():
            allows.setdefault(row.container, []).append((row.host, row.port))
        return allows

    @staticmethod
    def _entry(
        name: str,
        derived: Mapping[str, str],
        remote_direct: bool,
        alloc: Mapping[str, Any] | None,
        allows: list[tuple[str, int]],
    ) -> dict[str, Any]:
        zones = set(_split_words(derived.get("EGRESS", "")))
        grants: dict[tuple[str, str], set[int]] = {}

        def grant(target: str, ports: list[int]) -> None:
            if not ports and not _is_cidr_or_ip(target):
                zones.add(target)
                return
            key = ("cidr" if _is_cidr_or_ip(target) else "name", target)
            grants.setdefault(key, set()).update(ports)

        for item in zones.copy():
            target, ports = _split_target(item)
            if ports:
                zones.discard(item)
                grant(target, ports)
        for item in _split_words(derived.get("EGRESS_CIDRS", "")):
            target, ports = _split_target(item)
            grant(target, ports)
        for host, port in allows:
            if not _is_cidr_or_ip(host):
                if port in WEB_PORTS:
                    zones.add(host)
                else:
                    grant(host, [port])
            else:
                grant(host if "/" in host else f"{host}/32", [port])
        alloc = alloc or {}
        return {
            "network": f"{bottle_net.NETWORK_PREFIX}{name}" if alloc else None,
            "subnet": alloc.get("subnet"),
            "zones": sorted(zones),
            "grants": [
                {kind: target, "ports": sorted(ports)}
                for (kind, target), ports in sorted(grants.items())
            ],
            "host_ports": sorted(
                int(p) for p in _split_words(derived.get("HOST_MCP_PORTS", "")) if p.isdigit()
            ),
            "browser_port": alloc.get("browser_port"),
            "ssh_port": alloc.get("ssh_port"),
            "remote_direct": remote_direct,
            "open_relays": sorted(_split_words(derived.get("OPEN_RELAYS", ""))),
        }

    def _build(self) -> tuple[dict[str, Any], list[dict[str, str]]]:
        manifests = self._manifest_bottles()
        allocations = self._allocations()
        allows = self._live_allows()
        bottles: dict[str, Any] = {}
        for name, result in manifests.items():
            previous = self._bottles.get(name)
            if result is None:
                if previous is not None:
                    bottles[name] = previous
                continue
            if allocations is None:
                alloc = {
                    "subnet": previous.get("subnet") if previous else None,
                    "browser_port": previous.get("browser_port") if previous else None,
                    "ssh_port": previous.get("ssh_port") if previous else None,
                }
                alloc = alloc if alloc["subnet"] else None
            else:
                alloc = allocations.get(name)
            bottles[name] = self._entry(
                name,
                result["derived"],
                bool(result["remote_direct"]),
                alloc,
                allows.get(name, []),
            )
        denies: list[dict[str, str]] = []
        if self._denylist is not None:
            denies = sorted(
                ({"zone": e.zone, "scope": e.scope} for e in self._denylist.load()),
                key=lambda d: (d["scope"], d["zone"]),
            )
        return bottles, denies

    # -- revision ----------------------------------------------------------

    def recompute(self, cause: str, *, caller: str = "broker") -> bool:
        """Rebuild the document; bump and persist the revision iff it changed.

        Returns whether the revision moved. The build lock is held across the
        whole build-compare-bump so concurrent causes serialise: the later one
        always sees the earlier one's change in its inputs and its baseline.
        """
        started = time.monotonic()
        with self._build_lock:
            bottles, denies = self._build()
            with self._cond:
                changed = bottles != self._bottles or denies != self._denies
                if changed:
                    previous = (self._bottles, self._denies, self._revision)
                    self._bottles, self._denies = bottles, denies
                    self._revision += 1
                    try:
                        self._persist()
                    except OSError:
                        # Memory must not run ahead of disk: a revision handed
                        # to the gateway but lost on restart would let it go
                        # backwards.
                        self._bottles, self._denies, self._revision = previous
                        raise
                    self._cond.notify_all()
                revision = self._revision
        _log(
            "recompute",
            started,
            caller=caller,
            cause=cause,
            moved=str(changed).lower(),
            revision=revision,
            bottles=len(bottles),
            denylist=len(denies),
        )
        return changed

    def wait(self, after: int | None) -> Document:
        """The document, held until its revision passes `after` (or timeout)."""
        started = time.monotonic()
        with self._cond:
            if after is not None:
                deadline = started + self._wait_seconds
                while self._revision <= after:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
            doc = json.loads(json.dumps(self._document()))
        _log(
            "serve",
            started,
            wait="none" if after is None else after,
            revision=doc["revision"],
            bytes_out=len(json.dumps(doc)),
        )
        return doc
