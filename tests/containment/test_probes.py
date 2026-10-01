#!/usr/bin/env python3
"""The containment probes: one test per non-host probe of the egress epic.

Lab: the bottle image CI built (DJINN_CONTAINMENT_IMAGE), a recording sink on the
uplink, a recording lab upstream DNS, a listener in bottle 1 on every probed
port, and a decoy standing in for the gateway/host addresses. A probe passes
only when the recorders saw nothing. Today no gateway exists, so each probe
runs against the no-gateway variant (today's bottle) and is an *expected
failure* naming the child that flips it (lab_kit.UNMET_BY); flipping a probe is
part of that child's PR.

Skips only when the lab was not requested. When DJINN_CONTAINMENT_IMAGE is set and
docker is unavailable the class FAILS: the gate never goes green by skipping.

    docker build -t bottle . && DJINN_CONTAINMENT_IMAGE=bottle \\
        python3 -m unittest discover -s tests/containment -p 'test_probes.py' -v

CONTAINMENT_BLIND_RECORDER=1 blinds the recorders: the check must go red.
"""

import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lab_dns  # noqa: E402
import lab_env  # noqa: E402
import lab_kit  # noqa: E402
from lab_kit import Breach, probe, silence, tagged  # noqa: E402

IMAGE = os.environ.get("DJINN_CONTAINMENT_IMAGE", "")
HTTP_PORT, TLS_PORT = 80, 443
QTYPES_ALL = ("A", "AAAA", "MX", "TXT", "NS", "SOA", "CNAME", "ANY", "SRV", "PTR", "DS", "NAPTR",
              "CAA", "HTTPS", "SVCB", "AXFR", "TYPE65280")
QTYPES_EMBEDDED = ("A", "AAAA", "MX", "TXT", "ANY")
# What root might try to change; (label, command, undo). Each must be refused.
TAMPER = (
    ("add an address", "ip addr add 10.99.99.9/32 dev eth0", "ip addr del 10.99.99.9/32 dev eth0"),
    ("add a route", "ip route add 10.98.98.0/24 dev eth0", "ip route del 10.98.98.0/24 dev eth0"),
    ("add a link", "ip link add lab0 type dummy", "ip link del lab0"),
    ("insert a firewall rule", "iptables -I OUTPUT 1 -j ACCEPT", "iptables -D OUTPUT 1"),
    ("unshare -n", "unshare -n true", "true"),
    ("create a netns", "ip netns add labns", "ip netns del labns"),
)


def new_tag(probe_id: str) -> str:
    return f"{probe_id}-{uuid.uuid4().hex[:8]}"


