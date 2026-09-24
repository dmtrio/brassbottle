#!/usr/bin/env python3
"""Unit tests for the SQLite egress request store (queue + audit)."""
from __future__ import annotations
import json
import random
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import egress_store  # noqa: E402
from egress_store import _iso_ts  # noqa: E402
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
NOW_PLUS_1 = NOW + timedelta(seconds=1)
NOW_PLUS_2 = NOW + timedelta(seconds=2)
MAY = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
AUG_END = datetime(2026, 8, 31, 23, 0, 0, tzinfo=timezone.utc)
SEP_START = datetime(2026, 9, 1, 0, 5, 0, tzinfo=timezone.utc)
def _open_kwargs(**overrides):
    kwargs = {
        "request_id": "req-1",
        "container": "coding-brassbottle",
        "host": "docs.stripe.com",
        "port": 443,
        "host_is_ip": False,
        "uid": 1000,
        "comm": "curl",
        "reason": "api docs",
        "hold_seconds": 90,
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs

class EgressStoreTests(unittest.TestCase):

    def _store(self, root: Path) -> egress_store.EgressStore:
        return egress_store.EgressStore(root)
    # -- 1. schema and on-disk state -------------------------------------

    def test_fresh_root_creates_wal_db_with_schema_version_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            self.addCleanup(store.shutdown)
            db = root / "egress.db"
            self.assertTrue(db.is_file())
            probe = sqlite3.connect(db)
            try:
                self.assertEqual(probe.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(
                    probe.execute("SELECT version FROM schema_version").fetchall(),
                    [(2,)],
                )
                columns = [
                    row[1] for row in probe.execute("PRAGMA table_info(requests)").fetchall()
                ]
                self.assertEqual(
                    columns,
                    [
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
                    ],
                )
            finally:
                probe.close()

    def test_schema_pragma_pins_literal_columns_constraints_and_fk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            self.assertEqual(
                [
                    (row[1], row[2], row[3], row[5])
                    for row in store._conn.execute("PRAGMA table_info(requests)")
                ],
                [
                    ("request_id", "TEXT", 0, 1),
                    ("container", "TEXT", 1, 0),
                    ("host", "TEXT", 1, 0),
                    ("port", "INTEGER", 1, 0),
                    ("host_is_ip", "INTEGER", 1, 0),
                    ("uid", "INTEGER", 0, 0),
                    ("comm", "TEXT", 0, 0),
                    ("reason", "TEXT", 0, 0),
                    ("hold_seconds", "INTEGER", 0, 0),
                    ("opened_at", "TEXT", 1, 0),
                    ("last_hit_at", "TEXT", 1, 0),
                    ("hit_count", "INTEGER", 1, 0),
                    ("status", "TEXT", 1, 0),
                    ("scope", "TEXT", 0, 0),
                    ("decided_at", "TEXT", 0, 0),
                    ("decided_by", "TEXT", 0, 0),
                    ("deny_reason", "TEXT", 0, 0),
                    ("apply_status", "TEXT", 0, 0),
                    ("apply_attempts", "INTEGER", 1, 0),
                    ("last_error", "TEXT", 0, 0),
                    ("persist_status", "TEXT", 0, 0),
                    ("denylist_zone", "TEXT", 0, 0),
                    ("denylist_scope", "TEXT", 0, 0),
                    ("decision_body", "TEXT", 0, 0),
                ],
            )
            self.assertEqual(
                [
                    (row[1], row[2], row[3], row[5])
                    for row in store._conn.execute("PRAGMA table_info(events)")
                ],
                [
                    ("id", "INTEGER", 0, 1),
                    ("request_id", "TEXT", 1, 0),
                    ("kind", "TEXT", 1, 0),
                    ("ts", "TEXT", 1, 0),
                    ("fields", "TEXT", 0, 0),
                ],
            )
            self.assertEqual(
                [
                    (row[2], row[3], row[4])
                    for row in store._conn.execute("PRAGMA foreign_key_list(events)")
                ],
                [("requests", "request_id", None)],
            )
            self.assertEqual(store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_reopening_does_not_recreate_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.open_or_hit(**_open_kwargs())
            store.shutdown()
            reopened = self._store(root)
            self.addCleanup(reopened.shutdown)
            row = reopened.get("req-1")
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "open")
            probe = sqlite3.connect(root / "egress.db")
            try:
                self.assertEqual(
                    probe.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0], 1
                )
                self.assertEqual(
                    probe.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
                )
            finally:
                probe.close()

    def test_schema_version_3_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.open_or_hit(**_open_kwargs())
            store._conn.execute("UPDATE schema_version SET version = 3")
            store._conn.commit()
            store.shutdown()
            with self.assertRaises(egress_store.EgressStoreError):
                self._store(root)

    def test_non_sqlite_file_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "egress.db").write_bytes(b"this is definitely not a database file")
            with self.assertRaises(egress_store.EgressStoreError):
                self._store(root)
    # -- 2. open_or_hit ---------------------------------------------------

    def test_open_or_hit_new_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            row, is_new = store.open_or_hit(**_open_kwargs())
            self.assertTrue(is_new)
            self.assertEqual(row.request_id, "req-1")
            self.assertEqual(row.container, "coding-brassbottle")
            self.assertEqual(row.host, "docs.stripe.com")
            self.assertEqual(row.port, 443)
            self.assertEqual(row.host_is_ip, False)
            self.assertEqual(row.uid, 1000)
            self.assertEqual(row.comm, "curl")
            self.assertEqual(row.reason, "api docs")
            self.assertEqual(row.hold_seconds, 90)
            self.assertEqual(row.opened_at, NOW)
            self.assertEqual(row.last_hit_at, NOW)
            self.assertEqual(row.hit_count, 1)
            self.assertEqual(row.status, "open")
            self.assertIsNone(row.scope)
            self.assertIsNone(row.decided_at)
            self.assertIsNone(row.decided_by)
            self.assertIsNone(row.deny_reason)
            self.assertIsNone(row.apply_status)
            self.assertEqual(row.apply_attempts, 0)
            self.assertIsNone(row.last_error)
            self.assertIsNone(row.persist_status)
            self.assertIsNone(row.denylist_zone)
            self.assertIsNone(row.denylist_scope)
            self.assertIsNone(row.decision_body)
            events = store.events_for("req-1")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "requested")
            self.assertEqual(events[0].ts, NOW)
            self.assertEqual(
                events[0].fields,
                {
                    "container": "coding-brassbottle",
                    "host": "docs.stripe.com",
                    "port": 443,
                    "uid": 1000,
                    "comm": "curl",
                    "reason": "api docs",
                    "hold_seconds": 90,
                },
            )

    def test_open_or_hit_second_call_coalesces(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row, is_new = store.open_or_hit(
                **_open_kwargs(request_id="req-other", now=NOW_PLUS_1)
            )
            self.assertFalse(is_new)
            self.assertEqual(row.request_id, "req-1")
            self.assertEqual(row.hit_count, 2)
            self.assertEqual(row.last_hit_at, NOW_PLUS_1)
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-1")],
                [("requested", _open_kwargs_fields()), ("hit", {"count": 1})],
            )

    def test_open_or_hit_replayed_id_for_other_key_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            events_before = store.count_events()
            rows_before = store.list_open()
            with self.assertRaises(egress_store.EgressStoreError):
                store.open_or_hit(
                    **_open_kwargs(
                        request_id="req-1",
                        container="other-bottle",
                        host="api.github.com",
                        port=22,
                    )
                )
            self.assertEqual(store.count_events(), events_before)
            self.assertEqual(store.list_open(), rows_before)

    def test_open_or_hit_open_row_by_key_coalesces_regardless_of_supplied_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row, is_new = store.open_or_hit(
                **_open_kwargs(request_id="fresh-id", now=NOW_PLUS_1)
            )
            self.assertFalse(is_new)
            self.assertEqual(row.request_id, "req-1")
            self.assertEqual(row.hit_count, 2)
    # -- 3. close ----------------------------------------------------------

    def test_close_to_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            body = {"decision": "allow", "scope": "live"}
            closed = store.close(
                request_id="req-1",
                status="allowed",
                now=NOW_PLUS_2,
                decided_by="admin",
                scope="live",
                decision_body=body,
            )
            self.assertEqual(closed.status, "allowed")
            self.assertEqual(closed.decided_at, NOW_PLUS_2)
            self.assertEqual(closed.decided_by, "admin")
            self.assertEqual(closed.scope, "live")
            self.assertEqual(closed.decision_body, body)
            self.assertIsNone(closed.last_error)
            self.assertIsNone(closed.deny_reason)
            self.assertIsNone(closed.denylist_zone)
            self.assertIsNone(closed.denylist_scope)
            self.assertEqual(store.list_open(), [])
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-1")],
                [
                    ("requested", _open_kwargs_fields()),
                    ("allowed", {"scope": "live", "decided_by": "admin"}),
                ],
            )

    def test_close_to_denied_with_denylist_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            body = {"decision": "deny", "reason": "denylist", "zone": "evil.example"}
            closed = store.close(
                request_id="req-1",
                status="denied",
                now=NOW_PLUS_2,
                decided_by="denylist",
                deny_reason="denylist",
                denylist_zone="evil.example",
                denylist_scope="global",
                decision_body=body,
            )
            self.assertEqual(closed.status, "denied")
            self.assertEqual(closed.decided_at, NOW_PLUS_2)
            self.assertEqual(closed.decided_by, "denylist")
            self.assertIsNone(closed.scope)
            self.assertEqual(closed.deny_reason, "denylist")
            self.assertEqual(closed.denylist_zone, "evil.example")
            self.assertEqual(closed.denylist_scope, "global")
            self.assertEqual(closed.decision_body, body)
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-1")],
                [
                    ("requested", _open_kwargs_fields()),
                    (
                        "denied",
                        {
                            "reason": "denylist",
                            "decided_by": "denylist",
                            "zone": "evil.example",
                        },
                    ),
                ],
            )

    def test_close_to_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            body = {"decision": "deny", "reason": "stale"}
            closed = store.close(
                request_id="req-1",
                status="stale",
                now=NOW_PLUS_2,
                decided_by="sweep",
                deny_reason="stale",
                decision_body=body,
            )
            self.assertEqual(closed.status, "stale")
            self.assertEqual(closed.decided_by, "sweep")
            self.assertEqual(closed.decision_body, body)
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-1")],
                [
                    ("requested", _open_kwargs_fields()),
                    ("stale", {"reason": "stale", "decided_by": "sweep"}),
                ],
            )

    def test_close_on_closed_row_raises_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            store.close(
                request_id="req-1",
                status="allowed",
                now=NOW_PLUS_1,
                decided_by="admin",
                decision_body={"decision": "allow", "scope": "live"},
            )
            count_before = store.count_events()
            row_before = store.get("req-1")
            with self.assertRaises(egress_store.EgressStoreError):
                store.close(
                    request_id="req-1",
                    status="denied",
                    now=NOW_PLUS_2,
                    decided_by="cli",
                    decision_body={"decision": "deny"},
                )
            self.assertEqual(store.count_events(), count_before)
            self.assertEqual(store.get("req-1"), row_before)

    def test_close_invalid_status_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            with self.assertRaises(egress_store.EgressStoreError):
                store.close(
                    request_id="req-1",
                    status="open",
                    now=NOW_PLUS_1,
                    decided_by="cli",
                    decision_body={"decision": "deny"},
                )
    # -- 4. mark_apply -----------------------------------------------------

    def test_mark_apply_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row = store.mark_apply(request_id="req-1", outcome="applied", now=NOW_PLUS_1)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "applied")
            self.assertEqual(row.apply_attempts, 1)
            self.assertIsNone(row.last_error)
            self.assertEqual(
                [event.kind for event in store.events_for("req-1")],
                ["requested", "applied"],
            )

    def test_mark_apply_failure_records_attempt_and_keeps_row_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row = store.mark_apply(
                request_id="req-1", outcome="apply_failed", now=NOW_PLUS_1
            )
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "apply_failed")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(
                row.last_error,
                {"reason": "apply_failed", "attempt": 1, "at": "2026-09-23T12:00:01Z"},
            )
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-1")],
                [
                    ("requested", _open_kwargs_fields()),
                    ("apply_failed", {"reason": "apply_failed", "attempt": 1}),
                ],
            )

    def test_mark_apply_ip_requires_cidr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row = store.mark_apply(
                request_id="req-1", outcome="ip_requires_cidr", now=NOW_PLUS_1
            )
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "ip_requires_cidr")
            self.assertEqual(row.apply_attempts, 1)
            self.assertEqual(
                row.last_error,
                {"reason": "ip_requires_cidr", "attempt": 1, "at": "2026-09-23T12:00:01Z"},
            )
            self.assertEqual(
                [
                    (event.kind, event.fields)
                    for event in store.events_for("req-1")
                    if event.kind == "apply_failed"
                ],
                [("apply_failed", {"reason": "ip_requires_cidr", "attempt": 1})],
            )

    def test_mark_apply_failure_then_success_clears_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            store.mark_apply(request_id="req-1", outcome="apply_failed", now=NOW_PLUS_1)
            row = store.mark_apply(request_id="req-1", outcome="applied", now=NOW_PLUS_2)
            self.assertEqual(row.apply_attempts, 2)
            self.assertEqual(row.apply_status, "applied")
            self.assertIsNone(row.last_error)
            self.assertEqual(
                [event.kind for event in store.events_for("req-1")],
                ["requested", "apply_failed", "applied"],
            )

    def test_mark_apply_invalid_outcome_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            with self.assertRaises(egress_store.EgressStoreError):
                store.mark_apply(request_id="req-1", outcome="pending", now=NOW_PLUS_1)
    # -- 5. mark_persist -----------------------------------------------------

    def test_mark_persist_persisted_sets_status_and_appends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs(request_id="req-p"))
            events_before = store.count_events(request_id="req-p")
            row = store.mark_persist(
                request_id="req-p", outcome="persisted", now=NOW_PLUS_1
            )
            self.assertEqual(row.persist_status, "persisted")
            self.assertEqual(store.count_events(request_id="req-p"), events_before + 1)
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-p")],
                [
                    ("requested", _open_kwargs_fields()),
                    ("persisted", {"outcome": "persisted"}),
                ],
            )

    def test_mark_persist_persist_failed_sets_status_and_appends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs(request_id="req-q"))
            events_before = store.count_events(request_id="req-q")
            row = store.mark_persist(
                request_id="req-q", outcome="persist_failed", now=NOW_PLUS_1
            )
            self.assertEqual(row.persist_status, "persist_failed")
            self.assertEqual(store.count_events(request_id="req-q"), events_before + 1)
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-q")],
                [
                    ("requested", _open_kwargs_fields()),
                    ("persist_failed", {"outcome": "persist_failed"}),
                ],
            )

    def test_mark_persist_event_failure_rolls_back_status_and_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row_before = store.get("req-1")
            real_conn = self._fail_event_inserts(store)
            try:
                with self.assertRaises(RuntimeError):
                    store.mark_persist(
                        request_id="req-1", outcome="persisted", now=NOW_PLUS_1
                    )
            finally:
                store._conn = real_conn
            row = store.get("req-1")
            self.assertEqual(row, row_before)
            self.assertIsNone(row.persist_status)
            self.assertEqual(store.count_events(kind="persisted"), 0)
            self.assertEqual(store.count_events(kind="persist_failed"), 0)

    def test_mark_persist_invalid_outcome_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            with self.assertRaises(egress_store.EgressStoreError):
                store.mark_persist(request_id="req-1", outcome="denied", now=NOW_PLUS_1)
    # -- mark_notified -------------------------------------------------------

    def test_mark_notified_appends_event_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row_before = store.get("req-1")
            store.mark_notified("req-1", NOW_PLUS_1)
            self.assertEqual(store.get("req-1"), row_before)
            self.assertEqual(
                [event.kind for event in store.events_for("req-1")],
                ["requested", "notified"],
            )
            self.assertEqual(store.events_for("req-1")[1].fields, {})

    def test_mark_notified_unknown_request_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            with self.assertRaises(egress_store.EgressStoreError):
                store.mark_notified("no-such", NOW)
    # -- 6. denylist suppressed hits ------------------------------------------

    def _denylist_row(self, store: egress_store.EgressStore) -> None:
        store.open_or_hit(**_open_kwargs())
        store.close(
            request_id="req-1",
            status="denied",
            now=NOW_PLUS_1,
            decided_by="denylist",
            denylist_zone="evil.example",
            denylist_scope="global",
            decision_body={
                "decision": "deny",
                "reason": "denylist",
                "zone": "evil.example",
                "scope": "global",
            },
        )

    def test_suppressed_hit_increments_count_without_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            self._denylist_row(store)
            events_before = store.count_events()
            row = store.suppressed_hit("req-1", NOW_PLUS_2)
            self.assertEqual(row.status, "denied")
            self.assertEqual(row.hit_count, 2)
            self.assertEqual(row.last_hit_at, NOW_PLUS_2)
            self.assertEqual(store.count_events(), events_before)
            self.assertEqual(store.count_events(kind="hit"), 0)

    def test_suppressed_hit_on_open_row_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            events_before = store.count_events()
            row_before = store.get("req-1")
            with self.assertRaises(egress_store.EgressStoreError):
                store.suppressed_hit("req-1", NOW_PLUS_1)
            self.assertEqual(store.get("req-1"), row_before)
            self.assertEqual(store.count_events(), events_before)

    def test_suppressed_hit_on_operator_denied_row_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            store.close(
                request_id="req-1",
                status="denied",
                now=NOW_PLUS_1,
                decided_by="cli",
                decision_body={"decision": "deny"},
            )
            with self.assertRaises(egress_store.EgressStoreError):
                store.suppressed_hit("req-1", NOW_PLUS_2)

    def test_suppressed_hit_unknown_request_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            with self.assertRaises(egress_store.EgressStoreError):
                store.suppressed_hit("no-such", NOW)
    # -- 7. atomicity ----------------------------------------------------------

    def _fail_event_inserts(self, store: egress_store.EgressStore):
        """Patch the store's connection so any event insert raises; returns
        the real connection for the caller's finally block."""
        real_conn = store._conn
        class _FailingConn:
            def execute(self, sql, parameters=()):
                if "INSERT INTO events" in sql:
                    raise RuntimeError("injected event-write failure")
                return real_conn.execute(sql, parameters)
            def commit(self):
                real_conn.commit()
            def rollback(self):
                real_conn.rollback()
        store._conn = _FailingConn()
        return real_conn

    def test_failed_event_write_rolls_back_row_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row_before = store.get("req-1")
            real_conn = self._fail_event_inserts(store)
            try:
                with self.assertRaises(RuntimeError):
                    store.mark_apply(request_id="req-1", outcome="applied", now=NOW_PLUS_1)
            finally:
                store._conn = real_conn
            self.assertEqual(store.get("req-1"), row_before)
            self.assertEqual(store.count_events(kind="applied"), 0)

    def test_open_or_hit_new_request_event_failure_rolls_back_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            real_conn = self._fail_event_inserts(store)
            try:
                with self.assertRaises(RuntimeError):
                    store.open_or_hit(**_open_kwargs())
            finally:
                store._conn = real_conn
            self.assertIsNone(store.get("req-1"))
            self.assertEqual(store.list_open(), [])
            self.assertEqual(store.count_events(), 0)

    def test_close_event_failure_rolls_back_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row_before = store.get("req-1")
            real_conn = self._fail_event_inserts(store)
            try:
                with self.assertRaises(RuntimeError):
                    store.close(
                        request_id="req-1",
                        status="allowed",
                        now=NOW_PLUS_1,
                        decided_by="admin",
                        scope="live",
                        decision_body={"decision": "allow", "scope": "live"},
                    )
            finally:
                store._conn = real_conn
            row = store.get("req-1")
            self.assertEqual(row, row_before)
            self.assertEqual(row.status, "open")
            self.assertIsNone(row.decision_body)
            self.assertIsNone(row.decided_at)
            self.assertEqual(store.count_events(kind="allowed"), 0)
    # -- 8. concurrency ---------------------------------------------------------

    def test_concurrent_open_or_hit_on_one_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            barrier = threading.Barrier(2, timeout=30)
            errors = []
            def worker(request_id):
                try:
                    barrier.wait()
                    for index in range(50):
                        store.open_or_hit(
                            **_open_kwargs(request_id=request_id, now=NOW_PLUS_1)
                        )
                except BaseException as exc:  # collected, asserted below
                    errors.append(exc)
            threads = [
                threading.Thread(target=worker, args=(request_id,))
                for request_id in ("concurrent-1", "concurrent-2")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             "both threads must finish")
            self.assertEqual(errors, [])
            open_rows = store.list_open()
            self.assertEqual(len(open_rows), 1)
            self.assertEqual(open_rows[0].hit_count, 100)
            self.assertEqual(store.count_events(kind="requested"), 1)
            self.assertEqual(store.count_events(kind="hit"), 99)
            self.assertEqual(store.count_events(), 100)
    # -- 9. list_open / list_recent ---------------------------------------------

    def test_list_open_ordering_and_container_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs(request_id="req-1"))
            store.open_or_hit(
                **_open_kwargs(request_id="req-b", container="bottle-b", now=NOW_PLUS_2)
            )
            store.open_or_hit(
                **_open_kwargs(request_id="req-a", container="bottle-a", now=NOW_PLUS_1)
            )
            self.assertEqual(
                [row.request_id for row in store.list_open()],
                ["req-1", "req-a", "req-b"],
            )
            self.assertEqual(
                [row.request_id for row in store.list_open(container="bottle-a")],
                ["req-a"],
            )
            self.assertEqual(store.list_open(container="bottle-z"), [])

    def test_list_recent_ordering_since_boundary_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            for request_id, decided_at in (
                ("req-early", NOW),
                ("req-mid", NOW_PLUS_1),
                ("req-late", NOW_PLUS_2),
            ):
                store.open_or_hit(**_open_kwargs(request_id=request_id))
                store.close(
                    request_id=request_id,
                    status="denied",
                    now=decided_at,
                    decided_by="cli",
                    decision_body={"decision": "deny"},
                )
            still_open_id = "req-open"
            store.open_or_hit(**_open_kwargs(request_id=still_open_id))
            recent = store.list_recent(since=NOW)
            self.assertEqual(
                [row.request_id for row in recent],
                ["req-late", "req-mid", "req-early"],
                "newest first, boundary row included",
            )
            boundary = store.list_recent(since=NOW_PLUS_1)
            self.assertEqual(
                [row.request_id for row in boundary],
                ["req-late", "req-mid"],
                "a row decided exactly at since is included",
            )
            limited = store.list_recent(since=NOW, limit=2)
            self.assertEqual(
                [row.request_id for row in limited],
                ["req-late", "req-mid"],
            )
            self.assertEqual(
                [row.request_id for row in store.list_recent(since=NOW_PLUS_2)],
                ["req-late"],
            )
    # -- events_for / count_events ------------------------------------------------

    def test_events_for_in_id_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            store.mark_notified("req-1", NOW_PLUS_1)
            store.open_or_hit(
                **_open_kwargs(request_id="req-2", host="api.github.com", now=NOW_PLUS_1)
            )
            store.close(
                request_id="req-1",
                status="denied",
                now=NOW_PLUS_2,
                decided_by="cli",
                decision_body={"decision": "deny"},
            )
            events = store.events_for("req-1")
            self.assertEqual(
                [event.id for event in events],
                sorted(event.id for event in events),
            )
            self.assertEqual(
                [event.kind for event in events],
                ["requested", "notified", "denied"],
            )
            self.assertEqual(store.events_for("no-such"), [])

    def test_count_events_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs(request_id="req-1"))
            store.open_or_hit(**_open_kwargs(request_id="req-2", host="api.github.com"))
            store.mark_notified("req-1", NOW_PLUS_1)
            self.assertEqual(store.count_events(), 3)
            self.assertEqual(store.count_events(kind="requested"), 2)
            self.assertEqual(store.count_events(kind="notified"), 1)
            self.assertEqual(store.count_events(kind="allowed"), 0)
            self.assertEqual(store.count_events(request_id="req-1"), 2)
            self.assertEqual(store.count_events(kind="hit", request_id="req-1"), 0)
    # -- get -----------------------------------------------------------------------

    def test_get_unknown_request_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            self.assertIsNone(store.get("no-such"))
    # -- 10. import_open -------------------------------------------------------------

    def test_import_open_current_month_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                MAY,
                [
                    {
                        "ts": "2026-05-20T10:00:00Z",
                        "kind": "requested",
                        "request_id": "req-imp",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                        "uid": 1000,
                        "comm": "curl",
                        "reason": "api docs",
                    },
                    {"ts": "2026-05-20T10:00:01Z", "kind": "hit", "request_id": "req-imp", "count": 1},
                    {"ts": "2026-05-20T10:00:02Z", "kind": "hit", "request_id": "req-imp", "count": 1},
                ],
            )
            store = self._store(root)
            self.addCleanup(store.shutdown)
            inserted = store.import_open(now=MAY)
            self.assertEqual(inserted, 1)
            row = store.get("req-imp")
            self.assertIsNotNone(row)
            self.assertEqual(row.container, "coding-brassbottle")
            self.assertEqual(row.host, "docs.stripe.com")
            self.assertEqual(row.port, 443)
            self.assertEqual(row.uid, 1000)
            self.assertEqual(row.comm, "curl")
            self.assertEqual(row.reason, "api docs")
            self.assertIsNone(row.hold_seconds)
            self.assertEqual(row.hit_count, 3)
            self.assertEqual(row.status, "open")
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-imp")],
                [("requested", {"imported": True}), ("hit", {"count": 2})],
            )

    def test_import_open_second_call_inserts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                MAY,
                [
                    {
                        "ts": "2026-05-20T10:00:00Z",
                        "kind": "requested",
                        "request_id": "req-imp",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                    }
                ],
            )
            store = self._store(root)
            self.addCleanup(store.shutdown)
            self.assertEqual(store.import_open(now=MAY), 1)
            self.assertEqual(store.import_open(now=MAY), 0)
            self.assertEqual(store.count_events(), 1)

    def test_import_open_carried_forward_request_has_null_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                SEP_START,
                [
                    {
                        "ts": "2026-09-01T00:05:00Z",
                        "kind": "carry_forward",
                        "open": [
                            {
                                "request_id": "req-carry",
                                "state": "requested",
                                "container": "coding-brassbottle",
                                "host": "docs.stripe.com",
                                "port": 443,
                                "opened_at": "2026-08-31T23:00:00Z",
                            }
                        ],
                    }
                ],
            )
            store = self._store(root)
            self.addCleanup(store.shutdown)
            inserted = store.import_open(now=SEP_START)
            self.assertEqual(inserted, 1)
            row = store.get("req-carry")
            self.assertIsNotNone(row)
            self.assertEqual(row.container, "coding-brassbottle")
            self.assertEqual(row.host, "docs.stripe.com")
            self.assertEqual(row.port, 443)
            self.assertIsNone(row.uid)
            self.assertIsNone(row.comm)
            self.assertIsNone(row.reason)
            self.assertIsNone(row.hold_seconds)
            self.assertEqual(row.hit_count, 1)
            self.assertEqual(row.status, "open")
            self.assertEqual(
                [(event.kind, event.fields) for event in store.events_for("req-carry")],
                [("requested", {"imported": True})],
            )

    def test_import_open_skips_ids_already_in_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                MAY,
                [
                    {
                        "ts": "2026-05-20T10:00:00Z",
                        "kind": "requested",
                        "request_id": "req-imp",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                    }
                ],
            )
            store = self._store(root)
            self.addCleanup(store.shutdown)
            store.open_or_hit(
                **_open_kwargs(
                    request_id="req-imp",
                    container="coding-brassbottle",
                    host="docs.stripe.com",
                    port=443,
                )
            )
            events_before = store.count_events()
            self.assertEqual(store.import_open(now=MAY), 0)
            self.assertEqual(store.count_events(), events_before)
    # -- 11. legacy import helpers (fold/parse semantics, ported from the
    #       retired log module's own tests) ------------------------------

    def _write_legacy_log(self, root: Path, when: datetime, lines: list[dict]) -> Path:
        path = egress_store._legacy_log_path(
            root, egress_store._legacy_month_filename(when)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n"
            for line in lines
        )
        path.write_text(payload, encoding="utf-8")
        return path

    def test_legacy_fold_is_stateless_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                MARCH,
                [
                    {
                        "ts": "2026-03-31T12:00:00Z",
                        "kind": "requested",
                        "request_id": "req-a",
                        "container": "cb",
                        "host": "docs.stripe.com",
                        "port": 443,
                    }
                ],
            )
            first = egress_store._legacy_fold_queue(root, now=MARCH)
            second = egress_store._legacy_fold_queue(root, now=MARCH)
            self.assertEqual(set(first), {"req-a"})
            self.assertEqual(set(second), {"req-a"})
            # Read-only: nothing appears beside the log, and no month file is
            # created for a month that has none.
            self.assertEqual(sorted(q.name for q in root.iterdir()), ["log"])
            self.assertEqual(
                sorted(q.name for q in (root / "log").iterdir()),
                ["2026-03.jsonl"],
            )

    def test_legacy_fold_on_empty_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                egress_store._legacy_fold_queue(Path(tmp), now=SEP_START), {}
            )

    def test_legacy_carry_forward_carries_open_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                SEP_START,
                [
                    {
                        "ts": "2026-09-01T00:05:00Z",
                        "kind": "carry_forward",
                        "open": [
                            {
                                "request_id": "req-a",
                                "state": "requested",
                                "container": "coding-brassbottle",
                                "host": "docs.stripe.com",
                                "port": 443,
                                "opened_at": "2026-08-31T23:00:00Z",
                            }
                        ],
                    }
                ],
            )
            state = egress_store._legacy_fold_queue(root, now=SEP_START)
            self.assertIn("req-a", state)
            req = state["req-a"]
            self.assertEqual(req.container, "coding-brassbottle")
            self.assertEqual(req.host, "docs.stripe.com")
            self.assertEqual(req.port, 443)
            self.assertEqual(req.opened_at, "2026-08-31T23:00:00Z")

    def test_legacy_carry_forward_mid_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                APRIL,
                [
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "requested",
                        "request_id": "req-a",
                    }
                ],
            )
            path = egress_store._legacy_log_path(
                root, egress_store._legacy_month_filename(APRIL)
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            lines.insert(1, json.dumps({"ts": "2026-04-02T10:00:00Z", "kind": "carry_forward", "open": []}))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(egress_store.EgressStoreError):
                egress_store._legacy_fold_queue(root, now=APRIL)

    def test_legacy_closed_request_leaves_no_host_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                APRIL,
                [
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "requested",
                        "request_id": "req-i",
                        "host": "secret.example.com",
                    },
                    {
                        "ts": "2026-04-02T09:01:00Z",
                        "kind": "allowed",
                        "request_id": "req-i",
                    },
                ],
            )
            state = egress_store._legacy_fold_queue(root, now=APRIL)
            self.assertEqual(state, {})

    def test_legacy_torn_trailing_line_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                APRIL,
                [
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "requested",
                        "request_id": "req-e",
                    },
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "notified",
                        "request_id": "req-e",
                    },
                ],
            )
            path = egress_store._legacy_log_path(
                root, egress_store._legacy_month_filename(APRIL)
            )
            with path.open("ab") as handle:
                handle.write(b'{"ts":"2026-04-02T10:00:00Z","kind":"hit","request')
            state = egress_store._legacy_fold_queue(root, now=APRIL)
            self.assertEqual(state["req-e"].state, "notified")

    def test_legacy_unparsable_middle_line_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                APRIL,
                [
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "requested",
                        "request_id": "req-f",
                    }
                ],
            )
            path = egress_store._legacy_log_path(
                root, egress_store._legacy_month_filename(APRIL)
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            lines.insert(
                1,
                json.dumps(
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "notified",
                        "request_id": "req-f",
                    }
                ),
            )
            # A bad line between two good ones: not a torn trailing line.
            lines.insert(2, "{not valid json")
            lines.append(
                json.dumps(
                    {
                        "ts": "2026-04-02T09:00:01Z",
                        "kind": "hit",
                        "request_id": "req-f",
                    }
                )
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(egress_store.EgressStoreError):
                egress_store._legacy_fold_queue(root, now=APRIL)

    def test_legacy_unknown_event_kind_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                APRIL,
                [
                    {
                        "ts": "2026-04-02T09:00:00Z",
                        "kind": "expired",
                        "request_id": "req-g",
                    }
                ],
            )
            with self.assertRaises(egress_store.EgressStoreError):
                egress_store._legacy_fold_queue(root, now=APRIL)

    def test_legacy_details_for_ids_reads_hits_and_meta_in_one_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy_log(
                root,
                MAY,
                [
                    {
                        "ts": "2026-05-20T10:00:00Z",
                        "kind": "requested",
                        "request_id": "req-imp",
                        "container": "coding-brassbottle",
                        "host": "docs.stripe.com",
                        "port": 443,
                        "uid": 1000,
                        "comm": "curl",
                        "reason": "api docs",
                    },
                    {"ts": "2026-05-20T10:00:01Z", "kind": "hit", "request_id": "req-imp", "count": 1},
                    {"ts": "2026-05-20T10:00:02Z", "kind": "hit", "request_id": "req-imp", "count": 1},
                ],
            )
            queue = egress_store._legacy_fold_queue(root, now=MAY)
            details = egress_store._legacy_request_details_for_ids(
                root, list(queue), queue=queue, now=MAY
            )
            detail = details["req-imp"]
            self.assertEqual(detail.container, "coding-brassbottle")
            self.assertEqual(detail.host, "docs.stripe.com")
            self.assertEqual(detail.port, 443)
            self.assertEqual(detail.uid, 1000)
            self.assertEqual(detail.comm, "curl")
            self.assertEqual(detail.reason, "api docs")
            self.assertEqual(detail.hit_count, 3)

    def test_legacy_details_for_ids_empty_when_wanted_not_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                egress_store._legacy_request_details_for_ids(
                    root, ["req-x"], queue={}, now=MAY
                ),
                {},
            )

    def test_legacy_fold_never_exposes_allowlist_names(self):
        forbidden = re.compile(r"(?i)(allow|approve|permit)")
        for name in dir(egress_store):
            if not name.startswith("_legacy_") and not name.startswith("import_open"):
                continue
            with self.subTest(name=name):
                self.assertIsNone(
                    forbidden.search(name),
                    f"name {name!r} suggests an egress allowlist API",
                )
    # -- 12. timestamp helpers ---------------------------------------------------

    def test_iso_ts_formats_and_defaults_to_now(self):
        self.assertEqual(_iso_ts(NOW), "2026-09-23T12:00:00Z")
        self.assertEqual(_iso_ts(NOW.replace(tzinfo=None)), "2026-09-23T12:00:00Z")
        current = _iso_ts(None)
        self.assertRegex(current, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    # -- 13. request_count / events reads ------------------------------------------

    def test_request_count_and_events_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            self.assertEqual(store.request_count(), 0)
            store.open_or_hit(**_open_kwargs())
            store.open_or_hit(**_open_kwargs(request_id="req-2", host="api.github.com"))
            store.mark_notified("req-1", NOW_PLUS_1)
            self.assertEqual(store.request_count(), 2)
            self.assertEqual(
                [event.kind for event in store.events()],
                ["requested", "requested", "notified"],
            )
            self.assertEqual(
                [event.request_id for event in store.events(kind="requested")],
                ["req-1", "req-2"],
            )
            self.assertEqual(store.events(kind="allowed"), [])
# -- keyset-paged history (list_recent before/until/container) and the v1 -> v2
# migration ----------------------------------------------------------------
INDEX_NAME = "idx_requests_decided_at_request_id"
# The v1 DDL, copied from the store as of origin/main dedd5e2 (before the
# decided_at index existed), so the migration tests open a real v1 file.
_V1_DDL = """
CREATE TABLE requests (
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
CREATE INDEX idx_requests_status_container_opened_at
    ON requests(status, container, opened_at);
CREATE INDEX idx_requests_container_host_port_status
    ON requests(container, host, port, status);
CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES requests,
    kind            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    fields          TEXT
);
CREATE INDEX idx_events_request_id_id ON events(request_id, id);
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (1);
"""
def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA index_list(requests)")}
def _decide(store, request_id: str, when: datetime, *, container="coding-brassbottle", status="allowed"):
    """File and close one request; `when` becomes its decided_at."""
    store.open_or_hit(**_open_kwargs(request_id=request_id, container=container, now=when))
    store.close(
        request_id=request_id,
        status=status,
        now=when,
        decided_by="operator",
        decision_body={"decision": status},
    )
class ListRecentPagingTests(unittest.TestCase):
    # Seeded through the store API (open_or_hit + close): close(now=...) sets
    # decided_at, so no SQL back door is needed.
    TOTAL = 1000
    TIED = 300
    PAGE = 50
    TIE_AT = NOW - timedelta(days=1)
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = egress_store.EgressStore(Path(cls._tmp.name))
        # Shuffled ids, so neither insertion order nor decided_at order equals
        # request_id order; the 300 tied rows are ordered by request_id alone.
        ids = [f"req-{i:04d}" for i in range(cls.TOTAL)]
        random.Random(7).shuffle(ids)
        cls.when = {}
        cls.container = {}
        for n, request_id in enumerate(ids):
            when = cls.TIE_AT if n < cls.TIED else NOW - timedelta(minutes=n)
            cls.when[request_id] = when
            cls.container[request_id] = "bottle-a" if n % 2 == 0 else "bottle-b"
            _decide(
                cls.store,
                request_id,
                when,
                container=cls.container[request_id],
                status="allowed" if n % 3 else "denied",
            )
        cls.expected = sorted(ids, key=lambda r: (_iso_ts(cls.when[r]), r), reverse=True)
    @classmethod
    def tearDownClass(cls):
        cls.store.shutdown()
        cls._tmp.cleanup()
    @staticmethod
    def _cursor(row):
        return (_iso_ts(row.decided_at), row.request_id)
    def test_setup_has_the_tied_block(self):
        rows = self.store.list_recent()
        self.assertEqual(len(rows), self.TOTAL)
        self.assertEqual(sum(1 for r in rows if r.decided_at == self.TIE_AT), self.TIED)
    def test_paging_50_at_a_time_returns_each_row_exactly_once_in_order(self):
        seen: list[str] = []
        before = None
        pages = 0
        while True:
            page = self.store.list_recent(limit=self.PAGE, before=before)
            pages += 1
            seen.extend(r.request_id for r in page)
            if len(page) < self.PAGE:
                break
            before = self._cursor(page[-1])
        self.assertEqual(len(seen), self.TOTAL)
        self.assertEqual(len(set(seen)), self.TOTAL)
        self.assertEqual(seen, self.expected)
        # 1000 rows / 50 = 20 full pages, then one empty page.
        self.assertEqual(pages, 21)
    def test_cursor_inside_the_tied_block_neither_repeats_nor_skips(self):
        tied = [r for r in self.expected if self.when[r] == self.TIE_AT]
        boundary = tied[120]
        rest = self.store.list_recent(before=(_iso_ts(self.TIE_AT), boundary))
        self.assertEqual(
            [r.request_id for r in rest], self.expected[self.expected.index(boundary) + 1 :]
        )
    def test_cursor_after_the_last_row_is_empty(self):
        last = self.expected[-1]
        self.assertEqual(self.store.list_recent(before=(_iso_ts(self.when[last]), last)), [])
    def test_since_and_until_are_inclusive_bounds(self):
        mid = NOW - timedelta(minutes=500)
        self.assertEqual(
            [r.decided_at for r in self.store.list_recent(since=mid, until=mid)], [mid]
        )
        window = self.store.list_recent(
            since=NOW - timedelta(minutes=600), until=NOW - timedelta(minutes=500)
        )
        self.assertEqual(len(window), 101)  # n = 500..600
        older = self.store.list_recent(until=NOW - timedelta(minutes=900))
        # n = 900..999 is 100 rows, plus the 300 tied rows a day back.
        self.assertEqual(len(older), 100 + self.TIED)
    def test_container_filter(self):
        a = self.store.list_recent(container="bottle-a")
        b = self.store.list_recent(container="bottle-b")
        self.assertEqual(len(a) + len(b), self.TOTAL)
        self.assertTrue(all(r.container == "bottle-a" for r in a))
        self.assertEqual(self.store.list_recent(container="nobody"), [])
    def test_container_and_cursor_compose(self):
        page1 = self.store.list_recent(container="bottle-b", limit=10)
        page2 = self.store.list_recent(
            container="bottle-b", limit=10, before=self._cursor(page1[-1])
        )
        self.assertEqual(
            [r.request_id for r in page1 + page2],
            [r for r in self.expected if self.container[r] == "bottle-b"][:20],
        )
    def test_since_without_limit_keeps_the_legacy_call_shape(self):
        # The newest untied row is n=300, 300 minutes back; n=300..305 is 6 rows.
        rows = self.store.list_recent(since=NOW - timedelta(minutes=305))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0].decided_at, NOW - timedelta(minutes=300))
