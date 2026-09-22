#!/usr/bin/env python3
"""Synchronisation helpers for egress unit tests (no fixed sleeps as barriers)."""

from __future__ import annotations

import socket
import threading
import time

import egress_broker_host as broker


def wait_for_tcp_listening(
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    poll: float = 0.01,
) -> None:
    """Poll until a TCP connect to host:port succeeds."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(poll)
    raise TimeoutError(
        f"TCP {host}:{port} not accepting connections after {timeout}s: {last_error}"
    )


def wait_for_broker_open_request(
    b: broker.EgressBroker,
    *,
    count: int = 1,
    timeout: float = 10.0,
    poll: float = 0.01,
) -> str:
    """Poll until the broker's store has at least `count` open rows; return one id.

    Waiting on the observable state — the request actually reaching the
    store — rather than on a fixed sleep is what keeps these tests steady
    on a loaded CI runner. (file_request is synchronous now; this helper
    still exists for the HTTP-path tests where the filing happens on a
    server thread.)
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = b._store.list_open()
        if len(rows) >= count:
            return rows[0].request_id
        time.sleep(poll)
    raise TimeoutError(f"broker had fewer than {count} open request(s) after {timeout}s")


def join_thread_or_fail(
    thread: threading.Thread,
    *,
    timeout: float = 5.0,
    label: str = "thread",
) -> None:
    """Join a thread and fail loudly if it is still running."""
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError(f"{label} still alive after {timeout}s join")
