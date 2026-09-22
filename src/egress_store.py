#!/usr/bin/env python3
"""egress_store.py — SQLite request store for egress approval (queue + audit).

Owns `<egress_root>/egress.db`: one SQLite database that is both the open
decision queue and the append-only audit trail for egress filings. Stdlib
only; host-side (macOS and Linux). A leaf module: nothing in it imports
another repo module except `import_open`, which lazily imports the
egress_log helpers to migrate open requests at cutover.

Schema (SQLite, journal_mode=WAL, foreign_keys=ON):

    requests
      request_id      TEXT PRIMARY KEY
      container       TEXT NOT NULL
      host            TEXT NOT NULL
      port            INTEGER NOT NULL
      host_is_ip      INTEGER NOT NULL
      uid             INTEGER
      comm            TEXT
      reason          TEXT
      hold_seconds    INTEGER
      opened_at       TEXT NOT NULL      -- ISO 8601 UTC, second resolution
      last_hit_at     TEXT NOT NULL
      hit_count       INTEGER NOT NULL DEFAULT 1
      status          TEXT NOT NULL      -- open | allowed | denied | stale
      scope           TEXT               -- live | manifest | once | bottle | global
      decided_at      TEXT
      decided_by      TEXT               -- admin | cli | ntfy | sweep | denylist
      deny_reason     TEXT
      apply_status    TEXT               -- applied | apply_failed | ip_requires_cidr | none
      apply_attempts  INTEGER NOT NULL DEFAULT 0   -- completed operator allow attempts
      last_error      TEXT               -- JSON {reason, attempt, at}; NULL after a successful apply
      persist_status  TEXT               -- persisted | persist_failed | none
      denylist_zone   TEXT
      denylist_scope  TEXT               -- bottle | global
      decision_body   TEXT               -- JSON: the per-request body a bottle parses;
                                         -- set only by a terminal decision
      index (status, container, opened_at)
      index (container, host, port, status)

    events
      id              INTEGER PRIMARY KEY
      request_id      TEXT NOT NULL REFERENCES requests
      kind            TEXT NOT NULL      -- requested | hit | notified | allowed |
                                         -- denied | applied | apply_failed | stale
      ts              TEXT NOT NULL      -- ISO 8601 UTC
      fields          TEXT               -- JSON, the kind's extra fields
                                         -- (count, scope, reason, decided_by, zone)
      index (request_id, id)

    schema_version
      version         INTEGER NOT NULL   -- 1

Statuses: a filing starts `open`; it leaves that state only through `close`
(to `allowed`, `denied` or `stale`). Apply and persist outcomes are recorded
alongside by `mark_apply` / `mark_persist` and never change `status`.

Events rule: every state-changing method except `suppressed_hit` writes its
row change and its event in ONE transaction — a failure inside leaves
neither. An event whose `fields` dict is empty is stored as SQL NULL and
read back as `{}`.

Timestamps: every method takes and returns UTC-aware `datetime` values,
truncated to second resolution (the on-disk format is
`YYYY-MM-DDTHH:MM:SSZ`, so lexicographic order is chronological order).

Import loss (accepted, applies once at cutover): a request known only from
a carry-forward header imports with `uid`, `comm`, `reason` and
`hold_seconds` NULL and `hit_count` 1 — the monthly JSONL log records those
fields only in the month the request was filed, and the import reads the
current month only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DB_FILENAME = "egress.db"
SQLITE_HEADER = b"SQLite format 3\x00"

OPEN_STATUS = "open"
CLOSE_STATUSES = frozenset({"allowed", "denied", "stale"})
APPLY_OUTCOMES = frozenset({"applied", "apply_failed", "ip_requires_cidr"})
PERSIST_OUTCOMES = frozenset({"persisted", "persist_failed"})

_REQUEST_COLUMNS = (
    "request_id",
    "container",
    "host",
    "port",
    "host_is_ip",
    "uid",
    "comm",
    "reason",
    "hold_seconds",
    "opened_at",
    "last_hit_at",
    "hit_count",
    "status",
    "scope",
    "decided_at",
    "decided_by",
    "deny_reason",
    "apply_status",
    "apply_attempts",
    "last_error",
    "persist_status",
    "denylist_zone",
    "denylist_scope",
    "decision_body",
)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    container       TEXT NOT NULL,
    host            TEXT NOT NULL,
    port            INTEGER NOT NULL,
    host_is_ip      INTEGER NOT NULL,
    uid             INTEGER,
    comm            TEXT,
    reason          TEXT,
    hold_seconds    INTEGER,
    opened_at       TEXT NOT NULL,
    last_hit_at     TEXT NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,
    scope           TEXT,
    decided_at      TEXT,
    decided_by      TEXT,
    deny_reason     TEXT,
    apply_status    TEXT,
    apply_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    persist_status  TEXT,
    denylist_zone   TEXT,
    denylist_scope  TEXT,
    decision_body   TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_status_container_opened_at
    ON requests(status, container, opened_at);
CREATE INDEX IF NOT EXISTS idx_requests_container_host_port_status
    ON requests(container, host, port, status);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES requests,
    kind            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    fields          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_request_id_id
    ON events(request_id, id);
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER NOT NULL
);
"""


