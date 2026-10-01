"""Recorder: logs every IP packet that ARRIVES at this container, and listens on
the probed ports so connections complete and payloads (SNI, Host) arrive.

Run as the lab's uplink sink, as the listener inside bottle 1, and as the
stand-in for the gateway/host addresses. Capture is AF_PACKET (needs root and
NET_RAW): that is what makes the recorder see closed ports, ICMP and frames a
listener never would. One JSON object per line in --events; the first line
written once everything is up is {"kind": "ready"}.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

import lab_packets


class Log:
    def __init__(self, path: str):
        self.path, self._lock = path, threading.Lock()

    def write(self, event: dict) -> None:
        event.setdefault("t", time.time())
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")


def open_capture() -> socket.socket:
    """Opened on the main thread so a missing NET_RAW stops startup instead of
    leaving a recorder that is up but blind."""
    return socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))


def capture(log: Log, sock: socket.socket) -> None:
    while True:
        frame, meta = sock.recvfrom(65535)
        if meta[2] == lab_packets.PACKET_OUTGOING:
            continue
        event = lab_packets.parse_frame(frame)
        if event is None:
            continue
        event.update(kind="packet", iface=meta[0])
        log.write(event)


def listen_tcp(log: Log, port: int) -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    sock.listen(32)

    def drain(conn, peer):
        try:
            conn.settimeout(1)
            conn.recv(4096)
        except OSError:
            pass
        finally:
            conn.close()

    while True:
        conn, peer = sock.accept()
        log.write({"kind": "accept", "proto": "tcp", "src": peer[0].replace("::ffff:", ""),
                   "dport": port})
        threading.Thread(target=drain, args=(conn, peer), daemon=True).start()


def listen_udp(log: Log, port: int) -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    while True:
        data, peer = sock.recvfrom(4096)
        log.write({"kind": "accept", "proto": "udp", "src": peer[0].replace("::ffff:", ""),
                   "dport": port, "plen": len(data)})


def join_multicast(group: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")))
    threading.Event().wait()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--role", default="sink")
    ap.add_argument("--tcp", default="", help="comma-separated TCP ports to listen on")
    ap.add_argument("--udp", default="", help="comma-separated UDP ports to listen on")
    ap.add_argument("--no-capture", action="store_true", help="listeners only (unit tests)")
    ap.add_argument("--join", default="", help="multicast group to join")
    args = ap.parse_args(argv)
    log = Log(args.events)
    ports = lambda text: [int(p) for p in text.split(",") if p]  # noqa: E731
    threads = [threading.Thread(target=listen_tcp, args=(log, p), daemon=True) for p in ports(args.tcp)]
    threads += [threading.Thread(target=listen_udp, args=(log, p), daemon=True) for p in ports(args.udp)]
    if not args.no_capture:
        threads.append(threading.Thread(target=capture, args=(log, open_capture()), daemon=True))
    if args.join:
        threads.append(threading.Thread(target=join_multicast, args=(args.join,), daemon=True))
    for thread in threads:
        thread.start()
    time.sleep(0.5)
    log.write({"kind": "ready", "role": args.role, "tcp": ports(args.tcp), "udp": ports(args.udp),
               "capture": not args.no_capture})
    print(f"lab-sink[{args.role}]: ready", file=sys.stderr, flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
