#!/usr/bin/env python3
"""Record legacy admin daemon responses for the spa-mode regression fixture.

Drives the CURRENT src/admin_daemon.py over HTTP, before any spa-mode changes,
and writes tests/fixtures/admin_legacy_responses.json. Re-run this script from
the repo root whenever the legacy response contract intentionally changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

import admin_daemon as admin
from egress_test_sync import join_thread_or_fail, wait_for_tcp_listening


def _start_admin(home: Path) -> tuple[admin.AdminHTTPServer, threading.Thread]:
    egress_root = home / "run" / "egress"
    egress_root.mkdir(parents=True, exist_ok=True)
    token_path = egress_root / admin.OPERATOR_TOKEN_FILENAME
    token_path.write_text("operator-test-token\n", encoding="utf-8")
    server = admin.AdminHTTPServer(
        ("127.0.0.1", 0),
        egress_root=egress_root,
        session_secret="session-secret",
        operator_token="operator-test-token",
        admin_key="admin-test-key",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    env_map = {"DJINN_HOME": str(home)}
    patcher = mock.patch.dict(os.environ, env_map, clear=False)
    patcher.start()
    thread.start()
    wait_for_tcp_listening(server.server_address[0], server.server_address[1])
    return server, thread


def _request(
    host: str,
    port: int,
    path: str,
    *,
    cookie: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection(host, port, timeout=5)
    headers: dict[str, str] = {}
    if cookie is not None:
        headers["Cookie"] = f"{admin.SESSION_COOKIE_NAME}={cookie}"
    conn.request("GET", path, body=None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    resp_headers = {k: v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, resp_headers, raw


def _record(host: str, port: int, path: str, cookie: str | None) -> dict:
    status, headers, raw = _request(host, port, path, cookie=cookie)
    filtered: dict[str, str | bool] = {}
    for name in ("Content-Type", "Cache-Control", "Location"):
        value = headers.get(name)
        if value is not None:
            filtered[name] = value
    filtered["Set-Cookie"] = "Set-Cookie" in headers
    return {
        "route": path,
        "with_session": cookie is not None,
        "status": status,
        "headers": filtered,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        server, thread = _start_admin(home)
        host, port = server.server_address
        try:
            routes = [
                "/",
                "/health",
                "/session?key=admin-test-key",
                "/app.js",
                "/vendor/htm-preact-standalone.module.js",
                "/manifest.webmanifest",
                "/sw.js",
                "/icon-192.png",
                "/icon-512.png",
                "/nope",
            ]
            records: list[dict] = []
            for path in routes:
                records.append(_record(host, port, path, cookie=None))
                if path.startswith("/session?key="):
                    # After minting the session cookie, record the WITH-cookie
                    # variant too. The /session endpoint itself ignores the
                    # cookie, but the fixture captures that the response is
                    # identical (302, Set-Cookie, Location).
                    records.append(_record(host, port, path, cookie="session-secret"))
                elif path != "/nope":
                    records.append(_record(host, port, path, cookie="session-secret"))
                else:
                    # Unknown path should 404 regardless of session.
                    records.append(_record(host, port, path, cookie="session-secret"))
        finally:
            server.shutdown()
            server.server_close()
            join_thread_or_fail(thread, label="admin")

    fixture_path = TESTS_DIR / "fixtures" / "admin_legacy_responses.json"
    fixture_path.write_text(
        json.dumps(records, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records to {fixture_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
