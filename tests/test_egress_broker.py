#!/usr/bin/env python3
"""Offline unit tests for the in-container transparent egress broker."""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import egress_broker as broker  # noqa: E402


def _pack_original_dst(ip: str, port: int) -> bytes:
    # Fixture mirrors kernel sockaddr_in byte order: sin_family is native-endian
    # u16, sin_port is network-order (big-endian) u16, sin_addr is 4 raw octets.
    # Do not "simplify" to struct.pack("!HH...") — that yields family=512 on LE.
    octets = [int(part) for part in ip.split(".")]
    return struct.pack("=H", socket.AF_INET) + struct.pack("!H", port) + bytes(octets) + b"\x00" * 8


def _pack_original_dst_big_endian_family(ip: str, port: int) -> bytes:
    """Wrong fixture: treats sin_family as big-endian (0x0200 on little-endian)."""
    octets = [int(part) for part in ip.split(".")]
    return struct.pack("!H", socket.AF_INET) + struct.pack("!H", port) + bytes(octets) + b"\x00" * 8


def _build_client_hello(sni: str | None = None) -> bytes:
    body = bytearray()
    body.extend(b"\x03\x03")
    body.extend(b"\x00" * 32)
    body.append(0)  # session id length
    body.extend(b"\x00\x02\x00\xff")  # one cipher suite
    body.extend(b"\x01\x00")  # compression

    extensions = bytearray()
    if sni is not None:
        name = sni.encode("ascii")
        entry = bytes([broker.TLS_SNI_HOST_NAME]) + struct.pack("!H", len(name)) + name
        sni_list = struct.pack("!H", len(entry)) + entry
        extensions.extend(struct.pack("!H", broker.TLS_EXTENSION_SERVER_NAME))
        extensions.extend(struct.pack("!H", len(sni_list)))
        extensions.extend(sni_list)

    body.extend(struct.pack("!H", len(extensions)))
    body.extend(extensions)

    hs_len = len(body)
    handshake = bytes([broker.TLS_CLIENT_HELLO]) + struct.pack("!I", hs_len)[1:4] + body
    record = (
        bytes([broker.TLS_HANDSHAKE_RECORD, 0x03, 0x01])
        + struct.pack("!H", len(handshake))
        + handshake
    )
    return record


