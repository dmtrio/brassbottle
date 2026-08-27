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
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from egress_log import EgressLog, EgressLogError

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 8816
DEFAULT_HOLD_SECONDS = 45
STALE_HOURS = 24
HIT_COALESCE_SECONDS = 60
STALE_SWEEP_INTERVAL_SECONDS = 300

TOKENS_DIRNAME = "tokens"
LOCK_FILENAME = "daemon.lock"
CONFIG_FILENAME = "config.json"

IP_APPLY_FAILED_REASON = (
    "destination is an IP address; add it to the bottle manifest "
    "capabilities.egress_cidrs (ALLOWED_CIDRS) — allow-egress.sh accepts "
    "domain zones only"
)
IP_REQUIRES_CIDR_REASON = "ip_requires_cidr"
APPLY_FAILED_REASON = "apply_failed"

DOMAIN_RE = re.compile(
    r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$"
)


class EgressBrokerHostError(Exception):
    """Operator-facing egress broker error."""


class DaemonAlreadyRunning(EgressBrokerHostError):
    """Second singleton instance refused."""


@dataclass
class Decision:
    """Resolved approval outcome for a held long-poll."""

    decision: str
    scope: str | None = None
    reason: str | None = None


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
    waiter_outcome: Decision | None = None
    waiters: list[threading.Event] = field(default_factory=list)


NowFn = Callable[[], datetime]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


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


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _load_bottle_token(token_path: Path) -> str:
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
    LOG.info("egress broker bottle token created bottle=%s", token_path.stem)
    return token


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


def _request_key(container: str, host: str, port: int) -> tuple[str, str, int]:
    return (container, host, port)


class EgressBroker:
    """Queue, long-poll, and approval executor for egress requests."""

    def __init__(
        self,
        root: Path,
        *,
        repo_root: Path | None = None,
        now_fn: NowFn | None = None,
        hold_seconds_default: int = DEFAULT_HOLD_SECONDS,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._repo_root = (repo_root or _repo_root()).resolve()
        self._now_fn = now_fn or (lambda: _utc_now())
        self._hold_seconds_default = hold_seconds_default
        self._log = EgressLog(self._root)
        self._lock = threading.RLock()
        self._requests: dict[str, OpenRequestState] = {}
        self._key_index: dict[tuple[str, str, int], str] = {}
        self._rebuild_from_log()

    @property
    def root(self) -> Path:
        return self._root

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

    def _notify_operator(self, request_id: str, now: datetime) -> None:
        LOG.info("egress broker notify dispatch request_id=%s", request_id)
        self._log.append("notified", request_id, ts=now)

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
    ) -> tuple[dict[str, Any], str]:
        """File or coalesce an egress request; return JSON body and request_id."""
        key = _request_key(container, host, port)
        hold = hold_seconds if hold_seconds is not None else self._hold_seconds_default
        now = self.now()

        with self._lock:
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
                state = OpenRequestState(
                    request_id=request_id,
                    container=container,
                    host=host,
                    port=port,
                    opened_at=now,
                    host_is_ip=host_is_ip,
                )
                fields: dict[str, Any] = {
                    "container": container,
                    "host": host,
                    "port": port,
                }
                if host_is_ip:
                    fields["host_is_ip"] = True
                if uid is not None:
                    fields["uid"] = uid
                if comm is not None:
                    fields["comm"] = comm
                if reason is not None:
                    fields["reason"] = reason
                self._log.append("requested", request_id, ts=now, **fields)
                self._notify_operator(request_id, now)
                self._requests[request_id] = state
                self._key_index[key] = request_id
                LOG.info(
                    "egress broker request filed request_id=%s container=%s host=%s port=%d",
                    request_id,
                    container,
                    host,
                    port,
                )

            if state.decision is not None:
                body = self._decision_body(state.decision)
                return body, request_id

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
        return {"decision": "deny"}

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
        """Release held long-polls for one request (approver UI entry point).

        Returns None when the decision is final (allow applied, or deny). On
        allow paths where no rule was installed, returns the error reason string
        and keeps the request open for retry.
        """
        LOG.info(
            "egress broker decide enter request_id=%s decision=%s scope=%s",
            request_id,
            decision,
            scope or "",
        )
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                raise EgressBrokerHostError(f"no open request for request_id={request_id}")
            if state.decision is not None:
                raise EgressBrokerHostError(
                    f"request_id={request_id} already decided"
                )

            now = self.now()
            self._flush_hits(state)

            outcome: Decision | None = None
            allow_error: str | None = None

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
                    apply_ok = self._apply_allow(state, resolved_scope)
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
            elif decision == "deny":
                fields: dict[str, Any] = {}
                if reason is not None:
                    fields["reason"] = reason
                self._log.append("denied", request_id, ts=now, **fields)
                outcome = Decision(decision="deny")
                state.decision = outcome
            else:
                raise EgressBrokerHostError(
                    f"invalid decision {decision!r} (must be allow or deny)"
                )

            if outcome is not None:
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

    def _apply_allow(self, state: OpenRequestState, scope: str) -> bool:
        save_target = "yml" if scope == "manifest" else "none"
        cmd = [
            str(self._allow_script()),
            state.container,
            state.host,
            "--save",
            save_target,
        ]
        started = time.monotonic()
        LOG.info(
            "egress broker subprocess spawn argv_len=%d container=%s host=%s save=%s",
            len(cmd),
            state.container,
            state.host,
            save_target,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
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
    ) -> None:
        self.broker = broker
        self.token_store = token_store
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

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/egress":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
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
            )
        except EgressLogError as exc:
            LOG.info("egress broker request error reason=%s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "log error"})
            return

        self._send_json(HTTPStatus.OK, body)


def _stale_sweep_loop(broker: EgressBroker, stop_event: threading.Event) -> None:
    while not stop_event.wait(STALE_SWEEP_INTERVAL_SECONDS):
        try:
            broker.sweep_stale()
        except Exception:
            LOG.exception("egress broker stale sweep failed")


def resolve_run_path(base_path: Path) -> Path:
    return base_path / "run"


def resolve_egress_root(base_path: Path) -> Path:
    return resolve_run_path(base_path) / "egress"


def resolve_base_path(raw: str) -> Path:
    # expanduser throughout: ./.env is sourced by bash, so DJINN_HOME="$HOME/x"
    # arrives already expanded, but a literal ~/x does not — and Path("~/x")
    # would quietly create a directory actually named "~" in the cwd. That is
    # the kind of thing you only notice when the queue turns out to be empty.
    if raw:
        return Path(raw).expanduser()
    env = os.environ.get("DJINN_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return _repo_root() / ".djinn"


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

    broker = EgressBroker(
        egress_root,
        repo_root=repo_root,
        hold_seconds_default=hold_default,
    )
    server = EgressBrokerHTTPServer((host, port), broker, token_store)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    base_path = resolve_base_path(args.base_path)
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
