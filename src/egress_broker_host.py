#!/usr/bin/env python3
"""egress_broker_host.py — singleton HOST-side daemon for egress approval.

The broker only listens: it owns the request store (egress_store.EgressStore
at <egress_root>/egress.db), notifies the operator, answers a filing at once
with a `pending` body, and is the only component that shells out to
bin/allow-egress.sh. No HTTP thread ever waits on an operator — the bottle
clients poll GET /egress/<request_id> for their own decision. Stdlib only;
host-side (macOS and Linux).
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
from egress_notify import (
    EgressNotification,
    NtfyNotifier,
    load_ntfy_settings,
    ntfy_server_hostname,
)
from egress_store import (
    OPEN_STATUS,
    EgressStore,
    EgressStoreError,
    RequestRow,
    _iso_ts,
    _utc_now,
)

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 8816
DEFAULT_HOLD_SECONDS = 90
STALE_HOURS = 24
DENYLIST_SUPPRESS_SECONDS = 60
STALE_SWEEP_INTERVAL_SECONDS = 300
DECIDE_REASON_MAX_CHARS = 200
RECENT_WINDOW_HOURS = 24
RECENT_PAGE_DEFAULT = 50
RECENT_PAGE_MAX = 200
_CURSOR_TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_CURSOR_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")

TOKENS_DIRNAME = "tokens"
LOCK_FILENAME = "daemon.lock"
CONFIG_FILENAME = "config.json"
OPERATOR_TOKEN_FILENAME = "operator.token"
ENDPOINT_FILENAME = "daemon.json"
EGRESS_BROKER_URL_ENV = "EGRESS_BROKER_URL"
EGRESS_ADMIN_URL_ENV = "EGRESS_ADMIN_URL"
EGRESS_ACTIONS_URL_ENV = "EGRESS_ACTIONS_URL"
CONTAINER_MARKER_ENV = "DJINN_CONTAINER"
DAEMON_SKIP_NOTIFY_ENV = "DJINN_EGRESS_SKIP_NOTIFY"

# daemon.json version 2: an endpoint the daemon does not own as a process —
# docker restarts it, so a pid would be wrong (and pid 1's namespace check
# cannot see across the boundary anyway). A managed endpoint is live only
# when its /health answers; a file left behind by an unclean stop reads as
# dead instead of as a running daemon.
MANAGED_DOCKER = "docker"
MANAGED_PROBE_TIMEOUT_SECONDS = 1.0

IP_APPLY_FAILED_REASON = (
    "destination is an IP address; add it to the bottle manifest "
    "capabilities.egress_cidrs (ALLOWED_CIDRS) — allow-egress.sh accepts "
    "domain zones only"
)
IP_REQUIRES_CIDR_REASON = "ip_requires_cidr"
APPLY_FAILED_REASON = "apply_failed"
# decide()/_close_request return value when a request is skipped because a
# concurrent allow is already applying it: NOT decided, NOT an apply
# failure — the in-flight apply resolves it. Sweeps must not count it.
APPLY_IN_PROGRESS_REASON = "apply_in_progress"
DENYLIST_PERSIST_FAILED_REASON = "denylist_persist_failed"

DOMAIN_RE = re.compile(
    r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$"
)
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8}$")


class EgressBrokerHostError(Exception):
    """Operator-facing egress broker error."""


class UnknownRequest(EgressBrokerHostError):
    """A request id that does not name an open row for the calling bottle.

    The /egress handler answers it with 404 {"error": "unknown request"}
    and records no hit — a replayed id from another bottle learns nothing
    about the row it named.
    """


class DaemonAlreadyRunning(EgressBrokerHostError):
    """Second singleton instance refused."""


@dataclass
class Decision:
    """Resolved approval outcome for one request.

    zone/reason="denylist" are set only when this deny is linked to a
    persisted denylist entry (a sibling EgressBroker.persist_deny() call
    wrote it and is sweeping this request closed as part of that write) —
    see _decision_body, which surfaces them to the polling client so it
    learns why, not just that. decide() itself never writes a denylist
    entry any more (see persist_deny) — it only records that one was
    written, when told to.
    """

    decision: str
    scope: str | None = None
    reason: str | None = None
    zone: str | None = None


@dataclass
class PersistDenyResult:
    """Outcome of EgressBroker.persist_deny() — used consistently by both
    the /decide HTTP handler and decide_deny_for_zone so there is exactly
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
    by the denylist short-circuit.

    One object per key instead of parallel dicts; `request_id` names the
    denied row a repeat within the window suppresses onto.
    """

    last: datetime
    request_id: str | None = None
    suppressed: int = 0


def undeny_hint(zone: str, scope: str) -> str:
    """The `./djinn undeny ...` command that lifts a persisted deny entry.

    One implementation, two callers: egress_broker.py's HTTP 403 body (the
    container-side denial the requesting process sees) and the admin UI —
    both would otherwise build this string independently and drift.
    """
    if scope == "global":
        return f"./djinn undeny {zone} --global"
    return f"./djinn undeny {zone} --bottle {scope}"


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
    """The daemon's actual bind address, as recorded in daemon.json.

    pid is the host-side (version 1) liveness handle; managed=True marks a
    docker-managed daemon (version 2) whose liveness is its /health probe,
    not a pid.
    """

    host: str
    port: int
    pid: int | None = None
    managed: bool = False


def write_daemon_endpoint(
    egress_root: Path, host: str, port: int, *, managed: str | None = None
) -> Path:
    """Persist the daemon's actual bind address after the HTTP server is
    constructed — the single source of truth every host-side CLI/script
    reads to find a daemon that bound to a non-default host/port (a VPN
    --host for ntfy, or --port 0). Atomic write (tmp + os.replace); mode
    0o644 — host/port/pid are not secrets, the operator token still guards
    the actual API.

    `managed` names an external supervisor ("docker"): the payload records
    version 2 with no pid — the container's pid namespace is unreachable
    from the host and docker restarts the daemon anyway — and liveness is
    answered by probing /health on the recorded address.
    """
    egress_root.mkdir(parents=True, exist_ok=True)
    path = egress_root / ENDPOINT_FILENAME
    payload: dict[str, Any] = {
        "version": 2 if managed else 1,
        "host": host,
        "port": port,
        "started_at": _iso_ts(None),
    }
    if managed:
        payload["managed"] = managed
    else:
        payload["pid"] = os.getpid()
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


def _managed_alive(host: str, port: int) -> bool:
    """Liveness of a docker-managed daemon: GET /health on the recorded
    address must return 200 within MANAGED_PROBE_TIMEOUT_SECONDS. Never
    raises — an unreachable socket, a refused connection or a timeout all
    read as "not live", because daemon.json left behind by an unclean stop
    must not report a running daemon."""
    try:
        url = f"http://{_connect_host_for_bind(host)}:{port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=MANAGED_PROBE_TIMEOUT_SECONDS) as resp:
            resp.read(1)
            return resp.status == 200
    except Exception:  # noqa: BLE001 — any failure is "unreachable"
        return False


def read_daemon_endpoint(egress_root: Path) -> DaemonEndpoint | None:
    """Read daemon.json; None (never raises) when missing, corrupt, the
    wrong shape, or not live (a version-1 pid that died without cleaning
    up, or a version-2 managed daemon whose /health does not answer)."""
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
    version = payload.get("version", 1)
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
    if version == 2:
        if payload.get("managed") != MANAGED_DOCKER:
            LOG.warning(
                "egress broker endpoint unreadable path=%s error=%s",
                path,
                "unknown managed kind",
            )
            return None
        if not _managed_alive(host, port):
            LOG.info("egress broker endpoint managed unreachable host=%s port=%d", host, port)
            return None
        return DaemonEndpoint(host=host, port=port, pid=None, managed=True)
    pid = payload.get("pid")
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


class RecentQueryError(ValueError):
    """A malformed /recent parameter; the message is the 400 body's `error`."""