def _http_request(host: str | None) -> bytes:
    lines = ["GET / HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines.append("Connection: close")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("ascii")


class OriginalDstTests(unittest.TestCase):
    def test_parse_ipv4_original_dst(self):
        raw = _pack_original_dst("198.51.100.42", 443)
        dst = broker.parse_original_dst_bytes(raw)
        self.assertEqual(dst.ip, "198.51.100.42")
        self.assertEqual(dst.port, 443)

    def test_rejects_big_endian_family_fixture(self):
        raw = _pack_original_dst_big_endian_family("198.51.100.42", 443)
        with self.assertRaises(ValueError) as ctx:
            broker.parse_original_dst_bytes(raw)
        self.assertIn("512", str(ctx.exception))



class DaemonErrorDecisionTests(unittest.TestCase):
    """An apply that failed host-side must never reach the caller as an allow."""

    def test_error_decision_maps_to_daemon_error_not_allow(self):
        for reason in ("apply_failed", "ip_requires_cidr"):
            with self.subTest(reason=reason):
                body = {"decision": "error", "reason": reason}
                self.assertEqual(broker._decision_outcome(body), "daemon_error")

    def test_known_decisions_still_map(self):
        self.assertEqual(broker._decision_outcome({"decision": "allow"}), "allow")
        self.assertEqual(broker._decision_outcome({"decision": "deny"}), "deny")
        self.assertEqual(broker._decision_outcome({"decision": "pending"}), "pending")

    def test_unknown_decision_is_a_fault_not_an_allow(self):
        self.assertEqual(broker._decision_outcome({"decision": "banana"}), "daemon_error")

class SniExtractionTests(unittest.TestCase):
    def test_extract_sni_present(self):
        payload = _build_client_hello("api.example.com")
        self.assertEqual(
            broker.extract_sni_from_client_hello(payload),
            "api.example.com",
        )

    def test_extract_sni_absent(self):
        payload = _build_client_hello(None)
        self.assertIsNone(broker.extract_sni_from_client_hello(payload))

    def test_extract_sni_fragmented_across_segments(self):
        payload = _build_client_hello("split.example.com")
        first, second = payload[:10], payload[10:]
        self.assertIsNone(broker.tls_record_length(first))
        combined = first + second
        self.assertEqual(broker.tls_record_length(combined), len(payload))
        self.assertEqual(
            broker.extract_sni_from_client_hello(combined),
            "split.example.com",
        )

    def test_extract_sni_truncated_record_raises(self):
        payload = _build_client_hello("bad.example.com")[:20]
        with self.assertRaises(broker.BrokerPeekError):
            broker.extract_sni_from_client_hello(payload)

    def test_extract_sni_malformed_handshake_raises(self):
        payload = bytes([0x16, 0x03, 0x01, 0x00, 0x05, 0x02, 0x00, 0x00, 0x00, 0x00])
        with self.assertRaises(broker.BrokerPeekError):
            broker.extract_sni_from_client_hello(payload)

    def test_record_length_over_cap_raises(self):
        huge = bytes([broker.TLS_HANDSHAKE_RECORD, 0x03, 0x01, 0xFF, 0xFF])
        with self.assertRaises(broker.BrokerPeekError):
            broker.tls_record_length(huge)


class HttpHostParsingTests(unittest.TestCase):
    def test_parse_host_header(self):
        self.assertEqual(
            broker.parse_http_host(_http_request("docs.example.com")),
            "docs.example.com",
        )

    def test_parse_host_with_port(self):
        self.assertEqual(
            broker.parse_http_host(_http_request("docs.example.com:8080")),
            "docs.example.com",
        )

    def test_parse_host_absent(self):
        self.assertIsNone(broker.parse_http_host(_http_request(None)))

    def test_parse_host_incomplete_returns_none(self):
        self.assertIsNone(broker.parse_http_host(b"GET / HTTP/1.1\r\nHost: ex"))


class FailureSurfaceTests(unittest.TestCase):
    def test_tls_access_denied_alert_bytes(self):
        self.assertEqual(
            broker.TLS_ACCESS_DENIED_ALERT,
            bytes([0x15, 0x03, 0x03, 0x00, 0x02, 0x02, 0x31]),
        )

    def test_http_503_includes_required_headers(self):
        payload = broker.build_http_503("pending.example.com", "req-abc", "coding-brassbottle")
        text = payload.decode("latin-1")
        self.assertIn("HTTP/1.1 503 Egress pending approval: pending.example.com (req req-abc)", text)
        self.assertIn("Retry-After: 30", text)
        self.assertIn("X-Djinn-Egress: pending", text)
        self.assertIn("X-Djinn-Egress-Host: pending.example.com", text)
        self.assertIn("X-Djinn-Egress-Request: req-abc", text)
        self.assertIn("./djinn allow coding-brassbottle pending.example.com", text)

    def test_http_403_for_denied(self):
        payload = broker.build_http_403("denied.example.com", "req-deny")
        self.assertIn("HTTP/1.1 403 Egress denied: denied.example.com (req req-deny)", payload.decode("latin-1"))

    def test_http_403_names_denylist_zone_scope_and_undeny_hint(self):
        payload = broker.build_http_403(
            "us5.datadoghq.com",
            "req-deny",
            denylist_zone="datadoghq.com",
            denylist_scope="global",
        )
        text = payload.decode("latin-1")
        self.assertIn("persistent deny list", text)
        self.assertIn("zone datadoghq.com", text)
        self.assertIn("scope global", text)
        self.assertIn("./djinn undeny datadoghq.com --global", text)

    def test_http_403_bottle_scope_undeny_hint(self):
        payload = broker.build_http_403(
            "example.net",
            "req-deny",
            denylist_zone="example.net",
            denylist_scope="coding-hank",
        )
        text = payload.decode("latin-1")
        self.assertIn("./djinn undeny example.net --bottle coding-hank", text)

    def test_decision_outcome_denylist_body_still_maps_to_deny(self):
        body = {
            "decision": "deny",
            "reason": "denylist",
            "zone": "datadoghq.com",
            "scope": "global",
        }
        self.assertEqual(broker._decision_outcome(body), "deny")

    def test_http_502_for_daemon_fault(self):
        payload = broker.build_http_502()
        self.assertIn("HTTP/1.1 502 Egress broker unreachable", payload.decode("latin-1"))


class InterceptedConnectionTests(unittest.TestCase):
    def _config(self) -> broker.BrokerConfig:
        return broker.BrokerConfig(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
            hold_seconds=1,
            peek_timeout=1.0,
        )

    def _mock_client(
        self,
        *,
        dst_ip: str,
        dst_port: int,
        initial: bytes = b"",
    ) -> tuple[mock.Mock, socket.socket]:
        client_sock, feed_sock = socket.socketpair()
        if initial:
            feed_sock.sendall(initial)
        client = mock.Mock()
        client.getsockopt = mock.Mock(return_value=_pack_original_dst(dst_ip, dst_port))
        client.recv = client_sock.recv
        client.sendall = client_sock.sendall
        client.setblocking = client_sock.setblocking
        client.close = client_sock.close
        client.fileno = client_sock.fileno
        return client, feed_sock

    def test_fast_path_uses_ipset_not_log(self):
        client, feed = self._mock_client(dst_ip="93.184.216.34", dst_port=443, initial=_build_client_hello("allowed.example.com"))
        upstream_client, upstream = socket.socketpair()
        ipset_calls: list[str] = []
        file_calls: list[dict[str, object]] = []

        def ipset_check(ip: str) -> bool:
            ipset_calls.append(ip)
            return True

        def connect_fn(addr: tuple[str, int], timeout: float) -> socket.socket:
            self.assertEqual(addr, ("93.184.216.34", 443))
            return upstream

        with mock.patch.object(broker, "relay_sockets"):
            outcome = broker.handle_intercepted_connection(
                client,
                config=self._config(),
                ipset_check=ipset_check,
                file_fn=lambda **kwargs: file_calls.append(kwargs) or (None, None),
                connect_fn=connect_fn,
            )
        feed.close()
        upstream_client.close()

        self.assertEqual(outcome.action, "fast_path")
        self.assertEqual(ipset_calls, ["93.184.216.34"])
        self.assertEqual(file_calls, [])

    def test_buffered_bytes_replayed_identically(self):
        captured = _build_client_hello("replay.example.com")
        client, feed = self._mock_client(dst_ip="93.184.216.34", dst_port=443, initial=captured)
        sent_chunks: list[bytes] = []

        def file_fn(**kwargs: object) -> tuple[dict[str, str], None]:
            return {"decision": "allow", "request_id": "req-replay"}, None

        def connect_fn(addr: tuple[str, int], timeout: float) -> socket.socket:
            upstream = mock.Mock()
            upstream.sendall = lambda data: sent_chunks.append(data)
            return upstream

        with mock.patch.object(broker, "relay_sockets"):
            outcome = broker.handle_intercepted_connection(
                client,
                config=self._config(),
                ipset_check=lambda _ip: False,
                file_fn=file_fn,
                connect_fn=connect_fn,
            )
        feed.close()

        self.assertEqual(outcome.action, "allow")
        self.assertEqual(sent_chunks, [captured])

    def test_hold_timeout_emits_503_and_leaves_request_filed(self):
        client, feed = self._mock_client(
            dst_ip="93.184.216.34",
            dst_port=80,
            initial=_http_request("hold.example.com"),
        )
        filed = threading.Event()
        captured_ids: list[str] = []
        config = broker.BrokerConfig(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
            hold_seconds=1,
            peek_timeout=1.0,
        )

        def file_fn(**kwargs: object) -> tuple[dict[str, str], None]:
            captured_ids.append(str(kwargs.get("request_id")))
            filed.set()
            time.sleep(2.0)
            return {"decision": "pending"}, None

        with mock.patch.object(broker, "generate_request_id", return_value="abcd1234"):
            outcome = broker.handle_intercepted_connection(
                client,
                config=config,
                ipset_check=lambda _ip: False,
                file_fn=file_fn,
            )
        response = bytearray()
        feed.settimeout(0.5)
        try:
            while True:
                chunk = feed.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        except TimeoutError:
            pass
        feed.close()

        self.assertTrue(filed.is_set())
        self.assertEqual(captured_ids, ["abcd1234"])
        self.assertEqual(outcome.action, "pending")
        self.assertEqual(outcome.request_id, "abcd1234")
        text = response.decode("latin-1")
        self.assertIn("HTTP/1.1 503 Egress pending approval: hold.example.com", text)
        self.assertIn("X-Djinn-Egress: pending", text)
        self.assertIn("X-Djinn-Egress-Host: hold.example.com", text)
        self.assertIn("X-Djinn-Egress-Request: abcd1234", text)
        self.assertIn("request id abcd1234", text)
        self.assertNotIn("unknown", text.lower())

    def test_denied_emits_403(self):
        client, feed = self._mock_client(
            dst_ip="93.184.216.34",
            dst_port=80,
            initial=_http_request("deny.example.com"),
        )

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=lambda **kwargs: ({"decision": "deny", "request_id": "req-x"}, None),
        )
        response = feed.recv(4096)
        feed.close()
        self.assertEqual(outcome.action, "deny")
        self.assertIn(b"HTTP/1.1 403", response)
        self.assertNotIn(b"persistent deny list", response)

    def test_denylist_deny_emits_403_naming_zone_and_scope(self):
        client, feed = self._mock_client(
            dst_ip="93.184.216.34",
            dst_port=80,
            initial=_http_request("us5.datadoghq.com"),
        )

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=lambda **kwargs: (
                {
                    "decision": "deny",
                    "reason": "denylist",
                    "zone": "datadoghq.com",
                    "scope": "global",
                },
                None,
            ),
        )
        response = feed.recv(4096)
        feed.close()
        self.assertEqual(outcome.action, "deny")
        text = response.decode("latin-1")
        self.assertIn("HTTP/1.1 403", text)
        self.assertIn("persistent deny list", text)
        self.assertIn("zone datadoghq.com", text)
        self.assertIn("scope global", text)
        self.assertIn("./djinn undeny datadoghq.com --global", text)

    def test_daemon_fault_emits_502(self):
        client, feed = self._mock_client(
            dst_ip="93.184.216.34",
            dst_port=80,
            initial=_http_request("fault.example.com"),
        )

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=lambda **kwargs: (None, "connection refused"),
        )
        response = feed.recv(4096)
        feed.close()
        self.assertEqual(outcome.action, "daemon_error")
        self.assertIn(b"HTTP/1.1 502", response)

    def test_pending_https_emits_tls_alert(self):
        client, feed = self._mock_client(
            dst_ip="93.184.216.34",
            dst_port=443,
            initial=_build_client_hello("tls.example.com"),
        )

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=lambda **kwargs: ({"decision": "pending", "request_id": "req-tls"}, None),
        )
        response = feed.recv(4096)
        feed.close()
        self.assertEqual(outcome.action, "pending")
        self.assertEqual(response, broker.TLS_ACCESS_DENIED_ALERT)