class SchemaMigrationTests(unittest.TestCase):
    def _v1_root(self, tmp: str) -> Path:
        root = Path(tmp)
        conn = sqlite3.connect(root / "egress.db")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_V1_DDL)
        conn.execute(
            "INSERT INTO requests (request_id, container, host, port, host_is_ip,"
            " opened_at, last_hit_at, status, decided_at)"
            " VALUES ('old-1', 'b', 'x.example', 443, 0, '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z', 'allowed', '2026-01-01T00:05:00Z')"
        )
        conn.commit()
        conn.close()
        return root
    def test_v1_file_migrates_to_v2_with_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._v1_root(tmp)
            probe = sqlite3.connect(root / "egress.db")
            self.assertNotIn(INDEX_NAME, _index_names(probe))
            probe.close()
            with self.assertLogs("egress_store", level="INFO") as logs:
                store = egress_store.EgressStore(root)
            self.addCleanup(store.shutdown)
            self.assertTrue(
                any("migrate from=1 to=2 duration_ms=" in line for line in logs.output),
                logs.output,
            )
            probe = sqlite3.connect(root / "egress.db")
            try:
                self.assertEqual(
                    probe.execute("SELECT version FROM schema_version").fetchall(), [(2,)]
                )
                self.assertIn(INDEX_NAME, _index_names(probe))
            finally:
                probe.close()
            self.assertEqual([r.request_id for r in store.list_recent()], ["old-1"])
    def test_reopening_a_migrated_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._v1_root(tmp)
            egress_store.EgressStore(root).shutdown()
            with self.assertLogs("egress_store", level="INFO") as logs:
                again = egress_store.EgressStore(root)
            self.addCleanup(again.shutdown)
            self.assertFalse(any("migrate" in line for line in logs.output), logs.output)
            self.assertEqual(
                again._conn.execute("SELECT version FROM schema_version").fetchall(), [(2,)]
            )
    def test_fresh_database_has_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = egress_store.EgressStore(Path(tmp))
            self.addCleanup(store.shutdown)
            self.assertIn(INDEX_NAME, _index_names(store._conn))
    def test_failed_migration_leaves_a_clean_v1_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._v1_root(tmp)
            # A view named like the index makes CREATE INDEX fail mid-migration.
            conn = sqlite3.connect(root / "egress.db")
            conn.execute(f"CREATE VIEW {INDEX_NAME} AS SELECT 1")
            conn.commit()
            conn.close()
            with self.assertRaises(egress_store.EgressStoreError):
                egress_store.EgressStore(root)
            conn = sqlite3.connect(root / "egress.db")
            try:
                self.assertEqual(
                    conn.execute("SELECT version FROM schema_version").fetchall(), [(1,)]
                )
            finally:
                conn.close()
    def test_unknown_versions_still_raise(self):
        for version in (0, 3, 99):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory() as tmp:
                    root = self._v1_root(tmp)
                    conn = sqlite3.connect(root / "egress.db")
                    conn.execute("UPDATE schema_version SET version = ?", (version,))
                    conn.commit()
                    conn.close()
                    with self.assertRaises(egress_store.EgressStoreError):
                        egress_store.EgressStore(root)
def _open_kwargs_fields():
    return {
        "container": "coding-brassbottle",
        "host": "docs.stripe.com",
        "port": 443,
        "uid": 1000,
        "comm": "curl",
        "reason": "api docs",
        "hold_seconds": 90,
    }
if __name__ == "__main__":
    unittest.main()
