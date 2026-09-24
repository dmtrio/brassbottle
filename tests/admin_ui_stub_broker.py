#!/usr/bin/env python3
"""In-process stub of the egress broker for the admin UI behaviour suite.

Answers `GET /queue` and `POST /decide` with scripted outcomes. Every reply is
validated against the broker contract schemas (admin/contract/) with
tests/admin_contract_validator.py BEFORE it is served, so the stub cannot
drift from the real broker: a reply the schemas reject is never sent, and
`StubBroker.preflight()` refuses to start the suite on one.

Scripted outcomes (by host):
  * an IP-literal allow      -> 200, `ip_requires_cidr` in apply_failures
  * allow of m2.example.com  -> 200, `apply_failed` in apply_failures
  * anything for bad-request.example.com -> 400 `bad request from broker`
  * outage flag set          -> 500 on both routes (broker down)
  * decide_outage=<status>   -> that status on POST /decide only; GET /queue
                                stays healthy, so a page that shows a banner
                                for it did so because the decide failed
  * everything else          -> the row is decided and moves to `recent`

`GET /recent` serves the whole History store (HISTORY_ROWS older rows plus the
live `recent` list, so a row decided during a run shows up at the top) with
real keyset paging over `decided_at DESC, request_id DESC`. The fixture has a
tie block of `decided_at` values straddling the first page boundary and one row
decided exactly 30 days before the stub's clock (ARCHIVE_HOST).
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from admin_contract_validator import validate_document  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402

QUEUE_SCHEMA = "queue_snapshot.schema.json"
RECENT_SCHEMA = "recent_page.schema.json"
DECIDE_SCHEMA = "decide_response.schema.json"
ERROR_SCHEMA = "error_response.schema.json"

LONG_BOTTLE = "ci-runner-eu-west-1"   # wide enough to wrap a phone-width row
LONG_REASON = ("denylist: telemetry and advertising hosts are blocked for every bottle by the operator "
               "policy, see the egress section of the bottle manifest")   # far wider than any viewport's row
BAD_REQUEST_HOST = "bad-request.example.com"
ARCHIVE_HOST = "archive.example.com"      # decided exactly 30 days before the stub's clock
HISTORY_ROWS = 240
TIE_FIRST, TIE_COUNT = 45, 12            # older[45..56] share one decided_at, across page 1's end
RECENT_DEFAULT_LIMIT, RECENT_MAX_LIMIT = 50, 200
APPLY_FAILED_HOST = "m2.example.com"

SCOPE_OF_ACTION = {
    ("allow", "live"): "allow_live",
    ("allow", "manifest"): "allow_manifest",
    ("deny", "once"): "deny",
    ("deny", "bottle"): "deny_bottle",
    ("deny", "global"): "deny_global",
}
RECENT_SCOPE = {
    "allow_live": "live",
    "allow_manifest": "manifest",
    "deny": "once",
    "deny_bottle": "bottle",
    "deny_global": "global",
}

# (request_id, container, host, port, seconds ago it opened, hits, comm, reason,
#  last_error (reason, attempt) or None). Ages descend so the newest-first order
# is c18, a2, z3, m2, bad, a3, m1, z2, ip1, a1, z1. Alphabetical bottle order
# (alpha, mid, zeta), first-seen order (zeta, alpha, mid) and newest-bottle
# order (alpha, zeta, mid) all differ on purpose.
OPEN_ROWS = [
    ("c18", "alpha", "check18.example.com", 443, 60, 1, "node", None, None),
    ("a2", "alpha", "a2.example.com", 443, 90, 1, "ruby", None, None),
    ("z3", "zeta", "z3.example.com", 443, 120, 1, "wget", None, None),
    ("m2", "mid", APPLY_FAILED_HOST, 443, 150, 1, "python", None, ("apply_failed", 2)),
    ("bad", "mid", BAD_REQUEST_HOST, 443, 165, 1, "curl", None, None),
    ("a3", "alpha", "a3.example.com", 443, 180, 1, "go", None, None),
    ("m1", "mid", "m1.example.com", 443, 210, 4, "node", "build", None),
    ("z2", "zeta", "z2.example.com", 443, 240, 1, "wget", None, None),
    ("ip1", "alpha", "192.0.2.55", 5432, 270, 2, "psql", "db:migrate", ("ip_requires_cidr", 1)),
    ("a1", "alpha", "a1.example.com", 443, 300, 1, "pip", None, None),
    ("z1", "zeta", "z1.example.com", 443, 330, 3, "curl", "npm install", None),
]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_queue(now: datetime | None = None) -> dict[str, Any]:
    """The initial snapshot, in the broker's order (oldest open request first)."""
    now = now or datetime.now(timezone.utc)
    open_rows = []
    for rid, container, host, port, age, hits, comm, reason, error in OPEN_ROWS:
        opened = now - timedelta(seconds=age)
        open_rows.append({
            "request_id": rid, "container": container, "host": host, "port": port,
            "host_is_ip": host[0].isdigit(), "opened_at": _iso(opened), "age_seconds": age,
            "hit_count": hits, "uid": 1000, "comm": comm, "reason": reason,
            "attempt": error[1] if error else 0,
            "last_error": (
                {"reason": error[0], "attempt": error[1], "at": _iso(opened + timedelta(seconds=30))}
                if error else None
            ),
        })
    open_rows.sort(key=lambda row: row["opened_at"])

    def recent(rid, container, host, status, scope, ago, by, apply_status, reason):
        return {
            "request_id": rid, "container": container, "host": host, "port": 443, "status": status,
            "scope": scope, "decided_at": _iso(now - timedelta(seconds=ago)), "decided_by": by,
            "apply_status": apply_status, "deny_reason": reason,
        }

    return {
        "open": open_rows,
        "count": len(open_rows),
        "generated_at": _iso(now),
        "recent": [
            recent("r2", "alpha", "denied.example.com", "denied", "global", 600, "operator", None, "telemetry"),
            recent("r1", "mid", "registry.npmjs.org", "allowed", "live", 1800, "operator", "applied", None),
            recent("r3", "zeta", "ads.example.com", "denied", "global", 3600, "denylist", None, "denylist: telemetry"),
            recent("r4", LONG_BOTTLE, "telemetry.example.net", "denied", "global", 7200, "operator", None, None),
        ],
    }