def decided_row_json(row: RequestRow) -> dict[str, Any]:
    """One decided row as operator UIs see it.

    The single shaper for `queue_snapshot()["recent"]` and `/recent` pages, so
    the two shapes cannot drift (admin/contract/recent_page.schema.json).
    """
    return {
        "request_id": row.request_id,
        "container": row.container,
        "host": row.host,
        "port": row.port,
        "status": row.status,
        "scope": row.scope,
        "decided_at": _iso_ts(row.decided_at) if row.decided_at else None,
        "decided_by": row.decided_by,
        "apply_status": row.apply_status,
        "deny_reason": row.deny_reason,
    }


def parse_recent_cursor(raw: str) -> tuple[str, str]:
    """`<decided_at>,<request_id>` -> the store's keyset cursor."""
    ts, sep, request_id = raw.partition(",")
    if not sep or not _CURSOR_TS_RE.fullmatch(ts) or not _CURSOR_ID_RE.fullmatch(request_id):
        raise RecentQueryError("invalid before cursor: want <decided_at>,<request_id>")
    return ts, request_id


def _parse_recent_bound(name: str, raw: str) -> datetime:
    # Normalised to UTC here: an offset that pushes the instant past year 1 or
    # 9999 overflows in `astimezone`, which must be a 400 and not a dead handler.
    try:
        return _utc_now(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        raise RecentQueryError(f"invalid {name}: want an ISO 8601 timestamp") from None


def parse_recent_query(query: str) -> dict[str, Any]:
    """Validate a /recent query string into `EgressBroker.recent_page` kwargs.

    Unknown parameters are ignored. Raises RecentQueryError on a malformed
    `before`, `since`, `until` or `limit`; `limit` is clamped to
    1..RECENT_PAGE_MAX, and a blank parameter counts as absent.
    """
    params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items() if v and v[0] != ""}
    limit = RECENT_PAGE_DEFAULT
    if "limit" in params:
        try:
            limit = int(params["limit"])
        except ValueError:
            raise RecentQueryError("invalid limit: want an integer") from None
        limit = max(1, min(RECENT_PAGE_MAX, limit))
    return {
        "before": parse_recent_cursor(params["before"]) if "before" in params else None,
        "limit": limit,
        "container": params.get("container"),
        "since": _parse_recent_bound("since", params["since"]) if "since" in params else None,
        "until": _parse_recent_bound("until", params["until"]) if "until" in params else None,
    }


