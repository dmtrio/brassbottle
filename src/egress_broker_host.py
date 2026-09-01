#!/usr/bin/env python3
"""egress_broker_host.py — singleton HOST-side daemon for egress approval.

Owns the work queue, notifies the operator, and is the only component that
shells out to bin/allow-egress.sh. Persistence goes through egress_log.EgressLog
as-is. Stdlib only; host-side (macOS and Linux).
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from egress_denylist import (
    DENYLIST_FILENAME,
    VALID_DECIDE_SCOPES,
    DenyEntry,
    DenyList,
    DenyListError,
    _repo_root,
    host_covered_by_zone,
    resolve_base_path,
    resolve_egress_root,
    validate_bottle_scope,
)
from egress_log import EgressLog, EgressLogError, _iso_ts, _utc_now
from egress_notify import (
    EgressNotification,
    NtfyNotifier,
    load_ntfy_settings,
    ntfy_server_hostname,
)

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 8816
DEFAULT_HOLD_SECONDS = 45
STALE_HOURS = 24
HIT_COALESCE_SECONDS = 60
STALE_SWEEP_INTERVAL_SECONDS = 300
DECIDE_REASON_MAX_CHARS = 200

TOKENS_DIRNAME = "tokens"
LOCK_FILENAME = "daemon.lock"
CONFIG_FILENAME = "config.json"
OPERATOR_TOKEN_FILENAME = "operator.token"
ENDPOINT_FILENAME = "daemon.json"
EGRESS_BROKER_URL_ENV = "EGRESS_BROKER_URL"
DAEMON_SKIP_NOTIFY_ENV = "DJINN_EGRESS_SKIP_NOTIFY"

IP_APPLY_FAILED_REASON = (
    "destination is an IP address; add it to the bottle manifest "
    "capabilities.egress_cidrs (ALLOWED_CIDRS) — allow-egress.sh accepts "
    "domain zones only"
)
IP_REQUIRES_CIDR_REASON = "ip_requires_cidr"
APPLY_FAILED_REASON = "apply_failed"
DENYLIST_PERSIST_FAILED_REASON = "denylist_persist_failed"

DOMAIN_RE = re.compile(
    r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$"
)
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8}$")


class EgressBrokerHostError(Exception):
    """Operator-facing egress broker error."""


class DaemonAlreadyRunning(EgressBrokerHostError):
    """Second singleton instance refused."""


@dataclass
class Decision:
    """Resolved approval outcome for a held long-poll.

    zone/reason="denylist" are set only when this deny is linked to a
    persisted denylist entry (a sibling EgressBroker.persist_deny() call
    wrote it and is sweeping this request closed as part of that write) —
    see _decision_body, which surfaces them to the still-connected client
    so it learns why, not just that. decide() itself never writes a
    denylist entry any more (see persist_deny) — it only records that one
    was written, when told to.
    """

    decision: str
    scope: str | None = None
    reason: str | None = None
    zone: str | None = None


@dataclass
class PersistDenyResult:
    """Outcome of EgressBroker.persist_deny() — used consistently by both
    the watcher (D/G keys) and the /decide HTTP handler so there is exactly
    ONE shape for "what happened when we tried to persist a deny"."""

    decided: list[str]
    entry: DenyEntry | None
    error: str | None


@dataclass(frozen=True)
class ZoneDecisionResult:
    """Outcome of deciding all currently-open requests for one zone."""

    decided: list[str]
    apply_failures: list[tuple[str, str]]


@dataclass
class _DenylistHitState:
    """Coalesce-window bookkeeping for one (container, matched zone) key hit
    by the denylist short-circuit (finding #8).

    Replaces a pair of dicts (_denylist_hit_last / _denylist_suppressed)
    that were always mutated in lockstep at every site that touched either
    — one object per key instead of two parallel ones keeping the same key
    space in sync by convention.
    """

    last: datetime
    suppressed: int = 0


def undeny_hint(zone: str, scope: str) -> str:
    """The `./djinn undeny ...` command that lifts a persisted deny entry.

    One implementation, two callers: egress_broker.py's HTTP 403 body (the
    container-side denial the requesting process sees) and egress_watch.py's
    operator acknowledgment line (format_denylist_ack) — both used to build
    this string independently and could drift.
    """
    if scope == "global":
        return f"./djinn undeny {zone} --global"
    return f"./djinn undeny {zone} --bottle {scope}"


@dataclass
class OpenRequestState:
    """In-memory state for one open egress approval request."""

    request_id: str
    container: str
    host: str
    port: int
    opened_at: datetime
    host_is_ip: bool = False
    pending_hits: int = 0
    last_hit_logged: datetime | None = None
    decision: Decision | None = None
    applying: bool = False
    waiter_outcome: Decision | None = None
    waiters: list[threading.Event] = field(default_factory=list)


NowFn = Callable[[], datetime]


def _strip_host_candidate(raw: str) -> str:
    """Strip wildcards, scheme, port, and path; lowercase."""
    value = raw.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if "/" in value:
        value = value.split("/", 1)[0]
    if value.startswith("[") and "]" in value:
        inner, _, rest = value.partition("]")
        candidate = inner[1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return candidate
        return candidate
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if value.count(":") == 1:
        host_part, port_part = value.rsplit(":", 1)
        if port_part.isdigit():
            value = host_part
    if value.startswith("*."):
        value = value[2:]
    return value


def is_ip_literal(host: str) -> bool:
    """True when host is a normalized IPv4/IPv6 literal."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalize_destination(raw: str) -> tuple[str, bool]:
    """Normalize a filing destination; return (host, is_ip_literal)."""
    value = _strip_host_candidate(raw)
    try:
        return str(ipaddress.ip_address(value)), True
    except ValueError:
        pass
    if not DOMAIN_RE.fullmatch(value):
        raise ValueError(f"not a valid domain name or IP address: {raw!r}")
    return value, False


def normalize_host(raw: str) -> str:
    """Strip wildcards, scheme, port, and path; lowercase; validate domain syntax."""
    host, is_ip = normalize_destination(raw)
    if is_ip:
        raise ValueError(f"not a valid domain name: {raw!r}")
    return host


def validate_request_id(request_id: str) -> bool:
    """Return True when request_id matches broker-generated ids (uuid4 hex[:8])."""
    return bool(REQUEST_ID_RE.fullmatch(request_id))


def _load_secret_token(token_path: Path, *, created_log: str) -> str:
    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 in the open(2) call rather than write-then-chmod: the
    # latter leaves the bearer token world-readable for the window between the
    # two syscalls. O_EXCL so a racing daemon cannot have us clobber its token.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    LOG.info(created_log)
    return token


def _load_bottle_token(token_path: Path) -> str:
    return _load_secret_token(
        token_path,
        created_log=f"egress broker bottle token created bottle={token_path.stem}",
    )


def ensure_operator_token(egress_root: Path) -> str:
    """Create or return the host-only operator bearer token (never in containers)."""
    token_path = egress_root / OPERATOR_TOKEN_FILENAME
    return _load_secret_token(
        token_path,
        created_log="egress broker operator token created",
    )


def ensure_bottle_token(base_path: Path, bottle: str) -> str:
    """Create or return the per-bottle bearer token (host-side only)."""
    token_path = resolve_egress_root(base_path) / TOKENS_DIRNAME / f"{bottle}.token"
    return _load_bottle_token(token_path)


