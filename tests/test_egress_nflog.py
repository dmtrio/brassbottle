#!/usr/bin/env python3
"""Unit tests for the container-side NFLOG egress reader."""

from __future__ import annotations

import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import egress_nflog as nflog  # noqa: E402


def _ipv4_tcp_packet(
    dst: str,
    dport: int,
    *,
    ihl: int = 5,
    options: bytes = b"",
) -> bytes:
    dst_bytes = bytes(int(part) for part in dst.split("."))
    header_len = ihl * 4
    if len(options) != header_len - 20:
        raise ValueError("options length must match ihl")
    total_len = header_len + 20
    header = bytearray(
        [
            (4 << 4) | ihl,
            0,
            (total_len >> 8) & 0xFF,
            total_len & 0xFF,
            0,
            0,
            0x40,
            0,
            6,
            0,
            0,
            0,
            10,
            0,
            0,
            1,
            dst_bytes[0],
            dst_bytes[1],
            dst_bytes[2],
            dst_bytes[3],
        ]
    )
    header.extend(options)
    tcp = struct.pack("!HH", 12345, dport) + b"\x00" * 16
    return bytes(header) + tcp


def _build_packet_nlmsg(attrs: dict[int, bytes], *, group: int = 32) -> bytes:
    msg_type = (nflog.NFNL_SUBSYS_ULOG << 8) | 0
    body = struct.pack("=BBH", socket.AF_INET, 0, socket.htons(group))
    for attr_type in sorted(attrs):
        body += nflog.pack_attribute(attr_type, attrs[attr_type])
    total_len = 16 + len(body)
    return struct.pack("=IHHII", total_len, msg_type, 0, 1, 0) + body


class NflogParsingTests(unittest.TestCase):
    def test_parse_well_formed_payload_uid_prefix(self):
        payload = _ipv4_tcp_packet("192.0.2.55", 5432)
        message = _build_packet_nlmsg(
            {
                nflog.NFULA_PAYLOAD: payload,
                nflog.NFULA_UID: struct.pack("!I", 1000),
                nflog.NFULA_PREFIX: b"djinn-egress\x00",
            }
        )
        attrs = nflog.parse_nflog_attributes(message[16:])
        self.assertIn(nflog.NFULA_PAYLOAD, attrs)
        self.assertIn(nflog.NFULA_UID, attrs)
        self.assertIn(nflog.NFULA_PREFIX, attrs)

        packet = nflog.parse_packet_message(message)
        assert packet is not None
        self.assertEqual(packet.dst_ip, "192.0.2.55")
        self.assertEqual(packet.dst_port, 5432)
        self.assertEqual(packet.uid, 1000)
        self.assertEqual(packet.prefix, "djinn-egress")

    def test_parse_truncated_payload_attribute(self):
        body = struct.pack("=BBH", socket.AF_INET, 0, socket.htons(32))
        body += struct.pack("=HH", 8, nflog.NFULA_PAYLOAD) + b"\x45\x00"
        attrs = nflog.parse_nflog_attributes(body)
        dst_ip, dst_port = nflog.extract_ipv4_dst_port(attrs[nflog.NFULA_PAYLOAD])
        self.assertIsNone(dst_ip)
        self.assertIsNone(dst_port)

    def test_parse_unknown_attribute_type_is_skipped(self):
        body = struct.pack("=BBH", socket.AF_INET, 0, socket.htons(32))
        body += nflog.pack_attribute(99, b"ignored")
        body += nflog.pack_attribute(nflog.NFULA_UID, struct.pack("!I", 7))
        attrs = nflog.parse_nflog_attributes(body)
        self.assertEqual(struct.unpack("!I", attrs[nflog.NFULA_UID])[0], 7)
        message = _build_packet_nlmsg({99: b"ignored", nflog.NFULA_UID: struct.pack("!I", 7)})
        self.assertIsNone(nflog.parse_packet_message(message))

    def test_parse_tlv_four_byte_alignment_padding(self):
        body = struct.pack("=BBH", socket.AF_INET, 0, socket.htons(32))
        body += nflog.pack_attribute(5, b"ab")
        body += nflog.pack_attribute(nflog.NFULA_UID, struct.pack("!I", 42))
        attrs = nflog.parse_nflog_attributes(body)
        self.assertEqual(struct.unpack("!I", attrs[nflog.NFULA_UID])[0], 42)

    def test_extract_ipv4_with_nonstandard_ihl(self):
        options = b"\x01\x04\x00\x00"
        payload = _ipv4_tcp_packet("198.51.100.9", 5432, ihl=6, options=options)
        dst_ip, dst_port = nflog.extract_ipv4_dst_port(payload)
        self.assertEqual(dst_ip, "198.51.100.9")
        self.assertEqual(dst_port, 5432)