class EgressBroker:
    """Request store, instant filing, and approval executor for egress.

    Every read and write goes through the SQLite store; the daemon keeps no
    open-request state in memory beyond the denylist coalesce window and
    the set of request ids whose allow subprocess is in flight.
    """

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
        self._store = EgressStore(self._root)
        self._denylist = DenyList(self._root / DENYLIST_FILENAME)
        self._lock = threading.RLock()
        # Coalesce window for denylist short-circuits, keyed by
        # (container, matched zone) — not by request id, since a denylist
        # hit never opens a held request.
        self._denylist_hits: dict[tuple[str, str], _DenylistHitState] = {}
        # Request ids whose allow subprocess is in flight. decide() on one
        # of these is neither decided nor failed by the second caller —
        # the in-flight apply resolves it.
        self._applying: set[str] = set()
        self._import_open_once()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def denylist(self) -> DenyList:
        """The broker's own DenyList instance — same object matches()/
        persist_deny() consult, so a caller sees exactly what the broker
        sees, not a separately-loaded copy."""
        return self._denylist

    @property
    def store(self) -> EgressStore:
        return self._store

    def now(self) -> datetime:
        return _utc_now(self._now_fn())

    def _allow_script(self) -> Path:
        return self._repo_root / "bin" / "allow-egress.sh"

    # -- cutover import --------------------------------------------------

    def _import_open_once(self) -> None:
        """Carry still-open legacy requests across, exactly once.

        Runs only when the database has zero rows (first start after the
        store cutover); the count imported is logged and the legacy month
        files are never read again after that. A corrupt legacy file raises
        out of the constructor, exactly as the old fold did at startup.
        """
        if self._store.request_count() != 0:
            return
        imported = self._store.import_open(now=self.now())
        LOG.info("egress broker import exit imported=%d", imported)

    # -- filing ------------------------------------------------------------

    def _pending_body(self, row: RequestRow) -> dict[str, Any]:
        """The instant filing answer for an open row.

        `attempt` is the completed-apply counter the client takes as its
        baseline; `last_error` rides along when set so a client that files
        after a failed apply can tell the fresh baseline from the failure.
        """
        body: dict[str, Any] = {
            "decision": "pending",
            "request_id": row.request_id,
            "status": row.status,
            "poll": f"/egress/{row.request_id}",
            "attempt": row.apply_attempts,
        }
        if row.last_error is not None:
            body["last_error"] = row.last_error
        return body

    def _coalesce_row(
        self,
        container: str,
        host: str,
        port: int,
        request_id: str | None,
    ) -> RequestRow | None:
        """The row this filing coalesces onto, or None for a brand-new ask.

        A replayed id is honoured only when the row's (container, host,
        port) equals the filing's; anything else raises UnknownRequest and
        writes nothing.
        """
        if request_id is not None:
            row = self._store.get(request_id)
            if row is not None:
                if (row.container, row.host, row.port) != (container, host, port):
                    raise UnknownRequest("unknown request")
                return row
        return self._store.find_open(container, host, port)

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
        """File or coalesce an egress request; return JSON body and request_id.

        Never waits on an operator: a new or re-hit open request is
        answered with the `pending` body at once (the client polls
        /egress/<id> from there); a decided row answers with its stored
        per-request decision body. `hold_seconds` is recorded on the row
        and does not change server behaviour.
        """
        if request_id is not None and not validate_request_id(request_id):
            raise EgressBrokerHostError(
                f"invalid request_id {request_id!r} (expected 8 lowercase hex chars)"
            )
        now = self.now()
        hold = hold_seconds if hold_seconds is not None else self._hold_seconds_default
        notification: EgressNotification | None = None

        with self._lock:
            existing = self._coalesce_row(container, host, port, request_id)
            if existing is not None:
                if existing.status != OPEN_STATUS:
                    # Same id replayed after the row was decided: the stored
                    # per-request body is the answer; no hit is recorded.
                    return (
                        existing.decision_body or {"decision": "deny"},
                        existing.request_id,
                    )
                row, _is_new = self._store.open_or_hit(
                    request_id=existing.request_id,
                    container=container,
                    host=host,
                    port=port,
                    host_is_ip=host_is_ip,
                    uid=uid,
                    comm=comm,
                    reason=reason,
                    hold_seconds=hold,
                    now=now,
                )
                LOG.info(
                    "egress broker request coalesce request_id=%s container=%s host=%s port=%d",
                    row.request_id,
                    container,
                    host,
                    port,
                )
                return self._pending_body(row), row.request_id

            denylist_outcome = self._denylist_short_circuit(
                container,
                host,
                port,
                now,
                request_id,
                hold_seconds=hold,
                host_is_ip=host_is_ip,
                uid=uid,
                comm=comm,
                reason=reason,
            )
            if denylist_outcome is not None:
                return denylist_outcome

            row, is_new = self._store.open_or_hit(
                request_id=request_id if request_id is not None else uuid4().hex,
                container=container,
                host=host,
                port=port,
                host_is_ip=host_is_ip,
                uid=uid,
                comm=comm,
                reason=reason,
                hold_seconds=hold,
                now=now,
            )
            if is_new:
                notification = EgressNotification(
                    request_id=row.request_id,
                    container=container,
                    host=host,
                    port=port,
                    host_is_ip=host_is_ip,
                    uid=uid,
                    comm=comm,
                    reason=reason,
                )
                LOG.info(
                    "egress broker notify dispatch request_id=%s",
                    row.request_id,
                )
                self._store.mark_notified(row.request_id, now)
                LOG.info(
                    "egress broker request filed request_id=%s container=%s host=%s port=%d",
                    row.request_id,
                    container,
                    host,
                    port,
                )

        if notification is not None:
            self._dispatch_notifier(notification)

        return self._pending_body(row), row.request_id

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
        # polling client sees the real cause instead of a generic
        # "denied by the operator" (matches the short-circuit body shape).
        body = {"decision": "deny"}
        if decision.reason is not None:
            body["reason"] = decision.reason
        if decision.zone is not None:
            body["zone"] = decision.zone
        if decision.scope is not None:
            body["scope"] = decision.scope
        return body

    # -- denylist short-circuit ---------------------------------------------

    def _prune_denylist_hit_last(self, now: datetime) -> None:
        """Drop coalesce-window entries older than DENYLIST_SUPPRESS_SECONDS.

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
        cutoff = now - timedelta(seconds=DENYLIST_SUPPRESS_SECONDS)
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
        request_id: str | None,
        *,
        host_is_ip: bool,
        uid: int | None,
        comm: str | None,
        reason: str | None,
        hold_seconds: int | None,
    ) -> tuple[dict[str, Any], str] | None:
        """Deny immediately (no operator prompt) when host is denylisted.

        Called under self._lock, before a new row would be created — an
        already-open request never reaches here (the coalesce path returns
        first), so a later denylist entry cannot retroactively short-circuit
        it.

        First hit inside the coalesce window: a NEW row is written decided —
        status=denied, decided_by=denylist, denylist_zone/denylist_scope
        naming the entry — with one `denied` event. A repeat inside the
        window suppresses: `suppressed_hit` bumps that row's hit_count in
        its own transaction and writes no event, so a hot retry loop does
        not flood the audit trail.

        The id returned to the caller is the same one the row carries —
        the client-supplied id when given, else one freshly minted here.
        """
        entry = self._denylist.matches(container, host)
        if entry is None:
            return None

        key = (container, entry.zone)
        hit = self._denylist_hits.get(key)
        should_log = hit is None or (now - hit.last).total_seconds() >= DENYLIST_SUPPRESS_SECONDS
        if should_log:
            suppressed = hit.suppressed if hit is not None else 0
            rid = request_id if request_id is not None else uuid4().hex
            self._denylist_hits[key] = _DenylistHitState(last=now, request_id=rid)
            self._prune_denylist_hit_last(now)
            row, _ = self._store.open_or_hit(
                request_id=rid,
                container=container,
                host=host,
                port=port,
                host_is_ip=host_is_ip,
                uid=uid,
                comm=comm,
                reason=reason,
                hold_seconds=hold_seconds,
                now=now,
            )
            # denied event fields (store close()): reason carries the entry's
            # own operator free text when the entry has one; the literal
            # "denylist" stays confined to the HTTP response body returned
            # below, which the container-side readers must not change shape.
            self._store.close(
                request_id=rid,
                status="denied",
                now=self.now(),
                decided_by="denylist",
                deny_reason="denylist",
                denylist_zone=entry.zone,
                denylist_scope=entry.scope,
                decision_body={
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": entry.zone,
                    "scope": entry.scope,
                },
            )
            # LOG.info gated to the same coalesce window as the row/event
            # pair above — at INFO on every hit this floods just as badly
            # as the old audit log did. Hits suppressed in between are
            # surfaced here as suppressed=N rather than silently vanishing.
            LOG.info(
                "egress broker denylist short_circuit container=%s host=%s zone=%s scope=%s suppressed=%d",
                container,
                host,
                entry.zone,
                entry.scope,
                suppressed,
            )
            return {
                "decision": "deny",
                "reason": "denylist",
                "zone": entry.zone,
                "scope": entry.scope,
            }, rid

        hit.suppressed += 1
        assert hit.request_id is not None
        self._store.suppressed_hit(hit.request_id, now)
        return {
            "decision": "deny",
            "reason": "denylist",
            "zone": entry.zone,
            "scope": entry.scope,
        }, hit.request_id

    # -- deciding -----------------------------------------------------------

    def decide(
        self,
        request_id: str,
        decision: str,
        scope: str | None = None,
        *,
        reason: str | None = None,
        decided_by: str = "operator",
    ) -> str | None:
        """Public approver-facing entry point — decide one open request.

        Thin wrapper over _close_request(): its signature deliberately
        cannot accept denylist_zone/denylist_scope/persist_failed — those
        are internal to persist_deny()'s own sweep (see _close_request's
        docstring) and must never be reachable from an operator surface.
        The /decide HTTP handler, decide_allow_for_zone(), and
        decide_deny_for_zone() all go through THIS method, never
        _close_request() directly.
        """
        return self._close_request(
            request_id, decision, scope, reason=reason, decided_by=decided_by
        )

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
        decided_by: str = "operator",
    ) -> str | None:
        """Decide one open request, writing every outcome to the store.

        The deny path is a ONE-SHOT deny of THIS request only — it never
        writes to the denylist. `scope` on a deny is accepted purely for
        the audit/decision-body record of what was asked for; the only code
        path that ever calls DenyList.add() is EgressBroker.persist_deny(),
        which holds self._lock across the write (closing the reload/mutate
        race DenyList.matches()'s own _reload() could otherwise hit on a
        handler thread) and then sweeps every request it covers through
        repeated calls back into this method.

        Returns None when the decision is final (allow applied, or deny).
        On allow paths where no rule was installed, returns the error reason
        string and keeps the request open for retry; persist_failed=True
        (set only by persist_deny() when its own DenyList.add() raised)
        returns DENYLIST_PERSIST_FAILED_REASON in the same channel — the
        one-shot deny still completes rather than leaving the request stuck.

        denylist_zone/denylist_scope are INTERNAL — set only by
        EgressBroker.persist_deny() when it sweeps a request closed that an
        entry it JUST wrote now covers. They record the denylist context on
        the row WITHOUT overwriting `reason`, which stays the operator's
        free-text explanation (or None) — the two are deliberately kept
        distinct: `reason` is for a human reading the audit trail, while
        decision.reason="denylist" (surfaced to the polling client via the
        decision body) is the machine-readable cause. Do not pass
        denylist_zone/denylist_scope/persist_failed from an operator
        surface directly.
        """
        LOG.info(
            "egress broker decide enter request_id=%s decision=%s scope=%s",
            request_id,
            decision,
            scope or "",
        )
        allow_error: str | None = None
        apply_container: str | None = None
        apply_host: str | None = None
        resolved_scope: str | None = None
        run_apply_outside_lock = False

        with self._lock:
            try:
                row = self._store.get(request_id)
            except EgressStoreError as exc:
                raise EgressBrokerHostError(str(exc)) from exc
            if row is None:
                raise EgressBrokerHostError(
                    f"no open request for request_id={request_id}"
                )
            if row.status != OPEN_STATUS:
                raise EgressBrokerHostError(f"request_id={request_id} already decided")
            if request_id in self._applying:
                LOG.info(
                    "egress broker decide noop request_id=%s reason=apply_in_progress",
                    request_id,
                )
                return APPLY_IN_PROGRESS_REASON

            now = self.now()

            if decision == "allow":
                resolved_scope = scope or "live"
                if resolved_scope not in ("live", "manifest"):
                    raise EgressBrokerHostError(
                        f"invalid scope {resolved_scope!r} (must be live or manifest)"
                    )
                if row.host_is_ip or is_ip_literal(row.host):
                    self._store.mark_apply(
                        request_id=request_id, outcome="ip_requires_cidr", now=now
                    )
                    LOG.info(
                        "egress broker decide exit request_id=%s decision=%s allow_error=%s",
                        request_id,
                        decision,
                        IP_REQUIRES_CIDR_REASON,
                    )
                    return IP_REQUIRES_CIDR_REASON
                # self._lock must not be held across subprocess/network I/O
                # (allow-egress.sh can call back into this daemon).
                self._applying.add(request_id)
                apply_container = row.container
                apply_host = row.host
                run_apply_outside_lock = True
            elif decision == "deny":
                resolved_scope = scope or "once"
                if resolved_scope not in VALID_DECIDE_SCOPES:
                    raise EgressBrokerHostError(
                        f"invalid scope {resolved_scope!r} (must be once, bottle, or global)"
                    )
                if persist_failed:
                    # persist_deny()'s own DenyList.add() raised: nothing was
                    # written, so this decide() call degrades to a plain
                    # one-shot deny of just the triggering request — but the
                    # row still records that a persist was attempted and
                    # failed (persist_status=persist_failed), so the audit
                    # trail proves the deny-list entry was NOT written.
                    resolved_scope = "once"
                if denylist_zone is not None:
                    # Sweep closure from persist_deny(): a sibling call
                    # already wrote the entry under self._lock; this call
                    # only closes THIS request and tells its polling client
                    # why. `reason` (if any) stays the operator's own free
                    # text — never overwritten with the literal string
                    # "denylist" — and the denylist context lives in its own
                    # row columns (denylist_zone/denylist_scope), with the
                    # machine-readable cause in the decision body.
                    body: dict[str, Any] = {
                        "decision": "deny",
                        "reason": "denylist",
                        "zone": denylist_zone,
                        "scope": denylist_scope if denylist_scope is not None else resolved_scope,
                    }
                else:
                    body = {"decision": "deny"}
                self._store.close(
                    request_id=request_id,
                    status="denied",
                    now=now,
                    decided_by=decided_by,
                    scope=resolved_scope,
                    deny_reason=reason,
                    denylist_zone=denylist_zone,
                    denylist_scope=denylist_scope,
                    decision_body=body,
                )
                allow_error = DENYLIST_PERSIST_FAILED_REASON if persist_failed else None
            else:
                raise EgressBrokerHostError(
                    f"invalid decision {decision!r} (must be allow or deny)"
                )

        if run_apply_outside_lock:
            assert resolved_scope is not None
            assert apply_container is not None
            assert apply_host is not None
            apply_ok = self._apply_allow(apply_container, apply_host, resolved_scope)

            with self._lock:
                self._applying.discard(request_id)
                try:
                    row = self._store.get(request_id)
                except EgressStoreError:
                    row = None
                if row is None or row.status != OPEN_STATUS:
                    LOG.info(
                        "egress broker decide stale request_id=%s after apply",
                        request_id,
                    )
                    return allow_error
                now = self.now()
                if apply_ok:
                    self._store.mark_apply(request_id=request_id, outcome="applied", now=now)
                    self._store.close(
                        request_id=request_id,
                        status="allowed",
                        now=self.now(),
                        decided_by=decided_by,
                        scope=resolved_scope,
                        decision_body={
                            "decision": "allow",
                            "scope": resolved_scope,
                        },
                    )
                else:
                    self._store.mark_apply(request_id=request_id, outcome="apply_failed", now=now)
                    allow_error = APPLY_FAILED_REASON

        LOG.info(
            "egress broker decide exit request_id=%s decision=%s allow_error=%s",
            request_id,
            decision,
            allow_error or "",
        )
        return allow_error

    def _open_request_ids_for_zone(self, container: str | None, zone: str) -> list[str]:
        """Open request ids whose host falls under zone.

        container=None matches every container (persist_deny's global
        sweep); a concrete name restricts to that bottle's own requests.
        """
        with self._lock:
            return [
                row.request_id
                for row in self._store.list_open(container)
                if host_covered_by_zone(row.host, zone)
            ]

    def decide_allow_for_zone(
        self,
        container: str,
        domain: str,
        *,
        scope: str = "live",
        decided_by: str = "operator",
    ) -> ZoneDecisionResult:
        """Allow every open request whose host falls under domain (host-side only)."""
        zone, _is_ip = normalize_destination(domain)
        candidates = self._open_request_ids_for_zone(container, zone)
        decided: list[str] = []
        apply_failures: list[tuple[str, str]] = []
        for request_id in candidates:
            try:
                allow_error = self.decide(
                    request_id, "allow", scope=scope, decided_by=decided_by
                )
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker decide_allow_for_zone skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            if allow_error == APPLY_IN_PROGRESS_REASON:
                # A concurrent allow is mid-apply: this sweep neither decided
                # it nor failed it — the in-flight apply resolves it.
                LOG.info(
                    "egress broker decide_allow_for_zone skip request_id=%s reason=%s",
                    request_id,
                    allow_error,
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
        decided_by: str = "operator",
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
                deny_outcome = self.decide(
                    request_id,
                    "deny",
                    scope="once",
                    reason=reason,
                    decided_by=decided_by,
                )
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker decide_deny_for_zone skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            if deny_outcome == APPLY_IN_PROGRESS_REASON:
                LOG.info(
                    "egress broker decide_deny_for_zone skip request_id=%s reason=%s",
                    request_id,
                    deny_outcome,
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
        decided_by: str = "operator",
    ) -> PersistDenyResult:
        """Persist a deny entry for the ZONE THE CALLER NAMED — not any one
        open request's exact host — then close every open request it now
        covers: every container when scope is global, just `container` when
        scope is bottle. THE ONLY place that writes a denylist entry — the
        entry point for `/decide` deny with scope != once.

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
        the broker-wide lock — for the same duration. Once the write
        returns, this method takes self._lock just long enough to force
        self._denylist to reload from what was just written, so the next
        matches() call on any handler thread (always taken under
        self._lock too, via _denylist_short_circuit) sees the new entry
        rather than racing this method's post-write state update. Write
        still happens before any sweep below, unchanged.

        On a write failure (disk full/read-only, or a corrupt file refusing
        to be overwritten): nothing is swept (PersistDenyResult.entry is
        None), and if `trigger_request_id` names a still-open request, it is
        closed as a plain one-shot deny (persist_failed=True) so the polling
        client is released — mirrors decide()'s own never-raise-for-this-
        failure posture.

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
                        decided_by=decided_by,
                    )
                    self._store.mark_persist(
                        request_id=trigger_request_id,
                        outcome="persist_failed",
                        now=self.now(),
                    )
                except (EgressBrokerHostError, EgressStoreError) as close_exc:
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
                sweep_outcome = self._close_request(
                    request_id,
                    "deny",
                    scope=scope,
                    reason=reason,
                    denylist_zone=entry.zone,
                    denylist_scope=entry.scope,
                    decided_by=decided_by,
                )
            except EgressBrokerHostError as exc:
                LOG.info(
                    "egress broker persist_deny_sweep skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
            if sweep_outcome == APPLY_IN_PROGRESS_REASON:
                LOG.info(
                    "egress broker persist_deny_sweep skip request_id=%s reason=%s",
                    request_id,
                    sweep_outcome,
                )
                continue
            try:
                self._store.mark_persist(
                    request_id=request_id, outcome="persisted", now=self.now()
                )
            except EgressStoreError as exc:
                LOG.info(
                    "egress broker persist_deny mark_persist skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
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
        """Close unanswered requests older than STALE_HOURS as stale.

        The per-request body a polling client then receives is
        {"decision": "deny", "reason": "stale"} — a deny it already
        understands, distinguishable from an operator decision by the row's
        status/decided_by. A stale row is terminal; a later filing of the
        same host opens a new row. A request whose allow is in flight is
        left alone — the in-flight apply resolves it.
        """
        now = self.now()
        cutoff = now - timedelta(hours=STALE_HOURS)
        stale_ids: list[str] = []
        with self._lock:
            for row in self._store.list_open():
                if row.opened_at <= cutoff and row.request_id not in self._applying:
                    stale_ids.append(row.request_id)

        closed = 0
        for request_id in stale_ids:
            try:
                self._store.close(
                    request_id=request_id,
                    status="stale",
                    now=now,
                    decided_by="sweep",
                    deny_reason="stale",
                    decision_body={"decision": "deny", "reason": "stale"},
                )
                closed += 1
            except EgressStoreError as exc:
                LOG.info(
                    "egress broker stale sweep skip request_id=%s reason=%s",
                    request_id,
                    exc,
                )
                continue
        if closed:
            LOG.info("egress broker stale sweep closed=%d", closed)
        return closed

    # -- operator-facing reads ----------------------------------------------

    def queue_snapshot(self) -> dict[str, Any]:
        """Return the current decision queue for operator-facing UIs.

        This answers what was asked and decided, never whether host X is
        currently permitted - ipset allowed-domains is the sole authority.
        A caller must render decisions, not current egress state.

        `open` carries the still-open rows (each with its apply `attempt`
        and `last_error`); `recent` carries rows decided in the last 24
        hours, newest first.
        """

        with self._lock:
            now = self.now()
            open_rows: list[dict[str, Any]] = []
            for row in self._store.list_open():
                open_rows.append(
                    {
                        "request_id": row.request_id,
                        "container": row.container,
                        "host": row.host,
                        "port": row.port,
                        "host_is_ip": row.host_is_ip,
                        "opened_at": _iso_ts(row.opened_at),
                        "age_seconds": max(0, int((now - row.opened_at).total_seconds())),
                        "hit_count": row.hit_count,
                        "uid": row.uid,
                        "comm": row.comm,
                        "reason": row.reason,
                        "attempt": row.apply_attempts,
                        "last_error": row.last_error,
                    }
                )
            recent_rows = [
                decided_row_json(row)
                for row in self._store.list_recent(
                    since=now - timedelta(hours=RECENT_WINDOW_HOURS)
                )
            ]
            snapshot = {
                "open": open_rows,
                "count": len(open_rows),
                "recent": recent_rows,
                "generated_at": _iso_ts(now),
            }
        LOG.info("egress broker queue snapshot open=%d", snapshot["count"])
        return snapshot

    def recent_page(
        self,
        *,
        before: tuple[str, str] | None = None,
        limit: int = RECENT_PAGE_DEFAULT,
        container: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """One keyset page of decided rows, newest first, for the History tab.

        `next` is the cursor of the last row when the page is full and null
        when fewer than `limit` rows remained, so a null `next` always means
        the end. A page that ends exactly on the last row still carries a
        cursor; the page after it is empty with `next` null.
        """
        started = time.monotonic()
        rows = self._store.list_recent(
            since=since, until=until, container=container, before=before, limit=limit
        )
        last = rows[-1] if rows else None
        next_cursor = None
        if last is not None and len(rows) >= limit:
            next_cursor = f"{_iso_ts(last.decided_at)},{last.request_id}"
        LOG.info(
            "egress broker recent page rows=%d limit=%d has_next=%s duration_ms=%.1f",
            len(rows),
            limit,
            next_cursor is not None,
            (time.monotonic() - started) * 1000.0,
        )
        return {"rows": [decided_row_json(r) for r in rows], "next": next_cursor}

    def request_view(self, request_id: str, container: str) -> dict[str, Any] | None:
        """The public fields of one row, for GET /egress/<request_id>.

        None when the row is missing or belongs to another container — the
        handler answers 404 either way, without revealing which.
        """
        row = self._store.get(request_id)
        if row is None or row.container != container:
            return None
        body: dict[str, Any] = {
            "request_id": row.request_id,
            "status": row.status,
            "attempt": row.apply_attempts,
        }
        if row.last_error is not None:
            body["last_error"] = row.last_error
        if row.decision_body is not None:
            body["decision_body"] = row.decision_body
        return body


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
    """Threaded HTTP handler for egress filing, polling, and decisions.

    No handler ever blocks on an operator: POST /egress answers at once and
    GET /egress/<request_id> answers from the store.
    """

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
        poll_match = re.fullmatch(r"/egress/([0-9a-f]{32}|[0-9a-f]{8})", self.path)
        if poll_match:
            self._handle_egress_get(poll_match.group(1))
            return
        if self.path == "/queue":
            LOG.info("egress broker request enter path=/queue")
            if not self._resolve_operator_auth():
                return
            self._send_json(HTTPStatus.OK, self.server.broker.queue_snapshot())
            return
        recent_path, _, recent_query = self.path.partition("?")
        if recent_path == "/recent":
            self._handle_recent_get(recent_query)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_recent_get(self, query: str) -> None:
        LOG.info("egress broker request enter path=/recent query_bytes=%d", len(query))
        if not self._resolve_operator_auth():
            return
        try:
            kwargs = parse_recent_query(query)
        except RecentQueryError as exc:
            LOG.info("egress broker request rejected path=/recent error=%s", exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        page = self.server.broker.recent_page(**kwargs)
        LOG.info(
            "egress broker request exit path=/recent rows=%d has_next=%s",
            len(page["rows"]),
            page["next"] is not None,
        )
        self._send_json(HTTPStatus.OK, page)

    def do_POST(self) -> None:
        if self.path == "/decide":
            self._handle_decide_post()
            return
        if self.path != "/egress":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._handle_egress_post()

    def _handle_egress_get(self, request_id: str) -> None:
        container = self._resolve_bottle_from_auth()
        if container is None:
            return
        LOG.info(
            "egress broker request enter path=/egress/%s",
            request_id,
        )
        view = self.server.broker.request_view(request_id, container)
        if view is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown request"})
            return
        self._send_json(HTTPStatus.OK, view)

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

        # The admin daemon proxies decide calls server-side with the
        # operator token, so an admin-UI decision is not distinguishable
        # from a CLI one; both record decided_by="operator". ntfy's action
        # buttons identify themselves by User-Agent.
        user_agent = self.headers.get("User-Agent", "")
        decided_by = "ntfy" if user_agent_starts_with_ntfy(user_agent) else "operator"

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
            # isinstance check FIRST: VALID_DECIDE_SCOPES is a frozenset, and
            # `x not in frozenset` hashes x — an unhashable payload["scope"]
            # (a list or dict, both valid JSON) raises TypeError instead of a
            # 400, which escapes this handler thread with no response sent at
            # all. The "allow" branch above is safe as-is: `in` against a
            # tuple does equality comparisons, never a hash lookup.
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
            # A persistent deny targets a zone that may be an IP literal
            # (./djinn deny 93.0.2.55 --global is valid — the denylist has no
            # CIDR concept but does exact-match IPs). Allow requests may
            # target IP literals too; these are surfaced as apply_failures
            # (ip_requires_cidr) rather than rejected up front, so operator
            # clients learn why nothing was installed.
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
                    decided_by=decided_by,
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
                    decided_by=decided_by,
                )
                self._send_json(HTTPStatus.OK, {"decided": decided})
                return
            result = self.server.broker.persist_deny(
                host_raw,
                scope,
                container=container,
                reason=reason,
                decided_by=decided_by,
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
        except UnknownRequest as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown request"})
            return
        except EgressStoreError as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "store error"})
            return
        except EgressBrokerHostError as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.OK, body)


def user_agent_starts_with_ntfy(user_agent: str) -> bool:
    return user_agent.startswith("ntfy")


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
    advertise: tuple[str, int] | None = None,
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
        actions_url=(os.environ.get(EGRESS_ACTIONS_URL_ENV) or "").strip() or None,
        admin_url=(os.environ.get(EGRESS_ADMIN_URL_ENV) or "").strip() or None,
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
    # --advertise records the address host-side callers should use (the
    # published loopback port) as a docker-managed endpoint with no pid: the
    # container's pid namespace is unreachable and docker owns the lifetime.
    if advertise is not None:
        write_daemon_endpoint(
            egress_root, advertise[0], advertise[1], managed=MANAGED_DOCKER
        )
    else:
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
        "--bind-any",
        action="store_true",
        help=(
            "bind 0.0.0.0 instead of loopback — refused unless DJINN_CONTAINER=1 "
            "is set (the docker service sets it; a published 127.0.0.1 port is "
            "the only host-side reachability)"
        ),
    )
    parser.add_argument(
        "--advertise",
        metavar="HOST:PORT",
        help=(
            "record HOST:PORT in daemon.json as a docker-managed endpoint for "
            "host-side callers (the container binds 0.0.0.0, which they cannot "
            "use); liveness is answered by probing /health, not a pid"
        ),
    )
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


