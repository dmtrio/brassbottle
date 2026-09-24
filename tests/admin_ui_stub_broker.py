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
  * everything else          -> the row is decided and moves to `recent`
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
DECIDE_SCHEMA = "decide_response.schema.json"
ERROR_SCHEMA = "error_response.schema.json"

BAD_REQUEST_HOST = "bad-request.example.com"
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
        ],
    }


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


class StubBroker:
    """The stub server plus its scripted state. Thread-safe."""

    def __init__(self, log: Callable[[str], None] = lambda message: None):
        self.log = log
        self.lock = threading.Lock()
        self.queue = build_queue()
        self.decides: list[dict[str, Any]] = []
        self.outage = False
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
                    status, reply = decide_reply(broker.queue, body)
                    self._serve(status, reply, ERROR_SCHEMA if status >= 400 else DECIDE_SCHEMA)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def queue_body(self) -> dict[str, Any]:
        """The /queue reply. The one place a snapshot is built for serving."""
        return self.queue

    def reset(self) -> None:
        with self.lock:
            self.queue = build_queue()
            self.decides = []
            self.outage = False

    def preflight(self) -> None:
        """Validate every reply shape the stub can produce; raise before serving."""
        with self.lock:
            _checked(self.queue_body(), QUEUE_SCHEMA)
            _checked({"error": "broker down"}, ERROR_SCHEMA)
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
