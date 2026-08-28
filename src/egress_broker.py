#!/usr/bin/env python3
"""egress_broker.py — in-container transparent egress broker (djinnbroker uid).

Accepts TCP connections redirected by iptables REDIRECT (B3), recovers the
intended destination via SO_ORIGINAL_DST, and either fast-paths allowed traffic
through the ipset or files an approval request with the host daemon and holds
the client until allow/deny/timeout. Stdlib only; runs as djinnbroker.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from egress_broker_host import DEFAULT_HOLD_SECONDS, normalize_destination, undeny_hint
from egress_nflog import (
    default_broker_url,
    host_for_filing,
    load_broker_token,
    reverse_dns,
)

LOG = logging.getLogger(__name__)

BROKER_LISTEN_HOST = "127.0.0.1"
BROKER_LISTEN_PORT = 3128
BROKER_FIREWALL_SCRIPT = "/usr/local/bin/egress_broker_firewall.sh"
SUPERVISOR_LISTEN_TIMEOUT = 15.0
IPSET_NAME = "allowed-domains"
SOL_IP = 0
SO_ORIGINAL_DST = 80

TLS_HANDSHAKE_RECORD = 0x16
TLS_CLIENT_HELLO = 0x01
TLS_EXTENSION_SERVER_NAME = 0x0000
TLS_SNI_HOST_NAME = 0x00

DEFAULT_PEEK_TIMEOUT = 5.0
DEFAULT_MAX_PEEK_BYTES = 16_384

def generate_request_id() -> str:
    """Mint a short correlation id before filing with the host daemon."""
    return uuid.uuid4().hex[:8]


def broker_firewall_script() -> str:
    """Resolve the firewall helper path (repo checkout vs installed image)."""
    local = Path(__file__).resolve().parent / "egress_broker_firewall.sh"
    if local.is_file():
        return str(local)
    return BROKER_FIREWALL_SCRIPT


def remove_broker_firewall_rules(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> None:
    """Drop B3 REDIRECT + companion filter (watchdog / fail-safe)."""
    run = runner or subprocess.run
    run(
        [broker_firewall_script(), "remove"],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_for_broker_listen(
    host: str,
    port: int,
    *,
    timeout: float = SUPERVISOR_LISTEN_TIMEOUT,
    now_fn: NowFn = time.monotonic,
) -> bool:
    """Return True once something accepts TCP on host:port."""
    deadline = now_fn() + timeout
    while now_fn() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False

TLS_ACCESS_DENIED_ALERT = bytes([0x15, 0x03, 0x03, 0x00, 0x02, 0x02, 0x31])

ConnectFn = Callable[[tuple[str, int], float], socket.socket]
IpsetCheckFn = Callable[[str], bool]
FileFn = Callable[..., tuple[dict[str, Any] | None, str | None]]
NowFn = Callable[[], float]


class BrokerPeekError(Exception):
    """ClientHello or HTTP peek failed or exceeded bounds."""


@dataclass(frozen=True)
class OriginalDestination:
    """Recovered pre-REDIRECT destination."""

    ip: str
    port: int


@dataclass
class BrokerConfig:
    """Runtime configuration for the in-container broker."""

    container: str
    broker_url: str
    broker_token: str
    hold_seconds: int = DEFAULT_HOLD_SECONDS
    listen_host: str = BROKER_LISTEN_HOST
    listen_port: int = BROKER_LISTEN_PORT
    peek_timeout: float = DEFAULT_PEEK_TIMEOUT
    max_peek_bytes: int = DEFAULT_MAX_PEEK_BYTES


def is_loopback_destination(ip: str) -> bool:
    """True when the recovered original destination is loopback.

    Loopback is never egress, so it must never be filed with the operator.
    Two ways a connection lands here with a 127.0.0.0/8 destination:

      - A direct dial to the broker's own listen port. The companion filter
        rule ACCEPTs 127.0.0.1:3128, so an app configured with
        `http_proxy=http://127.0.0.1:3128` connects straight in. No NAT
        happened, so SO_ORIGINAL_DST returns the socket's own address and the
        broker files ITSELF as the destination — an IP-literal request the
        operator cannot allow (IP literals need a manifest CIDR) and, until
        the watcher's skip is fixed, cannot get rid of either. Observed as an
        endless `Allow traffic to 127.0.0.1? … 127.0.0.1:3128` prompt.
      - A local service on :80/:443. The nat rule now excludes 127.0.0.0/8,
        but a rule installed before this fix (or removed by hand) would send
        a local dev server's traffic through here.

    Malformed input is not loopback: get_original_dst has already validated
    the family, so anything unparseable here is treated as a real destination
    rather than silently fast-pathed.
    """
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def parse_original_dst_bytes(raw: bytes) -> OriginalDestination:
    """Parse SO_ORIGINAL_DST sockaddr bytes into (ip, port)."""
    if len(raw) < 16:
        raise ValueError("SO_ORIGINAL_DST payload too short")
    # struct sockaddr_in mixes byte orders: sin_family is HOST order, while
    # sin_port and sin_addr are NETWORK order. Reading the whole struct as
    # big-endian yields family=512 (0x0200) on a little-endian host and the
    # connection is rejected before it starts — verified against real kernel
    # bytes, not a hand-built fixture.
    (family,) = struct.unpack("=H", raw[0:2])
    (port,) = struct.unpack("!H", raw[2:4])
    a, b, c, d = raw[4:8]
    if family != socket.AF_INET:
        raise ValueError(f"unsupported address family {family}")
    return OriginalDestination(ip=f"{a}.{b}.{c}.{d}", port=port)


def get_original_dst(sock: socket.socket) -> OriginalDestination:
    """Recover the client's intended destination from a redirected socket."""
    raw = sock.getsockopt(SOL_IP, SO_ORIGINAL_DST, 16)
    return parse_original_dst_bytes(raw)