class BottleTokenStore:
    """Map bearer tokens to bottle names; reload from disk on auth miss."""

    def __init__(self, tokens_dir: Path) -> None:
        self._tokens_dir = tokens_dir
        self._token_to_bottle: dict[str, str] = {}
        self._lock = threading.RLock()
        self._reload()

    def _reload(self) -> None:
        mapping: dict[str, str] = {}
        if self._tokens_dir.is_dir():
            for path in self._tokens_dir.glob("*.token"):
                bottle = path.stem
                try:
                    token = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if token:
                    mapping[token] = bottle
        with self._lock:
            self._token_to_bottle = mapping

    def resolve_bottle(self, provided: str) -> str | None:
        """Return the bottle for a bearer token, or None if unknown."""
        for attempt in range(2):
            with self._lock:
                items = list(self._token_to_bottle.items())
            matched: str | None = None
            for token, bottle in items:
                if hmac.compare_digest(provided, token):
                    matched = bottle
            if matched is not None:
                return matched
            if attempt == 0:
                self._reload()
        return None


def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.info("egress broker config unreadable path=%s", config_path.name)
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class DaemonEndpoint:
    """The daemon's actual bind address, as recorded in daemon.json."""

    host: str
    port: int
    pid: int


def write_daemon_endpoint(egress_root: Path, host: str, port: int) -> Path:
    """Persist the daemon's actual bind address after the HTTP server is
    constructed — the single source of truth every host-side CLI/script
    reads to find a daemon that bound to a non-default host/port (a VPN
    --host for ntfy, or --port 0). Atomic write (tmp + os.replace); mode
    0o644 — host/port/pid are not secrets, the operator token still guards
    the actual API.
    """
    egress_root.mkdir(parents=True, exist_ok=True)
    path = egress_root / ENDPOINT_FILENAME
    payload = {
        "version": 1,
        "host": host,
        "port": port,
        "pid": os.getpid(),
        "started_at": _iso_ts(None),
    }
    tmp_path = path.with_name(f".{ENDPOINT_FILENAME}.tmp-{os.getpid()}")
    text = json.dumps(payload, separators=(",", ":")) + "\n"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    LOG.info(
        "egress broker endpoint write path=%s host=%s port=%d",
        path,
        host,
        port,
    )
    return path


def remove_daemon_endpoint(egress_root: Path) -> None:
    """Remove daemon.json on clean shutdown. Tolerates it already being
    gone (a crash, or a second run_daemon() that never wrote one this
    session) — never raises."""
    path = egress_root / ENDPOINT_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        LOG.info("egress broker endpoint remove failed path=%s error=%s", path, exc)
        return
    LOG.info("egress broker endpoint remove path=%s", path)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe via signal 0. Never raises."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user but the pid slot is occupied — alive.
        return True
    except OSError:
        return False
    return True


