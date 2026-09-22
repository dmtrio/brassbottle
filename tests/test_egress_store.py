#!/usr/bin/env python3
"""Unit tests for the SQLite egress request store (queue + audit)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import egress_log  # noqa: E402
import egress_store  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
NOW_PLUS_1 = NOW + timedelta(seconds=1)
NOW_PLUS_2 = NOW + timedelta(seconds=2)
MAY = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
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

    def test_fresh_root_creates_wal_db_with_schema_version_1(self):
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
                    [(1,)],
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

    def test_schema_version_2_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.open_or_hit(**_open_kwargs())
            store._conn.execute("UPDATE schema_version SET version = 2")
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

    def test_mark_persist_both_outcomes_set_status_without_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(
                **_open_kwargs(
                    request_id="req-p",
                    host="docs.stripe.com",
                )
            )
            store.open_or_hit(
                **_open_kwargs(
                    request_id="req-q",
                    host="api.github.com",
                )
 )

            row = store.mark_persist(request_id="req-p", outcome="persisted", now=NOW_PLUS_1)
            self.assertEqual(row.persist_status, "persisted")
            row = store.mark_persist(
                request_id="req-q", outcome="persist_failed", now=NOW_PLUS_1
            )
            self.assertEqual(row.persist_status, "persist_failed")

            self.assertEqual(store.count_events(request_id="req-p"), 1)
            self.assertEqual(store.count_events(request_id="req-q"), 1)

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

    def test_failed_event_write_rolls_back_row_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            store.open_or_hit(**_open_kwargs())
            row_before = store.get("req-1")

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
            try:
                with self.assertRaises(RuntimeError):
                    store.mark_apply(request_id="req-1", outcome="applied", now=NOW_PLUS_1)
            finally:
                store._conn = real_conn

            self.assertEqual(store.get("req-1"), row_before)
            self.assertEqual(store.count_events(kind="applied"), 0)

    # -- 8. concurrency ---------------------------------------------------------

    def test_concurrent_open_or_hit_on_one_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            self.addCleanup(store.shutdown)
            barrier = threading.Barrier(2, timeout=30)

            def worker():
                barrier.wait()
                for index in range(50):
                    store.open_or_hit(
                        **_open_kwargs(request_id="concurrent1", now=NOW_PLUS_1)
                    )

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            row = store.get("concurrent1")
            self.assertIsNotNone(row)
            self.assertEqual(row.hit_count, 100)
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
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-imp",
                ts=MAY,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
                uid=1000,
                comm="curl",
                reason="api docs",
            )
            log.append("hit", "req-imp", ts=MAY, count=1)
            log.append("hit", "req-imp", ts=MAY, count=1)

            store = self._store(root)
            self.addCleanup(store.shutdown)
            inserted = store.import_open(log, now=MAY)

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
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-imp",
                ts=MAY,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
            )
            store = self._store(root)
            self.addCleanup(store.shutdown)
            self.assertEqual(store.import_open(log, now=MAY), 1)
            self.assertEqual(store.import_open(log, now=MAY), 0)
            self.assertEqual(store.count_events(), 1)

    def test_import_open_carried_forward_request_has_null_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-carry",
                ts=AUG_END,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
                uid=1000,
                comm="curl",
                reason="api docs",
            )
            # An append in the next month rotates the log and writes the
            # carry-forward header; the prior month's records are not read
            # again, so uid/comm/reason are lost for the import.
            log.append("notified", "req-carry", ts=SEP_START)

            store = self._store(root)
            self.addCleanup(store.shutdown)
            inserted = store.import_open(log, now=SEP_START)

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
            log = egress_log.EgressLog(root)
            log.append(
                "requested",
                "req-imp",
                ts=MAY,
                container="coding-brassbottle",
                host="docs.stripe.com",
                port=443,
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

            self.assertEqual(store.import_open(log, now=MAY), 0)
            self.assertEqual(store.count_events(), events_before)

    # -- 11. Dark import audit ---------------------------------------------------------

    def test_dark_no_file_outside_store_mentions_egress_store(self):
        store_path = Path(egress_store.__file__).resolve()
        offenders = []
        for dirname in ("src", "bin"):
            base = REPO_ROOT / dirname
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if path.resolve() == store_path:
                    continue
                if path.suffix in {".pyc", ".pyo"} or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "egress_store" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders,
            [],
            "the store is Dark: no file outside src/egress_store.py may mention it",
        )


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
