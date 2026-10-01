"""Packet parsing and construction shared by the recorder, the in-bottle client
and the tests. Stdlib only: it runs inside the bottle image as well as on the
CI runner.
"""

import socket
import struct

ETH_HDR = 14
ETH_P_IP = 0x0800
ETH_P_IPV6 = 0x86DD
PACKET_OUTGOING = 4
PREVIEW_BYTES = 96

# MLD (130-132, 143) and neighbour/router discovery (133-137) are the kernel
# talking to itself on the lab bridge, not probe traffic.
ICMP6_NOISE = frozenset({130, 131, 132, 133, 134, 135, 136, 143})
TCP_FLAG_LETTERS = (("F", 0x01), ("S", 0x02), ("R", 0x04), ("P", 0x08), ("A", 0x10), ("U", 0x20))


def preview(data: bytes) -> str:
    """First PREVIEW_BYTES of a payload, non-printables as '.', for the event log."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data[:PREVIEW_BYTES])


def tcp_flags(byte: int) -> str:
    return "".join(letter for letter, bit in TCP_FLAG_LETTERS if byte & bit)


def _transport(proto: int, body: bytes, event: dict, v6: bool) -> bool:
    """Fill ports/flags/preview for TCP/UDP/ICMP. False means: ignore as noise."""
    if proto == 6 and len(body) >= 20:
        sport, dport = struct.unpack("!HH", body[:4])
        offset = (body[12] >> 4) * 4
        event.update(proto="tcp", sport=sport, dport=dport, flags=tcp_flags(body[13]),
                     preview=preview(body[offset:]), plen=max(0, len(body) - offset))
    elif proto == 17 and len(body) >= 8:
        sport, dport = struct.unpack("!HH", body[:4])
        event.update(proto="udp", sport=sport, dport=dport, preview=preview(body[8:]),
                     plen=max(0, len(body) - 8))
    elif (proto == 58) if v6 else (proto == 1):
        if not body:
            return False
        if v6 and body[0] in ICMP6_NOISE:
            return False
        event.update(proto="icmp6" if v6 else "icmp", icmp_type=body[0],
                     icmp_code=body[1] if len(body) > 1 else 0,
                     preview=preview(body[8:]), plen=max(0, len(body) - 8))
    elif not v6 and proto == 2:
        return False  # IGMP housekeeping
    else:
        event.update(proto=f"ip{proto}", plen=len(body))
    return True


def _parse_ipv4(ip: bytes):
    if len(ip) < 20 or ip[0] >> 4 != 4:
        return None
    ihl = (ip[0] & 0x0F) * 4
    total = struct.unpack("!H", ip[2:4])[0]
    frag = struct.unpack("!H", ip[6:8])[0] & 0x1FFF
    event = {"family": 4, "src": socket.inet_ntoa(ip[12:16]), "dst": socket.inet_ntoa(ip[16:20])}
    body = ip[ihl:total] if total >= ihl else ip[ihl:]
    if frag:
        event.update(proto="ipfrag", plen=len(body))
        return event
    return event if _transport(ip[9], body, event, False) else None


def _parse_ipv6(ip: bytes):
    if len(ip) < 40 or ip[0] >> 4 != 6:
        return None
    event = {"family": 6, "src": socket.inet_ntop(socket.AF_INET6, ip[8:24]),
             "dst": socket.inet_ntop(socket.AF_INET6, ip[24:40])}
    nxt, pos = ip[6], 40
    while nxt in (0, 43, 60) and pos + 8 <= len(ip):  # hop-by-hop, routing, destination
        nxt, pos = ip[pos], pos + (ip[pos + 1] + 1) * 8
    if nxt == 44:  # fragment header: no ports to read
        event.update(proto="ipfrag", plen=max(0, len(ip) - pos))
        return event
    return event if _transport(nxt, ip[pos:], event, True) else None


def parse_frame(frame: bytes):
    """One link-layer frame -> an event dict, or None for non-IP traffic and noise."""
    if len(frame) < ETH_HDR:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    if ethertype == ETH_P_IP:
        return _parse_ipv4(frame[ETH_HDR:])
    if ethertype == ETH_P_IPV6:
        return _parse_ipv6(frame[ETH_HDR:])
    return None


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_ipv4_udp(src: str, dst: str, sport: int, dport: int, payload: bytes) -> bytes:
    """A complete IPv4+UDP packet (UDP checksum 0, legal over IPv4)."""
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0x4C42, 0, 64, 17, 0,
                         socket.inet_aton(src), socket.inet_aton(dst))
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + udp


def build_ethernet(dst_mac: str, src_mac: str, ethertype: int, payload: bytes) -> bytes:
    def mac(text):
        return bytes(int(part, 16) for part in text.split(":"))
    return mac(dst_mac) + mac(src_mac) + struct.pack("!H", ethertype) + payload
