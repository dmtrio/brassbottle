#!/usr/bin/env python3
"""The behaviour suite's stub broker answers only what the broker contract allows."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import admin_ui_stub_broker as stub  # noqa: E402
from admin_contract_validator import validate_document  # noqa: E402


def _decide(base_queue, **payload):
    queue = copy.deepcopy(base_queue)
    status, body = stub.decide_reply(queue, payload)
    return queue, status, body


class StubBrokerTests(unittest.TestCase):
    def test_initial_queue_matches_the_contract_and_orders_three_ways(self):
        queue = stub.build_queue()
        self.assertEqual(validate_document(queue, stub.QUEUE_SCHEMA), [])
        self.assertEqual(queue["count"], len(queue["open"]))
        first_seen = list(dict.fromkeys(row["container"] for row in queue["open"]))
        newest = sorted(queue["open"], key=lambda row: row["opened_at"], reverse=True)
        newest_bottles = list(dict.fromkeys(row["container"] for row in newest))
        self.assertEqual(first_seen, ["zeta", "alpha", "mid"])
        self.assertEqual(newest_bottles, ["alpha", "zeta", "mid"])
        self.assertEqual(sorted(first_seen), ["alpha", "mid", "zeta"])

    def test_every_scripted_reply_validates(self):
        queue = stub.build_queue()
        cases = [
            ({"decision": "allow", "scope": "live", "host": "a1.example.com", "container": "alpha"}, 200),
            ({"decision": "allow", "scope": "live", "host": "192.0.2.55", "container": "alpha"}, 200),
            ({"decision": "allow", "scope": "live", "host": "m2.example.com", "container": "mid"}, 200),
            ({"decision": "allow", "scope": "manifest", "host": "a2.example.com", "container": "alpha"}, 200),
            ({"decision": "deny", "scope": "once", "host": "m1.example.com", "container": "mid"}, 200),
            ({"decision": "deny", "scope": "bottle", "host": "z2.example.com", "container": "zeta"}, 200),
            ({"decision": "deny", "scope": "global", "host": "z3.example.com"}, 200),
            ({"decision": "deny", "scope": "once", "host": stub.BAD_REQUEST_HOST, "container": "mid"}, 400),
        ]
        for payload, expected in cases:
            after, status, body = _decide(queue, **payload)
            self.assertEqual(status, expected, payload)
            schema = stub.ERROR_SCHEMA if status >= 400 else stub.DECIDE_SCHEMA
            self.assertEqual(validate_document(body, schema), [], payload)
            self.assertEqual(validate_document(after, stub.QUEUE_SCHEMA), [], payload)

    def test_decided_row_leaves_open_and_lands_in_recent(self):
        queue = stub.build_queue()
        after, _status, body = _decide(
            queue, decision="deny", scope="bottle", host="z2.example.com", container="zeta", reason="not needed")
        self.assertEqual(body, {"decided": ["z2"], "persisted": {"zone": "z2.example.com", "scope": "zeta"}})
        self.assertEqual(after["count"], queue["count"] - 1)
        self.assertNotIn("z2", [row["request_id"] for row in after["open"]])
        self.assertEqual(after["recent"][0]["deny_reason"], "not needed")

    def test_ip_and_apply_failed_allows_leave_the_row_queued(self):
        queue = stub.build_queue()
        after, _s, body = _decide(queue, decision="allow", scope="live", host="192.0.2.55", container="alpha")
        self.assertEqual(body, {"decided": [], "apply_failures": [{"request_id": "ip1", "reason": "ip_requires_cidr"}]})
        self.assertEqual(after["count"], queue["count"])
        _a, _s, body = _decide(queue, decision="allow", scope="live", host="m2.example.com", container="mid")
        self.assertEqual(body["apply_failures"], [{"request_id": "m2", "reason": "apply_failed"}])

    def test_decide_outage_fails_decide_only_and_leaves_the_queue_healthy(self):
        broker = stub.StubBroker()
        broker.decide_outage = 401
        base = broker.start()
        try:
            host, port = base.split("//")[1].split(":")[0], broker.server.server_address[1]
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/queue")
            resp = conn.getresponse()
            queue = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertEqual(validate_document(queue, stub.QUEUE_SCHEMA), [])
            payload = {"decision": "deny", "scope": "once", "host": "a1.example.com", "container": "alpha"}
            conn.request("POST", "/decide", body=json.dumps(payload), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual((resp.status, body), (401, {"error": "decide unavailable"}))
            self.assertEqual(broker.decides, [payload])  # the attempt is recorded
            self.assertEqual(broker.violations, [])
            self.assertEqual(len(broker.queue["open"]), broker.queue["count"])  # nothing was decided
            broker.reset()
            self.assertIsNone(broker.decide_outage)
        finally:
            broker.stop()

    def test_preflight_refuses_a_queue_reply_with_an_extra_field(self):
        broker = stub.StubBroker()
        broker.preflight()  # unmodified stub passes
        original = broker.queue_body
        broker.queue_body = lambda: {**original(), "extra": 1}
        with self.assertRaises(stub.ContractViolation):
            broker.preflight()

    def test_a_violating_reply_is_never_served_and_is_recorded(self):
        broker = stub.StubBroker()
        original = broker.queue_body
        broker.queue_body = lambda: {**original(), "extra": 1}
        base = broker.start()
        try:
            conn = HTTPConnection(base.split("//")[1].split(":")[0], broker.server.server_address[1], timeout=5)
            conn.request("GET", "/queue")
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 500)
            self.assertEqual(body, {"error": "stub contract violation"})
            self.assertEqual(len(broker.violations), 1)
            self.assertIn("extra", broker.violations[0])
        finally:
            broker.stop()

    def test_queue_features_fixture_is_twelve_rows_over_three_bottles_with_four_failed_applies(self):
        queue = stub.build_queue(fixture="queue-features")
        self.assertEqual(validate_document(queue, stub.QUEUE_SCHEMA), [])
        self.assertEqual(queue["count"], 12)
        per_bottle = {}
        for row in queue["open"]:
            per_bottle.setdefault(row["container"], []).append(row["request_id"])
        self.assertEqual({b: len(ids) for b, ids in per_bottle.items()}, {"alpha": 5, "mid": 4, "zeta": 3})
        failed = sorted(row["request_id"] for row in queue["open"] if row["last_error"])
        self.assertEqual(failed, ["fa2", "fa4", "fm2", "fz2"])
        denylist = [row for row in queue["recent"] if row["decided_by"] == "denylist"]
        self.assertEqual(sorted(row["hit_count"] for row in denylist), [2, 4, 7])

    def test_failing_nth_decide_answers_500_once_and_records_every_body(self):
        broker = stub.StubBroker()
        broker.use_fixture("queue-features")
        broker.fail_nth_decide(2)
        base = broker.start()
        try:
            conn = HTTPConnection(base.split("//")[1].split(":")[0], broker.server.server_address[1], timeout=5)
            statuses = []
            for host in ("fa1.example.com", "fa3.example.com", "fa5.example.com"):
                payload = {"decision": "deny", "scope": "once", "host": host, "container": "alpha"}
                conn.request("POST", "/decide", body=json.dumps(payload), headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                statuses.append(resp.status)
            conn.close()
            self.assertEqual(statuses, [200, 500, 200])
            self.assertEqual([b["host"] for b in broker.decides], ["fa1.example.com", "fa3.example.com", "fa5.example.com"])
            self.assertEqual(sorted(r["request_id"] for r in broker.queue["open"] if r["container"] == "alpha"), ["fa2", "fa3", "fa4"])
            self.assertEqual(broker.violations, [])
            broker.reset()
            self.assertIsNone(broker.failing_decide)
            self.assertEqual(broker.queue["count"], 11)  # reset restores the default fixture
        finally:
            broker.stop()


class StubHistoryTests(unittest.TestCase):
    """`GET /recent` is real keyset paging over a fixture with a tie block."""

    def _walk(self, rows, query=""):
        pages, seen, cursor = [], [], None
        while True:
            q = "&".join(part for part in (query, f"before={cursor}" if cursor else "") if part)
            status, page = stub.recent_reply(rows, q)
            self.assertEqual(status, 200)
            self.assertEqual(validate_document(page, stub.RECENT_SCHEMA), [])
            pages.append(page["rows"])
            seen.extend(row["request_id"] for row in page["rows"])
            cursor = page["next"]
            if cursor is None:
                return pages, seen

    def test_paging_returns_every_row_once_in_keyset_order(self):
        rows = stub.StubBroker().history_rows()
        pages, seen = self._walk(rows)
        self.assertEqual(len(seen), len(rows))
        self.assertEqual(len(set(seen)), len(rows))
        ordered = sorted(rows, key=lambda r: (r["decided_at"], r["request_id"]), reverse=True)
        self.assertEqual(seen, [r["request_id"] for r in ordered])
        self.assertEqual([len(p) for p in pages], [50, 50, 50, 50, 47])

    def test_a_tie_block_straddles_the_first_page_boundary(self):
        pages, _seen = self._walk(stub.StubBroker().history_rows())
        last_first, first_second = pages[0][-1], pages[1][0]
        self.assertEqual(last_first["decided_at"], first_second["decided_at"])
        self.assertGreater(last_first["request_id"], first_second["request_id"])

    def test_row_decided_thirty_days_ago_is_in_the_last_pages(self):
        broker = stub.StubBroker()
        _pages, seen = self._walk(broker.history_rows())
        archive = next(r for r in broker.history if r["host"] == stub.ARCHIVE_HOST)
        self.assertIn(archive["request_id"], seen[-3:])
        clock = datetime.strptime(broker.queue["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
        decided = datetime.strptime(archive["decided_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertAlmostEqual((clock - decided).total_seconds(), 30 * 86400, delta=60)

    def test_filters_limit_clamp_and_bad_query(self):
        rows = stub.StubBroker().history_rows()
        _s, only = stub.recent_reply(rows, "container=mid&limit=1000")
        self.assertTrue(only["rows"] and all(r["container"] == "mid" for r in only["rows"]))
        self.assertEqual(len(stub.recent_reply(rows, "limit=0")[1]["rows"]), 1)
        self.assertEqual(len(stub.recent_reply(rows, "limit=999")[1]["rows"]), 200)
        for bad in ("before=nope", "since=yesterday", "limit=x"):
            self.assertEqual(stub.recent_reply(rows, bad), (400, {"error": "invalid query"}), bad)

    def test_a_row_decided_during_a_run_tops_history(self):
        broker = stub.StubBroker()
        payload = {"decision": "deny", "scope": "once", "host": "a1.example.com", "container": "alpha"}
        _status, _reply = stub.decide_reply(broker.queue, payload)
        self.assertEqual(broker.history_rows()[0]["request_id"], "a1")
        _s, page = stub.recent_reply(broker.history_rows(), "")
        self.assertEqual(page["rows"][0]["request_id"], "a1")

    def test_preflight_refuses_a_history_reply_with_a_bad_cursor(self):
        broker = stub.StubBroker()
        broker.preflight()
        original = stub.recent_reply
        stub.recent_reply = lambda rows, q: (200, {**original(rows, q)[1], "next": "not a cursor"})
        self.addCleanup(setattr, stub, "recent_reply", original)
        with self.assertRaises(stub.ContractViolation):
            broker.preflight()


if __name__ == "__main__":
    unittest.main()