def _resolve_bind(args: argparse.Namespace) -> tuple[str, tuple[str, int] | None]:
    """The bind host, and the endpoint to advertise (None = version 1).

    --bind-any is container-only: outside the container a 0.0.0.0 bind is a
    network-exposed daemon with no session gate on the filing endpoint, so it
    is refused by name rather than silently honoured.
    """
    if args.bind_any:
        if os.environ.get(CONTAINER_MARKER_ENV) != "1":
            raise EgressBrokerHostError(
                "--bind-any refused: it is only valid inside the egress broker "
                "container (DJINN_CONTAINER=1) — run ./djinn egress start"
            )
        return "0.0.0.0", None
    return args.host, None


def _parse_advertise(raw: str | None) -> tuple[str, int] | None:
    if not raw:
        return None
    value = raw.strip()
    if value.startswith("[") and "]" in value:
        host_part, _, rest = value[1:].partition("]")
        if rest.startswith(":"):
            port_part = rest[1:]
        else:
            raise EgressBrokerHostError(f"invalid --advertise {raw!r} (want HOST:PORT)")
    elif ":" in value:
        host_part, _, port_part = value.rpartition(":")
    else:
        raise EgressBrokerHostError(f"invalid --advertise {raw!r} (want HOST:PORT)")
    if not host_part or not port_part.isdigit() or not (1 <= int(port_part) <= 65535):
        raise EgressBrokerHostError(f"invalid --advertise {raw!r} (want HOST:PORT)")
    return host_part, int(port_part)


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
        advertise = _parse_advertise(args.advertise)
        host, _ = _resolve_bind(args)
        run_daemon(base_path, host=host, port=args.port, advertise=advertise)
    except DaemonAlreadyRunning as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except EgressBrokerHostError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
