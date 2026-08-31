#!/usr/bin/env python3
"""egress_log.py — append-only audit log and queue-state fold for egress approval.

Owns everything under the caller-supplied root ($BASE_PATH/run/egress): monthly
JSONL logs and rotation carry_forward headers.
Stdlib only; host-side (macOS and Linux).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

LOG = logging.getLogger(__name__)

EVENT_KINDS = frozenset(
    {
        "requested",
        "notified",
        "hit",
        "allowed",
        "denied",
        "applied",
        "apply_failed",
    }
)
CARRY_FORWARD_KIND = "carry_forward"
CLOSING_KINDS = frozenset({"allowed", "denied"})
OPEN_STATE_KINDS = frozenset({"requested", "notified", "hit"})


class EgressLogError(Exception):
    """Invalid egress log operation or corrupted on-disk state."""


@dataclass(frozen=True)
class OpenRequest:
    """One still-open egress approval request (queue membership only).

    container/host/port/opened_at describe the pending ask, not permitted egress.
    Closed requests (allowed/denied) are removed from the fold; the log must never
    answer "is host X permitted" — ipset allowed-domains is the sole authority.
    """

    request_id: str
    state: str
    container: str | None = None
    host: str | None = None
    port: int | None = None
    opened_at: str | None = None


@dataclass(frozen=True)
class QueueState:
    """Fold result: open approval requests keyed by request_id.

    Open entries may carry the host they are asking about; closed requests leave
    no host trace — see fold_queue() and OpenRequest.
    """

    open_requests: Mapping[str, OpenRequest]


@dataclass(frozen=True)
class RequestDetails:
    """Operator-facing fields for one open egress request."""

    request_id: str
    container: str
    host: str
    port: int
    hit_count: int
    uid: int | None = None
    comm: str | None = None
    reason: str | None = None


def _month_records(log: "EgressLog", when: datetime) -> list[dict]:
    path = _log_path(log.root, _month_filename(when))
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


_META_KEYS = ("container", "host", "port", "uid", "comm", "reason")


def request_details_for_ids(
    log: "EgressLog",
    request_ids: Iterable[str],
    *,
    queue: QueueState,
    now: datetime,
) -> dict[str, RequestDetails]:
    """Build RequestDetails for many requests in ONE pass over the log.

    The per-request helper folds the queue AND re-reads the whole monthly
    audit log every call, so asking for details one id at a time makes a poll
    O(open requests x log size) — with a backlogged queue and a large month
    that delays the very prompt and notifications this loop exists to deliver.
    Callers that already hold a folded `queue` pass it in and get every
    request's details for a single read.

    Ids that are not open (or whose records are unusable) are simply absent
    from the result.
    """
    wanted = {rid for rid in request_ids if rid in queue.open_requests}
    if not wanted:
        return {}

    meta: dict[str, dict[str, object]] = {rid: {} for rid in wanted}
    hit_counts: dict[str, int] = {rid: 1 for rid in wanted}
    for record in _month_records(log, now):
        request_id = record.get("request_id")
        if request_id not in wanted:
            continue
        kind = record.get("kind")
        if kind == "requested":
            for key in _META_KEYS:
                if key in record:
                    meta[request_id][key] = record[key]
        elif kind == "hit":
            count = record.get("count", 1)
            if isinstance(count, int) and count > 0:
                hit_counts[request_id] += count

    details: dict[str, RequestDetails] = {}
    for request_id in wanted:
        built = _build_details(
            request_id,
            meta[request_id],
            hit_counts[request_id],
            queue.open_requests[request_id],
        )
        if built is not None:
            details[request_id] = built
    return details


def _build_details(
    request_id: str,
    meta: dict[str, object],
    hit_count: int,
    open_req: OpenRequest,
) -> RequestDetails | None:
    """Reconcile folded queue state with the request's own log records."""
    container = meta.get("container", open_req.container)
    host = meta.get("host", open_req.host)
    port = meta.get("port", open_req.port)
    if not isinstance(container, str) or not isinstance(host, str) or not isinstance(port, int):
        return None

    uid = meta.get("uid")
    comm = meta.get("comm")
    reason = meta.get("reason")
    return RequestDetails(
        request_id=request_id,
        container=container,
        host=host,
        port=port,
        hit_count=hit_count,
        uid=uid if isinstance(uid, int) else None,
        comm=comm if isinstance(comm, str) else None,
        reason=reason if isinstance(reason, str) else None,
    )


