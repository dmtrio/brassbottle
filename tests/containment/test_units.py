#!/usr/bin/env python3
"""Docker-free tests for the containment lab's own parts: packet and DNS wire
code, recorder parsing, the silence oracle, the expected-failure ledger, probe
command construction and the lab gate. These run in the plain discovery run;
the lab itself is test_probes.py.
"""

import ipaddress
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lab_client  # noqa: E402
import lab_dns  # noqa: E402
import lab_env  # noqa: E402
import lab_kit  # noqa: E402
import lab_packets  # noqa: E402
import lab_sink  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
EXPECTED_PROBES = ("A-root", "A-spoof", "B", "C", "C-inv", "C-form", "E", "F", "G", "H", "I", "J", "K")


def eth(ethertype: int, payload: bytes) -> bytes:
    return lab_packets.build_ethernet("02:00:00:00:00:01", "02:00:00:00:00:02", ethertype, payload)


def tcp_segment(sport, dport, flags, payload=b""):
    return struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 5 << 4, flags, 1024, 0, 0) + payload


def ipv4(proto: int, body: bytes, src="11.1.2.3", dst="11.1.2.10") -> bytes:
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 0, 0, 64, proto, 0,
                       socket.inet_aton(src), socket.inet_aton(dst)) + body


def ipv6(nxt: int, body: bytes, src="fd77::1", dst="fd77::10") -> bytes:
    return struct.pack("!IHBB16s16s", 6 << 28, len(body), nxt, 64,
                       socket.inet_pton(socket.AF_INET6, src), socket.inet_pton(socket.AF_INET6, dst)) + body


class PacketTests(unittest.TestCase):
    def test_udp_frame_round_trips_through_the_builder_and_parser(self):
        packet = lab_packets.build_ipv4_udp("11.1.2.3", "11.1.2.10", 4000, 443, b"hello-tag")
        event = lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, packet))
        self.assertEqual((event["proto"], event["src"], event["dst"], event["sport"], event["dport"]),
                         ("udp", "11.1.2.3", "11.1.2.10", 4000, 443))
        self.assertIn("hello-tag", event["preview"])

    def test_ipv4_header_checksum_verifies(self):
        packet = lab_packets.build_ipv4_udp("11.1.2.3", "11.1.2.10", 1, 2, b"x")
        self.assertEqual(lab_packets.checksum(packet[:20]), 0)

    def test_tcp_syn_and_payload(self):
        syn = lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, ipv4(6, tcp_segment(5, 443, 0x02))))
        self.assertEqual((syn["proto"], syn["flags"], syn["dport"]), ("tcp", "S", 443))
        data = lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, ipv4(6, tcp_segment(5, 80, 0x18, b"GET / HTTP"))))
        self.assertEqual(data["flags"], "PA")
        self.assertIn("GET / HTTP", data["preview"])

    def test_icmp_echo_is_recorded(self):
        event = lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, ipv4(1, b"\x08\0\0\0\0\0\0\0")))
        self.assertEqual((event["proto"], event["icmp_type"]), ("icmp", 8))

    def test_ipv6_tcp_is_recorded_with_family_6(self):
        event = lab_packets.parse_frame(eth(lab_packets.ETH_P_IPV6, ipv6(6, tcp_segment(5, 443, 0x02))))
        self.assertEqual((event["family"], event["proto"], event["dst"]), (6, "tcp", "fd77::10"))

    def test_ipv6_extension_header_is_skipped(self):
        hop = bytes([6, 0]) + b"\0" * 6          # hop-by-hop, next=tcp, length 8 bytes
        event = lab_packets.parse_frame(eth(lab_packets.ETH_P_IPV6, ipv6(0, hop + tcp_segment(5, 443, 2))))
        self.assertEqual((event["proto"], event["dport"]), ("tcp", 443))

    def test_neighbour_discovery_and_igmp_are_noise_but_echo6_is_not(self):
        for icmp_type in (133, 135, 136, 130):
            self.assertIsNone(lab_packets.parse_frame(
                eth(lab_packets.ETH_P_IPV6, ipv6(58, bytes([icmp_type, 0]) + b"\0" * 6))), icmp_type)
        self.assertIsNone(lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, ipv4(2, b"\x11\0\0\0\0\0\0\0"))))
        echo = lab_packets.parse_frame(eth(lab_packets.ETH_P_IPV6, ipv6(58, bytes([128, 0]) + b"\0" * 6)))
        self.assertEqual(echo["proto"], "icmp6")

    def test_arp_and_short_frames_are_ignored(self):
        self.assertIsNone(lab_packets.parse_frame(eth(0x0806, b"\0" * 28)))
        self.assertIsNone(lab_packets.parse_frame(b"\0" * 5))

    def test_unknown_ip_protocol_is_still_recorded(self):
        event = lab_packets.parse_frame(eth(lab_packets.ETH_P_IP, ipv4(47, b"\0" * 8)))
        self.assertEqual(event["proto"], "ip47")

    def test_preview_masks_non_printable_bytes(self):
        self.assertEqual(lab_packets.preview(b"a\x00\xffb"), "a..b")


