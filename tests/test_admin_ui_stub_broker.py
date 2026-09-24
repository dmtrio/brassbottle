#!/usr/bin/env python3
"""The behaviour suite's stub broker answers only what the broker contract allows."""
from __future__ import annotations

import copy
import json
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