def read_daemon_endpoint(egress_root: Path) -> DaemonEndpoint | None:
    """Read daemon.json; None (never raises) when missing, corrupt, the
    wrong shape, or the recorded pid is no longer alive (a daemon that
    crashed without cleaning up after itself)."""
    path = egress_root / ENDPOINT_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        LOG.warning("egress broker endpoint unreadable path=%s error=%s", path, exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOG.warning("egress broker endpoint unreadable path=%s error=%s", path, exc)
        return None
    if not isinstance(payload, dict):
        LOG.warning(
            "egress broker endpoint unreadable path=%s error=%s",
            path,
            "not a JSON object",
        )
        return None
    host = payload.get("host")
    port = payload.get("port")
    pid = payload.get("pid")
    # "" is a legitimate bind-all-interfaces host (see _connect_host_for_bind),
    # not a missing value — only reject when the key is absent/non-string.
    if not isinstance(host, str):
        LOG.warning(
            "egress broker endpoint unreadable path=%s error=%s", path, "missing/invalid host"
        )
        return None
    if not isinstance(port, int) or isinstance(port, bool):
        LOG.warning(
            "egress broker endpoint unreadable path=%s error=%s", path, "missing/invalid port"
        )
        return None
    if not isinstance(pid, int) or isinstance(pid, bool):
        LOG.warning(
            "egress broker endpoint unreadable path=%s error=%s", path, "missing/invalid pid"
        )
        return None
    if not _pid_alive(pid):
        LOG.info("egress broker endpoint stale pid=%d", pid)
        return None
    return DaemonEndpoint(host=host, port=port, pid=pid)


def address_family_for_host(bind_host: str) -> int:
    """The socket family EgressBrokerHTTPServer must bind with for this host.

    ThreadingHTTPServer hardcodes AF_INET, so an IPv6 bind host ("::",
    "::1", a link-local VPN literal) fails at bind() with "Address family
    for hostname not supported" — before daemon.json is ever written, which
    is why the connect-address mapping in _connect_host_for_bind was
    unreachable in practice. "" / "0.0.0.0" (all interfaces) stay IPv4;
    a name is resolved, preferring IPv4 (the historical behaviour) and
    falling back to IPv6 only when the name has no A record.
    """
    if bind_host in ("", "0.0.0.0"):
        return socket.AF_INET
    try:
        return (
            socket.AF_INET6
            if ipaddress.ip_address(bind_host).version == 6
            else socket.AF_INET
        )
    except ValueError:
        pass
    try:
        families = {info[0] for info in socket.getaddrinfo(bind_host, None)}
    except OSError:
        # Unresolvable: let bind() raise the real error rather than guessing.
        return socket.AF_INET
    return socket.AF_INET if socket.AF_INET in families else (
        socket.AF_INET6 if socket.AF_INET6 in families else socket.AF_INET
    )


def _connect_host_for_bind(bind_host: str) -> str:
    """Map a daemon's bind host to the address a client should connect to.

    0.0.0.0/"" (all interfaces) -> 127.0.0.1; "::" (all IPv6 interfaces) ->
    [::1]; any other IPv6 literal bracketed as-is; anything else (a
    hostname, or a specific IPv4/VPN literal like 10.8.0.5) used verbatim.
    """
    if bind_host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if bind_host == "::":
        return "[::1]"
    try:
        ipaddress.IPv6Address(bind_host)
    except ValueError:
        return bind_host
    return f"[{bind_host}]"


def daemon_base_url(egress_root: Path) -> str:
    """The address a host-side CLI/script should POST to reach the running
    daemon. EGRESS_BROKER_URL is the highest-precedence override (documented
    escape hatch) — checked before daemon.json is even read. Otherwise reads
    $egress_root/daemon.json (the single source of truth the daemon itself
    wrote after binding — see write_daemon_endpoint); falls back to
    http://127.0.0.1:{DEFAULT_PORT} when there is no live endpoint file.
    """
    env_override = os.environ.get(EGRESS_BROKER_URL_ENV, "").strip()
    if env_override:
        return env_override
    endpoint = read_daemon_endpoint(egress_root)
    if endpoint is not None:
        return f"http://{_connect_host_for_bind(endpoint.host)}:{endpoint.port}"
    return f"http://127.0.0.1:{DEFAULT_PORT}"


def _request_key(container: str, host: str, port: int) -> tuple[str, str, int]:
    return (container, host, port)


def _request_fields(
    container: str,
    host: str,
    port: int,
    *,
    host_is_ip: bool,
    uid: int | None,
    comm: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """The fields a "requested" audit entry carries — shared by a normal
    filing and a denylist short-circuit (finding #7: they must match)."""
    fields: dict[str, Any] = {"container": container, "host": host, "port": port}
    if host_is_ip:
        fields["host_is_ip"] = True
    if uid is not None:
        fields["uid"] = uid
    if comm is not None:
        fields["comm"] = comm
    if reason is not None:
        fields["reason"] = reason
    return fields


class EgressBroker:
    """Queue, long-poll, and approval executor for egress requests."""

    def __init__(
        self,
        root: Path,
        *,
        repo_root: Path | None = None,
        now_fn: NowFn | None = None,
        hold_seconds_default: int = DEFAULT_HOLD_SECONDS,
        notifier: Callable[[EgressNotification], object] | None = None,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._repo_root = (repo_root or _repo_root()).resolve()
        self._now_fn = now_fn or (lambda: _utc_now(None))
        self._hold_seconds_default = hold_seconds_default
        self._notifier = notifier
        self._log = EgressLog(self._root)
        self._denylist = DenyList(self._root / DENYLIST_FILENAME)
        self._lock = threading.RLock()
        self._requests: dict[str, OpenRequestState] = {}
        self._key_index: dict[tuple[str, str, int], str] = {}
        # Coalesce window for denylist short-circuit audit pairs, keyed by
        # (container, matched zone) — not by request id, since a denylist
        # hit never opens a held request. See _denylist_short_circuit.
        # finding #8: one dict of _DenylistHitState instead of two parallel
        # dicts (last-hit timestamp / suppressed count) mutated in lockstep
        # at every site that touched either.
        self._denylist_hits: dict[tuple[str, str], _DenylistHitState] = {}
        self._rebuild_from_log()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def denylist(self) -> DenyList:
        """The broker's own DenyList instance — same object matches()/
        persist_deny() consult, so a caller (the watcher's status line) sees
        exactly what the broker sees, not a separately-loaded copy."""
        return self._denylist

    def now(self) -> datetime:
        return self._utc_now(self._now_fn())

    def _utc_now(self, dt: datetime) -> datetime:
        return _utc_now(dt)

    def _allow_script(self) -> Path:
        return self._repo_root / "bin" / "allow-egress.sh"

    def _parse_ts(self, raw: str | None, fallback: datetime) -> datetime:
        if not isinstance(raw, str):
            return fallback
        try:
            if raw.endswith("Z"):
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                parsed = datetime.fromisoformat(raw)
            return _utc_now(parsed)
        except ValueError:
            return fallback

    def _rebuild_from_log(self) -> None:
        now = self.now()
        queue = self._log.fold_queue(now=now)
        self._requests.clear()
        self._key_index.clear()
        for request_id, open_req in queue.open_requests.items():
            container = open_req.container
            host = open_req.host
            port = open_req.port
            if not isinstance(container, str) or not isinstance(host, str):
                LOG.info(
                    "egress broker rebuild skip request_id=%s reason=missing_fields",
                    request_id,
                )
                continue
            if not isinstance(port, int):
                LOG.info(
                    "egress broker rebuild skip request_id=%s reason=missing_fields",
                    request_id,
                )
                continue
            opened_at = self._parse_ts(open_req.opened_at, now)
            host_is_ip = is_ip_literal(host)
            state = OpenRequestState(
                request_id=request_id,
                container=container,
                host=host,
                port=port,
                opened_at=opened_at,
                host_is_ip=host_is_ip,
            )
            self._requests[request_id] = state
            self._key_index[_request_key(container, host, port)] = request_id
        LOG.info(
            "egress broker rebuild exit open=%d indexed=%d",
            len(queue.open_requests),
            len(self._key_index),
        )

    def _notify_operator(
        self,
        request_id: str,
        now: datetime,
        *,
        container: str,
        host: str,
        port: int,
        host_is_ip: bool = False,
        uid: int | None = None,
        comm: str | None = None,
        reason: str | None = None,
    ) -> EgressNotification:
        LOG.info("egress broker notify dispatch request_id=%s", request_id)
        self._log.append("notified", request_id, ts=now)
        return EgressNotification(
            request_id=request_id,
            container=container,
            host=host,
            port=port,
            host_is_ip=host_is_ip,
            uid=uid,
            comm=comm,
            reason=reason,
        )

    def _dispatch_notifier(self, notification: EgressNotification) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(notification)
        except Exception as exc:
            LOG.warning(
                "egress broker notifier raised request_id=%s reason=%s",
                notification.request_id,
                exc.__class__.__name__,
            )

    def _log_hit(self, state: OpenRequestState, now: datetime, count: int) -> None:
        self._log.append("hit", state.request_id, ts=now, count=count)
        state.last_hit_logged = now

    def _record_hit(self, state: OpenRequestState) -> None:
        now = self.now()
        if state.last_hit_logged is None:
            self._log_hit(state, now, count=1)
            state.pending_hits = 0
            return

        state.pending_hits += 1
        elapsed = (now - state.last_hit_logged).total_seconds()
        if elapsed >= HIT_COALESCE_SECONDS:
            self._log_hit(state, now, count=state.pending_hits)
            state.pending_hits = 0

    def _flush_hits(self, state: OpenRequestState) -> None:
        if state.pending_hits <= 0:
            return
        self._log_hit(state, now=self.now(), count=state.pending_hits)
        state.pending_hits = 0

    def _wake_waiters(self, state: OpenRequestState, outcome: Decision) -> None:
        state.waiter_outcome = outcome
        for event in state.waiters:
            event.set()
        state.waiters.clear()

    def _remove_open(self, state: OpenRequestState) -> None:
        self._requests.pop(state.request_id, None)
        self._key_index.pop(_request_key(state.container, state.host, state.port), None)

    def _prune_denylist_hit_last(self, now: datetime) -> None:
        """Drop coalesce-window entries older than HIT_COALESCE_SECONDS.

        Called on every insert so the dict cannot grow without bound across
        the daemon's lifetime (one key per distinct (container, zone) ever
        hit) — a container hammering a stale rotating hostname would
        otherwise leak one entry per hostname forever. No `keep` exclusion
        needed: the caller always inserts/refreshes its own key's timestamp
        to `now` immediately before calling this, so that key can never be
        `< cutoff` in the same pass.

        An evicted key can still be carrying an unsurfaced suppressed count
        (hits that arrived inside its coalesce window after its one logged
        hit) — dropping that silently would contradict
        _denylist_short_circuit's own docstring, which promises suppressed
        hits are surfaced on the next logged hit for the SAME key. There is
        no "next logged hit" once the key is gone, so log it here instead of
        just discarding it.
        """
        cutoff = now - timedelta(seconds=HIT_COALESCE_SECONDS)
        stale = [k for k, hit in self._denylist_hits.items() if hit.last < cutoff]
        for k in stale:
            hit = self._denylist_hits.pop(k)
            if hit.suppressed > 0:
                container, zone = k
                LOG.info(
                    "egress broker denylist suppressed_evict container=%s zone=%s suppressed=%d",
                    container,
                    zone,
                    hit.suppressed,
                )

    def _denylist_short_circuit(
        self,
        container: str,
        host: str,
        port: int,
        now: datetime,
        request_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Deny immediately (no hold, no operator prompt) when host is denylisted.

        Called under self._lock, before a new OpenRequestState would be
        created — an already-open request (coalesce path) never reaches here,
        so a later denylist entry cannot retroactively short-circuit it.
        `request_id` is decided by the CALLER (client-supplied id if given,
        else one freshly minted id) BEFORE calling this, and is used for both
        the audit pair below and the id file_request returns — they must be
        the same id, not two different ones.

        `fields` is the SAME dict file_request builds for its own normal
        "requested" audit entry (container/host/port plus uid/comm/reason/
        host_is_ip when present) — finding #7: a short-circuited request is
        still a real filing, and the audit record for it must carry the
        same fields a normal one does, not a stripped-down container/host/
        port-only version.

        The audit trail is appended as a closed requested->denied pair,
        coalesced per (container, matched zone) — not per raw host, so many
        distinct subdomains hitting the same zone still coalesce together —
        to at most one pair per HIT_COALESCE_SECONDS, so a hot retry loop
        does not flood the log.
        """
        entry = self._denylist.matches(container, host)
        if entry is None:
            return None

        key = (container, entry.zone)
        hit = self._denylist_hits.get(key)
        should_log = hit is None or (now - hit.last).total_seconds() >= HIT_COALESCE_SECONDS
        if should_log:
            suppressed = hit.suppressed if hit is not None else 0
            self._denylist_hits[key] = _DenylistHitState(last=now)
            self._prune_denylist_hit_last(now)
            self._log.append("requested", request_id, ts=now, **fields)
            # Audit schema (finding #5): `via`/`zone`/`denylist_scope` name
            # the denylist entry that caused this — the same three keys the
            # persist_deny sweep closure below (_close_request's
            # denylist_zone/denylist_scope branch) writes, so a reader of
            # the audit log has ONE shape for "a denylist entry denied
            # this", not two. `scope` is deliberately NOT set here: on a
            # plain deny it means the request's own once/bottle/global
            # intent, which does not exist for an automatic short-circuit
            # (nobody called /decide for this one). `reason` carries the
            # entry's own operator free text (if any were given when it was
            # added) — never the literal string "denylist"; that literal
            # stays confined to the HTTP response body returned below,
            # which egress_broker.py/egress_request.py read on the
            # container side and which must not change shape.
            denied_fields: dict[str, Any] = {
                "via": "denylist",
                "zone": entry.zone,
                "denylist_scope": entry.scope,
            }
            if entry.reason is not None:
                denied_fields["reason"] = entry.reason
            self._log.append("denied", request_id, ts=now, **denied_fields)
            # LOG.info gated to the same coalesce window as the audit pair
            # above — at INFO on every hit this floods just as badly as the
            # audit log did (telemetry flushes every few seconds). Hits
            # suppressed in between are surfaced here as suppressed=N rather
            # than silently vanishing.
            LOG.info(
                "egress broker denylist short_circuit container=%s host=%s zone=%s scope=%s suppressed=%d",
                container,
                host,
                entry.zone,
                entry.scope,
                suppressed,
            )
        else:
            hit.suppressed += 1
        return {
            "decision": "deny",
            "reason": "denylist",
            "zone": entry.zone,
            "scope": entry.scope,
        }

    def _open_new_request(
        self,
        container: str,
        host: str,
        port: int,
        now: datetime,
        request_id: str,
        fields: dict[str, Any],
        *,
        host_is_ip: bool,
        uid: int | None,
        comm: str | None,
        reason: str | None,
    ) -> tuple[OpenRequestState | None, dict[str, Any] | None, EgressNotification | None]:
        """File a brand-new request — no coalesce match exists for it yet.

        Shared by file_request's two branches (client-supplied request_id
        vs. a freshly-minted one) — finding #7: both used to carry an
        identical copy of this block (denylist short-circuit check,
        OpenRequestState construction, "requested" audit append,
        _notify_operator call). Called under self._lock, same as both call
        sites were already doing.

        Returns (None, denylist_body, None) when the denylist
        short-circuited it — the caller returns denylist_body immediately,
        there is no state and nothing to notify. Otherwise returns (state,
        None, notification); the caller dispatches `notification` via
        self._dispatch_notifier OUTSIDE self._lock, exactly as before.
        """
        denylist_body = self._denylist_short_circuit(
            container, host, port, now, request_id, fields
        )
        if denylist_body is not None:
            return None, denylist_body, None

        state = OpenRequestState(
            request_id=request_id,
            container=container,
            host=host,
            port=port,
            opened_at=now,
            host_is_ip=host_is_ip,
        )
        self._log.append("requested", request_id, ts=now, **fields)
        notification = self._notify_operator(
            request_id,
            now,
            container=container,
            host=host,
            port=port,
            host_is_ip=host_is_ip,
            uid=uid,
            comm=comm,
            reason=reason,
        )
        self._requests[request_id] = state
        self._key_index[_request_key(container, host, port)] = request_id
        LOG.info(
            "egress broker request filed request_id=%s container=%s host=%s port=%d",
            request_id,
            container,
            host,
            port,
        )
        return state, None, notification

    def file_request(
        self,
        container: str,
        host: str,
        port: int,
        *,
        uid: int | None = None,
        comm: str | None = None,
        reason: str | None = None,
        hold_seconds: int | None = None,
        host_is_ip: bool = False,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """File or coalesce an egress request; return JSON body and request_id."""
        if request_id is not None and not validate_request_id(request_id):
            raise EgressBrokerHostError(
                f"invalid request_id {request_id!r} (expected 8 lowercase hex chars)"
            )

        key = _request_key(container, host, port)
        hold = hold_seconds if hold_seconds is not None else self._hold_seconds_default
        now = self.now()
        # Built once and reused for BOTH the normal "requested" audit entry
        # below and a denylist short-circuit's own "requested" entry
        # (finding #7: they must carry the same fields, not a stripped-down
        # container/host/port-only version for the short-circuit path).
        fields = _request_fields(
            container, host, port, host_is_ip=host_is_ip, uid=uid, comm=comm, reason=reason
        )
        pending_notification: EgressNotification | None = None

        with self._lock:
            if request_id is not None:
                existing_by_id = self._requests.get(request_id)
                if existing_by_id is not None:
                    self._record_hit(existing_by_id)
                    state = existing_by_id
                    request_id = existing_by_id.request_id
                    LOG.info(
                        "egress broker request coalesce request_id=%s container=%s host=%s port=%d",
                        request_id,
                        container,
                        host,
                        port,
                    )
                    if state.decision is not None:
                        body = self._decision_body(state.decision)
                        return body, request_id
                else:
                    existing_id = self._key_index.get(key)
                    if existing_id and existing_id in self._requests:
                        state = self._requests[existing_id]
                        self._record_hit(state)
                        request_id = existing_id
                        LOG.info(
                            "egress broker request coalesce request_id=%s container=%s host=%s port=%d",
                            request_id,
                            container,
                            host,
                            port,
                        )
                        if state.decision is not None:
                            body = self._decision_body(state.decision)
                            return body, request_id
                    else:
                        state, denylist_body, notification = self._open_new_request(
                            container,
                            host,
                            port,
                            now,
                            request_id,
                            fields,
                            host_is_ip=host_is_ip,
                            uid=uid,
                            comm=comm,
                            reason=reason,
                        )
                        if denylist_body is not None:
                            return denylist_body, request_id
                        pending_notification = notification
                        if state.decision is not None:
                            body = self._decision_body(state.decision)
                            return body, request_id
            else:
                existing_id = self._key_index.get(key)
                if existing_id and existing_id in self._requests:
                    state = self._requests[existing_id]
                    self._record_hit(state)
                    request_id = existing_id
                    LOG.info(
                        "egress broker request coalesce request_id=%s container=%s host=%s port=%d",
                        request_id,
                        container,
                        host,
                        port,
                    )
                else:
                    request_id = uuid4().hex
                    state, denylist_body, notification = self._open_new_request(
                        container,
                        host,
                        port,
                        now,
                        request_id,
                        fields,
                        host_is_ip=host_is_ip,
                        uid=uid,
                        comm=comm,
                        reason=reason,
                    )
                    if denylist_body is not None:
                        return denylist_body, request_id
                    pending_notification = notification

                if state.decision is not None:
                    body = self._decision_body(state.decision)
                    return body, request_id

        if pending_notification is not None:
            self._dispatch_notifier(pending_notification)

        decision = self._wait_for_decision(state, hold)
        if decision is None:
            return {"decision": "pending", "request_id": request_id}, request_id
        return self._decision_body(decision), request_id

    def _decision_body(self, decision: Decision) -> dict[str, Any]:
        if decision.decision == "allow":
            body: dict[str, Any] = {"decision": "allow", "scope": decision.scope or "live"}
            return body
        if decision.decision == "error":
            body = {"decision": "error", "reason": decision.reason or "error"}
            return body
        # A plain one-shot deny carries nothing but the verdict — same as
        # before. A deny linked to a denylist entry (this decide() call
        # persisted one, or a sibling persist_deny() call did and is
        # sweeping this request closed) additionally names why, so the
        # still-connected client sees the real cause instead of a generic
        # "denied by the operator" (matches the short-circuit body shape).
        body = {"decision": "deny"}
        if decision.reason is not None:
            body["reason"] = decision.reason
        if decision.zone is not None:
            body["zone"] = decision.zone
        if decision.scope is not None:
            body["scope"] = decision.scope
        return body

    def _wait_for_decision(self, state: OpenRequestState, hold_seconds: int) -> Decision | None:
        event = threading.Event()
        with self._lock:
            if state.decision is not None:
                return state.decision
            state.waiters.append(event)

        if not event.wait(timeout=hold_seconds):
            return None

        with self._lock:
            outcome = state.waiter_outcome
            state.waiter_outcome = None
            if outcome is not None:
                return outcome
            return state.decision

    def decide(
        self,
        request_id: str,
        decision: str,
        scope: str | None = None,
        *,
        reason: str | None = None,
    ) -> str | None:
        """Public approver-facing entry point — release held long-polls for
        one request (allow, or a plain one-shot deny).

        Thin wrapper over _close_request(): its signature deliberately
        cannot accept denylist_zone/denylist_scope/persist_failed — those
        are internal to persist_deny()'s own sweep (see _close_request's
        docstring) and must never be reachable from an operator surface.
        The watcher, the /decide HTTP handler, decide_allow_for_zone(), and
        decide_deny_for_zone() all go through THIS method, never
        _close_request() directly.
        """
        return self._close_request(request_id, decision, scope, reason=reason)

    def _close_request(
        self,
        request_id: str,
        decision: str,
        scope: str | None = None,
        *,
        reason: str | None = None,
        denylist_zone: str | None = None,
        denylist_scope: str | None = None,
        persist_failed: bool = False,
    ) -> str | None:
        """Release held long-polls for one request — the real implementation
        behind both decide() (the public wrapper above) and persist_deny()'s
        own sweep, which is the only caller that ever passes
        denylist_zone/denylist_scope/persist_failed.

        The deny path is a ONE-SHOT deny of THIS request only — it never
        writes to the denylist. `scope` on a deny is accepted purely for the
        audit/decision-body record of what was asked for; the only code path
        that ever calls DenyList.add() is EgressBroker.persist_deny(), which
        holds self._lock across the write (closing the reload/mutate race
        DenyList.matches()'s own _reload() could otherwise hit on a handler
        thread) and then sweeps every request it covers through repeated
        calls back into this method.

        Returns None when the decision is final (allow applied, or deny). On
        allow paths where no rule was installed, returns the error reason
        string and keeps the request open for retry; persist_failed=True
        (set only by persist_deny() when its own DenyList.add() raised)
        returns DENYLIST_PERSIST_FAILED_REASON in the same channel — the
        one-shot deny still completes rather than leaving the request stuck.

        denylist_zone/denylist_scope are INTERNAL — set only by
        EgressBroker.persist_deny() when it sweeps a request closed that an
        entry it JUST wrote now covers. They record the denylist context
        (via="denylist", zone, scope) on the audit event WITHOUT overwriting
        `reason`, which stays the operator's free-text explanation (or None)
        — the two are deliberately kept distinct: `reason` is for a human
        reading the audit log, while decision.reason="denylist" (surfaced to
        the still-held client via _decision_body) is the machine-readable
        cause. Do not pass denylist_zone/denylist_scope/persist_failed from
        an operator surface directly.
        """
        LOG.info(
            "egress broker decide enter request_id=%s decision=%s scope=%s",
            request_id,
            decision,
            scope or "",
        )
        allow_error: str | None = None
        resolved_scope: str | None = None
        apply_container: str | None = None
        apply_host: str | None = None
        run_apply_outside_lock = False

        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                raise EgressBrokerHostError(f"no open request for request_id={request_id}")
            if state.decision is not None:
                raise EgressBrokerHostError(
                    f"request_id={request_id} already decided"
                )
            if state.applying:
                LOG.info(
                    "egress broker decide noop request_id=%s reason=apply_in_progress",
                    request_id,
                )
                return None

            now = self.now()
            self._flush_hits(state)

            outcome: Decision | None = None

            if decision == "allow":
                resolved_scope = scope or "live"
                if resolved_scope not in ("live", "manifest"):
                    raise EgressBrokerHostError(
                        f"invalid scope {resolved_scope!r} (must be live or manifest)"
                    )
                if state.host_is_ip or is_ip_literal(state.host):
                    self._log.append(
                        "apply_failed",
                        request_id,
                        ts=now,
                        reason=IP_APPLY_FAILED_REASON,
                    )
                    allow_error = IP_REQUIRES_CIDR_REASON
                    outcome = Decision(decision="error", reason=IP_REQUIRES_CIDR_REASON)
                else:
                    # self._lock must not be held across subprocess/network I/O
                    # (allow-egress.sh can call back into this daemon).
                    state.applying = True
                    apply_container = state.container
                    apply_host = state.host
                    run_apply_outside_lock = True
            elif decision == "deny":
                resolved_scope = scope or "once"
                if resolved_scope not in VALID_DECIDE_SCOPES:
                    raise EgressBrokerHostError(
                        f"invalid scope {resolved_scope!r} (must be once, bottle, or global)"
                    )
                entry_zone: str | None = None
                entry_scope: str | None = None
                fields: dict[str, Any] = {"scope": resolved_scope}
                if reason is not None:
                    fields["reason"] = reason

                if denylist_zone is not None:
                    # Sweep closure from persist_deny(): a sibling call
                    # already wrote the entry under self._lock; this call
                    # only closes THIS request and tells its held connection
                    # why. `reason` above (if any) stays the operator's own
                    # free text — never overwritten with the literal string
                    # "denylist" — and the denylist context lives in its own
                    # fields instead, mirroring the short-circuit path's
                    # denied event.
                    #
                    # Audit schema (finding #5): `fields["scope"]` was being
                    # overwritten here with the denylist entry's scope (a
                    # bottle NAME, or "global") — losing whether the /decide
                    # call that triggered persist_deny() asked for
                    # scope=bottle vs scope=global (both collapse to
                    # indistinguishable values once scope=="global" also
                    # equals entry.scope=="global"). `scope` now keeps
                    # meaning request intent only (once|bottle|global,
                    # already set above from resolved_scope); the entry's
                    # own scope goes in the separate `denylist_scope` key,
                    # matching the short-circuit path's key set.
                    entry_zone, entry_scope = denylist_zone, denylist_scope
                    fields["via"] = "denylist"
                    fields["zone"] = entry_zone
                    fields["denylist_scope"] = (
                        entry_scope if entry_scope is not None else resolved_scope
                    )
                if persist_failed:
                    # persist_deny()'s own DenyList.add() raised: nothing was
                    # written, so this decide() call degrades to a plain
                    # one-shot deny of just the triggering request — but the
                    # audit still records that a persist was attempted and
                    # failed, so "Denied once; deny-list entry NOT written"
                    # (format_apply_failure) is provably true.
                    fields["scope"] = "once"
                    fields["persist_failed"] = True

                self._log.append("denied", request_id, ts=now, **fields)
                outcome = (
                    Decision(decision="deny", reason="denylist", zone=entry_zone, scope=entry_scope)
                    if entry_zone is not None
                    else Decision(decision="deny")
                )
                state.decision = outcome
                allow_error = DENYLIST_PERSIST_FAILED_REASON if persist_failed else None
            else:
                raise EgressBrokerHostError(
                    f"invalid decision {decision!r} (must be allow or deny)"
                )

            if not run_apply_outside_lock and outcome is not None:
                self._wake_waiters(state, outcome)
                if state.decision is not None:
                    self._remove_open(state)

        if run_apply_outside_lock:
            assert resolved_scope is not None
            assert apply_container is not None
            assert apply_host is not None
            apply_ok = self._apply_allow(apply_container, apply_host, resolved_scope)

            with self._lock:
                state = self._requests.get(request_id)
                if state is None or not state.applying:
                    LOG.info(
                        "egress broker decide stale request_id=%s after apply",
                        request_id,
                    )
                    return allow_error

                state.applying = False
                now = self.now()
                outcome = None

                if apply_ok:
                    self._log.append(
                        "allowed",
                        request_id,
                        ts=now,
                        scope=resolved_scope,
                        host=state.host,
                        container=state.container,
                    )
                    self._log.append("applied", request_id, ts=self.now())
                    outcome = Decision(decision="allow", scope=resolved_scope)
                    state.decision = outcome
                else:
                    self._log.append("apply_failed", request_id, ts=self.now())
                    allow_error = APPLY_FAILED_REASON
                    outcome = Decision(decision="error", reason=APPLY_FAILED_REASON)

                self._wake_waiters(state, outcome)
                if state.decision is not None:
                    self._remove_open(state)

        LOG.info(
            "egress broker decide exit request_id=%s decision=%s allow_error=%s",
            request_id,
            decision,
            allow_error or "",
        )
        return allow_error

    def _open_request_ids_for_zone(self, container: str | None, zone: str) -> list[str]:
        """Open, undecided request ids whose host falls under zone.

        container=None matches every container (persist_deny's global
        sweep); a concrete name restricts to that bottle's own requests.
        """
        with self._lock:
            return [
                state.request_id
                for state in self._requests.values()
                if state.decision is None
                and (container is None or state.container == container)
                and host_covered_by_zone(state.host, zone)
            ]

    def decide_allow_for_zone(
        self,
        container: str,
        domain: str,
        *,
        scope: str = "live",
    ) -> ZoneDecisionResult:
        """Release open requests whose host falls under domain (host-side only)."""
        zone, _is_ip = normalize_destination(domain)
        candidates = self._open_request_ids_for_zone(container, zone)
        decided: list[str] = []
        apply_failures: list[tuple[str, str]] = []
        for request_id in candidates:
            try:
                allow_error = self.decide(request_id, "allow", scope=scope)
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker decide_allow_for_zone skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            if allow_error is None:
                decided.append(request_id)
                continue
            apply_failures.append((request_id, allow_error))
        return ZoneDecisionResult(decided=decided, apply_failures=apply_failures)

    def decide_deny_for_zone(
        self,
        container: str,
        domain: str,
        *,
        reason: str | None = None,
    ) -> list[str]:
        """Deny (scope=once only) every open request whose host falls under
        domain, for this container (host-side only).

        A persistent deny (scope=bottle|global) is EgressBroker.persist_deny
        instead — it writes the caller-named zone regardless of whether any
        request is currently open, and sweeps every container it covers when
        global. This method never writes to the denylist.
        """
        zone, _is_ip = normalize_destination(domain)
        candidates = self._open_request_ids_for_zone(container, zone)
        decided: list[str] = []
        for request_id in candidates:
            try:
                self.decide(request_id, "deny", scope="once", reason=reason)
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker decide_deny_for_zone skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            decided.append(request_id)
        return decided

    def persist_deny(
        self,
        zone_raw: str,
        scope: str,
        *,
        container: str | None = None,
        reason: str | None = None,
        trigger_request_id: str | None = None,
    ) -> PersistDenyResult:
        """Persist a deny entry for the ZONE THE CALLER NAMED — not any one
        held request's exact host — then close every open request it now
        covers: every container when scope is global, just `container` when
        scope is bottle. THE ONLY place that writes a denylist entry — the
        entry point for both `/decide` deny with scope != once and the
        watcher's D/G keys.

        scope="bottle" requires `container`, and requires that bottle to
        already exist (a token at tokens/<container>.token) — a typo'd
        bottle name must never produce a dead, un-matchable entry. Raises
        EgressBrokerHostError for either violation (the /decide HTTP handler
        turns that into a 400); this is the only exception this method
        raises — a write failure is reported via PersistDenyResult.error,
        never raised.

        The write itself (DenyList.add) happens OUTSIDE self._lock: it takes
        DenyList's own flock (a SEPARATE, sibling-file lock — see
        DenyList._file_lock) across load->mutate->os.replace, and that can
        block for a while (a slow disk, or a concurrent `./djinn undeny` CLI
        process holding the same flock). Holding self._lock across that
        would stall every other handler thread's file_request/decide call —
        the broker-wide lock — for the same duration, which is exactly the
        stall this note used to describe as acceptable and is not. Once the
        write returns, this method takes self._lock just long enough to
        force self._denylist to reload from what was just written, so the
        next matches() call on any handler thread (always taken under
        self._lock too, via _denylist_short_circuit) sees the new entry
        rather than racing this method's post-write state update. Write
        still happens before any sweep below, unchanged.

        On a write failure (disk full/read-only, or a corrupt file refusing
        to be overwritten): nothing is swept (PersistDenyResult.entry is
        None), and if `trigger_request_id` names a still-open request, it is
        closed as a plain one-shot deny (persist_failed=True in the audit)
        so the held client is released and the watcher does not re-prompt
        it — mirrors decide()'s own never-raise-for-this-failure posture.

        On success, `trigger_request_id` needs no special handling: the
        request that triggered this call (if any) has the same host as
        `zone_raw` and so is naturally included in the sweep below.
        """
        if scope not in ("bottle", "global"):
            raise EgressBrokerHostError(
                f"invalid scope {scope!r} (must be bottle or global)"
            )
        zone, _is_ip = normalize_destination(zone_raw)
        if scope == "bottle":
            if not container:
                raise EgressBrokerHostError("scope=bottle requires container")
            try:
                validate_bottle_scope(container, self._root / TOKENS_DIRNAME)
            except DenyListError as exc:
                raise EgressBrokerHostError(str(exc)) from exc
            write_scope = container
        else:
            write_scope = "global"
        now = self.now()
        LOG.info(
            "egress broker persist_deny enter container=%s zone=%s scope=%s trigger_request_id=%s",
            container or "",
            zone,
            write_scope,
            trigger_request_id or "",
        )
        try:
            entry = self._denylist.add(
                zone=zone,
                scope=write_scope,
                reason=reason,
                by="operator",
                now=now,
            )
        except (OSError, DenyListError) as exc:
            LOG.info("egress denylist persist_failed reason=%s", exc)
            if trigger_request_id is not None:
                try:
                    # persist_failed=True is internal-only — goes through
                    # _close_request(), never the public decide() wrapper.
                    self._close_request(
                        trigger_request_id,
                        "deny",
                        scope="once",
                        reason=reason,
                        persist_failed=True,
                    )
                except EgressBrokerHostError as close_exc:
                    LOG.info(
                        "egress broker persist_deny_trigger skip request_id=%s reason=%s",
                        trigger_request_id,
                        close_exc,
                    )
            LOG.info(
                "egress broker persist_deny exit zone=%s scope=%s error=%s",
                zone,
                write_scope,
                DENYLIST_PERSIST_FAILED_REASON,
            )
            return PersistDenyResult(decided=[], entry=None, error=DENYLIST_PERSIST_FAILED_REASON)

        with self._lock:
            # Force a fresh reload of the SAME DenyList instance matches()
            # consults, under the same lock matches() is always called
            # under — see the docstring above.
            self._denylist.load()

        sweep_container = None if scope == "global" else container
        candidates = self._open_request_ids_for_zone(sweep_container, zone)
        decided: list[str] = []
        for request_id in candidates:
            try:
                # denylist_zone/denylist_scope are internal-only — goes
                # through _close_request(), never the public decide()
                # wrapper.
                self._close_request(
                    request_id,
                    "deny",
                    scope=scope,
                    reason=reason,
                    denylist_zone=entry.zone,
                    denylist_scope=entry.scope,
                )
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker persist_deny_sweep skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            decided.append(request_id)
        LOG.info(
            "egress broker persist_deny exit zone=%s scope=%s decided=%d",
            zone,
            write_scope,
            len(decided),
        )
        return PersistDenyResult(decided=decided, entry=entry, error=None)

    def _apply_allow(self, container: str, host: str, scope: str) -> bool:
        save_target = "yml" if scope == "manifest" else "none"
        cmd = [
            str(self._allow_script()),
            container,
            host,
            "--save",
            save_target,
        ]
        env = os.environ.copy()
        env[DAEMON_SKIP_NOTIFY_ENV] = "1"
        started = time.monotonic()
        LOG.info(
            "egress broker subprocess spawn argv_len=%d container=%s host=%s save=%s",
            len(cmd),
            container,
            host,
            save_target,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except OSError as exc:
            LOG.info(
                "egress broker subprocess error duration=%.2fs error=%s",
                time.monotonic() - started,
                exc,
            )
            return False
        LOG.info(
            "egress broker subprocess exit duration=%.2fs exit_code=%d",
            time.monotonic() - started,
            result.returncode,
        )
        return result.returncode == 0

    def sweep_stale(self) -> int:
        """Close unanswered requests older than STALE_HOURS as denied/stale."""
        cutoff = self.now() - timedelta(hours=STALE_HOURS)
        stale_ids: list[str] = []
        with self._lock:
            for request_id, state in self._requests.items():
                if state.opened_at <= cutoff:
                    stale_ids.append(request_id)

        closed = 0
        for request_id in stale_ids:
            try:
                self.decide(request_id, "deny", reason="stale")
                closed += 1
            except EgressBrokerHostError:
                continue
        if closed:
            LOG.info("egress broker stale sweep closed=%d", closed)
        return closed


class DaemonLock:
    """Hold an exclusive flock on daemon.lock for singleton enforcement."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DaemonAlreadyRunning(
                f"another egress broker instance is already running (lock: {self._lock_path})"
            )
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        finally:
            self._fd = None


class EgressBrokerHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying broker and auth token."""

    def __init__(
        self,
        server_address: tuple[str, int],
        broker: EgressBroker,
        token_store: BottleTokenStore,
        operator_token: str,
    ) -> None:
        self.broker = broker
        self.token_store = token_store
        self.operator_token = operator_token
        # Instance attribute, set BEFORE super().__init__ — socketserver reads
        # self.address_family when it creates the socket.
        self.address_family = address_family_for_host(server_address[0])
        LOG.info(
            "egress broker bind host=%s port=%d family=%s",
            server_address[0],
            server_address[1],
            "AF_INET6" if self.address_family == socket.AF_INET6 else "AF_INET",
        )
        super().__init__(server_address, EgressBrokerRequestHandler)


class EgressBrokerRequestHandler(BaseHTTPRequestHandler):
    """Threaded HTTP handler for egress approval long-poll."""

    server: EgressBrokerHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("egress broker http %s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        LOG.info(
            "egress broker response status=%d bytes=%d",
            status,
            len(payload),
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _resolve_bottle_from_auth(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        provided = header[7:].strip()
        if not provided:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        bottle = self.server.token_store.resolve_bottle(provided)
        if bottle is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        return bottle

    def _resolve_operator_auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        provided = header[7:].strip()
        if not provided:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        if not hmac.compare_digest(provided, self.server.operator_token):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/decide":
            self._handle_decide_post()
            return
        if self.path != "/egress":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._handle_egress_post()

    def _handle_decide_post(self) -> None:
        if not self._resolve_operator_auth():
            return

        length = int(self.headers.get("Content-Length", "0"))
        LOG.info("egress broker request enter path=/decide bytes=%d", length)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return

        host_raw = payload.get("host")
        if not isinstance(host_raw, str) or not host_raw:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "host is required"})
            return

        decision = payload.get("decision")
        if decision not in ("allow", "deny"):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "decision must be allow or deny"},
            )
            return

        scope: str
        reason: str | None = None

        if decision == "allow":
            if "reason" in payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "reason only applies to deny"},
                )
                return
            scope = payload.get("scope", "live")
            if scope not in ("live", "manifest"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid scope"})
                return
        else:
            scope = payload.get("scope", "once")
            # isinstance check FIRST (finding #3): VALID_DECIDE_SCOPES is a
            # frozenset, and `x not in frozenset` hashes x — an unhashable
            # payload["scope"] (a list or dict, both valid JSON) raises
            # TypeError instead of a 400, which escapes this handler thread
            # with no response sent at all. The "allow" branch above is safe
            # as-is: `in` against a tuple does equality comparisons, never a
            # hash lookup.
            if not isinstance(scope, str) or scope not in VALID_DECIDE_SCOPES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid scope"})
                return
            # Presence, not value: JSON null is a supplied (invalid) reason,
            # not an omitted one.
            if "reason" in payload:
                raw_reason = payload["reason"]
                if (
                    not isinstance(raw_reason, str)
                    or len(raw_reason) > DECIDE_REASON_MAX_CHARS
                ):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": (
                                "reason must be a string of at most 200 characters"
                            ),
                        },
                    )
                    return
                reason = raw_reason

        # container is required for everything EXCEPT a deny with
        # scope=global: persist_deny(scope="global") sweeps every container
        # itself and never needs one told to it. The CLI relies on this —
        # `./djinn deny <zone> --global` posts no `container` field at all.
        container_required = not (decision == "deny" and scope == "global")
        container = payload.get("container")
        if container_required:
            if not isinstance(container, str) or not container:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "container is required"})
                return
        else:
            if container is not None and not isinstance(container, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "container must be a string"},
                )
                return
            container = container or None

        try:
            if decision == "deny":
                # A persistent deny targets a zone that may be an IP literal
                # (./djinn deny 93.0.2.55 --global is valid — the denylist
                # has no CIDR concept but does exact-match IPs).
                normalize_destination(host_raw)
            else:
                # Allow requests may target IP literals too; these are surfaced
                # as apply_failures (ip_requires_cidr) rather than rejected
                # up front, so operator clients learn why nothing was installed.
                normalize_destination(host_raw)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid host"})
            return

        reason_len = len(reason) if reason is not None else 0
        LOG.info(
            "egress broker decide zone container=%s host=%s decision=%s scope=%s reason_len=%d",
            container,
            host_raw,
            decision,
            scope,
            reason_len,
        )

        try:
            if decision == "allow":
                result = self.server.broker.decide_allow_for_zone(
                    container,
                    host_raw,
                    scope=scope,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "decided": result.decided,
                        "apply_failures": [
                            {"request_id": request_id, "reason": reason}
                            for request_id, reason in result.apply_failures
                        ],
                    },
                )
                return
            if scope == "once":
                decided = self.server.broker.decide_deny_for_zone(
                    container,
                    host_raw,
                    reason=reason,
                )
                self._send_json(HTTPStatus.OK, {"decided": decided})
                return
            result = self.server.broker.persist_deny(
                host_raw,
                scope,
                container=container,
                reason=reason,
            )
        except EgressBrokerHostError as exc:
            LOG.info("egress broker decide error reason=%s", exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if result.error is not None:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": result.error})
            return
        assert result.entry is not None
        self._send_json(
            HTTPStatus.OK,
            {
                "decided": result.decided,
                "persisted": {"zone": result.entry.zone, "scope": result.entry.scope},
            },
        )

    def _handle_egress_post(self) -> None:
        container = self._resolve_bottle_from_auth()
        if container is None:
            return

        length = int(self.headers.get("Content-Length", "0"))
        LOG.info("egress broker request enter path=/egress bytes=%d", length)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return

        body_container = payload.get("container")
        if body_container is not None:
            if not isinstance(body_container, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "container must be a string"},
                )
                return
            if body_container != container:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return

        host_raw = payload.get("host")
        port = payload.get("port")
        if not isinstance(host_raw, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "host is required"})
            return
        if not isinstance(port, int):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "port must be an integer"})
            return

        try:
            host, host_is_ip = normalize_destination(host_raw)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid host"})
            return

        hold_seconds = payload.get("hold_seconds")
        if hold_seconds is not None and not isinstance(hold_seconds, int):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "hold_seconds must be an integer"},
            )
            return

        uid = payload.get("uid")
        if uid is not None and not isinstance(uid, int):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "uid must be an integer"})
            return
        comm = payload.get("comm")
        if comm is not None and not isinstance(comm, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "comm must be a string"})
            return
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "reason must be a string"})
            return

        client_request_id = payload.get("request_id")
        if client_request_id is not None:
            if not isinstance(client_request_id, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "request_id must be a string"},
                )
                return
            if not validate_request_id(client_request_id):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request_id"})
                return

        try:
            body, _request_id = self.server.broker.file_request(
                container,
                host,
                port,
                uid=uid,
                comm=comm,
                reason=reason,
                hold_seconds=hold_seconds,
                host_is_ip=host_is_ip,
                request_id=client_request_id,
            )
        except EgressLogError as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "log error"})
            return
        except EgressBrokerHostError as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.OK, body)


def _stale_sweep_loop(broker: EgressBroker, stop_event: threading.Event) -> None:
    while not stop_event.wait(STALE_SWEEP_INTERVAL_SECONDS):
        try:
            broker.sweep_stale()
        except Exception:
            LOG.exception("egress broker stale sweep failed")


def run_daemon(
    base_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    repo_root: Path | None = None,
) -> None:
    egress_root = resolve_egress_root(base_path)
    egress_root.mkdir(parents=True, exist_ok=True)

    tokens_dir = egress_root / TOKENS_DIRNAME
    tokens_dir.mkdir(parents=True, exist_ok=True)
    token_store = BottleTokenStore(tokens_dir)
    config = _load_config(egress_root / CONFIG_FILENAME)
    hold_default = config.get("hold_seconds", DEFAULT_HOLD_SECONDS)
    if not isinstance(hold_default, int):
        hold_default = DEFAULT_HOLD_SECONDS

    lock = DaemonLock(egress_root / LOCK_FILENAME)
    lock.acquire()

    operator_token = ensure_operator_token(egress_root)
    settings = load_ntfy_settings(
        base_path,
        os.environ,
        broker_host=host,
        broker_port=port,
        operator_token=operator_token,
    )
    notifier: Callable[[EgressNotification], object] | None = None
    if settings is not None:
        ntfy_notifier = NtfyNotifier(settings)
        notifier = ntfy_notifier.send_async
        LOG.info(
            "egress broker notify ntfy server=%s actions=%s",
            ntfy_server_hostname(settings.url),
            "on" if settings.broker_url else "off",
        )
    else:
        LOG.info("egress broker notify ntfy=off")

    broker = EgressBroker(
        egress_root,
        repo_root=repo_root,
        hold_seconds_default=hold_default,
        notifier=notifier,
    )
    server = EgressBrokerHTTPServer((host, port), broker, token_store, operator_token)
    # DaemonLock is already held above, so only one daemon ever writes this.
    write_daemon_endpoint(egress_root, host, server.server_address[1])
    stop_event = threading.Event()
    sweep_thread = threading.Thread(
        target=_stale_sweep_loop,
        args=(broker, stop_event),
        name="egress-stale-sweep",
        daemon=True,
    )
    sweep_thread.start()

    LOG.info("egress broker listen host=%s port=%d", host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        remove_daemon_endpoint(egress_root)
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="djinn egress approval broker (host)")
    parser.add_argument(
        "--base-path",
        default="",
        help="djinn home (defaults to DJINN_HOME or ./.djinn)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--ensure-bottle-token",
        metavar="BOTTLE",
        help="print (creating if needed) the per-bottle bearer token and exit",
    )
    parser.add_argument(
        "--print-endpoint",
        action="store_true",
        help=(
            "print the base URL a client should use to reach the running daemon "
            "(EGRESS_BROKER_URL env override, else the live daemon.json endpoint, "
            "else the default) and exit; exit 0 when that came from a live "
            "endpoint file or the env override, exit 3 when it is the default "
            "fallback (i.e. no daemon appears to be running)"
        ),
    )
    return parser


def _print_endpoint(egress_root: Path) -> int:
    url = daemon_base_url(egress_root)
    env_override = os.environ.get(EGRESS_BROKER_URL_ENV, "").strip()
    live = bool(env_override) or read_daemon_endpoint(egress_root) is not None
    print(url)
    return 0 if live else 3


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    if args.print_endpoint:
        return _print_endpoint(resolve_egress_root(base_path))
    if args.ensure_bottle_token:
        print(ensure_bottle_token(base_path, args.ensure_bottle_token))
        return 0
    try:
        run_daemon(base_path, host=args.host, port=args.port)
    except DaemonAlreadyRunning as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