class DnsWireTests(unittest.TestCase):
    ZONE = lab_dns.Zone("11.1.2.10", "fd77::10", {"rfc1918": [4, "10.77.0.5"], "ipv4-mapped": [6, "::ffff:10.77.0.5"]})

    def test_query_round_trip_with_edns(self):
        for edns in (False, True):
            wire = lab_dns.build_query("a.blocked.lab", lab_dns.qtype_number("MX"), edns=edns)
            _t, _f, qname, qtype, has_edns = lab_dns.parse_query(wire)
            self.assertEqual((qname, qtype, has_edns), ("a.blocked.lab", 15, edns))

    def test_every_malformed_kind_is_rejected_by_the_parser(self):
        for kind in lab_dns.MALFORMED_KINDS:
            with self.assertRaises(ValueError, msg=kind):
                lab_dns.parse_query(lab_dns.malformed_query(kind))

    def test_qtype_numbers(self):
        self.assertEqual([lab_dns.qtype_number(t) for t in ("A", "aaaa", "ANY", "TYPE65280", "257")],
                         [1, 28, 255, 65280, 257])

    def test_allowed_and_blocked_share_the_sink_address(self):
        for name in ("allowed.lab", "tag.blocked.lab"):
            rdata = {r for t, r in self.ZONE.records(name) if t == 1}
            self.assertEqual(rdata, {socket.inet_aton("11.1.2.10")}, name)

    def test_class_names_resolve_to_their_address_and_family(self):
        self.assertEqual(self.ZONE.records("rfc1918.allowed.lab"), [(1, socket.inet_aton("10.77.0.5"))])
        mapped = self.ZONE.records("ipv4-mapped.allowed.lab")
        self.assertEqual(mapped[0][0], 28)
        self.assertEqual(socket.inet_ntop(socket.AF_INET6, mapped[0][1]), "::ffff:10.77.0.5")

    def test_cname_chain_and_unknown_zone(self):
        types = [t for t, _ in self.ZONE.records("cname.blocked.lab")]
        self.assertEqual(types, [5, 1, 28])
        self.assertIsNone(self.ZONE.records("example.com"))

    def test_response_for_an_answer_nxdomain_and_garbage(self):
        ok = lab_dns.build_response(lab_dns.build_query("allowed.lab", 1), self.ZONE)
        self.assertEqual(struct.unpack("!HHHHHH", ok[:12])[3], 1)           # one answer
        nx = lab_dns.build_response(lab_dns.build_query("example.com", 1), self.ZONE)
        self.assertEqual(struct.unpack("!H", nx[2:4])[0] & 0xF, lab_dns.RCODE_NXDOMAIN)
        bad = lab_dns.build_response(lab_dns.malformed_query("truncated"), self.ZONE)
        self.assertEqual(struct.unpack("!H", bad[2:4])[0] & 0xF, lab_dns.RCODE_FORMERR)

    def test_unparseable_arrival_is_still_an_event(self):
        event = lab_dns.dns_event("11.1.2.21", "udp", lab_dns.malformed_query("label-overrun"))
        self.assertEqual((event["parsed"], event["qname"], event["src"], event["raw_len"]),
                         (False, None, "11.1.2.21", len(lab_dns.malformed_query("label-overrun"))))

    def test_server_records_every_query_kind_over_both_transports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dns.jsonl")
            with socket.socket() as probe_sock:
                probe_sock.bind(("127.0.0.1", 0))
                port = probe_sock.getsockname()[1]
            server = lab_dns.Server(self.ZONE, path, port=port, bind4="127.0.0.1", bind6="::1")
            threading.Thread(target=server.serve, daemon=True).start()
            time.sleep(1.0)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(2)
            udp.sendto(lab_dns.build_query("t1.blocked.lab", 1), ("127.0.0.1", port))
            reply, _ = udp.recvfrom(512)
            self.assertEqual(struct.unpack("!HHHHHH", reply[:12])[3], 1)
            udp.sendto(lab_dns.malformed_query("pointer-loop"), ("127.0.0.1", port))
            udp.recvfrom(512)
            udp.close()
            tcp = socket.create_connection(("127.0.0.1", port), timeout=2)
            wire = lab_dns.build_query("t2.blocked.lab", 16)
            tcp.sendall(struct.pack("!H", len(wire)) + wire)
            tcp.recv(512)
            tcp.close()
            time.sleep(0.3)
            events, dropped = lab_kit.parse_events(Path(path).read_text(), "dns")
            self.assertEqual(dropped, 0)
            seen = [(e["transport"], e["qname"], e["parsed"]) for e in events if e["kind"] == "dns"]
            self.assertEqual(seen, [("udp", "t1.blocked.lab", True), ("udp", None, False),
                                    ("tcp", "t2.blocked.lab", True)])
            self.assertTrue(any(e["kind"] == "ready" for e in events))


