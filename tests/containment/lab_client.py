"""In-bottle probe client: one subcommand per way of trying to leave.

Runs as root inside the bottle image (stdlib only), driven by `docker exec`.
Each subcommand prints one JSON line {"action", "ok", "detail"}; `ok` says the
attempt was *permitted locally* (a connect completed, a frame was sent), NOT
that it escaped: the recorders are the oracle. Pure builders are unit-tested.
"""

import argparse
import json
import socket
import ssl
import struct
import sys
import time

import lab_dns
import lab_packets

HTTP_FORMS = ("origin", "absolute", "connect", "no-host")
TIMEOUT = 3.0


def http_request(form: str, name: str, target: str = "") -> bytes:
    """The request bytes for probe C-form's HTTP shapes. `name` goes in Host;
    `target` (default: name) is where absolute-form and CONNECT point."""
    target = target or name
    if form == "origin":
        text = f"GET / HTTP/1.1\r\nHost: {name}\r\n\r\n"
    elif form == "absolute":
        text = f"GET http://{target}/ HTTP/1.1\r\nHost: {name}\r\n\r\n"
    elif form == "connect":
        text = f"CONNECT {target}:443 HTTP/1.1\r\nHost: {name}\r\n\r\n"
    elif form == "no-host":
        text = "GET / HTTP/1.0\r\n\r\n"
    else:
        raise ValueError(f"unknown form {form!r}")
    return text.encode("ascii")


def result(action: str, ok: bool, detail: str = "") -> dict:
    return {"action": action, "ok": ok, "detail": detail}


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def tcp(host: str, port: int, payload: bytes = b"") -> dict:
    """Connect by address or name, optionally send, report whether connect completed."""
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
    except OSError as exc:
        return result("tcp", False, f"{type(exc).__name__}: {exc}")
    try:
        if payload:
            sock.sendall(payload)
            time.sleep(0.2)
        return result("tcp", True, "connected")
    except OSError as exc:
        return result("tcp", False, f"{type(exc).__name__}: {exc}")
    finally:
        sock.close()


def udp(host: str, port: int, payload: bytes) -> dict:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        family, _t, _p, _c, addr = infos[0]
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.sendto(payload, addr)
        sock.close()
        return result("udp", True, "sent")
    except OSError as exc:
        return result("udp", False, f"{type(exc).__name__}: {exc}")


def tls(host: str, port: int, sni: str) -> dict:
    """Send a real ClientHello; the lab listener never answers, so ok=False is normal."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
    except OSError as exc:
        return result("tls", False, f"connect {type(exc).__name__}: {exc}")
    try:
        sock.settimeout(TIMEOUT)
        ctx.wrap_socket(sock, server_hostname=None if sni == "none" else sni)
        return result("tls", True, "handshake completed")
    except (OSError, ssl.SSLError) as exc:
        return result("tls", True, f"hello sent, no handshake: {type(exc).__name__}")
    finally:
        sock.close()


def http(host: str, port: int, form: str, name: str, target: str) -> dict:
    return tcp(host, port, http_request(form, name, target))


def icmp(host: str) -> dict:
    v6 = ":" in host
    try:
        sock = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_RAW,
                             socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP)
        sock.settimeout(1)
        body = struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, 0x4C42, 1) + b"labping"
        if not v6:
            body = body[:2] + struct.pack("!H", lab_packets.checksum(body)) + body[4:]
        sock.sendto(body, (host, 0))
        sock.close()
        return result("icmp", True, "echo sent")
    except OSError as exc:
        return result("icmp", False, f"{type(exc).__name__}: {exc}")


def dns(server: str, qname: str, qtype: str, transport: str, edns: bool, malformed: str) -> dict:
    wire = (lab_dns.malformed_query(malformed) if malformed
            else lab_dns.build_query(qname, lab_dns.qtype_number(qtype), edns=edns))
    family = _family(server)
    try:
        if transport == "tcp":
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(TIMEOUT)
            sock.connect((server, 53))
            sock.sendall(struct.pack("!H", len(wire)) + wire)
            sock.recv(512)
        else:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(1.5)
            sock.sendto(wire, (server, 53))
            sock.recvfrom(4096)
        sock.close()
        return result("dns", True, "answered")
    except OSError as exc:
        return result("dns", False, f"{type(exc).__name__}: {exc}")


def afpacket(iface: str, dst_mac: str, src_ip: str, dst_ip: str, dport: int, payload: bytes) -> dict:
    """Probe B: a hand-built frame written straight to the link, below the IP stack."""
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((iface, 0))
        src_mac = ":".join(f"{b:02x}" for b in sock.getsockname()[4][:6])
        packet = lab_packets.build_ipv4_udp(src_ip, dst_ip, 40000, dport, payload)
        sent = sock.send(lab_packets.build_ethernet(dst_mac, src_mac, lab_packets.ETH_P_IP, packet))
        sock.close()
        return result("afpacket", True, f"sent {sent}B")
    except OSError as exc:
        return result("afpacket", False, f"{type(exc).__name__}: {exc}")


def rawspoof(src_ip: str, dst_ip: str, dport: int, payload: bytes) -> dict:
    """Probe A-spoof: an IP packet carrying a source address that is not ours."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
        sent = sock.sendto(lab_packets.build_ipv4_udp(src_ip, dst_ip, 40001, dport, payload), (dst_ip, 0))
        sock.close()
        return result("rawspoof", True, f"sent {sent}B as {src_ip}")
    except OSError as exc:
        return result("rawspoof", False, f"{type(exc).__name__}: {exc}")