def tls_record_length(buffer: bytes) -> int | None:
    """Return full TLS record length, or None when more bytes are needed."""
    if len(buffer) < 5:
        return None
    if buffer[0] != TLS_HANDSHAKE_RECORD:
        raise BrokerPeekError("expected TLS handshake record")
    rec_len = struct.unpack("!H", buffer[3:5])[0]
    total = 5 + rec_len
    if total > DEFAULT_MAX_PEEK_BYTES:
        raise BrokerPeekError("TLS record exceeds peek cap")
    if len(buffer) < total:
        return None
    return total


def _parse_client_hello_extensions(
  data: bytes,
  offset: int,
) -> dict[int, bytes]:
    extensions: dict[int, bytes] = {}
    if offset + 2 > len(data):
        return extensions
    ext_total = struct.unpack("!H", data[offset : offset + 2])[0]
    pos = offset + 2
    end = pos + ext_total
    while pos + 4 <= end and pos + 4 <= len(data):
        ext_type, ext_len = struct.unpack("!HH", data[pos : pos + 4])
        pos += 4
        ext_data = data[pos : pos + ext_len]
        pos += ext_len
        extensions[ext_type] = ext_data
    return extensions


def _sni_from_extension(payload: bytes) -> str | None:
    if len(payload) < 2:
        return None
    list_len = struct.unpack("!H", payload[:2])[0]
    pos = 2
    end = min(2 + list_len, len(payload))
    while pos + 3 <= end:
        name_type = payload[pos]
        name_len = struct.unpack("!H", payload[pos + 1 : pos + 3])[0]
        pos += 3
        name = payload[pos : pos + name_len]
        pos += name_len
        if name_type != TLS_SNI_HOST_NAME:
            continue
        try:
            return name.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def extract_sni_from_client_hello(buffer: bytes) -> str | None:
    """Extract SNI from a complete TLS ClientHello record (first record only)."""
    total = tls_record_length(buffer)
    if total is None:
        raise BrokerPeekError("incomplete TLS record")
    record = buffer[:total]
    if len(record) < 5 + 4:
        raise BrokerPeekError("truncated TLS handshake")
    if record[5] != TLS_CLIENT_HELLO:
        raise BrokerPeekError("expected ClientHello")

    pos = 5 + 4  # skip handshake header
    if pos + 2 + 32 + 1 > len(record):
        raise BrokerPeekError("truncated ClientHello body")
    pos += 2 + 32  # version + random
    session_len = record[pos]
    pos += 1 + session_len
    if pos + 2 > len(record):
        raise BrokerPeekError("truncated cipher suites length")
    cipher_len = struct.unpack("!H", record[pos : pos + 2])[0]
    pos += 2 + cipher_len
    if pos + 1 > len(record):
        raise BrokerPeekError("truncated compression methods length")
    comp_len = record[pos]
    pos += 1 + comp_len
    extensions = _parse_client_hello_extensions(record, pos)
    sni_ext = extensions.get(TLS_EXTENSION_SERVER_NAME)
    if not sni_ext:
        return None
    return _sni_from_extension(sni_ext)