class SinkListenerTests(unittest.TestCase):
    def test_listener_logs_accepts_and_signals_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ev.jsonl")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                tport = s.getsockname()[1]
            log = lab_sink.Log(path)
            threading.Thread(target=lab_sink.listen_tcp, args=(log, tport), daemon=True).start()
            time.sleep(0.5)
            socket.create_connection(("127.0.0.1", tport), timeout=2).close()
            time.sleep(0.3)
            events, dropped = lab_kit.parse_events(Path(path).read_text(), "sink")
            self.assertEqual(dropped, 0)
            self.assertEqual([(e["kind"], e["proto"], e["dport"]) for e in events], [("accept", "tcp", tport)])


class ClientTests(unittest.TestCase):
    def test_http_forms(self):
        self.assertEqual(lab_client.http_request("origin", "a.lab"), b"GET / HTTP/1.1\r\nHost: a.lab\r\n\r\n")
        self.assertEqual(lab_client.http_request("absolute", "a.lab", "b.lab"),
                         b"GET http://b.lab/ HTTP/1.1\r\nHost: a.lab\r\n\r\n")
        self.assertEqual(lab_client.http_request("connect", "a.lab", "b.lab"),
                         b"CONNECT b.lab:443 HTTP/1.1\r\nHost: a.lab\r\n\r\n")
        self.assertNotIn(b"Host", lab_client.http_request("no-host", "a.lab"))
        with self.assertRaises(ValueError):
            lab_client.http_request("bogus", "a.lab")

    def test_parser_accepts_every_subcommand_the_probes_use(self):
        parse = lab_client.build_parser().parse_args
        self.assertEqual(parse(["tcp", "--host", "h", "--port", "443", "--payload", "t"]).payload, "t")
        self.assertEqual(parse(["tls", "--host", "h", "--port", "443", "--sni", "none"]).sni, "none")
        self.assertEqual(parse(["sweep", "--host", "h", "--tcp-ports", "1,2", "--udp-ports", "3"]).tcp_ports, "1,2")
        self.assertTrue(parse(["dns", "--server", "s", "--qname", "q", "--edns"]).edns)
        self.assertEqual(parse(["dns", "--server", "s", "--malformed", "truncated"]).malformed, "truncated")
        self.assertEqual(parse(["afpacket", "--dst-mac", "m", "--src-ip", "a", "--dst-ip", "b", "--dport", "9"]).iface, "eth0")
        self.assertEqual(parse(["rawspoof", "--src-ip", "a", "--dst-ip", "b", "--dport", "9"]).dport, 9)
        self.assertEqual(parse(["stream", "--host", "h", "--port", "1", "--duration", "2"]).duration, 2.0)

    def test_tcp_to_a_closed_port_reports_not_ok_without_raising(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        res = lab_client.tcp("127.0.0.1", port, b"x")
        self.assertFalse(res["ok"])

    def test_tcp_to_a_listener_reports_ok(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        res = lab_client.tcp("127.0.0.1", srv.getsockname()[1], b"x")
        srv.close()
        self.assertEqual((res["action"], res["ok"]), ("tcp", True))


class KitTests(unittest.TestCase):
    def test_parse_events_counts_garbage_instead_of_skipping_it(self):
        events, dropped = lab_kit.parse_events('{"kind":"packet"}\nnot json\n[1]\n\n{"kind":"dns"}\n', "sink")
        self.assertEqual((len(events), dropped), (2, 2))
        self.assertEqual(events[0]["source"], "sink")

    def test_recorder_with_unparseable_lines_fails_loudly(self):
        rec = lab_kit.Recorder("sink", lambda: '{"kind":"packet"}\n{broken\n')
        with self.assertRaises(AssertionError):
            rec.snapshot()

    def test_traffic_ignores_accepts_and_marks_and_filters(self):
        events = [{"kind": "ready"}, {"kind": "accept", "src": "a"},
                  {"kind": "packet", "src": "11.0.0.12"}, {"kind": "packet", "src": "11.0.0.21", "preview": "tag-1"},
                  {"kind": "dns", "src": "11.0.0.21", "qname": "tag-2.lab"}]
        self.assertEqual(len(lab_kit.traffic(events)), 3)
        self.assertEqual(len(lab_kit.traffic(events, since=4)), 1)
        self.assertEqual(len(lab_kit.traffic(events, src=lambda s: s != "11.0.0.12")), 2)
        self.assertEqual(len(lab_kit.traffic(events, match=lab_kit.tagged("tag-2"))), 1)
        self.assertEqual(len(lab_kit.traffic(events, match=lab_kit.tagged("tag-1"))), 1)

    def _rec(self, name, events, blind=False):
        return lab_kit.Recorder(name, lambda: "\n".join(json.dumps(e) for e in events), blind=blind)

    def test_silence_passes_when_nothing_arrived_after_the_mark(self):
        rec = self._rec("sink", [{"kind": "packet", "src": "11.0.0.21", "proto": "tcp", "dst": "x"}])
        lab_kit.silence("X", [rec], {"sink": 1})

    def test_silence_names_the_traffic_it_saw(self):
        rec = self._rec("sink", [{"kind": "packet", "src": "11.0.0.21", "dst": "11.0.0.10", "proto": "tcp",
                                  "sport": 5, "dport": 443, "flags": "S", "preview": "tag"}])
        with self.assertRaises(lab_kit.Breach) as ctx:
            lab_kit.silence("X", [rec], {"sink": 0})
        self.assertEqual(len(ctx.exception.evidence), 1)
        self.assertIn("11.0.0.21", ctx.exception.evidence[0])
        self.assertIn("443", ctx.exception.evidence[0])

    def test_silence_ignores_control_sources(self):
        rec = self._rec("sink", [{"kind": "packet", "src": "11.0.0.12", "proto": "udp"}])
        lab_kit.silence("X", [rec], {"sink": 0}, ignore=("11.0.0.12",))

    def test_blind_recorder_sees_nothing_so_the_baseline_cannot_pass(self):
        events = [{"kind": "packet", "src": "a", "proto": "tcp"}]
        seen = self._rec("sink", events).snapshot()
        blind = self._rec("sink", events, blind=True).snapshot()
        wanted = {"tcp": lambda e: e.get("proto") == "tcp"}
        self.assertEqual(lab_kit.missing_from(seen, wanted), [])
        self.assertEqual(lab_kit.missing_from(blind, wanted), ["tcp"])

    def test_blind_recorder_turns_a_met_probe_into_an_unexpected_success(self):
        events = [{"kind": "packet", "src": "11.0.0.21", "dst": "d", "proto": "tcp"}]
        live, blind = self._rec("sink", events), self._rec("sink", events, blind=True)
        with self.assertRaises(lab_kit.Breach):
            lab_kit.silence("C", [live], {"sink": 0})
        lab_kit.silence("C", [blind], {"sink": 0})   # blind: silent, so an expected failure now "passes"

    def test_classify_matrix(self):
        breach = lab_kit.Breach("X", ["line"])
        cases = [(None, None, "pass"), ("11", None, "unexpected-pass"), ("11", breach, "unmet"),
                 (None, breach, "regression"), ("11", RuntimeError("docker"), "harness-error"),
                 (None, RuntimeError("docker"), "harness-error")]
        for unmet_by, exc, want in cases:
            self.assertEqual(lab_kit.classify("X", unmet_by, exc).status, want, (unmet_by, exc))
        empty = lab_kit.Breach.__new__(lab_kit.Breach)
        empty.probe, empty.evidence = "X", []
        self.assertEqual(lab_kit.classify("X", "11", empty).status, "harness-error")

    def _run_probe(self, unmet_by, body):
        results, errors = [], []
        with mock.patch.dict(lab_kit.UNMET_BY, {"Z": unmet_by}):
            class Case(unittest.TestCase):
                @lab_kit.probe("Z", results=results, harness_errors=errors)
                def test_it(self):
                    body()
            run = lab_kit.run_wrapped(Case)
        return run, results, errors

    def test_unmet_probe_is_an_expected_failure(self):
        def body():
            raise lab_kit.Breach("Z", ["saw it"])
        run, results, _ = self._run_probe("11", body)
        self.assertEqual((len(run.expectedFailures), run.wasSuccessful(), results[0].status), (1, True, "unmet"))

    def test_probe_that_now_passes_fails_the_run_until_flipped(self):
        run, results, _ = self._run_probe("11", lambda: None)
        self.assertEqual((len(run.unexpectedSuccesses), run.wasSuccessful()), (1, False))
        self.assertEqual(results[0].status, "unexpected-pass")

    def test_broken_lab_under_an_expected_failure_is_not_swallowed(self):
        def body():
            raise RuntimeError("docker exploded")
        run, results, errors = self._run_probe("11", body)
        self.assertEqual((len(run.expectedFailures), len(run.unexpectedSuccesses), run.wasSuccessful()), (0, 1, False))
        self.assertEqual(results[0].status, "harness-error")
        with self.assertRaises(AssertionError) as ctx:
            lab_kit.assert_no_harness_errors(errors)
        self.assertIn("docker exploded", str(ctx.exception))

    def test_probe_without_a_ledger_entry_is_a_plain_test_that_fails_on_breach(self):
        def body():
            raise lab_kit.Breach("Z", ["saw it"])
        run, results, _ = self._run_probe("", body)
        self.assertEqual((len(run.failures), len(run.expectedFailures), results[0].status), (1, 0, "regression"))
        run, results, _ = self._run_probe("", lambda: None)
        self.assertEqual((run.wasSuccessful(), results[0].status), (True, "pass"))

    def test_ledger_covers_exactly_the_non_host_probes_with_numbered_children(self):
        self.assertEqual(tuple(lab_kit.UNMET_BY), EXPECTED_PROBES)
        self.assertEqual(lab_kit.PROBE_IDS, EXPECTED_PROBES)
        for probe_id, child in lab_kit.UNMET_BY.items():
            self.assertRegex(child, r"^\d\d$", probe_id)

    def test_probe_decorator_rejects_an_unknown_probe(self):
        with self.assertRaises(KeyError):
            lab_kit.probe("D")

    def test_every_probe_in_the_ledger_has_a_test_method(self):
        source = (Path(__file__).parent / "test_probes.py").read_text()
        for probe_id in lab_kit.PROBE_IDS:
            self.assertIn(f'@probe("{probe_id}")', source, probe_id)

    def test_report_names_each_probe_its_result_and_child(self):
        results = [lab_kit.ProbeResult("C", "unmet", "11", ["e"]), lab_kit.ProbeResult("A-root", "pass")]
        table = lab_kit.report_table(results)
        self.assertRegex(table, r"A-root\s+pass\s+-\s+0")
        self.assertRegex(table, r"C\s+unmet\s+child 11\s+1")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            lab_kit.write_report(results, path)
            data = json.loads(Path(path).read_text())
        self.assertEqual([(d["probe"], d["status"], d["unmet_by_child"]) for d in data],
                         [("C", "unmet", "11"), ("A-root", "pass", None)])


class LabPlanTests(unittest.TestCase):
    def test_uplink_addresses_are_global_unicast(self):
        """The gateway dials only global addresses, so the lab sink must be one."""
        addrs = lab_env.plan_addresses(210, 5)
        for field in ("sink4", "dns4", "decoy4", "b1_4", "ctl4"):
            self.assertTrue(ipaddress.ip_address(getattr(addrs, field)).is_global, field)
        self.assertEqual((addrs.sink4, addrs.b1_4, addrs.sink6), ("11.210.5.10", "11.210.5.21", "fd77:6a:d2:5::10"))
        for field in ("sink4", "b2_4", "decoy4"):
            self.assertIn(ipaddress.ip_address(getattr(addrs, field)), ipaddress.ip_network(addrs.subnet4))

    def test_network_is_dual_stack_with_a_pinned_gateway(self):
        argv = lab_env.network_create_args("n", lab_env.plan_addresses(210, 5))
        self.assertEqual(argv, ["network", "create", "--ipv6", "--subnet", "11.210.5.0/24", "--gateway",
                                "11.210.5.1", "--subnet", "fd77:6a:d2:5::/64", "n"])

    def test_run_args_mount_lab_read_only_run_as_root_and_set_caps_dns_and_host(self):
        argv = lab_env.run_args("c", "img", "n", "11.1.1.21", "fd::21", ["sleep", "infinity"],
                                ("NET_ADMIN", "NET_RAW"), dns="11.1.1.11", add_host="host.docker.internal:11.1.1.2",
                                labels={"k": "v"})
        self.assertEqual(argv[:4], ["run", "-d", "--name", "c"])
        self.assertIn("root", argv)
        self.assertIn(f"{lab_env.LAB_DIR}:/lab:ro", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--cap-add"], ["NET_ADMIN", "NET_RAW"])
        self.assertEqual(argv[argv.index("--dns") + 1], "11.1.1.11")
        self.assertEqual(argv[argv.index("--add-host") + 1], "host.docker.internal:11.1.1.2")
        self.assertEqual(argv[argv.index("--entrypoint") + 1:], ["sleep", "img", "infinity"])

    def test_sink_args_list_every_probed_port(self):
        argv = lab_env.sink_args("sink", join="239.255.77.5")
        self.assertEqual(argv[argv.index("--tcp") + 1], "22,53,80,443,8080,8816,9100,9901,9902")
        self.assertEqual(argv[argv.index("--udp") + 1], "53,443,60001")
        self.assertEqual(argv[-2:], ["--join", "239.255.77.5"])

    def test_alias_and_route_commands_cover_every_dialable_class_and_forbidden_member(self):
        addrs = lab_env.plan_addresses(210, 5)
        aliases = {c[3] for c in lab_env.alias_commands(addrs)}
        routes = {c[3] for c in lab_env.route_commands(addrs)}
        for addr in ("10.77.0.5", "169.254.77.5", "100.64.77.5", "240.77.0.5", "172.31.77.5",
                     "172.30.77.5", "172.29.255.5", "198.18.77.5", "198.19.77.5"):
            self.assertIn(f"{addr}/32", aliases)
            self.assertIn(f"{addr}/32", routes)
        self.assertNotIn("0.77.0.5/32", aliases)     # the kernel refuses 0/8
        self.assertNotIn("127.77.0.5/32", aliases)   # loopback is observed in the dialer
        self.assertIn("239.255.77.5/32", routes)

    def test_every_class_the_epic_names_is_probed(self):
        for label in ("this-network", "loopback", "rfc1918", "link-local", "cgnat", "multicast",
                      "reserved", "ipv4-mapped", "djinn-net"):
            self.assertIn(label, lab_env.CLASS_TARGETS)
        members = set(lab_env.FORBIDDEN_LITERALS)
        for label in ("loopback", "link-local", "198.18/15 low", "198.18/15 high", "bottle-pool",
                      "djinn-net", "egress-control"):
            self.assertIn(label, members)

    def test_class_addresses_really_fall_in_their_class(self):
        for label, (fam, addr, _obs) in lab_env.CLASS_TARGETS.items():
            ip = ipaddress.ip_address(addr)
            ip = getattr(ip, "ipv4_mapped", None) or ip if fam == 6 else ip
            # multicast is "global" to `ipaddress` but is never dialled by the edge
            self.assertTrue(ip.is_multicast or not ip.is_global, label)

    def test_only_the_no_gateway_variant_exists_yet(self):
        with self.assertRaises(NotImplementedError):
            lab_env.Lab("img", variant="gateway")
        lab = lab_env.Lab("img")
        self.assertEqual(lab.gateway_containers(), [])
        with self.assertRaises(LookupError):
            lab.gateway_exec("gateway-ns", ["true"])
        self.assertEqual(lab.bottle_resolver(), lab.addrs.dns4)

    def test_blind_recorder_switch_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"CONTAINMENT_BLIND_RECORDER": "1"}):
            self.assertTrue(lab_env.Lab("img").blind)
        with mock.patch.dict(os.environ, {"CONTAINMENT_BLIND_RECORDER": ""}):
            self.assertFalse(lab_env.Lab("img").blind)


class LabGateTests(unittest.TestCase):
    def test_requested_lab_with_no_docker_fails_instead_of_skipping(self):
        with self.assertRaises(AssertionError) as ctx:
            lab_env.require_lab("bottle", probe=lambda: False)
        self.assertIn("refusing to skip", str(ctx.exception))

    def test_requested_lab_with_no_image_fails(self):
        with self.assertRaises(AssertionError):
            lab_env.require_lab("", probe=lambda: True)

    def test_available_docker_passes(self):
        lab_env.require_lab("bottle", probe=lambda: True)

    def test_lab_is_requested_by_the_image_variable(self):
        with mock.patch.dict(os.environ, {"DJINN_CONTAINMENT_IMAGE": "x"}):
            self.assertTrue(lab_env.lab_requested())
        with mock.patch.dict(os.environ, {"DJINN_CONTAINMENT_IMAGE": ""}):
            self.assertFalse(lab_env.lab_requested())

    def test_probe_class_skips_only_when_the_lab_is_not_requested(self):
        source = (Path(__file__).parent / "test_probes.py").read_text()
        self.assertIn("@unittest.skipUnless(lab_env.lab_requested()", source)
        self.assertIn("lab_env.require_lab(IMAGE)", source)


class HarnessSoundnessTests(unittest.TestCase):
    """Review findings on the lab: no probe may pass because the harness broke."""

    def _lab(self):
        return lab_env.Lab("img")

    def _docker(self, rc=0, out="", err=""):
        return mock.patch.object(lab_env, "docker", return_value=mock.Mock(returncode=rc, stdout=out, stderr=err))

    def test_client_crash_or_missing_json_raises_instead_of_reading_as_refused(self):
        lab = self._lab()
        with self._docker(rc=1, err="Traceback ..."), self.assertRaises(lab_kit.HarnessError):
            lab.client("b1", "tcp", "--host", "x")
        with self._docker(rc=0, out="no json here"), self.assertRaises(lab_kit.HarnessError):
            lab.client("b1", "tcp", "--host", "x")

    def test_client_refusal_is_a_result_not_an_error(self):
        with self._docker(out='{"action": "tcp", "ok": false, "detail": "refused"}'):
            self.assertFalse(self._lab().client("b1", "tcp", "--host", "x")["ok"])

    def test_unreadable_recorder_file_raises_instead_of_reading_as_silence(self):
        with self._docker(rc=1, err="No such container"), self.assertRaises(lab_kit.HarnessError):
            self._lab()._events_text("sink")

    def test_recorder_that_goes_backwards_is_a_harness_error(self):
        texts = ['{"kind": "packet"}\n{"kind": "packet"}', '{"kind": "packet"}']
        rec = lab_kit.Recorder("sink", lambda: texts.pop(0))
        self.assertEqual(len(rec.snapshot()), 2)
        with self.assertRaises(lab_kit.HarnessError):
            rec.snapshot()

    def test_silence_ignores_the_canary_but_not_other_traffic(self):
        events = [{"kind": "packet", "src": "11.0.0.99", "proto": "tcp", "preview": "canary-1"},
                  {"kind": "packet", "src": "11.0.0.21", "proto": "tcp", "preview": "x"}]
        rec = lab_kit.Recorder("sink", lambda: "\n".join(json.dumps(e) for e in events))
        with self.assertRaises(lab_kit.Breach) as ctx:
            lab_kit.silence("X", [rec], {"sink": 0}, canary="canary-1")
        self.assertEqual(len(ctx.exception.evidence), 1)
        only = lab_kit.Recorder("sink", lambda: json.dumps(events[0]))
        lab_kit.silence("X", [only], {"sink": 0}, canary="canary-1")

    def _prove(self, seen_by):
        lab = self._lab()
        lab.recorders = {n: lab_kit.Recorder(n, lambda n=n: json.dumps(
            {"kind": "packet", "src": "c", "preview": "tag-1"}) if n in seen_by else "")
            for n in ("sink", "dns")}
        with mock.patch.object(lab, "client", return_value={"ok": True}) as client, \
                mock.patch.object(lab_env, "CANARY_TIMEOUT", 0.6), mock.patch.object(lab_env.time, "sleep"):
            lab.prove_alive(["sink", "dns"], "tag-1")
        return client

    def test_canary_goes_through_every_recorder_the_probe_relies_on(self):
        client = self._prove({"sink", "dns"})
        self.assertEqual(client.call_count, 2)
        self.assertEqual({c.args[1] for c in client.call_args_list}, {"tcp", "dns"})

    def test_a_recorder_that_misses_the_canary_fails_the_probe_as_a_harness_error(self):
        with self.assertRaisesRegex(lab_kit.HarnessError, r"\['dns'\]"):
            self._prove({"sink"})

    def test_not_built_probe_is_reported_as_not_built_and_stays_an_expected_failure(self):
        self.assertEqual(lab_kit.classify("J", "09", lab_kit.NotBuilt("J", ["a"])).status, "not-built")

        def body():
            raise lab_kit.NotBuilt("Z", ["no assertion"])
        results, errors = [], []
        with mock.patch.dict(lab_kit.UNMET_BY, {"Z": "09"}):
            class Case(unittest.TestCase):
                @lab_kit.probe("Z", results=results, harness_errors=errors)
                def test_it(self):
                    body()
            run = lab_kit.run_wrapped(Case)
        self.assertEqual((len(run.expectedFailures), results[0].status, errors), (1, "not-built", []))

    def test_bottle_hardening_is_read_from_the_shipped_compose(self):
        hard = lab_env.bottle_hardening()
        self.assertEqual(hard["cap_add"], ["NET_ADMIN", "NET_RAW"])
        self.assertEqual(hard["cap_drop"], [])
        self.assertEqual(self._lab().hardening, hard)

    def test_bottle_hardening_follows_a_cap_drop_and_ignores_other_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yml"
            path.write_text("services:\n  other:\n    cap_add:\n      - SYS_ADMIN\n  djinn:\n"
                            "    cap_drop:\n      - NET_ADMIN  # gone\n      - NET_RAW\n"
                            "    security_opt:\n      - 'no-new-privileges:true'\n    image: x\n")
            self.assertEqual(lab_env.bottle_hardening(path),
                             {"cap_add": [], "cap_drop": ["NET_ADMIN", "NET_RAW"],
                              "security_opt": ["no-new-privileges:true"]})
            path.write_text("services:\n  other:\n    image: x\n")
            with self.assertRaises(RuntimeError):
                lab_env.bottle_hardening(path)

    def test_run_args_carry_cap_drop_and_security_opt(self):
        argv = lab_env.run_args("c", "img", "n", "11.1.1.21", "fd::21", ["sleep"], cap_drop=("NET_RAW",),
                                security_opt=("no-new-privileges:true",))
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "NET_RAW")
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges:true")

    def test_root_and_non_ascii_queries_are_answered_and_recorded_not_fatal(self):
        zone = DnsWireTests.ZONE
        root = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + b"\0" + struct.pack("!HH", 1, 1)
        reply = lab_dns.build_response(root, zone)
        self.assertEqual(struct.unpack("!H", reply[2:4])[0] & 0xF, lab_dns.RCODE_NXDOMAIN)
        odd = struct.pack("!HHHHHH", 8, 0x0100, 1, 0, 0, 0) + b"\x02\xc3\xa9\0" + struct.pack("!HH", 1, 1)
        self.assertEqual(struct.unpack("!H", lab_dns.build_response(odd, zone)[:2])[0], 8)

    def test_server_survives_a_query_it_cannot_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = lab_dns.Server(DnsWireTests.ZONE, os.path.join(tmp, "e.jsonl"))
            with mock.patch.object(lab_dns, "build_response", side_effect=RuntimeError("boom")):
                srv.failed("udp", ("11.1.2.21", 1), RuntimeError("boom"))
            lines = [json.loads(ln) for ln in Path(tmp, "e.jsonl").read_text().splitlines()]
        self.assertEqual((lines[0]["kind"], lines[0]["error"]), ("error", "RuntimeError"))
        self.assertEqual(lab_kit.traffic(lines), [])   # an error line is never counted as traffic

    def test_baseline_covers_the_decoy_bottle_witnesses_and_a_closing_run(self):
        source = (Path(__file__).parent / "test_probes.py").read_text()
        for needle in ("def test_03_baseline_decoy_and_bottle_recorders", "def test_99_closing_baseline",
                       "self.lab.prove_alive(recorders, canary)"):
            self.assertIn(needle, source)

    def test_probe_f_attributes_by_arrival_not_by_source(self):
        source = (Path(__file__).parent / "test_probes.py").read_text()
        body = source.split("def test_40_F")[1].split("@probe(")[0]
        self.assertNotIn("filters", body)
        self.assertNotIn("b1_4", body)


class StagedWorkflowTests(unittest.TestCase):
    def test_staged_workflow_runs_the_containment_job_and_keeps_the_existing_jobs(self):
        staged = REPO / "ci-staged" / "ci.yml"
        if not staged.exists():
            self.skipTest("no pending staged workflow")
        text = staged.read_text()
        current = (REPO / ".github" / "workflows" / "ci.yml").read_text()
        self.assertRegex(text, r"(?m)^  containment:\n(?:    #.*\n)+    name: containment$")
        self.assertIn("DJINN_CONTAINMENT_IMAGE", text)
        self.assertIn("tests/containment", text)
        # Full copy: everything in the live workflow's test job is still there.
        for line in current.splitlines():
            if line.strip().startswith("- name:"):
                self.assertIn(line.strip(), text)


if __name__ == "__main__":
    unittest.main()