def build_history(now: datetime | None = None) -> list[dict[str, Any]]:
    """Decided rows older than the queue's `recent` list, newest first.

    HISTORY_ROWS rows 37 minutes apart from four hours back, a tie block of
    TIE_COUNT rows sharing one `decided_at`, then the archive rows at 29, 30 and
    61 days. Ids are zero padded so `request_id DESC` orders the tie block.
    """
    now = now or datetime.now(timezone.utc)
    bottles = ["alpha", "mid", "zeta", LONG_BOTTLE]
    kinds = [
        ("allowed", "live", "operator", "applied", None),
        ("denied", "once", "operator", None, "not needed"),
        ("allowed", "manifest", "operator", "applied", None),
        ("denied", "global", "denylist", None, LONG_REASON),
        ("denied", "bottle", "operator", None, None),
        ("stale", None, "sweep", None, "stale"),
    ]
    rows = []
    tie_at = now - timedelta(hours=4, minutes=37 * TIE_FIRST)
    for i in range(HISTORY_ROWS):
        status, scope, by, applied, reason = kinds[i % len(kinds)]
        in_tie = TIE_FIRST <= i < TIE_FIRST + TIE_COUNT
        when = tie_at if in_tie else now - timedelta(hours=4, minutes=37 * i)
        rows.append({
            "request_id": f"h{i:04d}", "container": bottles[i % len(bottles)],
            "host": f"history-{i:03d}.example.com", "port": 443, "status": status,
            "scope": scope, "decided_at": _iso(when), "decided_by": by,
            "apply_status": applied, "deny_reason": reason,
        })
    for rid, host, days in (("h9001", "twentynine.example.com", 29), ("h9002", ARCHIVE_HOST, 30),
                            ("h9003", "old.example.com", 61)):
        rows.append({
            "request_id": rid, "container": "alpha", "host": host, "port": 443,
            "status": "allowed", "scope": "live", "decided_at": _iso(now - timedelta(days=days)),
            "decided_by": "operator", "apply_status": "applied", "deny_reason": None,
        })
    return rows