class LoopbackDestinationTests(unittest.TestCase):
    """Loopback is never egress and must never reach the operator queue."""

    def test_classifies_loopback_addresses(self):
        self.assertTrue(broker.is_loopback_destination("127.0.0.1"))
        self.assertTrue(broker.is_loopback_destination("127.0.0.53"))
        self.assertFalse(broker.is_loopback_destination("93.184.216.34"))
        self.assertFalse(broker.is_loopback_destination("10.0.0.1"))

    def test_unparseable_address_is_not_treated_as_loopback(self):
        # Fail towards filing, never towards silently splicing an unknown
        # destination straight through.
        self.assertFalse(broker.is_loopback_destination("not-an-ip"))


class LoopbackInterceptTests(unittest.TestCase):
    """The 127.0.0.1:3128 prompt loop, at the two places it can start.

    Borrows the two helpers rather than subclassing InterceptedConnectionTests,
    which would re-run every one of its tests under this name too.
    """

    _config = InterceptedConnectionTests._config
    _mock_client = InterceptedConnectionTests._mock_client

    def test_self_dial_is_refused_without_filing(self):
        # An app with http_proxy=http://127.0.0.1:3128 dials the broker port
        # directly. No NAT happened, so SO_ORIGINAL_DST hands back the
        # broker's own address and the pre-fix code filed 127.0.0.1:3128 with
        # the operator — an IP literal that cannot be allowed.
        client, feed = self._mock_client(
            dst_ip="127.0.0.1", dst_port=3128, initial=b"GET / HTTP/1.1\r\n\r\n"
        )
        file_calls: list[dict[str, object]] = []

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=lambda **kwargs: file_calls.append(kwargs) or (None, None),
            connect_fn=lambda addr, timeout: self.fail(f"must not relay to {addr}"),
        )
        feed.close()

        self.assertEqual(outcome.action, "self_dial")
        self.assertEqual(file_calls, [], "a self-dial must never reach the operator")

    def test_local_service_is_spliced_through_without_filing(self):
        # Defence in depth behind the nat rule's ! -d 127.0.0.0/8: a local
        # dev server on :80 keeps working rather than prompting the operator.
        client, feed = self._mock_client(
            dst_ip="127.0.0.1", dst_port=80, initial=b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        upstream_client, upstream = socket.socketpair()
        file_calls: list[dict[str, object]] = []
        ipset_calls: list[str] = []

        def ipset_check(ip: str) -> bool:
            ipset_calls.append(ip)
            return False

        def connect_fn(addr: tuple[str, int], timeout: float) -> socket.socket:
            self.assertEqual(addr, ("127.0.0.1", 80))
            return upstream

        with mock.patch.object(broker, "relay_sockets"):
            outcome = broker.handle_intercepted_connection(
                client,
                config=self._config(),
                ipset_check=ipset_check,
                file_fn=lambda **kwargs: file_calls.append(kwargs) or (None, None),
                connect_fn=connect_fn,
            )
        feed.close()
        upstream_client.close()

        self.assertEqual(outcome.action, "fast_path")
        self.assertEqual(file_calls, [], "loopback must never reach the operator")
        self.assertEqual(ipset_calls, [], "loopback short-circuits before the ipset probe")

    def test_a_real_destination_on_the_broker_port_still_files(self):
        # The port alone must not trigger the self-dial branch: :3128 on a
        # remote host is ordinary egress.
        client, feed = self._mock_client(
            dst_ip="93.184.216.34", dst_port=3128, initial=b"GET / HTTP/1.1\r\n\r\n"
        )
        file_calls: list[dict[str, object]] = []

        def file_fn(**kwargs: object) -> tuple[dict[str, str], None]:
            file_calls.append(kwargs)
            return {"decision": "deny", "request_id": "req-remote"}, None

        outcome = broker.handle_intercepted_connection(
            client,
            config=self._config(),
            ipset_check=lambda _ip: False,
            file_fn=file_fn,
            connect_fn=lambda addr, timeout: self.fail("denied must not connect"),
        )
        feed.close()

        self.assertNotEqual(outcome.action, "self_dial")
        self.assertEqual(len(file_calls), 1)


class IpsetHelperTests(unittest.TestCase):
    def test_ipset_allowed_invokes_ipset_test(self):
        calls: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> mock.Mock:
            calls.append(argv)
            return mock.Mock(returncode=0)

        self.assertTrue(broker.ipset_allowed("1.2.3.4", runner=runner))
        self.assertEqual(calls, [["ipset", "test", broker.IPSET_NAME, "1.2.3.4"]])


if __name__ == "__main__":
    unittest.main()
