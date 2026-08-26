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
    timeout: float = 10.0,
    poll: float = 0.01,
) -> str:
    """Poll until the broker has at least one open request; return its id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with b._lock:
            if b._requests:
                return next(iter(b._requests))
        time.sleep(poll)
    raise TimeoutError(f"broker had no open request after {timeout}s")


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