def recent_reply(rows: list[dict[str, Any]], query: str) -> tuple[int, dict[str, Any]]:
    """The broker's `GET /recent` over `rows`: keyset paging, filters, limit clamp.

    Written against the contract, not by calling the real broker, so the
    behaviour suite still runs on a bare stub. 400 on a malformed cursor, date
    or limit, like the broker.
    """
    from urllib.parse import parse_qs
    q = {k: v[0] for k, v in parse_qs(query).items() if v and v[0] != ""}
    try:
        limit = max(1, min(RECENT_MAX_LIMIT, int(q.get("limit", RECENT_DEFAULT_LIMIT))))
        before = None
        if "before" in q:
            ts, comma, rid = q["before"].partition(",")
            datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            if not comma or not rid:
                raise ValueError("cursor")
            before = (ts, rid)
        bounds = {}
        for name in ("since", "until"):
            if name in q:
                moment = datetime.fromisoformat(q[name].replace("Z", "+00:00"))
                if moment.tzinfo is None:  # the real broker reads an offset-less bound as UTC, not local time
                    moment = moment.replace(tzinfo=timezone.utc)
                bounds[name] = _iso(moment.astimezone(timezone.utc))
    except (ValueError, OverflowError):
        return 400, {"error": "invalid query"}
    picked = [
        r for r in rows
        if (not q.get("container") or r["container"] == q["container"])
        and ("since" not in bounds or r["decided_at"] >= bounds["since"])
        and ("until" not in bounds or r["decided_at"] <= bounds["until"])
        and (before is None or (r["decided_at"], r["request_id"]) < before)
    ]
    picked.sort(key=lambda r: (r["decided_at"], r["request_id"]), reverse=True)
    page = picked[:limit]
    nxt = f"{page[-1]['decided_at']},{page[-1]['request_id']}" if len(page) >= limit else None
    return 200, {"rows": page, "next": nxt}


class ContractViolation(AssertionError):
    """A stub reply the broker contract schemas reject."""


def _checked(body: dict[str, Any], schema: str) -> dict[str, Any]:
    errors = validate_document(body, schema)
    if errors:
        raise ContractViolation(f"stub reply violates {schema}: {errors[:3]}")
    return body