class NflogConfigTests(unittest.TestCase):
    def test_config_message_subsystem_and_ack_flags(self):
        attr = nflog.pack_attribute(nflog.NFULA_CFG_CMD, struct.pack("=B", nflog.NFULNL_CFG_CMD_PF_BIND))
        message = nflog.build_config_nlmsg(0, [attr], seq=1)
        msg_len, msg_type, flags, seq, _pid = struct.unpack("=IHHII", message[:16])
        self.assertEqual(msg_type, nflog.MSG_CONFIG)
        self.assertEqual(msg_type >> 8, 4)
        self.assertEqual(flags, nflog.NLM_F_REQUEST | nflog.NLM_F_ACK)
        self.assertEqual(seq, 1)
        self.assertGreater(msg_len, 16)

    def test_ack_nonzero_error_raises(self):
        ack = struct.pack("=IHHII", 20, nflog.NLMSG_ERROR, 0, 1, 0)
        ack += struct.pack("=i", -2)
        self.assertEqual(nflog.parse_ack_error(ack), -2)

        sock = mock.Mock()
        sock.recv.return_value = ack
        with self.assertRaises(nflog.NflogError):
            nflog.configure_nflog_socket(sock, 32)
        sock.send.assert_called()
        sock.recv.assert_called()


class FilingCoalescerTests(unittest.TestCase):
    def test_tight_loop_produces_few_filings(self):
        coalescer = nflog.FilingCoalescer(coalesce_seconds=60)
        key = ("coding-brassbottle", "blocked.example.com", 5432)
        filings = 0
        now = 1000.0
        for _ in range(100):
            if coalescer.should_file(key, now):
                filings += 1
                coalescer.record_filed(key, now)
            now += 0.01
        self.assertEqual(filings, 1)

        now += 60.0
        self.assertTrue(coalescer.should_file(key, now))


class NflogReaderTests(unittest.TestCase):
    def test_handle_message_files_once_per_coalesce_window(self):
        payload = _ipv4_tcp_packet("192.0.2.55", 5432)
        message = _build_packet_nlmsg(
            {
                nflog.NFULA_PAYLOAD: payload,
                nflog.NFULA_UID: struct.pack("!I", 1000),
                nflog.NFULA_PREFIX: b"djinn-egress",
            }
        )
        filings: list[dict[str, object]] = []
        now = {"t": 1000.0}

        def file_fn(**kwargs: object) -> None:
            filings.append(kwargs)

        reader = nflog.NflogReader(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
            file_fn=file_fn,
            now_fn=lambda: now["t"],
        )
        with mock.patch.object(nflog, "host_for_filing", return_value="blocked.example.com"):
            for _ in range(50):
                reader.handle_message(message)
                now["t"] += 0.05
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0]["host"], "blocked.example.com")
        self.assertEqual(filings[0]["port"], 5432)
        self.assertEqual(filings[0]["uid"], 1000)
        self.assertIn("192.0.2.55", str(filings[0]["reason"]))

    def test_handle_message_skips_loopback_destination(self):
        payload = _ipv4_tcp_packet("127.0.0.1", 3128)
        message = _build_packet_nlmsg(
            {
                nflog.NFULA_PAYLOAD: payload,
                nflog.NFULA_UID: struct.pack("!I", 1000),
                nflog.NFULA_PREFIX: b"djinn-egress",
            }
        )
        filings: list[dict[str, object]] = []
        reader = nflog.NflogReader(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
            file_fn=lambda **kwargs: filings.append(kwargs),
        )
        reader.handle_message(message)
        self.assertEqual(filings, [])


class HostForFilingTests(unittest.TestCase):
    def test_is_loopback_destination(self):
        self.assertTrue(nflog.is_loopback_destination("127.0.0.1"))
        self.assertTrue(nflog.is_loopback_destination("127.255.255.255"))
        self.assertTrue(nflog.is_loopback_destination("::1"))
        self.assertFalse(nflog.is_loopback_destination("192.0.2.55"))

    def test_reverse_dns_success_yields_domain(self):
        with mock.patch.object(nflog, "reverse_dns", return_value="db.example.com"):
            self.assertEqual(nflog.host_for_filing("192.0.2.55"), "db.example.com")

    def test_reverse_dns_failure_falls_back_to_ip(self):
        with mock.patch.object(nflog, "reverse_dns", return_value=None):
            self.assertEqual(nflog.host_for_filing("192.0.2.55"), "192.0.2.55")

    def test_ipv6_fallback_to_literal(self):
        with mock.patch.object(nflog, "reverse_dns", return_value=None):
            self.assertEqual(nflog.host_for_filing("2001:db8::1"), "2001:db8::1")


if __name__ == "__main__":
    unittest.main()