def http_headers_complete(buffer: bytes) -> bool:
    return b"\r\n\r\n" in buffer


def parse_http_host(buffer: bytes) -> str | None:
    """Return the Host header value, or None when absent or incomplete."""
    if not http_headers_complete(buffer):
        return None
    header_blob, _, _ = buffer.partition(b"\r\n\r\n")
    try:
        text = header_blob.decode("latin-1")
    except UnicodeDecodeError:
        raise BrokerPeekError("invalid HTTP header encoding") from None
    host: str | None = None
    for line in text.split("\r\n")[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() == "host":
            host = value.strip()
            break
    if host is None:
        return None
    if host.startswith("[") and "]" in host:
        inner, _, rest = host.partition("]")
        candidate = inner[1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return candidate
        return candidate
    if host.count(":") == 1:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            return host_part
    return host


def filing_host_for_destination(
    *,
    dst_ip: str,
    dst_port: int,
    peek_buffer: bytes,
    reverse_dns_fn: Callable[[str], str | None] = reverse_dns,
) -> tuple[str, bool]:
    """Choose the host field for filing from peeked bytes or IP fallback."""
    candidate: str | None = None
    if dst_port == 443:
        if tls_record_length(peek_buffer) is not None:
            candidate = extract_sni_from_client_hello(peek_buffer)
    elif dst_port == 80:
        if http_headers_complete(peek_buffer):
            candidate = parse_http_host(peek_buffer)

    if candidate:
        try:
            return normalize_destination(candidate)
        except ValueError:
            LOG.info(
                "egress broker peek host invalid dst=%s port=%d host=%s",
                dst_ip,
                dst_port,
                candidate,
            )

    fallback = host_for_filing(dst_ip)
    try:
        return normalize_destination(fallback)
    except ValueError:
        return fallback, True


def build_http_503(host: str, request_id: str, bottle: str) -> bytes:
    reason = f"Egress pending approval: {host} (req {request_id})"
    body = (
        f"Outbound access to {host} is pending operator approval.\n"
        f"On your Mac, run:\n\n"
        f"  ./djinn allow {bottle} {host}\n\n"
        f"Then retry this request (request id {request_id}).\n"
    ).encode("utf-8")
    headers = [
        f"HTTP/1.1 503 {reason}",
        "Retry-After: 30",
        "X-Djinn-Egress: pending",
        f"X-Djinn-Egress-Host: {host}",
        f"X-Djinn-Egress-Request: {request_id}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def build_http_403(
    host: str,
    request_id: str,
    *,
    denylist_zone: str | None = None,
    denylist_scope: str | None = None,
) -> bytes:
    if denylist_zone is not None and denylist_scope is not None:
        reason = f"Egress denied: {host} is on the persistent deny list (req {request_id})"
        body = (
            f"Outbound access to {host} is on the persistent deny list "
            f"(zone {denylist_zone}, scope {denylist_scope}) "
            f"(request id {request_id}).\n"
            f"An operator can lift it with `{undeny_hint(denylist_zone, denylist_scope)}`.\n"
        ).encode("utf-8")
    else:
        reason = f"Egress denied: {host} (req {request_id})"
        body = (
            f"Outbound access to {host} was denied by the operator "
            f"(request id {request_id}).\n"
        ).encode("utf-8")
    headers = [
        f"HTTP/1.1 403 {reason}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _decision_outcome(body: object) -> str:
    """Map a host-daemon decision body to a broker outcome.

    Anything that is not an explicit allow/deny/pending is a FAULT, never an
    allow. In particular decision="error" means the host attempted the grant
    and it did not take — allow-egress.sh failed, or the destination is an IP
    literal needing a manual CIDR. Reporting that as "allow" would send the
    caller retrying into a rule that was never installed.
    """
    if not isinstance(body, dict):
        return "daemon_error"
    decision = body.get("decision")
    if decision in ("allow", "deny", "pending"):
        return decision
    return "daemon_error"


def build_http_502() -> bytes:
    body = b"Egress approval broker unreachable.\n"
    headers = [
        "HTTP/1.1 502 Egress broker unreachable",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def ipset_allowed(
    dst_ip: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Return True when dst_ip is already in the allowed-domains ipset."""
    run = runner or subprocess.run
    try:
        result = run(
            ["ipset", "test", IPSET_NAME, dst_ip],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.info("egress broker ipset test failed ip=%s error=%s", dst_ip, exc)
        return False
    return result.returncode == 0


def file_egress_with_hold(
    *,
    url: str,
    token: str,
    container: str,
    host: str,
    port: int,
    request_id: str,
    host_is_ip: bool = False,
    hold_seconds: int = DEFAULT_HOLD_SECONDS,
    timeout: float | None = None,
    reason: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST /egress and return (json_body, error_reason)."""
    if not token:
        return None, "missing_broker_token"

    payload: dict[str, Any] = {
        "container": container,
        "host": host,
        "port": port,
        "hold_seconds": hold_seconds,
        "request_id": request_id,
    }
    if host_is_ip:
        payload["host_is_ip"] = True
    if reason is not None:
        payload["reason"] = reason

    endpoint = url.rstrip("/") + "/egress"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    wait = timeout if timeout is not None else float(hold_seconds) + 5.0
    try:
        with open_fn(request, timeout=wait) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return None, str(exc.reason)
    except TimeoutError:
        return None, "timeout"

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def peek_initial_bytes(
    sock: socket.socket,
    *,
    dst_port: int,
    timeout: float,
    max_bytes: int,
    now_fn: NowFn = time.monotonic,
) -> bytes:
    """Read until the protocol peek is complete or timeout/cap is hit."""
    sock.setblocking(False)
    buffer = bytearray()
    deadline = now_fn() + timeout
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    try:
        while now_fn() < deadline:
            if dst_port == 443:
                complete = tls_record_length(buffer) is not None
            elif dst_port == 80:
                complete = http_headers_complete(buffer)
            else:
                complete = True
            if complete:
                return bytes(buffer)

            remaining = deadline - now_fn()
            events = sel.select(timeout=max(0.0, remaining))
            if not events:
                break
            try:
                chunk = sock.recv(max_bytes - len(buffer))
            except BlockingIOError:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise BrokerPeekError("peek buffer cap exceeded")
        if dst_port == 443 and buffer:
            if tls_record_length(buffer) is None:
                raise BrokerPeekError("incomplete TLS record")
        if dst_port == 80 and buffer and not http_headers_complete(buffer):
            raise BrokerPeekError("incomplete HTTP headers")
        return bytes(buffer)
    finally:
        sel.unregister(sock)
        sel.close()


def relay_sockets(
    left: socket.socket,
    right: socket.socket,
    *,
    initial_left: bytes = b"",
) -> None:
    """Bidirectional relay until both sides close."""
    if initial_left:
        right.sendall(initial_left)
    left.setblocking(False)
    right.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(left, selectors.EVENT_READ)
    sel.register(right, selectors.EVENT_READ)
    open_sides = {left, right}
    try:
        while open_sides:
            for key, _ in sel.select(timeout=1.0):
                src = key.fileobj
                assert isinstance(src, socket.socket)
                dst = right if src is left else left
                try:
                    data = src.recv(8192)
                except BlockingIOError:
                    continue
                if not data:
                    sel.unregister(src)
                    open_sides.discard(src)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dst.sendall(data)
    finally:
        sel.close()


def wait_for_filing_or_client_abort(
    client: socket.socket,
    filing_thread: threading.Thread,
    filing_result: dict[str, Any | None],
    filing_error: dict[str, str | None],
    *,
    deadline: float,
    now_fn: NowFn = time.monotonic,
) -> str:
    """Wait until filing completes, client aborts, or hold deadline passes."""
    client.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ)
    outcome = "pending"
    try:
        while now_fn() < deadline:
            if not filing_thread.is_alive():
                if filing_error.get("value"):
                    return "daemon_error"
                return _decision_outcome(filing_result.get("value"))

            remaining = deadline - now_fn()
            events = sel.select(timeout=min(1.0, max(0.0, remaining)))
            if events:
                try:
                    chunk = client.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    continue
                if chunk == b"":
                    return "client_abort"
        return "pending"
    finally:
        sel.unregister(client)
        sel.close()


@dataclass
class ConnectionOutcome:
    """Summary of one handled intercepted connection (for tests/logging)."""

    action: str
    dst_ip: str
    dst_port: int
    host: str | None = None
    request_id: str | None = None
    filed: bool = False
    upstream_bytes: bytes = field(default_factory=bytes)


def handle_intercepted_connection(
    client: socket.socket,
    *,
    config: BrokerConfig,
    ipset_check: IpsetCheckFn = ipset_allowed,
    file_fn: FileFn = file_egress_with_hold,
    connect_fn: ConnectFn | None = None,
    reverse_dns_fn: Callable[[str], str | None] = reverse_dns,
    now_fn: NowFn = time.monotonic,
    conn_id: str | None = None,
) -> ConnectionOutcome:
    """Handle one redirected client connection to completion."""
    started = now_fn()
    conn_id = conn_id or f"conn-{id(client)}"
    connect = connect_fn or (lambda addr, t: socket.create_connection(addr, timeout=t))

    dst = get_original_dst(client)
    LOG.info(
        "egress broker accept conn_id=%s dst=%s:%d",
        conn_id,
        dst.ip,
        dst.port,
    )

    loopback = is_loopback_destination(dst.ip)
    if loopback and dst.port == config.listen_port:
        # Relaying would dial this same listener again. Refuse loudly instead:
        # the cause is always a misconfigured client pointing an http_proxy at
        # the broker, which is a TRANSPARENT proxy (it reads the destination
        # from the kernel), not a forward proxy.
        LOG.warning(
            "egress broker self_dial conn_id=%s dst=%s:%d "
            "(client has a proxy configured at the broker port; "
            "the broker is transparent and takes no proxy clients)",
            conn_id,
            dst.ip,
            dst.port,
        )
        _emit_failure(
            client,
            dst.port,
            host=dst.ip,
            request_id="unknown",
            bottle=config.container,
            reason="daemon_error",
        )
        return ConnectionOutcome(action="self_dial", dst_ip=dst.ip, dst_port=dst.port)

    # Loopback shares the allowlist fast path: it is local traffic, so it is
    # spliced straight through and never filed.
    if loopback or ipset_check(dst.ip):
        LOG.info(
            "egress broker fast_path conn_id=%s dst=%s:%d via=%s",
            conn_id,
            dst.ip,
            dst.port,
            "loopback" if loopback else "allowlist",
        )
        peek = peek_initial_bytes(
            client,
            dst_port=dst.port,
            timeout=config.peek_timeout,
            max_bytes=config.max_peek_bytes,
            now_fn=now_fn,
        )
        upstream = connect((dst.ip, dst.port), 5.0)
        relay_started = now_fn()
        relay_sockets(client, upstream, initial_left=peek)
        LOG.info(
            "egress broker splice_close conn_id=%s duration=%.3fs bytes=%d",
            conn_id,
            now_fn() - relay_started,
            len(peek),
        )
        return ConnectionOutcome(
            action="fast_path",
            dst_ip=dst.ip,
            dst_port=dst.port,
            upstream_bytes=peek,
        )

    try:
        peek = peek_initial_bytes(
            client,
            dst_port=dst.port,
            timeout=config.peek_timeout,
            max_bytes=config.max_peek_bytes,
            now_fn=now_fn,
        )
    except BrokerPeekError as exc:
        LOG.info(
            "egress broker peek_failed conn_id=%s dst=%s:%d error=%s",
            conn_id,
            dst.ip,
            dst.port,
            exc,
        )
        _emit_failure(client, dst.port, host=dst.ip, request_id="unknown", bottle=config.container, reason="daemon_error")
        return ConnectionOutcome(action="peek_failed", dst_ip=dst.ip, dst_port=dst.port)

    host, host_is_ip = filing_host_for_destination(
        dst_ip=dst.ip,
        dst_port=dst.port,
        peek_buffer=peek,
        reverse_dns_fn=reverse_dns_fn,
    )
    request_id = generate_request_id()
    LOG.info(
        "egress broker file conn_id=%s host=%s port=%d host_is_ip=%s peek_bytes=%d request_id=%s",
        conn_id,
        host,
        dst.port,
        host_is_ip,
        len(peek),
        request_id,
    )

    filing_result: dict[str, Any | None] = {"value": None}
    filing_error: dict[str, str | None] = {"value": None}

    def _file_worker() -> None:
        body, err = file_fn(
            url=config.broker_url,
            token=config.broker_token,
            container=config.container,
            host=host,
            port=dst.port,
            request_id=request_id,
            host_is_ip=host_is_ip,
            hold_seconds=config.hold_seconds,
        )
        filing_result["value"] = body
        filing_error["value"] = err

    filing_thread = threading.Thread(target=_file_worker, name=f"egress-file-{conn_id}", daemon=True)
    file_started = now_fn()
    filing_thread.start()

    outcome = wait_for_filing_or_client_abort(
        client,
        filing_thread,
        filing_result,
        filing_error,
        deadline=file_started + config.hold_seconds,
        now_fn=now_fn,
    )

    if outcome == "client_abort":
        LOG.info("egress broker client_abort conn_id=%s request_id=%s", conn_id, request_id)
        return ConnectionOutcome(
            action="client_abort",
            dst_ip=dst.ip,
            dst_port=dst.port,
            host=host,
            request_id=request_id,
            filed=True,
        )

    if outcome == "allow":
        LOG.info(
            "egress broker decide_allow conn_id=%s request_id=%s duration=%.3fs",
            conn_id,
            request_id,
            now_fn() - file_started,
        )
        upstream = connect((dst.ip, dst.port), 5.0)
        upstream.sendall(peek)
        relay_sockets(client, upstream)
        LOG.info(
            "egress broker splice_close conn_id=%s request_id=%s replay_bytes=%d",
            conn_id,
            request_id,
            len(peek),
        )
        return ConnectionOutcome(
            action="allow",
            dst_ip=dst.ip,
            dst_port=dst.port,
            host=host,
            request_id=request_id,
            filed=True,
            upstream_bytes=peek,
        )

    if outcome == "deny":
        decision_body = filing_result.get("value")
        denylist_zone: str | None = None
        denylist_scope: str | None = None
        if isinstance(decision_body, dict) and decision_body.get("reason") == "denylist":
            zone_val = decision_body.get("zone")
            scope_val = decision_body.get("scope")
            if isinstance(zone_val, str) and isinstance(scope_val, str):
                denylist_zone, denylist_scope = zone_val, scope_val
        if denylist_zone is not None:
            LOG.info(
                "egress broker decide_deny_denylist conn_id=%s request_id=%s zone=%s scope=%s",
                conn_id,
                request_id,
                denylist_zone,
                denylist_scope,
            )
        else:
            LOG.info("egress broker decide_deny conn_id=%s request_id=%s", conn_id, request_id)
        _emit_failure(
            client,
            dst.port,
            host=host,
            request_id=request_id,
            bottle=config.container,
            reason="deny",
            denylist_zone=denylist_zone,
            denylist_scope=denylist_scope,
        )
        return ConnectionOutcome(
            action="deny",
            dst_ip=dst.ip,
            dst_port=dst.port,
            host=host,
            request_id=request_id,
            filed=True,
        )

    if outcome == "daemon_error":
        LOG.info("egress broker daemon_error conn_id=%s request_id=%s", conn_id, request_id)
        _emit_failure(client, dst.port, host=host, request_id=request_id, bottle=config.container, reason="daemon_error")
        return ConnectionOutcome(
            action="daemon_error",
            dst_ip=dst.ip,
            dst_port=dst.port,
            host=host,
            request_id=request_id,
            filed=True,
        )

    LOG.info(
        "egress broker hold_timeout conn_id=%s request_id=%s duration=%.3fs",
        conn_id,
        request_id,
        now_fn() - started,
    )
    _emit_failure(client, dst.port, host=host, request_id=request_id, bottle=config.container, reason="pending")
    return ConnectionOutcome(
        action="pending",
        dst_ip=dst.ip,
        dst_port=dst.port,
        host=host,
        request_id=request_id,
        filed=True,
    )


def _emit_failure(
    client: socket.socket,
    dst_port: int,
    *,
    host: str,
    request_id: str,
    bottle: str,
    reason: str,
    denylist_zone: str | None = None,
    denylist_scope: str | None = None,
) -> None:
    if dst_port == 443:
        # TLS carries no room for a message; the LOG line at the call site
        # already distinguishes a denylist hit from an operator deny.
        client.sendall(TLS_ACCESS_DENIED_ALERT)
        return
    if reason == "deny":
        client.sendall(
            build_http_403(
                host,
                request_id,
                denylist_zone=denylist_zone,
                denylist_scope=denylist_scope,
            )
        )
    elif reason == "daemon_error":
        client.sendall(build_http_502())
    else:
        client.sendall(build_http_503(host, request_id, bottle))


@dataclass
class _ActiveConnection:
    client: socket.socket
    conn_id: str
    future: Future[ConnectionOutcome] | None = None


class EgressBrokerServer:
    """selectors-driven accept loop for redirected egress connections."""

    def __init__(
        self,
        config: BrokerConfig,
        *,
        ipset_check: IpsetCheckFn = ipset_allowed,
        file_fn: FileFn = file_egress_with_hold,
        connect_fn: ConnectFn | None = None,
        max_workers: int = 32,
    ) -> None:
        self._config = config
        self._ipset_check = ipset_check
        self._file_fn = file_fn
        self._connect_fn = connect_fn
        self._selector = selectors.DefaultSelector()
        self._listen: socket.socket | None = None
        self._stop = threading.Event()
        self._next_conn = 0
        self._active: dict[socket.socket, _ActiveConnection] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="egress-conn")

    def serve_forever(self) -> None:
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind((self._config.listen_host, self._config.listen_port))
        listen.listen(128)
        listen.setblocking(False)
        self._listen = listen
        self._selector.register(listen, selectors.EVENT_READ)
        LOG.info(
            "egress broker listen host=%s port=%d container=%s",
            self._config.listen_host,
            self._config.listen_port,
            self._config.container,
        )
        try:
            while not self._stop.is_set():
                for key, _ in self._selector.select(timeout=1.0):
                    if key.fileobj is listen:
                        client, _addr = listen.accept()
                        self._next_conn += 1
                        conn_id = f"c{self._next_conn}"
                        client.setblocking(True)
                        future = self._executor.submit(
                            self._serve_connection,
                            client,
                            conn_id,
                        )
                        self._active[client] = _ActiveConnection(
                            client=client,
                            conn_id=conn_id,
                            future=future,
                        )
                self._reap_finished()
        finally:
            self._selector.unregister(listen)
            self._selector.close()
            listen.close()
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _serve_connection(self, client: socket.socket, conn_id: str) -> ConnectionOutcome:
        try:
            return handle_intercepted_connection(
                client,
                config=self._config,
                ipset_check=self._ipset_check,
                file_fn=self._file_fn,
                connect_fn=self._connect_fn,
                conn_id=conn_id,
            )
        except Exception:
            LOG.exception("egress broker connection error conn_id=%s", conn_id)
            return ConnectionOutcome(action="error", dst_ip="", dst_port=0)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _reap_finished(self) -> None:
        done: list[socket.socket] = []
        for sock, active in self._active.items():
            if active.future is not None and active.future.done():
                done.append(sock)
        for sock in done:
            self._active.pop(sock, None)

    def stop(self) -> None:
        self._stop.set()


def drop_to_djinnbroker() -> None:
    """Drop privileges to the djinnbroker uid (no-op when already non-root)."""
    try:
        import pwd

        pw = pwd.getpwnam("djinnbroker")
    except KeyError as exc:
        raise RuntimeError("djinnbroker user is missing") from exc
    if os.geteuid() == 0:
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)


def build_config_from_env() -> BrokerConfig:
    container = os.environ.get("CONTAINER_NAME", "unnamed")
    hold_raw = os.environ.get("EGRESS_HOLD_SECONDS", "")
    hold_seconds = DEFAULT_HOLD_SECONDS
    if hold_raw.isdigit():
        hold_seconds = int(hold_raw)
    return BrokerConfig(
        container=container,
        broker_url=default_broker_url(),
        broker_token=load_broker_token(),
        hold_seconds=hold_seconds,
    )


def run_broker(config: BrokerConfig | None = None) -> None:
    cfg = config or build_config_from_env()
    drop_to_djinnbroker()
    EgressBrokerServer(cfg).serve_forever()


def supervise_broker(
    config: BrokerConfig | None = None,
    *,
    listen_timeout: float = SUPERVISOR_LISTEN_TIMEOUT,
    firewall_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> int:
    """Run as root: fork broker child, wait for listen, watchdog-remove rules on exit."""
    if os.geteuid() != 0:
        LOG.error("egress broker supervise requires root")
        return 1

    cfg = config or build_config_from_env()
    pid = os.fork()
    if pid == 0:
        try:
            run_broker(cfg)
        except Exception:
            LOG.exception("egress broker child fatal")
        finally:
            os._exit(1)

    if not wait_for_broker_listen(
        cfg.listen_host,
        cfg.listen_port,
        timeout=listen_timeout,
    ):
        os.kill(pid, signal.SIGTERM)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        remove_broker_firewall_rules(runner=firewall_runner)
        LOG.error(
            "egress broker failed to listen on %s:%d",
            cfg.listen_host,
            cfg.listen_port,
        )
        return 1

    LOG.info(
        "egress broker supervised pid=%d listen=%s:%d",
        pid,
        cfg.listen_host,
        cfg.listen_port,
    )
    try:
        _dead, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 1
    remove_broker_firewall_rules(runner=firewall_runner)
    LOG.error("egress broker exited status=%s", status)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="djinn in-container egress broker")
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=None,
        help=f"approval hold window (default {DEFAULT_HOLD_SECONDS})",
    )
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="run as root supervisor (fork, listen check, firewall watchdog)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    config = build_config_from_env()
    if args.hold_seconds is not None:
        config = BrokerConfig(
            container=config.container,
            broker_url=config.broker_url,
            broker_token=config.broker_token,
            hold_seconds=args.hold_seconds,
            listen_host=config.listen_host,
            listen_port=config.listen_port,
            peek_timeout=config.peek_timeout,
            max_peek_bytes=config.max_peek_bytes,
        )
    try:
        if args.supervise:
            return supervise_broker(config)
        run_broker(config)
    except RuntimeError as exc:
        LOG.error("egress broker fatal: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