def decide_reply(queue: dict[str, Any], body: dict[str, Any], *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    """Apply one /decide to `queue` (mutating it) and return (status, body)."""
    now = now or datetime.now(timezone.utc)
    action = SCOPE_OF_ACTION.get((body.get("decision"), body.get("scope")))
    host, container = body.get("host"), body.get("container")
    if action is None:
        return 400, {"error": "invalid decision"}
    if host == BAD_REQUEST_HOST:
        return 400, {"error": "bad request from broker"}
    match = next(
        (row for row in queue["open"]
         if row["host"] == host and (action == "deny_global" or row["container"] == container)),
        None,
    )
    if match is None:
        return 200, {"decided": [], "apply_failures": []} if action.startswith("allow") else {"decided": []}
    rid = match["request_id"]
    if action == "allow_live" and match["host_is_ip"]:
        return 200, {"decided": [], "apply_failures": [{"request_id": rid, "reason": "ip_requires_cidr"}]}
    if action == "allow_live" and host == APPLY_FAILED_HOST:
        return 200, {"decided": [], "apply_failures": [{"request_id": rid, "reason": "apply_failed"}]}

    queue["open"].remove(match)
    queue["count"] = len(queue["open"])
    allowed = action.startswith("allow")
    queue["recent"].insert(0, {
        "request_id": rid, "container": match["container"], "host": host, "port": match["port"],
        "status": "allowed" if allowed else "denied", "scope": RECENT_SCOPE[action],
        "decided_at": _iso(now), "decided_by": "operator",
        "apply_status": "applied" if allowed else None,
        "deny_reason": None if allowed else body.get("reason"),
    })
    if action in ("deny_bottle", "deny_global"):
        return 200, {"decided": [rid], "persisted": {
            "zone": host, "scope": container if action == "deny_bottle" else "global"}}
    if allowed:
        return 200, {"decided": [rid], "apply_failures": []}
    return 200, {"decided": [rid]}


class HeldReply:
    """A `/recent` reply the stub computes at once but sends only when released."""

    def __init__(self) -> None:
        self._release = threading.Event()
        self.sent = threading.Event()

    def release(self) -> None:
        self._release.set()

    def wait_released(self, timeout: float = 15.0) -> None:
        self._release.wait(timeout)


class StubBroker:
    """The stub server plus its scripted state. Thread-safe."""

    def __init__(self, log: Callable[[str], None] = lambda message: None):
        self.log = log
        self.lock = threading.Lock()
        self.queue = build_queue()
        self.history = build_history()
        self.recent_queries: list[str] = []
        self.recent_hold: HeldReply | None = None
        self.decides: list[dict[str, Any]] = []
        self.outage = False
        self.decide_outage: int | None = None
        self.violations: list[str] = []
        self.served = 0
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                broker.served += 1
                broker.log(f"stub reply path={self.path} status={status} bytes={len(raw)}")

            def _serve(self, status: int, body: dict[str, Any], schema: str) -> None:
                try:
                    _checked(body, schema)
                except ContractViolation as exc:
                    broker.violations.append(str(exc))
                    return self._send(500, {"error": "stub contract violation"})
                self._send(status, body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/recent":
                    with broker.lock:
                        broker.recent_queries.append(query)
                        held, broker.recent_hold = broker.recent_hold, None
                        if broker.outage:
                            status, reply, schema = 500, {"error": "broker down"}, ERROR_SCHEMA
                        else:
                            status, reply = recent_reply(broker.history_rows(), query)
                            schema = ERROR_SCHEMA if status >= 400 else RECENT_SCHEMA
                    if held:
                        held.wait_released()   # outside the lock, so later requests are served meanwhile
                    self._serve(status, reply, schema)
                    if held:
                        held.sent.set()
                    return
                if self.path != "/queue":
                    return self._send(404, {"error": "not found"})
                with broker.lock:
                    if broker.outage:
                        return self._serve(500, {"error": "broker down"}, ERROR_SCHEMA)
                    self._serve(200, broker.queue_body(), QUEUE_SCHEMA)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/decide":
                    return self._send(404, {"error": "not found"})
                with broker.lock:
                    broker.decides.append(body)
                    if broker.outage:
                        return self._serve(500, {"error": "broker down"}, ERROR_SCHEMA)
                    if broker.decide_outage is not None:
                        return self._serve(broker.decide_outage, {"error": "decide unavailable"}, ERROR_SCHEMA)
                    status, reply = decide_reply(broker.queue, body)
                    self._serve(status, reply, ERROR_SCHEMA if status >= 400 else DECIDE_SCHEMA)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def queue_body(self) -> dict[str, Any]:
        """The /queue reply. The one place a snapshot is built for serving."""
        return self.queue

    def hold_next_recent(self) -> HeldReply:
        """Hold the reply to the next `GET /recent` until `.release()`; later ones are not held."""
        held = HeldReply()
        with self.lock:
            self.recent_hold = held
        return held

    def history_rows(self) -> list[dict[str, Any]]:
        """Every decided row: the live `recent` list (decided during the run
        included) plus the fixed older store, each request once."""
        live = {row["request_id"]: row for row in self.queue["recent"]}
        return list(live.values()) + [r for r in self.history if r["request_id"] not in live]

    def reset(self) -> None:
        with self.lock:
            self.queue = build_queue()
            self.history = build_history()
            self.recent_queries = []
            if self.recent_hold:
                self.recent_hold.release()
            self.recent_hold = None
            self.decides = []
            self.outage = False
            self.decide_outage = None

    def preflight(self) -> None:
        """Validate every reply shape the stub can produce; raise before serving."""
        with self.lock:
            _checked(self.queue_body(), QUEUE_SCHEMA)
            for query in ("", "limit=50", "limit=500&container=alpha", "before=x", "since=nope"):
                status, reply = recent_reply(self.history_rows(), query)
                _checked(reply, ERROR_SCHEMA if status >= 400 else RECENT_SCHEMA)
            _checked({"error": "broker down"}, ERROR_SCHEMA)
            _checked({"error": "decide unavailable"}, ERROR_SCHEMA)
            scratch = copy.deepcopy(self.queue)
            for payload in (
                {"decision": "allow", "scope": "live", "host": "a1.example.com", "container": "alpha"},
                {"decision": "allow", "scope": "live", "host": "192.0.2.55", "container": "alpha"},
                {"decision": "allow", "scope": "live", "host": APPLY_FAILED_HOST, "container": "mid"},
                {"decision": "allow", "scope": "manifest", "host": "a2.example.com", "container": "alpha"},
                {"decision": "deny", "scope": "once", "host": "m1.example.com", "container": "mid"},
                {"decision": "deny", "scope": "bottle", "host": "z2.example.com", "container": "zeta", "reason": "r"},
                {"decision": "deny", "scope": "global", "host": "z3.example.com"},
                {"decision": "deny", "scope": "once", "host": BAD_REQUEST_HOST, "container": "mid"},
            ):
                status, reply = decide_reply(scratch, payload)
                _checked(reply, ERROR_SCHEMA if status >= 400 else DECIDE_SCHEMA)
                _checked(scratch, QUEUE_SCHEMA)

    def start(self) -> str:
        self.thread.start()
        wait_for_tcp_listening(*self.server.server_address)
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        join_thread_or_fail(self.thread, label="stub broker")