def sweep(host: str, tcp_ports, udp_ports, tag: str) -> dict:
    """One address, every probed port: TCP and UDP, plus an ICMP echo. Probes H."""
    sent = [tcp(host, p, tag.encode())["ok"] for p in tcp_ports]
    sent += [udp(host, p, tag.encode())["ok"] for p in udp_ports]
    sent.append(icmp(host)["ok"])
    return result("sweep", True, f"{sum(sent)}/{len(sent)} attempts locally permitted")


def stream(host: str, port: int, duration: float, interval: float) -> dict:
    """Probe I: a steady mix of TCP, UDP and ICMP to one address for `duration` seconds."""
    end, rounds = time.monotonic() + duration, 0
    while time.monotonic() < end:
        tcp(host, port, b"lab-stream")
        udp(host, port, b"lab-stream")
        icmp(host)
        rounds += 1
        time.sleep(interval)
    return result("stream", True, f"{rounds} rounds")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("tcp", "udp", "tls", "http", "stream", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--host", required=True)
        p.add_argument("--port", type=int, default=0)
        p.add_argument("--payload", default="")
        p.add_argument("--sni", default="none")
        p.add_argument("--form", default="origin", choices=HTTP_FORMS)
        p.add_argument("--name", default="")
        p.add_argument("--target", default="")
        p.add_argument("--tcp-ports", default="")
        p.add_argument("--udp-ports", default="")
        p.add_argument("--duration", type=float, default=5.0)
        p.add_argument("--interval", type=float, default=0.2)
    p = sub.add_parser("icmp")
    p.add_argument("--host", required=True)
    p = sub.add_parser("dns")
    p.add_argument("--server", required=True)
    p.add_argument("--qname", default="")
    p.add_argument("--qtype", default="A")
    p.add_argument("--transport", default="udp", choices=("udp", "tcp"))
    p.add_argument("--edns", action="store_true")
    p.add_argument("--malformed", default="", choices=("",) + lab_dns.MALFORMED_KINDS)
    p = sub.add_parser("afpacket")
    p.add_argument("--iface", default="eth0")
    p.add_argument("--dst-mac", required=True)
    p.add_argument("--src-ip", required=True)
    p.add_argument("--dst-ip", required=True)
    p.add_argument("--dport", type=int, required=True)
    p.add_argument("--payload", default="")
    p = sub.add_parser("rawspoof")
    p.add_argument("--src-ip", required=True)
    p.add_argument("--dst-ip", required=True)
    p.add_argument("--dport", type=int, required=True)
    p.add_argument("--payload", default="")
    return ap


def run(args) -> dict:
    payload = args.payload.encode() if hasattr(args, "payload") else b""
    if args.action == "tcp":
        return tcp(args.host, args.port, payload)
    if args.action == "udp":
        return udp(args.host, args.port, payload)
    if args.action == "tls":
        return tls(args.host, args.port, args.sni)
    if args.action == "http":
        return http(args.host, args.port, args.form, args.name, args.target)
    if args.action == "sweep":
        ports = lambda text: [int(p) for p in text.split(",") if p]  # noqa: E731
        return sweep(args.host, ports(args.tcp_ports), ports(args.udp_ports), args.payload)
    if args.action == "stream":
        return stream(args.host, args.port, args.duration, args.interval)
    if args.action == "icmp":
        return icmp(args.host)
    if args.action == "dns":
        return dns(args.server, args.qname, args.qtype, args.transport, args.edns, args.malformed)
    if args.action == "afpacket":
        return afpacket(args.iface, args.dst_mac, args.src_ip, args.dst_ip, args.dport, payload)
    return rawspoof(args.src_ip, args.dst_ip, args.dport, payload)


def main(argv=None) -> None:
    print(json.dumps(run(build_parser().parse_args(argv))), flush=True)


if __name__ == "__main__":
    sys.exit(main())
