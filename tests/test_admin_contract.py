#!/usr/bin/env python3
"""Contract tests: real broker output must validate against admin/contract/.

Seeds a real EgressStore/EgressBroker (temp dir, no mocks of the broker or
store) with every row shape the operator queue can carry, then asserts
queue_snapshot() and a real /decide (allow, IP literal) response body
validate against the JSON Schemas in admin/contract/. Adding, removing or
renaming a broker field without updating the schema fails here.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

import egress_broker_host as broker  # noqa: E402
from admin_contract_validator import validate_document  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now


class AdminContractTests(unittest.TestCase):
    OPERATOR_TOKEN = "operator-test-token"

    def _broker(self, root: Path) -> broker.EgressBroker:
        clock = FakeClock(NOW)
        return broker.EgressBroker(
            root,
            repo_root=REPO_ROOT,
            now_fn=clock.now,
            hold_seconds_default=5,
        )

    def _serve(self, root: Path, b: broker.EgressBroker) -> tuple[str, int]:
        store = broker.BottleTokenStore(root / broker.TOKENS_DIRNAME)
        server = broker.EgressBrokerHTTPServer(
            ("127.0.0.1", 0), b, store, self.OPERATOR_TOKEN
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)

        def stop_server() -> None:
            server.shutdown()
            join_thread_or_fail(thread, label="broker server")

        self.addCleanup(stop_server)
        return host, port

    def _post_decide(self, host: str, port: int, payload: dict) -> tuple[int, dict]:
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/decide",
            json.dumps(payload),
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.OPERATOR_TOKEN}",
            },
        )
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def _seed(self, b: broker.EgressBroker) -> dict[str, str]:
        """File every row shape the queue can carry; return ids by role."""
        ids: dict[str, str] = {}

        # 1. Plain open row, no optional fields set.
        _, ids["open_plain"] = b.file_request("coding-brassbottle", "neon.tech", 443)

        # 2. Open row whose apply failed: last_error set, attempts >= 1.
        _, failed_id = b.file_request(
            "coding-brassbottle",
            "api.stripe.com",
            443,
            uid=1001,
            comm="python",
            reason="db connect",
        )
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=1)
            self.assertEqual(b.decide(failed_id, "allow", scope="live"), "apply_failed")
        ids["open_apply_failed"] = failed_id

        # 3. Allowed row.
        _, allowed_id = b.file_request("coding-brassbottle", "docs.stripe.com", 443)
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0)
            self.assertIsNone(b.decide(allowed_id, "allow", scope="live"))
        ids["allowed"] = allowed_id

        # 4. Row denied by the operator, with a deny reason.
        _, denied_id = b.file_request("coding-brassbottle", "a.example.com", 443)
        self.assertIsNone(
            b.decide(denied_id, "deny", scope="once", reason="not needed for work")
        )
        ids["denied_operator"] = denied_id

        # 5. Row denied by the denylist, bumped to hit_count 5 via
        # suppressed_hit (one logged hit plus four suppressed repeats
        # inside the coalesce window).
        b._denylist.add(zone="datadoghq.com", scope="global", reason="telemetry")
        for _ in range(5):
            _, denylist_id = b.file_request(
                "coding-brassbottle", "app.datadoghq.com", 443
            )
        ids["denied_denylist"] = denylist_id
        return ids

    def test_queue_snapshot_validates_and_recent_carries_seeded_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            ids = self._seed(b)

            snapshot = b.queue_snapshot()
            self.assertEqual(
                validate_document(snapshot, "queue_snapshot.schema.json"), []
            )

            # Non-vacuous: the seeded decided rows really are in recent.
            recent_by_id = {row["request_id"]: row for row in snapshot["recent"]}
            self.assertIn(ids["allowed"], recent_by_id)
            self.assertEqual(recent_by_id[ids["allowed"]]["status"], "allowed")
            self.assertIn(ids["denied_operator"], recent_by_id)
            self.assertEqual(
                recent_by_id[ids["denied_operator"]]["status"], "denied"
            )
            self.assertEqual(
                recent_by_id[ids["denied_operator"]]["deny_reason"],
                "not needed for work",
            )
            self.assertEqual(
                recent_by_id[ids["denied_operator"]]["decided_by"], "operator"
            )
            self.assertIn(ids["denied_denylist"], recent_by_id)
            self.assertEqual(
                recent_by_id[ids["denied_denylist"]]["decided_by"], "denylist"
            )
            denylist_row = b.store.get(ids["denied_denylist"])
            self.assertEqual(denylist_row.hit_count, 5)

            # And the two open rows really are open, one carrying the
            # failed-apply state.
            open_by_id = {row["request_id"]: row for row in snapshot["open"]}
            self.assertIn(ids["open_plain"], open_by_id)
            self.assertIn(ids["open_apply_failed"], open_by_id)
            self.assertEqual(open_by_id[ids["open_apply_failed"]]["attempt"], 1)
            self.assertEqual(
                open_by_id[ids["open_apply_failed"]]["last_error"]["reason"],
                "apply_failed",
            )
            self.assertEqual(snapshot["count"], 2)

    def test_decide_allow_ip_literal_response_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            _, request_id = b.file_request("coding-brassbottle", "192.0.2.55", 443)
            host, port = self._serve(root, b)

            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "coding-brassbottle",
                    "host": "192.0.2.55",
                    "decision": "allow",
                    "scope": "live",
                },
            )
            self.assertEqual(status, HTTPStatus.OK)
            # The body comes from the production handler, not hand-written;
            # check the seam did what the contract says before validating.
            self.assertEqual(body["decided"], [])
            self.assertEqual(
                body["apply_failures"],
                [
                    {
                        "request_id": request_id,
                        "reason": broker.IP_REQUIRES_CIDR_REASON,
                    }
                ],
            )
            self.assertEqual(validate_document(body, "decide_response.schema.json"), [])

            row = b.store.get(request_id)
            self.assertEqual(row.status, "open")
            self.assertEqual(row.apply_status, "ip_requires_cidr")

    def test_decide_deny_once_response_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            _, request_id = b.file_request("coding-brassbottle", "once.example.com", 443)
            host, port = self._serve(root, b)

            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "coding-brassbottle",
                    "host": "once.example.com",
                    "decision": "deny",
                    "scope": "once",
                },
            )
            self.assertEqual(status, HTTPStatus.OK)
            # Branch non-vacuous: the one-shot deny really closed the row.
            self.assertEqual(body["decided"], [request_id])
            self.assertEqual(validate_document(body, "decide_response.schema.json"), [])
            self.assertEqual(b.store.get(request_id).status, "denied")

    def test_decide_deny_bottle_response_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tokens = root / broker.TOKENS_DIRNAME
            tokens.mkdir(parents=True, exist_ok=True)
            (tokens / "coding-brassbottle.token").write_text("tok\n", encoding="utf-8")
            b = self._broker(root)
            _, request_id = b.file_request("coding-brassbottle", "b2.example.com", 443)
            host, port = self._serve(root, b)

            status, body = self._post_decide(
                host,
                port,
                {
                    "container": "coding-brassbottle",
                    "host": "b2.example.com",
                    "decision": "deny",
                    "scope": "bottle",
                },
            )
            self.assertEqual(status, HTTPStatus.OK)
            # Branch non-vacuous: an entry was persisted for the named zone,
            # in the bottle's scope (persist_deny records write_scope — the
            # bottle name — not the literal "bottle"), and the open request
            # was swept closed.
            self.assertEqual(
                body["persisted"],
                {"zone": "b2.example.com", "scope": "coding-brassbottle"},
            )
            self.assertEqual(body["decided"], [request_id])
            self.assertEqual(validate_document(body, "decide_response.schema.json"), [])
            self.assertEqual(b.store.get(request_id).status, "denied")
            # The sweep records the entry's zone on the row (decided_by
            # stays "operator" — only the short-circuit path decides as
            # "denylist") and marks the persist outcome.
            self.assertEqual(b.store.get(request_id).denylist_zone, "b2.example.com")
            self.assertEqual(b.store.get(request_id).persist_status, "persisted")

    def test_decide_deny_global_response_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            host, port = self._serve(root, b)

            status, body = self._post_decide(
                host,
                port,
                {
                    "host": "g3.example.com",
                    "decision": "deny",
                    "scope": "global",
                },
            )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(
                body["persisted"], {"zone": "g3.example.com", "scope": "global"}
            )
            self.assertEqual(body["decided"], [])
            self.assertEqual(validate_document(body, "decide_response.schema.json"), [])

    def test_snapshot_after_decide_still_validates(self):
        """A snapshot whose IP row carries ip_requires_cidr state validates too."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            _, request_id = b.file_request("coding-brassbottle", "192.0.2.55", 443)
            self.assertEqual(
                b.decide(request_id, "allow", scope="live"),
                broker.IP_REQUIRES_CIDR_REASON,
            )
            snapshot = b.queue_snapshot()
            self.assertEqual(
                validate_document(snapshot, "queue_snapshot.schema.json"), []
            )
            open_row = snapshot["open"][0]
            self.assertEqual(open_row["request_id"], request_id)
            self.assertEqual(open_row["last_error"]["reason"], "ip_requires_cidr")


if __name__ == "__main__":
    unittest.main()
