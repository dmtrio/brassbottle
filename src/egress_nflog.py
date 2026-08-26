#!/usr/bin/env python3
"""egress_nflog.py — NFLOG reader for blocked egress (container-side, root).

Copies blocked outbound packets from iptables NFLOG group 32, resolves the
destination, and files an egress approval request with the host broker over
HTTP. Stdlib only; runs inside the container as root.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from egress_broker_host import DEFAULT_PORT, HIT_COALESCE_SECONDS, normalize_host

LOG = logging.getLogger(__name__)

NETLINK_NETFILTER = 12
NFNL_SUBSYS_ULOG = 4
NFULNL_MSG_CONFIG = 1
MSG_CONFIG = (NFNL_SUBSYS_ULOG << 8) | NFULNL_MSG_CONFIG
NFULNL_CFG_CMD_BIND = 1
NFULNL_CFG_CMD_PF_BIND = 3
NFULA_CFG_CMD = 1
NFULA_CFG_MODE = 2
NFULNL_COPY_PACKET = 2

NFULA_PAYLOAD = 9
NFULA_PREFIX = 10
NFULA_UID = 11

NLM_F_REQUEST = 1
NLM_F_ACK = 4
NLMSG_ERROR = 2

DEFAULT_GROUP = 32
DEFAULT_PREFIX = "djinn-egress"
DEFAULT_BROKER_HOST = "host.docker.internal"


class NflogError(Exception):
    """NFLOG setup or netlink protocol error."""


@dataclass(frozen=True)
class ParsedPacket:
    """One blocked-egress packet extracted from an NFLOG message."""

    dst_ip: str
    dst_port: int | None
    uid: int | None
    prefix: str | None


def _align_tlv(length: int) -> int:
    return (length + 3) & ~3


def pack_attribute(attr_type: int, payload: bytes) -> bytes:
    """Build one netlink attribute with 4-byte padding."""
    total_len = 4 + len(payload)
    pad = (4 - (total_len % 4)) % 4
    return struct.pack("=HH", total_len, attr_type) + payload + (b"\x00" * pad)


def build_config_nlmsg(
    res_id: int,
    attributes: list[bytes],
    *,
    seq: int = 1,
) -> bytes:
    """Build one NFLOG configuration netlink message."""
    body = struct.pack("=BBH", socket.AF_INET, 0, socket.htons(res_id)) + b"".join(
        attributes
    )
    total_len = 16 + len(body)
    flags = NLM_F_REQUEST | NLM_F_ACK
    return struct.pack("=IHHII", total_len, MSG_CONFIG, flags, seq, 0) + body


def parse_ack_error(data: bytes) -> int:
    """Return the signed errno from an NLMSG_ERROR ACK (0 == success)."""
    if len(data) < 20:
        raise NflogError("truncated netlink ACK")
    _ln, msg_type, _flags, _seq, _pid = struct.unpack("=IHHII", data[:16])
    if msg_type != NLMSG_ERROR:
        raise NflogError(f"expected NLMSG_ERROR, got type {msg_type}")
    return struct.unpack("=i", data[16:20])[0]


def parse_nflog_attributes(body: bytes, *, start: int = 4) -> dict[int, bytes]:
    """Parse TLV attributes from an NFLOG message body (after nfgenmsg)."""
    attrs: dict[int, bytes] = {}
    offset = start
    while offset + 4 <= len(body):
        alen, atype = struct.unpack("=HH", body[offset : offset + 4])
        if alen < 4:
            break
        value = body[offset + 4 : offset + alen]
        attrs[atype & 0x3FFF] = value
        offset += _align_tlv(alen)
    return attrs


def extract_ipv4_dst_port(payload: bytes) -> tuple[str | None, int | None]:
    """Extract IPv4 destination address and TCP/UDP destination port."""
    if len(payload) < 20:
        return None, None
    version = payload[0] >> 4
    if version == 4:
        ihl = (payload[0] & 0x0F) * 4
        if len(payload) < ihl + 4:
            return None, None
        dst_ip = ".".join(str(b) for b in payload[16:20])
        dst_port = struct.unpack("!H", payload[ihl + 2 : ihl + 4])[0]
        return dst_ip, dst_port
    if version == 6:
        if len(payload) < 40:
            return None, None
        dst_ip = socket.inet_ntop(socket.AF_INET6, payload[24:40])
        return dst_ip, None
    return None, None


def parse_packet_message(data: bytes) -> ParsedPacket | None:
    """Parse one inbound NFLOG packet netlink message."""
    if len(data) < 20:
        return None
    ln, msg_type, _flags, _seq, _pid = struct.unpack("=IHHII", data[:16])
    if msg_type == NLMSG_ERROR:
        return None
    body = data[16:ln]
    if len(body) < 4:
        return None

    attrs = parse_nflog_attributes(body)
    raw_payload = attrs.get(NFULA_PAYLOAD)
    if not raw_payload:
        return None

    dst_ip, dst_port = extract_ipv4_dst_port(raw_payload)
    if dst_ip is None:
        return None

    uid = None
    uid_raw = attrs.get(NFULA_UID)
    if uid_raw and len(uid_raw) >= 4:
        uid = struct.unpack("!I", uid_raw[:4])[0]

    prefix = None
    prefix_raw = attrs.get(NFULA_PREFIX)
    if prefix_raw:
        prefix = prefix_raw.rstrip(b"\x00").decode(errors="replace")

    return ParsedPacket(dst_ip=dst_ip, dst_port=dst_port, uid=uid, prefix=prefix)


def reverse_dns(ip: str) -> str | None:
    """Reverse-DNS lookup; None when it does not resolve."""
    try:
        host, _, _ = socket.gethostbyaddr(ip)
    except (OSError, socket.herror, socket.gaierror):
        return None
    return host if host else None


def host_for_filing(dst_ip: str) -> str:
    """Choose the broker host field: validated rDNS when possible, else the IP."""
    rdns = reverse_dns(dst_ip)
    if rdns:
        try:
            return normalize_host(rdns)
        except ValueError:
            LOG.info("egress nflog rdns not a valid domain ip=%s rdns=%s", dst_ip, rdns)
    return dst_ip


class FilingCoalescer:
    """Client-side rate limit matching broker HIT_COALESCE_SECONDS."""

    def __init__(self, coalesce_seconds: int = HIT_COALESCE_SECONDS) -> None:
        self._coalesce_seconds = coalesce_seconds
        self._last_filed: dict[tuple[str, str, int], float] = {}

    def should_file(self, key: tuple[str, str, int], now: float) -> bool:
        last = self._last_filed.get(key)
        if last is None:
            return True
        return (now - last) >= self._coalesce_seconds

    def record_filed(self, key: tuple[str, str, int], now: float) -> None:
        self._last_filed[key] = now


def default_broker_url() -> str:
    host = os.environ.get("EGRESS_BROKER_HOST", DEFAULT_BROKER_HOST)
    port = os.environ.get("EGRESS_BROKER_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def load_broker_token() -> str:
    # Host-side up.sh injects EGRESS_BROKER_TOKEN at container create time.
    # Visible to processes in the container — filing is expected; approval stays
    # host-side, and per-bottle tokens limit blast radius to this bottle only.
    return os.environ.get("EGRESS_BROKER_TOKEN", "").strip()


def file_egress_request(
    *,
    url: str,
    token: str,
    container: str,
    host: str,
    port: int,
    uid: int | None = None,
    comm: str | None = None,
    reason: str | None = None,
    hold_seconds: int = 0,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> None:
    """POST one egress filing to the host broker (same path as other clients)."""
    if not token:
        LOG.info("egress nflog skip filing reason=missing_broker_token host=%s port=%d", host, port)
        return

    payload: dict[str, Any] = {
        "container": container,
        "host": host,
        "port": port,
        "hold_seconds": hold_seconds,
    }
    if uid is not None:
        payload["uid"] = uid
    if comm is not None:
        payload["comm"] = comm
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
    try:
        with open_fn(request, timeout=timeout) as response:
            response.read()
    except urllib.error.URLError as exc:
        LOG.info(
            "egress nflog filing failed host=%s port=%d error=%s",
            host,
            port,
            exc,
        )


def configure_nflog_socket(sock: socket.socket, group: int) -> None:
    """Bind a netlink socket to NFLOG group; verify ACKs for each config step."""
    def cfg_attr(attr_type: int, payload: bytes) -> bytes:
        return pack_attribute(attr_type, payload)

    steps = [
        (
            0,
            cfg_attr(NFULA_CFG_CMD, struct.pack("=B", NFULNL_CFG_CMD_PF_BIND)),
            1,
        ),
        (
            group,
            cfg_attr(NFULA_CFG_CMD, struct.pack("=B", NFULNL_CFG_CMD_BIND)),
            2,
        ),
        (
            group,
            cfg_attr(
                NFULA_CFG_MODE,
                struct.pack("=IBB", socket.htonl(0xFFFF), NFULNL_COPY_PACKET, 0),
            ),
            3,
        ),
    ]
    for res_id, attribute, seq in steps:
        sock.send(build_config_nlmsg(res_id, [attribute], seq=seq))
        ack = sock.recv(65535)
        err = parse_ack_error(ack)
        if err != 0:
            raise NflogError(
                f"NFLOG config step seq={seq} failed: {os.strerror(-err)} (err={err})"
            )


def open_nflog_socket(group: int) -> socket.socket:
    """Open and configure a raw netlink socket for NFLOG."""
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
        sock.bind((0, 0))
        configure_nflog_socket(sock, group)
    except OSError as exc:
        raise NflogError(f"cannot open NFLOG netlink socket: {exc}") from exc
    return sock


class NflogReader:
    """Read blocked-egress NFLOG events and file them with the host broker."""

    def __init__(
        self,
        *,
        container: str,
        group: int = DEFAULT_GROUP,
        prefix: str = DEFAULT_PREFIX,
        broker_url: str | None = None,
        broker_token: str | None = None,
        coalescer: FilingCoalescer | None = None,
        sock: socket.socket | None = None,
        file_fn: Callable[..., None] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._container = container
        self._group = group
        self._prefix = prefix
        self._broker_url = broker_url if broker_url is not None else default_broker_url()
        self._broker_token = broker_token if broker_token is not None else load_broker_token()
        self._coalescer = coalescer or FilingCoalescer()
        self._sock = sock
        self._file_fn = file_fn or file_egress_request
        self._now_fn = now_fn or time.monotonic

    def handle_message(self, data: bytes) -> None:
        packet = parse_packet_message(data)
        if packet is None:
            return
        if packet.prefix and packet.prefix != self._prefix:
            return

        port = packet.dst_port if packet.dst_port is not None else 0
        host = host_for_filing(packet.dst_ip)
        key = (self._container, host, port)
        now = self._now_fn()
        if not self._coalescer.should_file(key, now):
            return

        reason = f"blocked destination {packet.dst_ip}"
        self._file_fn(
            url=self._broker_url,
            token=self._broker_token,
            container=self._container,
            host=host,
            port=port,
            uid=packet.uid,
            reason=reason,
            hold_seconds=0,
        )
        self._coalescer.record_filed(key, now)

    def run_forever(self) -> None:
        owns_sock = self._sock is None
        sock = self._sock or open_nflog_socket(self._group)
        self._sock = sock
        LOG.info(
            "egress nflog listening group=%d prefix=%s container=%s",
            self._group,
            self._prefix,
            self._container,
        )
        try:
            while True:
                data = sock.recv(65535)
                self.handle_message(data)
        finally:
            if owns_sock:
                sock.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    container = os.environ.get("CONTAINER_NAME", "unnamed")
    group = int(os.environ.get("EGRESS_NFLOG_GROUP", str(DEFAULT_GROUP)))
    try:
        NflogReader(container=container, group=group).run_forever()
    except NflogError as exc:
        LOG.error("egress nflog fatal: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
