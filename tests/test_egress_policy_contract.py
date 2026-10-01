#!/usr/bin/env python3
"""Contract tests: the broker's GET /policy and POST /policy/refresh.

Real broker, real store, real HTTP server on a temp dir; only the manifest
derivation (yq + manifest.py, covered by tests/test_manifest.py) is replaced
by a fake so each test controls the manifest input. Asserts the document
validates against egress/contract/policy.schema.json, that the revision moves
for every changing cause and for no unchanged one, the long-poll, the
per-token refusals, persistence across a restart, and concurrency.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

import bottle_net  # noqa: E402
import egress_broker_host as broker  # noqa: E402
import egress_policy  # noqa: E402
from admin_contract_validator import validate_document  # noqa: E402
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening  # noqa: E402

SCHEMA = "policy.schema.json"
SCHEMA_DIR = REPO_ROOT / "egress" / "contract"
OPERATOR = "operator-test-token"
GATEWAY = "gateway-test-token"
BOTTLE = "bottle-test-token"
BOTTLE_NAME = "coding-brassbottle"


def doc_errors(doc: dict) -> list[str]:
    errors = validate_document(doc, SCHEMA, base_dir=SCHEMA_DIR)
    for name, entry in doc["bottles"].items():
        for grant in entry["grants"]:
            if ("name" in grant) == ("cidr" in grant):
                errors.append(f"{name}: grant needs exactly one of name|cidr: {grant}")
    return errors


class PolicyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "run" / "egress"
        self.bottles = self.home / "bottles"
        self.bottles.mkdir(parents=True)
        (self.bottles / f"{BOTTLE_NAME}.yml").write_text("task: coding\n")
        self.derived = {
            "EGRESS": "pypi.org,files.pythonhosted.org",
            "EGRESS_CIDRS": "",
            "HOST_MCP_PORTS": "9100",
            "OPEN_RELAYS": "",
        }
        self.remote_direct = True
        self.derive_calls = 0
        self.servers: list[tuple] = []

    # -- helpers -----------------------------------------------------------

    def _derive(self, name: str, manifest: Path):
        self.derive_calls += 1
        return {"derived": dict(self.derived), "remote_direct": self.remote_direct}

    def _broker(self, wait_seconds: float = 0.4) -> broker.EgressBroker:
        return broker.EgressBroker(
            self.root,
            repo_root=REPO_ROOT,
            hold_seconds_default=5,
            policy_derive=self._derive,
            policy_wait_seconds=wait_seconds,
        )

    def _serve(self, b: broker.EgressBroker) -> tuple[str, int]:
        tokens = self.root / broker.TOKENS_DIRNAME
        tokens.mkdir(parents=True, exist_ok=True)
        (tokens / f"{BOTTLE_NAME}.token").write_text(BOTTLE + "\n")
        server = broker.EgressBrokerHTTPServer(
            ("127.0.0.1", 0),
            b,
            broker.BottleTokenStore(tokens),
            OPERATOR,
            GATEWAY,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)

        def stop() -> None:
            server.shutdown()
            server.server_close()
            join_thread_or_fail(thread, label="broker server")

        self.addCleanup(stop)
        return host, port

    def _call(self, addr, method, path, token=GATEWAY, body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        conn = HTTPConnection(*addr, timeout=10)
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def _get(self, addr, path="/policy", token=GATEWAY):
        return self._call(addr, "GET", path, token)

    def _refresh(self, addr, token=OPERATOR):
        return self._call(addr, "POST", "/policy/refresh", token, b"")

    def _revision(self, addr) -> int:
        status, doc = self._get(addr)
        self.assertEqual(status, HTTPStatus.OK)
        return doc["revision"]

    def _file_and_allow(self, b, host="docs.example.com", port=443, scope="live"):
        _, rid = b.file_request(BOTTLE_NAME, host, port)
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertIsNone(b.decide(rid, "allow", scope=scope))

    def _write_alloc(self, subnet="172.31.0.0/28"):
        path = self.home / "run" / bottle_net.ALLOC_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({BOTTLE_NAME: {"subnet": subnet, "browser_port": 8900, "ssh_port": 2222}})
        )

    # -- document ----------------------------------------------------------

    def test_document_validates_and_carries_every_planned_field(self):
        self.derived["EGRESS"] = "pypi.org,db.example.com:5432"
        self.derived["EGRESS_CIDRS"] = "10.1.2.0/24:8080,203.0.113.7"
        self.derived["OPEN_RELAYS"] = "proxyman"
        self.remote_direct = False
        self._write_alloc()
        b = self._broker()
        addr = self._serve(b)
        status, doc = self._get(addr)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(doc_errors(doc), [])
        self.assertEqual(
            doc["bottles"][BOTTLE_NAME],
            {
                "network": "djinn-b-coding-brassbottle",
                "subnet": "172.31.0.0/28",
                "zones": ["pypi.org"],
                "grants": [
                    {"cidr": "10.1.2.0/24", "ports": [8080]},
                    {"cidr": "203.0.113.7", "ports": []},
                    {"name": "db.example.com", "ports": [5432]},
                ],
                "host_ports": [9100],
                "browser_port": 8900,
                "ssh_port": 2222,
                "remote_direct": False,
                "open_relays": ["proxyman"],
            },
        )
        self.assertEqual(doc["revision"], 1)

    def test_allocation_fields_are_empty_without_bottle_nets(self):
        addr = self._serve(self._broker())
        _, doc = self._get(addr)
        entry = doc["bottles"][BOTTLE_NAME]
        self.assertEqual(doc_errors(doc), [])
        self.assertIsNone(entry["network"])
        self.assertIsNone(entry["subnet"])
        self.assertIsNone(entry["browser_port"])
        self.assertIsNone(entry["ssh_port"])
        self.assertTrue(entry["remote_direct"])

    def test_template_manifest_is_not_a_bottle(self):
        (self.bottles / "TEMPLATE.yml").write_text("task: coding\n")
        _, doc = self._get(self._serve(self._broker()))
        self.assertEqual(list(doc["bottles"]), [BOTTLE_NAME])

    # -- revision: one test per changing cause -----------------------------

    def test_allow_bumps_the_revision_and_adds_the_zone(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)
        self._file_and_allow(b)
        status, doc = self._get(addr)
        self.assertEqual(doc["revision"], before + 1)
        self.assertIn("docs.example.com", doc["bottles"][BOTTLE_NAME]["zones"])
        self.assertEqual(doc_errors(doc), [])

    def test_allow_of_a_non_web_port_is_a_grant(self):
        b = self._broker()
        addr = self._serve(b)
        self._file_and_allow(b, host="db.example.com", port=5432)
        _, doc = self._get(addr)
        grants = doc["bottles"][BOTTLE_NAME]["grants"]
        self.assertEqual(grants, [{"name": "db.example.com", "ports": [5432]}])

    def test_persistent_deny_bumps_the_revision_and_lists_the_zone(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)
        _, rid = b.file_request(BOTTLE_NAME, "evil.example.net", 443)
        result = b.persist_deny("example.net", "global", trigger_request_id=rid)
        self.assertIsNone(result.error)
        _, doc = self._get(addr)
        self.assertEqual(doc["revision"], before + 1)
        self.assertEqual(doc["denylist"], [{"zone": "example.net", "scope": "global"}])
        self.assertEqual(doc_errors(doc), [])

    def test_denylist_edit_through_refresh_bumps_the_revision(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)
        b.denylist.add(zone="telemetry.example", scope="global", reason="x")
        self.assertEqual(self._revision(addr), before)  # not yet seen
        status, body = self._refresh(addr)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, {"revision": before + 1, "changed": True})
        b.denylist.remove(zone="telemetry.example", scope="global")
        self.assertEqual(self._refresh(addr)[1], {"revision": before + 2, "changed": True})

    def test_once_deny_changes_nothing_and_does_not_bump(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)
        _, rid = b.file_request(BOTTLE_NAME, "nope.example.org", 443)
        self.assertIsNone(b.decide(rid, "deny", scope="once"))
        self.assertEqual(self._revision(addr), before)

    def test_refresh_bumps_for_each_manifest_allocation_and_relay_change(self):
        b = self._broker()
        addr = self._serve(b)
        revision = self._revision(addr)
        steps = [
            ("manifest", lambda: self.derived.update(EGRESS="pypi.org,npmjs.org")),
            ("allocation", lambda: self._write_alloc()),
            ("relay", lambda: self.derived.update(OPEN_RELAYS="rhinomcp")),
            ("remote_direct", lambda: setattr(self, "remote_direct", False)),
            ("allocation purge", lambda: (self.home / "run" / bottle_net.ALLOC_FILENAME).unlink()),
        ]
        for label, change in steps:
            change()
            status, body = self._refresh(addr)
            revision += 1
            self.assertEqual(status, HTTPStatus.OK, label)
            self.assertEqual(body, {"revision": revision, "changed": True}, label)
            self.assertEqual(self._revision(addr), revision, label)

    def test_unchanged_refresh_does_not_bump(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)
        for _ in range(3):
            status, body = self._refresh(addr)
            self.assertEqual((status, body), (HTTPStatus.OK, {"revision": before, "changed": False}))
        self.assertEqual(self._revision(addr), before)
        calls = self.derive_calls
        self.assertGreater(calls, 3)  # the refresh really re-read the manifest

    def test_decision_that_changes_nothing_does_not_bump(self):
        b = self._broker()
        addr = self._serve(b)
        self._file_and_allow(b)
        before = self._revision(addr)
        self._file_and_allow(b)  # same host again: document already has it
        self.assertEqual(self._revision(addr), before)

    def test_unreadable_manifest_keeps_the_previous_entry(self):
        b = self._broker()
        addr = self._serve(b)
        before = self._revision(addr)

        def broken(name, manifest):
            raise egress_policy.PolicyError("yq exit=1")

        b.policy._derive = broken
        self.assertEqual(self._refresh(addr)[1], {"revision": before, "changed": False})
        self.assertIn(BOTTLE_NAME, self._get(addr)[1]["bottles"])

    # -- long poll ---------------------------------------------------------

    def test_wait_returns_immediately_when_already_past(self):
        addr = self._serve(self._broker(wait_seconds=5))
        started = time.monotonic()
        status, doc = self._get(addr, "/policy?wait=0")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(doc["revision"], 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_wait_times_out_with_the_unchanged_document(self):
        addr = self._serve(self._broker(wait_seconds=0.3))
        revision = self._revision(addr)
        started = time.monotonic()
        status, doc = self._get(addr, f"/policy?wait={revision}")
        elapsed = time.monotonic() - started
        self.assertEqual((status, doc["revision"]), (HTTPStatus.OK, revision))
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 3)

    def test_wait_wakes_on_a_change(self):
        b = self._broker(wait_seconds=10)
        addr = self._serve(b)
        revision = self._revision(addr)
        result: dict = {}

        def waiter():
            started = time.monotonic()
            result["resp"] = self._get(addr, f"/policy?wait={revision}")
            result["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.3)
        self._file_and_allow(b)
        join_thread_or_fail(thread, label="waiter")
        status, doc = result["resp"]
        self.assertEqual((status, doc["revision"]), (HTTPStatus.OK, revision + 1))
        self.assertLess(result["elapsed"], 5)

    def test_bad_wait_values_are_400(self):
        addr = self._serve(self._broker())
        for query in ("wait=abc", "wait=-1", "wait=", "wait=1&wait=2", "other=1"):
            status, body = self._get(addr, f"/policy?{query}")
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, query)
            self.assertIn("error", body)

    # -- tokens ------------------------------------------------------------

    def test_get_refuses_every_token_but_the_gateway_token(self):
        addr = self._serve(self._broker())
        for label, token in (
            ("operator", OPERATOR),
            ("bottle", BOTTLE),
            ("garbage", "nope"),
            ("none", None),
        ):
            status, body = self._get(addr, token=token)
            self.assertEqual(status, HTTPStatus.UNAUTHORIZED, label)
            self.assertEqual(body, {"error": "unauthorized"}, label)
        self.assertEqual(self._get(addr)[0], HTTPStatus.OK)

    def test_get_refuses_all_when_no_gateway_token_is_provisioned(self):
        b = self._broker()
        tokens = self.root / broker.TOKENS_DIRNAME
        tokens.mkdir(parents=True, exist_ok=True)
        server = broker.EgressBrokerHTTPServer(
            ("127.0.0.1", 0), b, broker.BottleTokenStore(tokens), OPERATOR
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        addr = server.server_address
        wait_for_tcp_listening(*addr)
        for token in (GATEWAY, OPERATOR, ""):
            self.assertEqual(self._get(addr, token=token or None)[0], HTTPStatus.UNAUTHORIZED)

    def test_refresh_refuses_gateway_bottle_and_missing_tokens(self):
        addr = self._serve(self._broker())
        before = self._revision(addr)
        self.derived["EGRESS"] = "changed.example"
        for label, token in (("gateway", GATEWAY), ("bottle", BOTTLE), ("none", None)):
            status, body = self._refresh(addr, token=token)
            self.assertEqual(status, HTTPStatus.UNAUTHORIZED, label)
            self.assertEqual(body, {"error": "unauthorized"}, label)
        self.assertEqual(self._revision(addr), before)  # refusals did not refresh
        self.assertEqual(self._refresh(addr)[0], HTTPStatus.OK)

    def test_gateway_token_cannot_reach_operator_endpoints(self):
        addr = self._serve(self._broker())
        self.assertEqual(self._call(addr, "GET", "/queue", GATEWAY)[0], HTTPStatus.UNAUTHORIZED)

    def test_ensure_gateway_token_is_stable_private_and_distinct(self):
        first = broker.ensure_gateway_token(self.root)
        self.assertEqual(broker.ensure_gateway_token(self.root), first)
        path = self.root / broker.GATEWAY_TOKEN_FILENAME
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(first, broker.ensure_operator_token(self.root))

    # -- persistence -------------------------------------------------------

    def test_revision_survives_a_broker_restart(self):
        b = self._broker()
        addr = self._serve(b)
        self._file_and_allow(b)
        revision = self._revision(addr)
        self.assertEqual(revision, 2)
        b.store.shutdown()
        b2 = self._broker()
        self.assertEqual(b2.policy.revision, revision)  # unchanged inputs: no bump
        self.assertIn("docs.example.com", b2.policy.document()["bottles"][BOTTLE_NAME]["zones"])
        self.derived["EGRESS"] = "pypi.org,more.example"
        b3 = self._broker()
        self.assertEqual(b3.policy.revision, revision + 1)  # change while down moves it forward

    def test_corrupt_policy_file_refuses_to_start_rather_than_reset(self):
        self._broker().store.shutdown()
        (self.root / egress_policy.POLICY_FILENAME).write_text("{not json")
        with self.assertRaises(egress_policy.PolicyError):
            self._broker()

    # -- real derivation ---------------------------------------------------

    @unittest.skipUnless(shutil.which("yq"), "SKIP: yq not installed")
    def test_default_derive_reads_a_real_manifest_through_manifest_py(self):
        manifest = self.bottles / f"{BOTTLE_NAME}.yml"
        manifest.write_text(
            "task: coding\n"
            "capabilities:\n"
            "  egress: [pypi.org, db.example.com:5432]\n"
            "  egress_cidrs: [10.9.0.0/24]\n"
        )
        result = egress_policy.default_derive(REPO_ROOT)(BOTTLE_NAME, manifest)
        # remote.direct is not a derive-accepted key yet: the default holds.
        self.assertTrue(result["remote_direct"])
        self.assertEqual(result["derived"]["EGRESS_CIDRS"], "10.9.0.0/24")
        self.assertIn("db.example.com:5432", result["derived"]["EGRESS"].split(","))
        manifest.write_text("task: [unterminated\n")
        with self.assertRaises(egress_policy.PolicyError):
            egress_policy.default_derive(REPO_ROOT)(BOTTLE_NAME, manifest)

    # -- concurrency -------------------------------------------------------

    def test_concurrent_allow_and_refresh_both_land_and_every_waiter_wakes(self):
        b = self._broker(wait_seconds=15)
        addr = self._serve(b)
        start = self._revision(addr)
        woken: list = []
        lock = threading.Lock()

        def waiter():
            status, doc = self._get(addr, f"/policy?wait={start}")
            with lock:
                woken.append((status, doc["revision"]))

        waiters = [threading.Thread(target=waiter) for _ in range(6)]
        for t in waiters:
            t.start()
        time.sleep(0.3)

        self.derived["EGRESS"] = "pypi.org,refreshed.example"
        _, rid = b.file_request(BOTTLE_NAME, "allowed.example", 443)
        gate = threading.Barrier(2)

        def do_allow():
            gate.wait()
            with mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0)
                b.decide(rid, "allow", scope="live")

        def do_refresh():
            gate.wait()
            self._refresh(addr)

        workers = [threading.Thread(target=do_allow), threading.Thread(target=do_refresh)]
        for t in workers:
            t.start()
        for t in workers + waiters:
            join_thread_or_fail(t, label="concurrent")

        self.assertEqual(len(woken), 6)
        self.assertTrue(all(s == HTTPStatus.OK and r > start for s, r in woken), woken)
        self.assertTrue(all(r <= start + 2 for _, r in woken))
        _, doc = self._get(addr)
        zones = doc["bottles"][BOTTLE_NAME]["zones"]
        self.assertIn("allowed.example", zones)
        self.assertIn("refreshed.example", zones)
        self.assertEqual(doc["revision"], start + 2)
        # Monotonic and persisted: the file on disk is the document served.
        on_disk = json.loads((self.root / egress_policy.POLICY_FILENAME).read_text())
        self.assertEqual(on_disk, doc)

    def test_revision_never_decreases_under_interleaved_causes(self):
        b = self._broker()
        addr = self._serve(b)
        seen: list[int] = []

        def poll():
            for _ in range(30):
                seen.append(self._revision(addr))

        t = threading.Thread(target=poll)
        t.start()
        for i in range(5):
            self.derived["EGRESS"] = f"pypi.org,z{i}.example"
            self._refresh(addr)
        join_thread_or_fail(t, label="poller")
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(self._revision(addr), 1 + 5)


if __name__ == "__main__":
    unittest.main()