class EgressStoreError(Exception):
    """Invalid egress store operation or unusable on-disk state."""


@dataclass(frozen=True)
class RequestRow:
    """One row of the requests table — the full current state of a filing."""

    request_id: str
    container: str
    host: str
    port: int
    host_is_ip: bool
    uid: int | None
    comm: str | None
    reason: str | None
    hold_seconds: int | None
    opened_at: datetime
    last_hit_at: datetime
    hit_count: int
    status: str
    scope: str | None
    decided_at: datetime | None
    decided_by: str | None
    deny_reason: str | None
    apply_status: str | None
    apply_attempts: int
    last_error: dict[str, Any] | None
    persist_status: str | None
    denylist_zone: str | None
    denylist_scope: str | None
    decision_body: dict[str, Any] | None


@dataclass(frozen=True)
class Event:
    """One row of the append-only events table."""

    id: int
    request_id: str
    kind: str
    ts: datetime
    fields: dict[str, Any]


def _utc_now(dt: datetime) -> datetime:
    """Normalize to UTC-aware and truncate to second resolution."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _iso_ts(dt: datetime) -> str:
    return _utc_now(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(raw: str) -> datetime:
    """Parse a stored ISO 8601 timestamp; raise on garbage (corrupt file)."""
    try:
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EgressStoreError(f"corrupt timestamp in egress.db: {raw!r}") from exc
    return _utc_now(parsed)


def _json_or_none(raw: str | None, column: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EgressStoreError(
            f"corrupt JSON in egress.db column {column}: {raw[:80]!r}"
        ) from exc
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise EgressStoreError(f"corrupt JSON in egress.db column {column}: not an object")
    return parsed


def _is_ip_literal(host: str) -> bool:
    """True when host looks like an IPv4 or IPv6 literal.

    Import-time only (import_open); the broker passes host_is_ip explicitly
    everywhere else, so this is deliberately a cheap local check rather than
    a dependency on the broker module.
    """
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return True
    return ":" in host


def _check_sqlite_header(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise EgressStoreError(f"cannot read {path}: {exc}") from exc
    if header != SQLITE_HEADER:
        raise EgressStoreError(f"{path} is not a SQLite database")


class EgressStore:
    """SQLite-backed request store; all methods are thread-safe.

    One connection guarded by one lock; each state-changing method runs its
    row update and its event insert inside a single transaction, so a failure
    partway through leaves the database exactly as it was.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EgressStoreError(f"cannot create egress root {self._root}: {exc}") from exc
        self._db_path = self._root / DB_FILENAME
        self._lock = threading.Lock()
        fresh = not self._db_path.exists() or self._db_path.stat().st_size == 0
        if not fresh:
            _check_sqlite_header(self._db_path)
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if fresh:
                self._create_schema()
            else:
                self._check_schema_version()
        except sqlite3.Error as exc:
            raise EgressStoreError(f"cannot open {self._db_path}: {exc}") from exc
        LOG.info(
            "egress_store open root=%s fresh=%s",
            self._root,
            fresh,
        )

    # -- schema ---------------------------------------------------------

    def _create_schema(self) -> None:
        self._conn.executescript(_SCHEMA_DDL)
        self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        self._conn.commit()

    def _check_schema_version(self) -> None:
        try:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.DatabaseError as exc:
            raise EgressStoreError(
                f"{self._db_path} has no readable schema_version table: {exc}"
            ) from exc
        if row is None:
            raise EgressStoreError(f"{self._db_path} has an empty schema_version table")
        version = row[0]
        if not isinstance(version, int) or version != SCHEMA_VERSION:
            raise EgressStoreError(
                f"{self._db_path} has schema version {version!r}; expected {SCHEMA_VERSION}"
            )

    def shutdown(self) -> None:
        """Close the connection. Tolerates being called twice."""
        conn = getattr(self, "_conn", None)
        if conn is not None:
            with self._lock:
                conn.close()
            self._conn = None

    # -- transaction plumbing -------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run one all-or-nothing unit: commit on success, rollback on any
        exception. Callers must hold self._lock."""
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _log_boundary(
        self,
        method: str,
        request_id: str,
        status: str,
        started: float,
        **extra: Any,
    ) -> None:
        """One boundary line per call — method, request_id, status, duration.

        Never logs reason, comm or any body content; identifiers and sizes
        only.
        """
        duration_ms = (time.monotonic() - started) * 1000.0
        suffix = "".join(f" {key}={value}" for key, value in extra.items())
        LOG.info(
            "egress_store %s request_id=%s status=%s duration_ms=%.1f%s",
            method,
            request_id,
            status,
            duration_ms,
            suffix,
        )

    # -- row / event reads ----------------------------------------------

    def _fetch_row(self, request_id: str) -> RequestRow | None:
        placeholders = ", ".join(_REQUEST_COLUMNS)
        cursor = self._conn.execute(
            f"SELECT {placeholders} FROM requests WHERE request_id = ?",
            (request_id,),
        )
        raw = cursor.fetchone()
        if raw is None:
            return None
        return self._row(raw)

    def _row(self, raw: tuple) -> RequestRow:
        values = dict(zip(_REQUEST_COLUMNS, raw))
        return RequestRow(
            request_id=values["request_id"],
            container=values["container"],
            host=values["host"],
            port=values["port"],
            host_is_ip=bool(values["host_is_ip"]),
            uid=values["uid"],
            comm=values["comm"],
            reason=values["reason"],
            hold_seconds=values["hold_seconds"],
            opened_at=_parse_ts(values["opened_at"]),
            last_hit_at=_parse_ts(values["last_hit_at"]),
            hit_count=values["hit_count"],
            status=values["status"],
            scope=values["scope"],
            decided_at=_parse_ts(values["decided_at"]) if values["decided_at"] else None,
            decided_by=values["decided_by"],
            deny_reason=values["deny_reason"],
            apply_status=values["apply_status"],
            apply_attempts=values["apply_attempts"],
            last_error=_json_or_none(values["last_error"], "last_error"),
            persist_status=values["persist_status"],
            denylist_zone=values["denylist_zone"],
            denylist_scope=values["denylist_scope"],
            decision_body=_json_or_none(values["decision_body"], "decision_body"),
        )

    @staticmethod
    def _event_fields_json(fields: dict[str, Any]) -> str | None:
        """Empty-field events are stored as NULL and read back as {}."""
        if not fields:
            return None
        return json.dumps(fields, separators=(",", ":"), sort_keys=True)

    def _insert_event(
        self,
        request_id: str,
        kind: str,
        ts: datetime,
        fields: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (request_id, kind, ts, fields) VALUES (?, ?, ?, ?)",
            (request_id, kind, _iso_ts(ts), self._event_fields_json(fields)),
        )

    def _event(self, raw: tuple) -> Event:
        return Event(
            id=raw[0],
            request_id=raw[1],
            kind=raw[2],
            ts=_parse_ts(raw[3]),
            fields=_json_or_none(raw[4], "events.fields") or {},
        )

    # -- state changes ---------------------------------------------------

    def open_or_hit(
        self,
        *,
        request_id: str,
        container: str,
        host: str,
        port: int,
        host_is_ip: bool,
        uid: int | None,
        comm: str | None,
        reason: str | None,
        hold_seconds: int | None,
        now: datetime,
    ) -> tuple[RequestRow, bool]:
        """File a request or coalesce onto the open row for the same key.

        Returns (row, is_new): is_new True for a fresh filing with one
        `requested` event; False for a hit on the existing `open` row with
        the same (container, host, port), which increments hit_count and
        appends a `hit` event with count 1.

        A supplied request_id that already exists but belongs to a different
        (container, host, port) raises EgressStoreError("unknown request")
        and writes nothing.
        """
        now_ts = _utc_now(now)
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                existing = self._fetch_row(request_id)
                if existing is not None:
                    if (existing.container, existing.host, existing.port) != (
                        container,
                        host,
                        port,
                    ):
                        raise EgressStoreError("unknown request")
                    if existing.status != OPEN_STATUS:
                        raise EgressStoreError(
                            f"request {request_id} already {existing.status}"
                        )
                    row = self._apply_hit(existing.request_id, now_ts)
                    is_new = False
                else:
                    open_row = self._fetch_open_by_key(container, host, port)
                    if open_row is not None:
                        row = self._apply_hit(open_row.request_id, now_ts)
                        is_new = False
                    else:
                        row = self._insert_open(
                            request_id=request_id,
                            container=container,
                            host=host,
                            port=port,
                            host_is_ip=host_is_ip,
                            uid=uid,
                            comm=comm,
                            reason=reason,
                            hold_seconds=hold_seconds,
                            opened_at=now_ts,
                        )
                        is_new = True
        self._log_boundary(
            "open_or_hit",
            row.request_id,
            row.status,
            started,
            hit_count=row.hit_count,
        )
        return row, is_new

    def _fetch_open_by_key(self, container: str, host: str, port: int) -> RequestRow | None:
        cursor = self._conn.execute(
            "SELECT %s FROM requests WHERE container = ? AND host = ? AND port = ?"
            " AND status = ?" % ", ".join(_REQUEST_COLUMNS),
            (container, host, port, OPEN_STATUS),
        )
        raw = cursor.fetchone()
        return self._row(raw) if raw is not None else None

    def _apply_hit(self, request_id: str, now_ts: datetime) -> RequestRow:
        self._conn.execute(
            "UPDATE requests SET hit_count = hit_count + 1, last_hit_at = ?"
            " WHERE request_id = ?",
            (_iso_ts(now_ts), request_id),
        )
        self._insert_event(request_id, "hit", now_ts, {"count": 1})
        row = self._fetch_row(request_id)
        assert row is not None
        return row

    def _insert_open(
        self,
        *,
        request_id: str,
        container: str,
        host: str,
        port: int,
        host_is_ip: bool,
        uid: int | None,
        comm: str | None,
        reason: str | None,
        hold_seconds: int | None,
        opened_at: datetime,
        hit_count: int = 1,
        requested_fields: dict[str, Any] | None = None,
    ) -> RequestRow:
        if requested_fields is None:
            requested_fields = self._requested_event_fields(
                container=container,
                host=host,
                port=port,
                host_is_ip=host_is_ip,
                uid=uid,
                comm=comm,
                reason=reason,
                hold_seconds=hold_seconds,
            )
        self._conn.execute(
            "INSERT INTO requests (%s) VALUES (%s)"
            % (
                ", ".join(_REQUEST_COLUMNS),
                ", ".join("?" for _ in _REQUEST_COLUMNS),
            ),
            (
                request_id,
                container,
                host,
                port,
                int(host_is_ip),
                uid,
                comm,
                reason,
                hold_seconds,
                _iso_ts(opened_at),
                _iso_ts(opened_at),
                hit_count,
                OPEN_STATUS,
                None,
                None,
                None,
                None,
                None,
                0,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        self._insert_event(request_id, "requested", opened_at, requested_fields)
        row = self._fetch_row(request_id)
        assert row is not None
        return row

    @staticmethod
    def _requested_event_fields(
        *,
        container: str,
        host: str,
        port: int,
        host_is_ip: bool,
        uid: int | None,
        comm: str | None,
        reason: str | None,
        hold_seconds: int | None,
    ) -> dict[str, Any]:
        """The `requested` event fields for a live filing — the same field
        set the broker's audit entries carry today."""
        fields: dict[str, Any] = {"container": container, "host": host, "port": port}
        if host_is_ip:
            fields["host_is_ip"] = True
        if uid is not None:
            fields["uid"] = uid
        if comm is not None:
            fields["comm"] = comm
        if reason is not None:
            fields["reason"] = reason
        if hold_seconds is not None:
            fields["hold_seconds"] = hold_seconds
        return fields

    def suppressed_hit(self, request_id: str, now: datetime) -> RequestRow:
        """Count a suppressed repeat of a denylist hit on a decided row.

        EVENT EXEMPTION — the one documented exception to the events rule:
        this method updates hit_count and last_hit_at in its own transaction
        and deliberately writes NO event, so the event count for a hot
        retry loop against a denylisted host matches the pre-store audit
        volume (one denied event per coalesce window, not per hit).

        Raises EgressStoreError for an unknown id and for any row that is
        not a `denied` row with decided_by == "denylist".
        """
        now_ts = _utc_now(now)
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                row = self._fetch_row(request_id)
                if row is None:
                    raise EgressStoreError(f"no request {request_id}")
                if row.status != "denied" or row.decided_by != "denylist":
                    raise EgressStoreError(
                        f"request {request_id} is not a denylist denial"
                        f" (status={row.status}, decided_by={row.decided_by})"
                    )
                self._conn.execute(
                    "UPDATE requests SET hit_count = hit_count + 1, last_hit_at = ?"
                    " WHERE request_id = ?",
                    (_iso_ts(now_ts), request_id),
                )
                updated = self._fetch_row(request_id)
                assert updated is not None
        self._log_boundary(
            "suppressed_hit",
            request_id,
            updated.status,
            started,
            hit_count=updated.hit_count,
        )
        return updated

    def mark_notified(self, request_id: str, now: datetime) -> None:
        """Append the `notified` event only (the row itself does not change)."""
        now_ts = _utc_now(now)
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                if self._fetch_row(request_id) is None:
                    raise EgressStoreError(f"no request {request_id}")
                self._insert_event(request_id, "notified", now_ts, {})
        self._log_boundary("mark_notified", request_id, "notified", started)

    def close(
        self,
        *,
        request_id: str,
        status: str,
        now: datetime,
        decided_by: str,
        scope: str | None = None,
        deny_reason: str | None = None,
        denylist_zone: str | None = None,
        denylist_scope: str | None = None,
        decision_body: dict[str, Any],
    ) -> RequestRow:
        """Move an `open` row to its terminal status and append that event.

        status must be one of allowed | denied | stale; decided_at,
        decided_by, scope, deny_reason, the denylist columns and
        decision_body are set as passed, last_error is cleared. Closing a
        non-open row raises and writes nothing. The event of the same name
        carries {"scope", "reason", "decided_by", "zone"} where set.
        """
        if status not in CLOSE_STATUSES:
            raise EgressStoreError(
                f"invalid close status {status!r} (must be one of {sorted(CLOSE_STATUSES)})"
            )
        now_ts = _utc_now(now)
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                row = self._fetch_row(request_id)
                if row is None:
                    raise EgressStoreError(f"no request {request_id}")
                if row.status != OPEN_STATUS:
                    raise EgressStoreError(f"request {request_id} already {row.status}")
                self._conn.execute(
                    "UPDATE requests SET status = ?, decided_at = ?, decided_by = ?,"
                    " scope = ?, deny_reason = ?, denylist_zone = ?, denylist_scope = ?,"
                    " decision_body = ?, last_error = NULL WHERE request_id = ?",
                    (
                        status,
                        _iso_ts(now_ts),
                        decided_by,
                        scope,
                        deny_reason,
                        denylist_zone,
                        denylist_scope,
                        json.dumps(decision_body, separators=(",", ":"), sort_keys=True),
                        request_id,
                    ),
                )
                event_fields: dict[str, Any] = {}
                if scope is not None:
                    event_fields["scope"] = scope
                if deny_reason is not None:
                    event_fields["reason"] = deny_reason
                event_fields["decided_by"] = decided_by
                if denylist_zone is not None:
                    event_fields["zone"] = denylist_zone
                self._insert_event(request_id, status, now_ts, event_fields)
                closed = self._fetch_row(request_id)
                assert closed is not None
        self._log_boundary("close", request_id, status, started, decided_by=decided_by)
        return closed

    def mark_apply(self, *, request_id: str, outcome: str, now: datetime) -> RequestRow:
        """Record one completed operator allow attempt.

        outcome must be applied | apply_failed | ip_requires_cidr.
        apply_attempts — the completed-attempt counter — advances here and
        nowhere else. `applied` sets apply_status, clears last_error and
        appends `applied`; the two failures set apply_status and last_error
        and append `apply_failed`, leaving status untouched (the row stays
        open; the broker closes it separately only on success).
        """
        if outcome not in APPLY_OUTCOMES:
            raise EgressStoreError(
                f"invalid apply outcome {outcome!r} (must be one of {sorted(APPLY_OUTCOMES)})"
            )
        now_ts = _utc_now(now)
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                row = self._fetch_row(request_id)
                if row is None:
                    raise EgressStoreError(f"no request {request_id}")
                attempt = row.apply_attempts + 1
                if outcome == "applied":
                    self._conn.execute(
                        "UPDATE requests SET apply_status = ?, apply_attempts = ?,"
                        " last_error = NULL WHERE request_id = ?",
                        ("applied", attempt, request_id),
                    )
                    self._insert_event(request_id, "applied", now_ts, {})
                else:
                    last_error = {
                        "reason": outcome,
                        "attempt": attempt,
                        "at": _iso_ts(now_ts),
                    }
                    self._conn.execute(
                        "UPDATE requests SET apply_status = ?, apply_attempts = ?,"
                        " last_error = ? WHERE request_id = ?",
                        (
                            outcome,
                            attempt,
                            json.dumps(last_error, separators=(",", ":"), sort_keys=True),
                            request_id,
                        ),
                    )
                    self._insert_event(
                        request_id,
                        "apply_failed",
                        now_ts,
                        {"reason": outcome, "attempt": attempt},
                    )
                updated = self._fetch_row(request_id)
                assert updated is not None
        self._log_boundary("mark_apply", request_id, updated.status, started, outcome=outcome)
        return updated

    def mark_persist(self, *, request_id: str, outcome: str, now: datetime) -> RequestRow:
        """Record the persistent-deny write outcome; no event of its own.

        outcome must be persisted | persist_failed. The `denied` event
        already carries the zone, so this only sets persist_status.
        """
        if outcome not in PERSIST_OUTCOMES:
            raise EgressStoreError(
                f"invalid persist outcome {outcome!r} (must be one of {sorted(PERSIST_OUTCOMES)})"
            )
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                if self._fetch_row(request_id) is None:
                    raise EgressStoreError(f"no request {request_id}")
                self._conn.execute(
                    "UPDATE requests SET persist_status = ? WHERE request_id = ?",
                    (outcome, request_id),
                )
                updated = self._fetch_row(request_id)
                assert updated is not None
        self._log_boundary("mark_persist", request_id, updated.status, started, outcome=outcome)
        return updated

    # -- reads -----------------------------------------------------------

    def get(self, request_id: str) -> RequestRow | None:
        with self._lock:
            return self._fetch_row(request_id)

    def list_open(self, container: str | None = None) -> list[RequestRow]:
        with self._lock:
            if container is None:
                cursor = self._conn.execute(
                    "SELECT %s FROM requests WHERE status = ?"
                    " ORDER BY opened_at, request_id" % ", ".join(_REQUEST_COLUMNS),
                    (OPEN_STATUS,),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT %s FROM requests WHERE status = ? AND container = ?"
                    " ORDER BY opened_at, request_id" % ", ".join(_REQUEST_COLUMNS),
                    (OPEN_STATUS, container),
                )
            return [self._row(raw) for raw in cursor.fetchall()]

    def list_recent(self, *, since: datetime, limit: int | None = None) -> list[RequestRow]:
        since_ts = _iso_ts(_utc_now(since))
        sql = (
            "SELECT %s FROM requests WHERE decided_at IS NOT NULL AND decided_at >= ?"
            " ORDER BY decided_at DESC, request_id DESC" % ", ".join(_REQUEST_COLUMNS)
        )
        params: list[Any] = [since_ts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            return [self._row(raw) for raw in cursor.fetchall()]

    def events_for(self, request_id: str) -> list[Event]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, request_id, kind, ts, fields FROM events"
                " WHERE request_id = ? ORDER BY id",
                (request_id,),
            )
            return [self._event(raw) for raw in cursor.fetchall()]

    def count_events(
        self,
        *,
        kind: str | None = None,
        request_id: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if request_id is not None:
            clauses.append("request_id = ?")
            params.append(request_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cursor = self._conn.execute(f"SELECT COUNT(*) FROM events{where}", tuple(params))
            return int(cursor.fetchone()[0])

    # -- migration ---------------------------------------------------------

    def import_open(self, log: Any, *, now: datetime) -> int:
        """Import still-open requests from the monthly JSONL log, once.

        `log` is an egress_log.EgressLog — the ONLY egress_log dependency in
        this module, imported lazily below so the store stays a leaf module
        otherwise. Folds the queue, fills uid/comm/reason/hit_count from the
        current month's records, and inserts each open request as an `open`
        row with a single `requested` event carrying {"imported": true}
        (plus a `hit` event with count = hit_count - 1 when hits exceed 1).

        A request known only from a carry-forward header imports with uid,
        comm, reason and hold_seconds NULL and hit_count 1 — see the module
        docstring for the accepted loss. Ids already present are skipped.
        Returns the number of rows inserted.
        """
        from egress_log import request_details_for_ids

        now_ts = _utc_now(now)
        queue = log.fold_queue(now=now_ts)
        details = request_details_for_ids(
            log, list(queue.open_requests.keys()), queue=queue, now=now_ts
        )
        started = time.monotonic()
        with self._lock:
            with self._transaction():
                inserted = 0
                for request_id in sorted(queue.open_requests):
                    if self._fetch_row(request_id) is not None:
                        continue
                    detail = details.get(request_id)
                    open_req = queue.open_requests[request_id]
                    container = detail.container if detail else open_req.container
                    host = detail.host if detail else open_req.host
                    port = detail.port if detail else open_req.port
                    if not isinstance(container, str) or not isinstance(host, str):
                        LOG.info(
                            "egress_store import_open skip request_id=%s reason=missing_fields",
                            request_id,
                        )
                        continue
                    if not isinstance(port, int) or isinstance(port, bool):
                        LOG.info(
                            "egress_store import_open skip request_id=%s reason=missing_fields",
                            request_id,
                        )
                        continue
                    opened_at = (
                        _parse_ts(open_req.opened_at)
                        if isinstance(open_req.opened_at, str) and open_req.opened_at
                        else now_ts
                    )
                    hit_count = detail.hit_count if detail else 1
                    self._insert_open(
                        request_id=request_id,
                        container=container,
                        host=host,
                        port=port,
                        host_is_ip=_is_ip_literal(host),
                        uid=detail.uid if detail else None,
                        comm=detail.comm if detail else None,
                        reason=detail.reason if detail else None,
                        hold_seconds=None,
                        opened_at=opened_at,
                        hit_count=hit_count,
                        requested_fields={"imported": True},
                    )
                    if hit_count > 1:
                        self._insert_event(
                            request_id, "hit", opened_at, {"count": hit_count - 1}
                        )
                    inserted += 1
        self._log_boundary(
            "import_open",
            "-",
            "imported",
            started,
            imported=inserted,
        )
        return inserted
