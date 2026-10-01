"""The lab's upstream DNS: answers a tiny `.lab` zone and records EVERY query.

Unparseable datagrams are recorded too (qname null): the oracle for probe F is
"the upstream saw nothing", so a malformed packet that reached it counts.
Stdlib only; runs in the bottle image. Pure wire helpers are unit-tested.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

QTYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28,
          "SRV": 33, "NAPTR": 35, "DS": 43, "SVCB": 64, "HTTPS": 65, "AXFR": 252, "ANY": 255,
          "CAA": 257}
RCODE_FORMERR, RCODE_NXDOMAIN = 1, 3
MAX_NAME = 255


def qtype_number(text: str) -> int:
    """'A' -> 1; 'TYPE65280' or '65280' -> 65280."""
    upper = text.upper()
    if upper in QTYPES:
        return QTYPES[upper]
    return int(upper[4:] if upper.startswith("TYPE") else upper)


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad label {label!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def build_query(qname: str, qtype: int, txid: int = 0x1234, edns: bool = False) -> bytes:
    """A standard query; edns adds an OPT record with a cookie option."""
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 1 if edns else 0)
    wire = header + encode_name(qname) + struct.pack("!HH", qtype, 1)
    if edns:
        cookie = struct.pack("!HH", 10, 8) + b"\x01" * 8
        wire += b"\0" + struct.pack("!HHIH", 41, 4096, 0, len(cookie)) + cookie
    return wire


def malformed_query(kind: str, txid: int = 0x4242) -> bytes:
    """Deliberately broken queries; each kind breaks a different parser rule."""
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    if kind == "truncated":
        return header[:7]
    if kind == "label-overrun":  # label claims 63 bytes, 3 follow
        return header + b"\x3fabc"
    if kind == "overlong-name":  # 5 labels of 63 = 320 octets, over the 255 limit
        return header + (b"\x3f" + b"a" * 63) * 5 + b"\0" + struct.pack("!HH", 1, 1)
    if kind == "pointer-loop":  # compression pointer to itself
        return header + b"\xc0\x0c" + struct.pack("!HH", 1, 1)
    if kind == "qdcount-lies":  # header says 5 questions, body has none
        return struct.pack("!HHHHHH", txid, 0x0100, 5, 0, 0, 0)
    raise ValueError(f"unknown malformed kind {kind!r}")


MALFORMED_KINDS = ("truncated", "label-overrun", "overlong-name", "pointer-loop", "qdcount-lies")


def parse_query(data: bytes):
    """-> (txid, flags, qname, qtype, edns). Raises ValueError on anything off."""
    return _parse(data)[:5]


def _parse(data: bytes):
    """parse_query plus the offset where the question ends (the echo slice)."""
    if len(data) < 12:
        raise ValueError("short header")
    txid, flags, qdcount, _an, _ns, arcount = struct.unpack("!HHHHHH", data[:12])
    if qdcount != 1:
        raise ValueError(f"qdcount={qdcount}")
    pos, labels = 12, []
    while True:
        if pos >= len(data):
            raise ValueError("name runs off the packet")
        length = data[pos]
        if length & 0xC0:
            raise ValueError("compression in question")
        if length == 0:
            pos += 1
            break
        if pos + 1 + length > len(data):
            raise ValueError("label overruns packet")
        labels.append(data[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length
    qname = ".".join(labels)
    if len(qname) + 1 > MAX_NAME:
        raise ValueError("name too long")
    if pos + 4 > len(data):
        raise ValueError("no qtype/qclass")
    qtype = struct.unpack("!H", data[pos:pos + 2])[0]
    return txid, flags, qname, qtype, arcount > 0, pos + 4


class Zone:
    """The lab zone: allowed.lab and blocked.lab share the sink's address (the
    point of probe C), plus one name per non-global class for probe C-inv."""

    def __init__(self, sink4: str, sink6: str, classes: dict):
        self.sink4, self.sink6, self.classes = sink4, sink6, classes

    def records(self, name: str):
        """-> list of (rtype, rdata) for `name`, following CNAMEs; [] = no data; None = NXDOMAIN."""
        name = name.lower().rstrip(".")
        if name == "cname.blocked.lab":
            return [(5, encode_name("allowed.lab"))] + self.records("allowed.lab")
        if name.endswith(".allowed.lab"):
            label = name[: -len(".allowed.lab")].split(".")[-1]
            if label in self.classes:
                family, addr = self.classes[label]
                if family == 6:
                    return [(28, socket.inet_pton(socket.AF_INET6, addr))]
                return [(1, socket.inet_aton(addr))]
        if name in ("allowed.lab", "blocked.lab") or name.endswith((".allowed.lab", ".blocked.lab")):
            return [(1, socket.inet_aton(self.sink4)), (28, socket.inet_pton(socket.AF_INET6, self.sink6))]
        return None


def build_response(query: bytes, zone: Zone) -> bytes:
    """Response for a (possibly broken) query; FORMERR when it cannot be parsed."""
    txid = struct.unpack("!H", query[:2])[0] if len(query) >= 2 else 0
    try:
        _txid, _flags, qname, qtype, _edns, q_end = _parse(query)
    except ValueError:
        return struct.pack("!HHHHHH", txid, 0x8000 | RCODE_FORMERR, 0, 0, 0, 0)
    records = zone.records(qname)
    question = query[12:q_end]   # echoed as sent: a root or non-ASCII name must not need re-encoding
    if records is None:
        return struct.pack("!HHHHHH", txid, 0x8400 | RCODE_NXDOMAIN, 1, 0, 0, 0) + question
    answers = b"".join(
        b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 60, len(rdata)) + rdata
        for rtype, rdata in records if rtype == qtype or rtype == 5 or qtype == 255)
    count = sum(1 for rtype, _ in records if rtype == qtype or rtype == 5 or qtype == 255)
    return struct.pack("!HHHHHH", txid, 0x8400, 1, count, 0, 0) + question + answers


def dns_event(src: str, transport: str, data: bytes) -> dict:
    """The record written for one arrival, parsed or not."""
    event = {"kind": "dns", "t": time.time(), "src": src, "transport": transport,
             "raw_len": len(data), "qname": None, "qtype": None, "parsed": False}
    try:
        _txid, _flags, qname, qtype, edns = parse_query(data)
        event.update(qname=qname, qtype=qtype, edns=edns, parsed=True)
    except ValueError:
        pass
    return event


class Server:
    def __init__(self, zone: Zone, events_path: str, port: int = 53, bind4: str = "0.0.0.0",
                 bind6: str = "::"):
        self.zone, self.events_path, self.port = zone, events_path, port
        self.bind4, self.bind6 = bind4, bind6
        self._lock = threading.Lock()

    def record(self, event: dict) -> None:
        with self._lock, open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def failed(self, transport: str, peer, exc: Exception) -> None:
        """A query the server could not answer: logged and recorded, never fatal."""
        print(f"lab-dns: {transport} from {peer[0]} not answered: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        self.record({"kind": "error", "t": time.time(), "src": peer[0], "transport": transport,
                     "error": type(exc).__name__})

    def _udp(self, family: int, bind: str) -> None:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((bind, self.port))
        while True:
            data, peer = sock.recvfrom(4096)
            self.record(dns_event(peer[0], "udp", data))
            try:
                sock.sendto(build_response(data, self.zone), peer)
            except Exception as exc:  # noqa: BLE001 — one bad query must never end the recorder
                self.failed("udp", peer, exc)

    def _tcp_conn(self, conn: socket.socket, peer) -> None:
        try:
            conn.settimeout(3)
            head = conn.recv(2)
            if len(head) < 2:
                self.record({"kind": "dns", "t": time.time(), "src": peer[0], "transport": "tcp",
                             "raw_len": len(head), "qname": None, "qtype": None, "parsed": False})
                return
            want = struct.unpack("!H", head)[0]
            data = b""
            while len(data) < want:
                chunk = conn.recv(want - len(data))
                if not chunk:
                    break
                data += chunk
            self.record(dns_event(peer[0], "tcp", data))
            reply = build_response(data, self.zone)
            conn.sendall(struct.pack("!H", len(reply)) + reply)
        except OSError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.failed("tcp", peer, exc)
        finally:
            conn.close()

    def _tcp(self, family: int, bind: str) -> None:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((bind, self.port))
        sock.listen(16)
        while True:
            conn, peer = sock.accept()
            threading.Thread(target=self._tcp_conn, args=(conn, peer), daemon=True).start()

    def serve(self) -> None:
        for target, family, bind in ((self._udp, socket.AF_INET, self.bind4),
                                     (self._udp, socket.AF_INET6, self.bind6),
                                     (self._tcp, socket.AF_INET, self.bind4),
                                     (self._tcp, socket.AF_INET6, self.bind6)):
            threading.Thread(target=target, args=(family, bind), daemon=True).start()
        time.sleep(0.5)
        self.record({"kind": "ready", "t": time.time(), "role": "dns"})
        print("lab-dns: ready", file=sys.stderr, flush=True)
        threading.Event().wait()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--sink4", required=True)
    ap.add_argument("--sink6", required=True)
    ap.add_argument("--classes", required=True, help="JSON {label: [family, address]}")
    ap.add_argument("--port", type=int, default=53)
    args = ap.parse_args(argv)
    zone = Zone(args.sink4, args.sink6, json.loads(args.classes))
    Server(zone, args.events, port=args.port).serve()


if __name__ == "__main__":
    main()
