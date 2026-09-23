#!/usr/bin/env python3
"""Contract tests: real broker output must validate against admin/contract/.

Seeds a real EgressStore/EgressBroker (temp dir, no mocks of the broker or
store) with every row shape the operator queue can carry, then asserts
queue_snapshot(), the three /decide success bodies (allow, deny once,
persistent deny) and its error bodies (400 per validation branch, 401)
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
            server.server_close()
            join_thread_or_fail(thread, label="broker server")

        self.addCleanup(stop_server)
        return host, port

    def _post_decide(self, host: str, port: int, payload: dict) -> tuple[int, dict]:
        return self._post_raw(host, port, json.dumps(payload).encode("utf-8"))

    def _post_raw(
        self, host: str, port: int, body: bytes, *, authorization: str | None = "operator"
    ) -> tuple[int, dict]:
        """POST /decide. authorization: "operator" sends the real token, None
        omits the header, any other string is sent verbatim."""
        headers = {"Content-Type": "application/json"}
        if authorization == "operator":
            headers["Authorization"] = f"Bearer {self.OPERATOR_TOKEN}"
        elif authorization is not None:
            headers["Authorization"] = authorization
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/decide", body, headers)
        resp = conn.getresponse()
        parsed = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, parsed

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

    # One request per distinct error branch of _handle_decide_post, plus the
    # operator auth gate in front of it, each through the real HTTP handler.
    # The two branches that need broker state (EgressBrokerHostError -> 400,
    # persist failure -> 500) have their own tests below.
    DECIDE_ERROR_CASES = (
        ("invalid json", b"{not json", "operator", HTTPStatus.BAD_REQUEST, "invalid json"),
        ("host missing", b'{"decision": "allow", "scope": "live", "container": "c"}', "operator", HTTPStatus.BAD_REQUEST, "host is required"),
        ("bad decision", b'{"host": "x.example.com", "decision": "maybe"}', "operator", HTTPStatus.BAD_REQUEST, "decision must be allow or deny"),
        ("reason on allow", b'{"host": "x.example.com", "decision": "allow", "scope": "live", "container": "c", "reason": "r"}', "operator", HTTPStatus.BAD_REQUEST, "reason only applies to deny"),
        ("bad scope", b'{"host": "x.example.com", "decision": "allow", "scope": "forever", "container": "c"}', "operator", HTTPStatus.BAD_REQUEST, "invalid scope"),
        ("json not an object", b'[1]', "operator", HTTPStatus.BAD_REQUEST, "invalid json"),
        ("deny bad scope", b'{"host": "x.example.com", "decision": "deny", "scope": "forever", "container": "c"}', "operator", HTTPStatus.BAD_REQUEST, "invalid scope"),
        ("reason too long", ('{"host": "x.example.com", "decision": "deny", "scope": "once", "container": "c", "reason": "%s"}' % ("r" * 201)).encode(), "operator", HTTPStatus.BAD_REQUEST, "reason must be a string of at most 200 characters"),
        ("container missing", b'{"host": "x.example.com", "decision": "allow", "scope": "live"}', "operator", HTTPStatus.BAD_REQUEST, "container is required"),
        ("container not a string", b'{"host": "x.example.com", "decision": "deny", "scope": "global", "container": 5}', "operator", HTTPStatus.BAD_REQUEST, "container must be a string"),
        ("invalid host", b'{"host": "bad host!", "decision": "deny", "scope": "once", "container": "c"}', "operator", HTTPStatus.BAD_REQUEST, "invalid host"),
        ("no bearer header", b'{"host": "x.example.com", "decision": "deny", "scope": "once", "container": "c"}', None, HTTPStatus.UNAUTHORIZED, "unauthorized"),
        ("empty bearer token", b'{"host": "x.example.com", "decision": "deny", "scope": "once", "container": "c"}', "Bearer ", HTTPStatus.UNAUTHORIZED, "unauthorized"),
        ("wrong bearer token", b'{"host": "x.example.com", "decision": "deny", "scope": "once", "container": "c"}', "Bearer not-the-token", HTTPStatus.UNAUTHORIZED, "unauthorized"),
    )

    def test_decide_error_bodies_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            host, port = self._serve(root, b)
            for label, body, authorization, status, error in self.DECIDE_ERROR_CASES:
                with self.subTest(case=label):
                    got_status, parsed = self._post_raw(host, port, body, authorization=authorization)
                    self.assertEqual(got_status, status, parsed)
                    self.assertEqual(validate_document(parsed, "error_response.schema.json"), [])
                    # The exact text proves the intended branch answered.
                    self.assertEqual(parsed["error"], error)

    def test_decide_broker_error_body_validates(self):
        # A bottle-scoped deny for a bottle with no token file: persist_deny's
        # validate_bottle_scope raises, the handler's `except
        # EgressBrokerHostError` answers 400.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            host, port = self._serve(root, b)
            status, parsed = self._post_decide(
                host,
                port,
                {"host": "x.example.com", "decision": "deny", "scope": "bottle", "container": "no-such-bottle"},
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, parsed)
            self.assertEqual(validate_document(parsed, "error_response.schema.json"), [])
            self.assertTrue(parsed["error"])

    def test_decide_persist_failure_body_validates(self):
        # A directory where the denylist file belongs makes DenyList.add fail
        # with OSError; persist_deny returns an error and the handler
        # answers 500 with result.error.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = self._broker(root)
            host, port = self._serve(root, b)
            (root / broker.DENYLIST_FILENAME).mkdir()
            status, parsed = self._post_decide(
                host,
                port,
                {"host": "x.example.com", "decision": "deny", "scope": "global"},
            )
            self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR, parsed)
            self.assertEqual(validate_document(parsed, "error_response.schema.json"), [])
            self.assertEqual(parsed["error"], broker.DENYLIST_PERSIST_FAILED_REASON)



if __name__ == "__main__":
    unittest.main()