def request_details_from_log(
    log: "EgressLog",
    request_id: str,
    *,
    now: datetime | None = None,
) -> RequestDetails | None:
    """Fold request metadata and hit count from the monthly audit log.

    Single-request convenience wrapper; a caller handling several requests
    should fold once and use request_details_for_ids instead.
    """
    when = now or datetime.now(timezone.utc)
    queue = log.fold_queue(now=when)
    return request_details_for_ids(log, [request_id], queue=queue, now=when).get(
        request_id
    )


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso_ts(dt: datetime) -> str:
    return _utc_now(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_filename(dt: datetime) -> str:
    utc = _utc_now(dt)
    return f"{utc.year:04d}-{utc.month:02d}.jsonl"


def _prior_month(dt: datetime) -> datetime:
    utc = _utc_now(dt)
    if utc.month == 1:
        return datetime(utc.year - 1, 12, 1, tzinfo=timezone.utc)
    return datetime(utc.year, utc.month - 1, 1, tzinfo=timezone.utc)


def _log_dir(root: Path) -> Path:
    return root / "log"


def _log_path(root: Path, filename: str) -> Path:
    return _log_dir(root) / filename



def _ensure_log_dir(root: Path) -> None:
    try:
        _log_dir(root).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EgressLogError(f"cannot create log directory {_log_dir(root)}: {exc}") from exc


def _validate_kind(kind: str) -> None:
    if kind not in EVENT_KINDS:
        raise EgressLogError(f"unknown event kind {kind!r}")


def _merge_open_fields(
    record: dict[str, Any],
    existing: OpenRequest | None,
    *,
    kind: str,
) -> tuple[str | None, str | None, int | None, str | None]:
    container = record.get("container")
    host = record.get("host")
    port = record.get("port")
    if not isinstance(container, str):
        container = existing.container if existing else None
    if not isinstance(host, str):
        host = existing.host if existing else None
    if not isinstance(port, int):
        port = existing.port if existing else None

    if kind == "requested" and isinstance(record.get("ts"), str):
        opened_at = record["ts"]
    elif isinstance(record.get("opened_at"), str):
        opened_at = record["opened_at"]
    elif existing is not None:
        opened_at = existing.opened_at
    else:
        opened_at = None
    return container, host, port, opened_at


def _apply_record(
    open_map: dict[str, OpenRequest],
    record: dict[str, Any],
    *,
    index: int,
) -> None:
    kind = record.get("kind")
    if kind == CARRY_FORWARD_KIND:
        if index != 0:
            raise EgressLogError(
                f"carry_forward record must be first line of log file (got line {index + 1})"
            )
        open_map.clear()
        for item in record.get("open") or []:
            if not isinstance(item, dict):
                continue
            request_id = item.get("request_id") or item.get("id")
            state = item.get("state")
            if not isinstance(request_id, str) or not isinstance(state, str):
                continue
            container = item.get("container")
            host = item.get("host")
            port = item.get("port")
            opened_at = item.get("opened_at")
            open_map[request_id] = OpenRequest(
                request_id=request_id,
                state=state,
                container=container if isinstance(container, str) else None,
                host=host if isinstance(host, str) else None,
                port=port if isinstance(port, int) else None,
                opened_at=opened_at if isinstance(opened_at, str) else None,
            )
        return

    if kind not in EVENT_KINDS:
        raise EgressLogError(f"unknown event kind {kind!r}")

    request_id = record.get("request_id")
    if not isinstance(request_id, str):
        raise EgressLogError("event record missing request_id")

    if kind in CLOSING_KINDS:
        open_map.pop(request_id, None)
        return

    if kind in OPEN_STATE_KINDS:
        existing = open_map.get(request_id)
        container, host, port, opened_at = _merge_open_fields(
            record,
            existing,
            kind=kind,
        )
        open_map[request_id] = OpenRequest(
            request_id=request_id,
            state=kind,
            container=container,
            host=host,
            port=port,
            opened_at=opened_at,
        )
        return

    # applied / apply_failed — audit-only; queue membership unchanged.


def _fold_records(records: list[dict[str, Any]]) -> QueueState:
    # SECURITY INVARIANT: this fold tracks queue membership (open vs answered
    # requests) only. It must never accumulate allowed hosts or expose egress
    # permit predicates — ipset allowed-domains is the sole authority.
    open_map: dict[str, OpenRequest] = {}
    for index, record in enumerate(records):
        _apply_record(open_map, record, index=index)
    return QueueState(open_requests=dict(open_map))


def _parse_jsonl_lines(
    data: bytes,
    *,
    path: Path,
) -> tuple[list[dict[str, Any]], int]:
    """Parse JSONL bytes; discard an unparsable final line only."""
    if not data:
        return [], 0

    text = data.decode("utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    records: list[dict[str, Any]] = []
    skipped = 0
    for index, line in enumerate(lines):
        if not line:
            if index < len(lines) - 1:
                raise EgressLogError(f"empty line in log {path} at line {index + 1}")
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                skipped = 1
                LOG.info(
                    "egress_log read discarded torn trailing line path=%s line=%d",
                    path.name,
                    index + 1,
                )
                break
            raise EgressLogError(
                f"corrupt log line in {path} at line {index + 1}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise EgressLogError(
                f"corrupt log line in {path} at line {index + 1}: not a JSON object"
            )
        records.append(parsed)
    return records, skipped


def _read_file_records(
    path: Path,
    *,
    offset: int,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Read records from one log file starting at byte offset."""
    LOG.debug(
        "egress_log read enter path=%s offset=%d",
        path.name,
        offset,
    )
    if not path.is_file():
        LOG.debug(
            "egress_log read exit path=%s records=0 skipped=0 offset=%d",
            path.name,
            offset,
        )
        return [], offset, 0, 0

    size = path.stat().st_size
    if offset > size:
        LOG.debug(
            "egress_log read exit path=%s records=0 skipped=0 offset=0 replay=full",
            path.name,
        )
        return _read_file_records(path, offset=0)

    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()

    records, skipped = _parse_jsonl_lines(data, path=path)
    new_offset = offset + len(data)
    LOG.debug(
        "egress_log read exit path=%s records=%d skipped=%d offset=%d",
        path.name,
        len(records),
        skipped,
        new_offset,
    )
    return records, new_offset, len(records), skipped




def _append_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd is not None:
            os.close(fd)


def _ensure_month_file(root: Path, ts: datetime) -> None:
    """Create the monthly log with a carry_forward header when missing (writer path)."""
    _ensure_log_dir(root)
    target_name = _month_filename(ts)
    target_path = _log_path(root, target_name)
    if target_path.exists():
        return

    prior_path = _log_path(root, _month_filename(_prior_month(ts)))
    if prior_path.is_file():
        prior_records, _, _, _ = _read_file_records(prior_path, offset=0)
        open_map = dict(_fold_records(prior_records).open_requests)
    else:
        open_map = {}

    LOG.info(
        "egress_log ensure_month_file creating file=%s open=%d",
        target_name,
        len(open_map),
    )
    _write_carry_forward(
        root,
        target_file=target_name,
        open_map=open_map,
        ts=ts,
    )


def _write_carry_forward(
    root: Path,
    *,
    target_file: str,
    open_map: Mapping[str, OpenRequest],
    ts: datetime,
) -> None:
    open_list: list[dict[str, Any]] = []
    for req in sorted(open_map.values(), key=lambda item: item.request_id):
        entry: dict[str, Any] = {"request_id": req.request_id, "state": req.state}
        if req.container is not None:
            entry["container"] = req.container
        if req.host is not None:
            entry["host"] = req.host
        if req.port is not None:
            entry["port"] = req.port
        if req.opened_at is not None:
            entry["opened_at"] = req.opened_at
        open_list.append(entry)
    record = {
        "ts": _iso_ts(ts),
        "kind": CARRY_FORWARD_KIND,
        "open": open_list,
    }
    line = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    path = _log_path(root, target_file)
    _append_bytes(path, line)


class EgressLog:
    """Append-only egress approval log with queue-state fold."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def append(
        self,
        kind: str,
        request_id: str,
        *,
        ts: datetime | None = None,
        **fields: Any,
    ) -> None:
        """Append one audit event (never secrets or personal data in fields)."""
        LOG.info(
            "egress_log append enter kind=%s request_id=%s",
            kind,
            request_id,
        )
        _validate_kind(kind)
        if not request_id:
            raise EgressLogError("request_id must not be empty")

        event_ts = _utc_now(ts)
        record: dict[str, Any] = {
            "ts": _iso_ts(event_ts),
            "kind": kind,
            "request_id": request_id,
        }
        for key, value in fields.items():
            if key in ("ts", "kind", "request_id"):
                continue
            record[key] = value

        _ensure_month_file(self._root, event_ts)
        target_name = _month_filename(event_ts)
        target_path = _log_path(self._root, target_name)

        line = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        _append_bytes(target_path, line)
        LOG.info(
            "egress_log append exit kind=%s request_id=%s file=%s",
            kind,
            request_id,
            target_name,
        )

    def fold_queue(self, *, now: datetime | None = None) -> QueueState:
        """Replay the current monthly log (never prior months) into queue state."""
        event_now = _utc_now(now)
        current_name = _month_filename(event_now)
        _ensure_month_file(self._root, event_now)
        path = _log_path(self._root, current_name)

        # The fold is stateless: it always replays the whole current file
        # (carry_forward header plus this month's events). A cursor was carried
        # here for a while, but nothing ever read from it — the offset was
        # consulted only to decide which log line to print, and being AT eof
        # (the normal, fully-caught-up state) was reported as a fallback. Since
        # the watcher folds on every poll, that produced several lines a second
        # of misleading noise for something that optimised nothing. Replay of a
        # month's file is milliseconds; if an incremental reader is ever added,
        # reintroduce the cursor then, and make it actually seek.
        records, _new_offset, parsed, skipped = _read_file_records(path, offset=0)

        state = _fold_records(records)

        # DEBUG, not INFO: this runs on every watcher poll. It is a read, not a
        # handoff, so it does not carry the boundary-logging requirement.
        LOG.debug(
            "egress_log fold file=%s records=%d skipped=%d open=%d",
            current_name,
            parsed,
            skipped,
            len(state.open_requests),
        )
        return state