@unittest.skipUnless(lab_env.lab_requested(), "DJINN_CONTAINMENT_IMAGE not set: lab not requested")
class ContainmentProbes(unittest.TestCase):
    lab = None

    @classmethod
    def setUpClass(cls):
        lab_env.require_lab(IMAGE)
        cls.lab = lab_env.Lab(IMAGE)
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()

    @classmethod
    def tearDownClass(cls):
        lab_kit.emit_report(lab_kit.RESULTS)
        lab_kit.assert_no_harness_errors()
        if os.environ.get("CONTAINMENT_REQUIRE_ALL") == "1":
            ran = {r.probe for r in lab_kit.RESULTS}
            missing = [p for p in lab_kit.PROBE_IDS if p not in ran]
            if missing:
                raise AssertionError(f"probes did not run: {missing}")

    # ── helpers ───────────────────────────────────────────────────────────

    def rec(self, *names):
        return [self.lab.recorders[n] for n in names]

    def finish(self, probe_id, marks, recorders=("sink",), extra=(), **kw):
        """Wait for the recorders to catch up, then require silence; `extra`
        are breach lines the probe found by other means (a command that worked)."""
        self.lab.settle()
        kw.setdefault("ignore", self.lab.ignore_sources)
        try:
            silence(probe_id, self.rec(*recorders), marks, **kw)
        except Breach as breach:
            raise Breach(probe_id, list(extra) + breach.evidence)
        if extra:
            raise Breach(probe_id, extra)

    # ── baseline: the recorders are not blind ─────────────────────────────

    def test_00_baseline_recorder_sees_every_traffic_class(self):
        """Control traffic from `ctl` (outside any bottle policy) must show up in the
        recorders, once per class later probes call 'seen nothing'. If a runner
        cannot route IPv6 or send AF_PACKET frames this fails here, by name,
        rather than letting probe E or B pass blind."""
        lab, a, tag = self.lab, self.lab.addrs, new_tag("base")
        before = lab.marks()
        sent = {
            "tcp": lab.client("ctl", "tcp", "--host", a.sink4, "--port", "443", "--payload", f"{tag}-tcp"),
            "udp": lab.client("ctl", "udp", "--host", a.sink4, "--port", "443", "--payload", f"{tag}-udp"),
            "icmp": lab.client("ctl", "icmp", "--host", a.sink4),
            "tcp6": lab.client("ctl", "tcp", "--host", a.sink6, "--port", "443", "--payload", f"{tag}-tcp6"),
            "frame": lab.client("ctl", "afpacket", "--dst-mac", lab.mac("sink"), "--src-ip", a.ctl4,
                                "--dst-ip", a.sink4, "--dport", "60001", "--payload", f"{tag}-frame"),
            "dns-udp": lab.client("ctl", "dns", "--server", a.dns4, "--qname", f"{tag}.allowed.lab"),
            "dns-tcp": lab.client("ctl", "dns", "--server", a.dns4, "--qname", f"{tag}-t.allowed.lab",
                                  "--transport", "tcp"),
        }
        lab.settle()
        sink = lab_kit.traffic(lab.recorders["sink"].snapshot(), before["sink"])
        dns = lab_kit.traffic(lab.recorders["dns"].snapshot(), before["dns"])
        wanted = {
            "tcp": lambda e: e.get("proto") == "tcp" and f"{tag}-tcp" in e.get("preview", ""),
            "udp": lambda e: e.get("proto") == "udp" and f"{tag}-udp" in e.get("preview", ""),
            "icmp": lambda e: e.get("proto") == "icmp" and e.get("src") == a.ctl4,
            "tcp6": lambda e: e.get("family") == 6 and f"{tag}-tcp6" in e.get("preview", ""),
            "frame": lambda e: e.get("proto") == "udp" and f"{tag}-frame" in e.get("preview", ""),
        }
        missing = lab_kit.missing_from(sink, wanted)
        missing += [f"dns:{n}" for n in lab_kit.missing_from(dns, {
            "udp": lambda e: e.get("qname") == f"{tag}.allowed.lab" and e.get("transport") == "udp",
            "tcp": lambda e: e.get("qname") == f"{tag}-t.allowed.lab" and e.get("transport") == "tcp"})]
        self.assertEqual(missing, [], f"recorders missed control traffic (blind, or the runner cannot "
                                      f"carry it: IPv6/AF_PACKET forks to the user): {missing}\nsent={sent}")

    def test_01_baseline_allowed_flow_from_a_bottle(self):
        """The variant's allowed flow (no gateway: any flow) reaches the sink from bottle 1."""
        lab, tag = self.lab, new_tag("flow")
        before = lab.marks()
        res = lab.client("b1", "tcp", "--host", "allowed.lab", "--port", "443", "--payload", tag)
        lab.settle()
        seen = lab_kit.traffic(lab.recorders["sink"].snapshot(), before["sink"], match=tagged(tag))
        dns = lab_kit.traffic(lab.recorders["dns"].snapshot(), before["dns"],
                              match=lambda e: e.get("qname") == "allowed.lab")
        self.assertTrue(seen, f"allowed flow not recorded at the sink: {res}")
        self.assertTrue(dns, "allowed name never reached the lab DNS")

    def test_02_baseline_every_alias_address_reaches_the_sink(self):
        """Probes C-inv and K dial addresses that exist only as sink aliases (and
        loopback in bottle 1). Prove the aliases and routes took: a probe that dials
        an address nobody can receive at would pass for the wrong reason."""
        lab, tag = self.lab, new_tag("alias")
        targets = {label: addr for label, (fam, addr, obs) in lab_env.CLASS_TARGETS.items()
                   if fam == 4 and obs == "sink" and addr not in lab_env.UNROUTABLE
                   and not addr.startswith("239.")}
        targets.update({f"lit-{k}": v for k, v in lab_env.FORBIDDEN_LITERALS.items()
                        if not v.startswith("127.")})
        before = lab.marks()
        for label, addr in targets.items():
            lab.client("ctl", "tcp", "--host", addr, "--port", "443", "--payload", f"{tag}-{label}")
        lab.client("b1", "tcp", "--host", "127.77.0.5", "--port", "443", "--payload", f"{tag}-loopback")
        lab.settle()
        sink = lab_kit.traffic(lab.recorders["sink"].snapshot(), before["sink"])
        own = lab_kit.traffic(lab.recorders["b1"].snapshot(), before["b1"], lambda s: s.startswith("127."))
        wanted = {label: (lambda e, lb=label: f"{tag}-{lb}" in e.get("preview", "")) for label in targets}
        missing = lab_kit.missing_from(sink, wanted)
        missing += lab_kit.missing_from(own, {"loopback": lambda e: f"{tag}-loopback" in e.get("preview", "")})
        self.assertEqual(missing, [], f"addresses no recorder could see: {missing}")

    # ── probes ────────────────────────────────────────────────────────────

    @probe("A-root")
    def test_10_A_root_cannot_change_routes_rules_or_addresses(self):
        lab, tag = self.lab, new_tag("Aroot")
        marks, worked = lab.marks(), []
        for label, cmd, undo in TAMPER:
            if lab.exec("b1", ["sh", "-c", cmd], check=False).returncode == 0:
                worked.append(f"root in bottle 1 managed to {label}: `{cmd}`")
                lab.exec("b1", ["sh", "-c", undo], check=False)
        lab.client("b1", "tcp", "--host", lab.addrs.sink4, "--port", "443", "--payload", tag)
        self.finish("A-root", marks, extra=worked)

    @probe("A-spoof")
    def test_11_A_spoof_forged_sources_are_dropped(self):
        # The gateway variant grants NET_RAW to this bottle for this probe only;
        # today's bottle already has it.
        lab, a, tag = self.lab, self.lab.addrs, new_tag("Aspoof")
        marks = lab.marks()
        for src in (a.b2_4, a.b1_4.rsplit(".", 1)[0] + ".99", "11.0.0.1"):
            lab.client("b1", "rawspoof", "--src-ip", src, "--dst-ip", a.sink4, "--dport", "443",
                       "--payload", f"{tag}-{src}")
        self.finish("A-spoof", marks, match=tagged(tag))

    @probe("B")
    def test_12_B_af_packet_frame_does_not_reach_the_sink(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("B")
        marks = lab.marks()
        lab.client("b1", "afpacket", "--dst-mac", lab.mac("sink"), "--src-ip", a.b1_4,
                   "--dst-ip", a.sink4, "--dport", "60001", "--payload", tag)
        self.finish("B", marks, match=tagged(tag))

    @probe("C")
    def test_20_C_blocked_name_on_an_allowed_ip(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("C")
        marks = lab.marks()
        lab.client("b1", "tls", "--host", a.sink4, "--port", "443", "--sni", f"{tag}.blocked.lab")
        lab.client("b1", "http", "--host", a.sink4, "--port", "80", "--name", f"{tag}.blocked.lab")
        lab.client("b1", "tls", "--host", f"{tag}.blocked.lab", "--port", "443", "--sni", f"{tag}.blocked.lab")
        lab.client("b1", "http", "--host", f"{tag}.blocked.lab", "--port", "80", "--name", f"{tag}.blocked.lab")
        self.finish("C", marks)

    @probe("C-inv")
    def test_21_C_inv_non_global_names_and_forbidden_grants(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("Cinv")
        marks = lab.marks()
        # A bottle that is NOT granted the plugin port must not reach the host
        # address (a literal cidr:port grant over HOST_GATEWAY_IP is refused;
        # reachable only via a bottle's own host_ports). Positive control for
        # bottle 1's own port joins when the gateway variant exists (child 09).
        lab.client("b2", "tcp", "--host", "host.docker.internal", "--port", str(lab_env.HOST_PLUGIN_PORT),
                   "--payload", f"{tag}-host-name")
        lab.client("b2", "tcp", "--host", a.decoy4, "--port", str(lab_env.HOST_PLUGIN_PORT),
                   "--payload", f"{tag}-host-ip")
        # An allowed name resolving to each non-global class.
        for label in lab_env.CLASS_TARGETS:
            lab.client("b1", "tcp", "--host", f"{label}.allowed.lab", "--port", "443",
                       "--payload", f"{tag}-class-{label}")
        # A literal cidr:port grant overlapping each forbidden-set member.
        for label, addr in lab_env.FORBIDDEN_LITERALS.items():
            lab.client("b1", "tcp", "--host", addr, "--port", "443", "--payload", f"{tag}-lit-{label}")
        self.finish("C-inv", marks, recorders=("sink", "decoy", "b1"), match=tagged(tag),
                    filters={"b1": lambda src: src.startswith("127.")})

    @probe("C-form")
    def test_22_C_form_malformed_requests_for_an_allowed_name(self):
        lab, tag = self.lab, new_tag("Cform")
        marks = lab.marks()
        host = "allowed.lab"
        lab.client("b1", "http", "--host", host, "--port", "80", "--form", "absolute",
                   "--name", host, "--target", f"{tag}.blocked.lab")
        lab.client("b1", "http", "--host", host, "--port", "80", "--form", "connect",
                   "--name", host, "--target", f"{tag}.blocked.lab")
        lab.client("b1", "http", "--host", host, "--port", "80", "--form", "no-host", "--name", tag)
        lab.client("b1", "tls", "--host", host, "--port", "443", "--sni", "none")
        lab.client("b1", "tls", "--host", host, "--port", "443", "--sni", f"{tag}.blocked.lab")
        lab.client("b1", "udp", "--host", host, "--port", "443", "--payload", f"{tag}-quic")
        self.finish("C-form", marks)

    @probe("E")
    def test_30_E_ipv6_does_not_leave(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("E")
        marks = lab.marks()
        lab.client("b1", "tcp", "--host", a.sink6, "--port", "443", "--payload", f"{tag}-tcp")
        lab.client("b1", "udp", "--host", a.sink6, "--port", "443", "--payload", f"{tag}-udp")
        lab.client("b1", "icmp", "--host", a.sink6)
        self.finish("E", marks, filters={"sink": lambda src: ":" in src and src not in lab.ignore_sources})

    @probe("F")
    def test_40_F_dns_for_non_allowed_zones_stays_in_the_gateway(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("F")
        marks, server = lab.marks(), lab.bottle_resolver()
        for qtype in QTYPES_ALL:
            for transport in ("udp", "tcp"):
                lab.client("b1", "dns", "--server", server, "--qname", f"{tag}-{qtype.lower()}.blocked.lab",
                           "--qtype", qtype, "--transport", transport)
        lab.client("b1", "dns", "--server", server, "--qname", f"{tag}-edns.blocked.lab", "--edns")
        for kind in lab_dns.MALFORMED_KINDS:
            lab.client("b1", "dns", "--server", server, "--malformed", kind)
            lab.client("b1", "dns", "--server", server, "--malformed", kind, "--transport", "tcp")
        lab.client("b1", "dns", "--server", server, "--qname", "cname.blocked.lab")
        self.finish("F", marks, recorders=("dns",), filters={"dns": lambda src: src == a.b1_4})

    @probe("G")
    def test_41_G_docker_embedded_dns_does_not_forward(self):
        lab, tag = self.lab, new_tag("G")
        marks = lab.marks()
        for qtype in QTYPES_EMBEDDED:
            for transport in ("udp", "tcp"):
                lab.client("b1", "dns", "--server", "127.0.0.11", "--qname",
                           f"{tag}-{qtype.lower()}.blocked.lab", "--qtype", qtype, "--transport", transport)
        lab.client("b1", "dns", "--server", "127.0.0.11", "--qname", f"{tag}-edns.blocked.lab", "--edns")
        self.finish("G", marks, recorders=("dns",), match=tagged(tag), filters={"dns": lambda src: True})

    @probe("H")
    def test_50_H_bottle_to_bottle_and_gateway_listeners(self):
        lab, a, tag = self.lab, self.lab.addrs, new_tag("H")
        marks = lab.marks()
        ports = dict(tcp_ports=",".join(map(str, lab_env.TCP_PORTS)),
                     udp_ports=",".join(map(str, lab_env.UDP_PORTS)))
        # Bottle 2 -> bottle 1's listeners.
        lab.client("b2", "sweep", "--host", a.b1_4, "--payload", f"{tag}-b2b1", "--tcp-ports", ports["tcp_ports"],
                   "--udp-ports", ports["udp_ports"])
        # Any bottle -> the `.1`/`.2` listener ports (the decoy stands in for
        # the gateway/supervisor/Newt addresses; 9901 is the filing port).
        for role in ("b1", "b2"):
            lab.client(role, "sweep", "--host", a.decoy4, "--payload", f"{tag}-{role}-gw",
                       "--tcp-ports", ports["tcp_ports"], "--udp-ports", ports["udp_ports"])
        self.finish("H", marks, recorders=("b1", "decoy"), filters={
            "b1": lambda src: src == a.b2_4, "decoy": lambda src: src in (a.b1_4, a.b2_4)})

    @probe("I")
    def test_51_I_nothing_leaks_while_the_gateway_boots_restarts_or_is_recreated(self):
        # Bootstrap phase (Lab.start(broker_up=False)) and per-container restart
        # and recreate arrive with the gateway variant (child 09); the stream
        # machinery is here now so the zero-at-the-sink assertion is already live.
        lab, a = self.lab, self.lab.addrs
        marks = lab.marks()
        stream = threading.Thread(target=lab.client, args=("b1", "stream", "--host", a.sink4, "--port", "443",
                                                           "--duration", "5"), kwargs={"timeout": 30})
        stream.start()
        for name in lab.gateway_containers():  # none until child 09
            lab.gateway_exec(name, ["true"])
        stream.join()
        self.finish("I", marks, match=lambda e: "lab-stream" in (e.get("preview") or "") or e.get("proto") == "icmp")

    @probe("J")
    def test_52_J_route_invariant_after_create_attach_and_recreate(self):
        lab, evidence = self.lab, []
        for phase in ("create", "live attach", "recreate"):
            try:
                out = lab.gateway_exec("gateway-ns", ["ip", "route", "show", "default"])
                evidence.append(f"{phase}: default route {out!r} (no assertion built yet)")
            except LookupError as exc:
                evidence.append(f"{phase}: {exc}")
        raise Breach("J", evidence)

    @probe("K")
    def test_60_K_per_bottle_policy_and_fake_ip_namespaces(self):
        lab = self.lab
        tag1, tag2 = new_tag("K1"), new_tag("K2")
        marks = lab.marks()
        # Opposing policies: bottle 1 is allowed allowed.lab:443, bottle 2 is not.
        lab.client("b1", "tcp", "--host", "allowed.lab", "--port", "443", "--payload", tag1)
        lab.client("b2", "tcp", "--host", "allowed.lab", "--port", "443", "--payload", tag2)
        # A fake IP mapped in bottle 1 but unmapped in bottle 2, used by bottle 2.
        lab.client("b2", "tcp", "--host", "198.18.77.5", "--port", "443", "--payload", f"{tag2}-unmapped")
        # Bottle 2 on bottle 1's host-relay port.
        lab.client("b2", "tcp", "--host", "host.docker.internal", "--port", str(lab_env.HOST_PLUGIN_PORT),
                   "--payload", f"{tag2}-host")
        lab.settle()
        control = lab_kit.traffic(lab.recorders["sink"].snapshot(), marks["sink"], match=tagged(tag1))
        if not control:
            raise AssertionError("bottle 1's own allowed flow was not recorded: the probe has no control")
        extra = []
        for step in ("restart the edge", "allocate 50 fake IPs and re-resolve a pre-restart address",
                     "read each filing's bottle from the broker"):
            try:
                lab.gateway_exec("gateway-edge", ["true"])
            except LookupError as exc:
                extra.append(f"cannot {step}: {exc}")
        self.finish("K", marks, recorders=("sink", "decoy"), match=tagged(tag2), extra=extra)


if __name__ == "__main__":
    unittest.main()
