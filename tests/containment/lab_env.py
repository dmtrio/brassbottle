"""The lab: docker networks and containers, driven through the docker CLI.

Variant "no-gateway" is today's bottle: no gateway exists, so bottles share one
flat network with the uplink and keep the capabilities the real bottle has.
Children 09+ add a "gateway" variant (gateway containers, per-bottle networks,
a bottle without NET_ADMIN/NET_RAW); the probes are written against the Lab
methods below so flipping one means changing the variant, not the probe.

Everything runs from the bottle image CI built (no other image is pulled):
the sink, lab DNS and control client are that image running a lab script.
"""

import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import lab_kit

LAB_DIR = Path(__file__).resolve().parent
TCP_PORTS = (22, 53, 80, 443, 8080, 8816, 9100, 9901, 9902)
UDP_PORTS = (53, 443, 60001)
HOST_PLUGIN_PORT = 9100   # a plugin port bottle 1 would be granted; bottle 2 is not
EVENTS = "/tmp/events.jsonl"
READY_TIMEOUT = 40
CANARY_TIMEOUT = 10
REPO = LAB_DIR.parents[1]
BOTTLE_COMPOSE = REPO / "compose" / "docker-compose.local.yml"

# name -> (family, address, observed_by). The classes probe C-inv makes a name
# resolve to; each address is an alias on the sink, routed there from the
# dialer, except loopback (observed in the dialer's own namespace).
CLASS_TARGETS = {
    "this-network": (4, "0.77.0.5", "sink"),
    "loopback": (4, "127.77.0.5", "dialer"),
    "rfc1918": (4, "10.77.0.5", "sink"),
    "link-local": (4, "169.254.77.5", "sink"),
    "cgnat": (4, "100.64.77.5", "sink"),
    "multicast": (4, "239.255.77.5", "sink"),
    "reserved": (4, "240.77.0.5", "sink"),
    "ipv4-mapped": (6, "::ffff:10.77.0.5", "sink"),
    "djinn-net": (4, "172.30.77.5", "sink"),
    "egress-control": (4, "172.29.255.5", "sink"),
    "bottle-pool": (4, "172.31.77.5", "sink"),
}
# Forbidden-set members dialled as literals (probe C-inv, second half). Gateway
# own addresses join this set when the gateway variant exists (child 09).
FORBIDDEN_LITERALS = {
    "loopback": "127.77.0.5",
    "link-local": "169.254.77.5",
    "198.18/15 low": "198.18.77.5",
    "198.18/15 high": "198.19.77.5",
    "bottle-pool": "172.31.77.5",
    "djinn-net": "172.30.77.5",
    "egress-control": "172.29.255.5",
}
UNROUTABLE = {"0.77.0.5"}   # the kernel refuses a route or alias here


@dataclass(frozen=True)
class Addrs:
    subnet4: str
    subnet6: str
    gw4: str
    decoy4: str
    sink4: str
    dns4: str
    ctl4: str
    b1_4: str
    b2_4: str
    decoy6: str
    sink6: str
    dns6: str
    ctl6: str
    b1_6: str
    b2_6: str


def plan_addresses(a: int, b: int) -> Addrs:
    """Lab addressing. The uplink is 11.a.b.0/24 on purpose: the gateway dials
    only global-unicast addresses, and 11/8 is globally routable per
    `ipaddress`, whereas RFC 1918 or TEST-NET space would be refused by the
    very rule the probes test."""
    p4 = f"11.{a}.{b}"
    p6 = f"fd77:6a:{a:x}:{b:x}"
    return Addrs(f"{p4}.0/24", f"{p6}::/64", f"{p4}.1", f"{p4}.2", f"{p4}.10", f"{p4}.11",
                 f"{p4}.12", f"{p4}.21", f"{p4}.22", f"{p6}::2", f"{p6}::10", f"{p6}::11", f"{p6}::12",
                 f"{p6}::21", f"{p6}::22")


def bottle_hardening(path: Path = BOTTLE_COMPOSE) -> dict:
    """The bottle service's `cap_add`, `cap_drop` and `security_opt`, read from the
    compose file up.sh renders, so the probes judge what ships (a hardcoded list
    would let child 16's cap_drop flip A-root/B early and hide a regression that
    re-adds caps). Parsed by `yq` (the repo's YAML tool, installed in CI), so flow
    lists, compact indents and anchors read the same as block lists. Fails
    closed: a missing yq, an unparseable file, a missing `djinn` service, or a
    key that is declared but is not a non-empty list of strings raises, never
    returns an empty list that would hand the lab a bottle with no caps."""
    try:
        res = subprocess.run(["yq", "-o=json", ".services.djinn | explode(.)", str(path)],
                             text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot run yq to read {path}: {exc}") from exc
    if res.returncode != 0:
        raise RuntimeError(f"yq could not parse {path} (rc={res.returncode}): {res.stderr.strip()[:300]}")
    try:
        service = json.loads(res.stdout)
    except ValueError as exc:
        raise RuntimeError(f"yq output for {path} is not JSON: {exc}") from exc
    if not isinstance(service, dict):
        raise RuntimeError(f"no djinn service found in {path}")
    out = {}
    for key in ("cap_add", "cap_drop", "security_opt"):
        if key not in service:
            out[key] = []
            continue
        value = service[key]
        if not (isinstance(value, list) and value and all(isinstance(v, str) and v for v in value)):
            raise RuntimeError(f"djinn.{key} in {path} is declared but is not a non-empty list of strings: {value!r}")
        out[key] = list(value)
    return out


def docker(*args: str, check: bool = True, timeout: int = 300, **kw) -> subprocess.CompletedProcess:
    started = time.monotonic()
    try:
        res = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as exc:
        lab_kit.log("docker", f"{args[0]} TIMEOUT after {timeout}s")
        res = subprocess.CompletedProcess(exc.cmd, 124, "", f"timeout after {timeout}s")
    lab_kit.log("docker", f"{args[0]} rc={res.returncode} secs={time.monotonic() - started:.1f} "
                          f"out={len(res.stdout)}B err={len(res.stderr)}B")
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed rc={res.returncode}\n"
                           f"stdout:\n{res.stdout[-2000:]}\nstderr:\n{res.stderr[-2000:]}")
    return res


def docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def network_create_args(name: str, addrs: Addrs) -> list:
    return ["network", "create", "--ipv6", "--subnet", addrs.subnet4, "--gateway", addrs.gw4,
            "--subnet", addrs.subnet6, name]


def run_args(name: str, image: str, network: str, ip4: str, ip6: str, entrypoint: list,
             caps=(), dns=None, add_host=None, labels=None, cap_drop=(), security_opt=()) -> list:
    """argv after `docker` for one lab container. Always root, /lab mounted read-only."""
    argv = ["run", "-d", "--name", name, "--network", network, "--ip", ip4, "--ip6", ip6,
            "--user", "root", "-v", f"{LAB_DIR}:/lab:ro"]
    for cap in caps:
        argv += ["--cap-add", cap]
    for cap in cap_drop:
        argv += ["--cap-drop", cap]
    for opt in security_opt:
        argv += ["--security-opt", opt]
    if dns:
        argv += ["--dns", dns]
    if add_host:
        argv += ["--add-host", add_host]
    for key, value in (labels or {}).items():
        argv += ["--label", f"{key}={value}"]
    return argv + ["--entrypoint", entrypoint[0], image, *entrypoint[1:]]


def sink_args(role: str, tcp=TCP_PORTS, udp=UDP_PORTS, join: str = "") -> list:
    args = ["/lab/lab_sink.py", "--events", EVENTS, "--role", role,
            "--tcp", ",".join(map(str, tcp)), "--udp", ",".join(map(str, udp))]
    return args + (["--join", join] if join else [])


def alias_commands(addrs: Addrs) -> list:
    """`ip` commands run on the sink so every class/forbidden address arrives there."""
    wanted = {addr for fam, addr, obs in CLASS_TARGETS.values()
              if fam == 4 and obs == "sink" and not addr.startswith("239.")}
    wanted |= {a for a in FORBIDDEN_LITERALS.values() if not a.startswith("127.")}
    return [["ip", "addr", "add", f"{a}/32", "dev", "eth0"] for a in sorted(wanted - UNROUTABLE)]


def route_commands(addrs: Addrs) -> list:
    """`ip` commands run in the dialer's namespace: send alias addresses to the sink."""
    wanted = {addr for fam, addr, obs in CLASS_TARGETS.values()
              if fam == 4 and obs == "sink" and not addr.startswith("239.")}
    wanted |= {a for a in FORBIDDEN_LITERALS.values() if not a.startswith("127.")}
    cmds = [["ip", "route", "add", f"{a}/32", "via", addrs.sink4] for a in sorted(wanted - UNROUTABLE)]
    cmds.append(["ip", "route", "add", "239.255.77.5/32", "dev", "eth0"])
    return cmds


def class_zone_entries() -> dict:
    return {label: [fam, addr] for label, (fam, addr, _obs) in CLASS_TARGETS.items()}


class Lab:
    """One throwaway lab. start() builds it; stop() removes every container and network."""

    VARIANTS = ("no-gateway",)

    def __init__(self, image: str, variant: str = "no-gateway"):
        if variant not in self.VARIANTS:
            raise NotImplementedError(f"lab variant {variant!r} not built yet")
        self.image, self.variant = image, variant
        self.suffix = f"{os.getpid()}-{random.randrange(0xFFFF):04x}"
        self.addrs = plan_addresses(random.randrange(200, 250), random.randrange(0, 255))
        self.net = f"djinn-ct-{self.suffix}"
        self.names = {role: f"djinn-ct-{role}-{self.suffix}"
                      for role in ("decoy", "sink", "dns", "ctl", "b1", "b2")}
        self.recorders = {}
        self.hardening = bottle_hardening()
        self.blind = os.environ.get("CONTAINMENT_BLIND_RECORDER") == "1"

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, broker_up: bool = True) -> None:
        """Build the lab. `broker_up` is the probe-I switch for the gateway
        variant's bootstrap phase; with no gateway there is nothing to gate."""
        t0 = time.monotonic()
        lab_kit.log("lab", f"start variant={self.variant} net={self.net} subnet={self.addrs.subnet4} "
                           f"image={self.image} broker_up={broker_up}")
        docker(*network_create_args(self.net, self.addrs))
        a, n = self.addrs, self.names
        label = {"djinn.containment-lab": self.suffix}
        py = "python3"
        self._run("decoy", a.decoy4, a.decoy6, [py, "-u", *sink_args("decoy")],
                  ("NET_RAW",), labels=label)
        self._run("sink", a.sink4, a.sink6, [py, "-u", *sink_args("sink", join="239.255.77.5")],
                  ("NET_RAW", "NET_ADMIN"), labels=label)
        self._run("dns", a.dns4, a.dns6, [py, "-u", "/lab/lab_dns.py", "--events", EVENTS,
                                          "--sink4", a.sink4, "--sink6", a.sink6,
                                          "--classes", json.dumps(class_zone_entries())],
                  (), labels=label)
        self._run("ctl", a.ctl4, a.ctl6, ["sleep", "infinity"], ("NET_RAW", "NET_ADMIN"), labels=label)
        host_alias = f"host.docker.internal:{a.decoy4}"
        hard = self.hardening
        lab_kit.log("lab", f"bottle hardening from {BOTTLE_COMPOSE.name}: {hard}")
        for role, ip4, ip6 in (("b1", a.b1_4, a.b1_6), ("b2", a.b2_4, a.b2_6)):
            self._run(role, ip4, ip6, ["sleep", "infinity"], tuple(hard["cap_add"]),
                      dns=a.dns4, add_host=host_alias, labels=label,
                      cap_drop=tuple(hard["cap_drop"]), security_opt=tuple(hard["security_opt"]))
        # Best effort: a kernel that refuses an alias shows up as a failed
        # baseline (test_02), by name, not as a silent gap in a probe.
        for cmd in alias_commands(a):
            self._best_effort("sink", cmd)
        for role in ("ctl", "b1", "b2"):
            for cmd in route_commands(a):
                self._best_effort(role, cmd)
        # Listener inside bottle 1 on every probed port (probe H's target).
        docker("exec", "-d", "-u", "root", n["b1"], "python3", "-u", *sink_args("bottle1"))
        for role in ("decoy", "sink", "dns", "b1"):
            self._wait_ready(role)
        self.recorders = {
            role: lab_kit.Recorder(role, lambda r=role: self._events_text(r), blind=self.blind)
            for role in ("decoy", "sink", "dns", "b1")}
        lab_kit.log("lab", f"ready secs={time.monotonic() - t0:.1f} blind_recorder={self.blind}")

    def stop(self) -> None:
        for name in self.names.values():
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
        subprocess.run(["docker", "network", "rm", self.net], capture_output=True, timeout=120)
        lab_kit.log("lab", f"stopped net={self.net}")

    def _run(self, role, ip4, ip6, entrypoint, caps, dns=None, add_host=None, labels=None,
             cap_drop=(), security_opt=()) -> None:
        docker(*run_args(self.names[role], self.image, self.net, ip4, ip6, entrypoint, caps, dns,
                         add_host, labels, cap_drop, security_opt))

    def _best_effort(self, role: str, cmd) -> None:
        res = self.exec(role, cmd, check=False)
        if res.returncode != 0:
            lab_kit.log("lab", f"WARN {role}: `{' '.join(cmd)}` rc={res.returncode} {res.stderr.strip()[:120]}")

    def _events_text(self, role: str) -> str:
        """The recorder's event file. A dead container or a missing file is a
        harness error: returning "" would read as silence."""
        res = docker("exec", self.names[role], "cat", EVENTS, check=False)
        if res.returncode != 0:
            raise lab_kit.HarnessError(f"recorder {role} unreadable: rc={res.returncode} "
                                       f"{res.stderr.strip()[-200:]}")
        return res.stdout

    def _wait_ready(self, role: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                text = self._events_text(role)
            except lab_kit.HarnessError:   # not started yet; the deadline bounds the wait
                time.sleep(1)
                continue
            events, _ = lab_kit.parse_events(text, role)
            if any(e.get("kind") == "ready" for e in events):
                return
            time.sleep(1)
        logs = docker("logs", self.names[role], check=False)
        raise RuntimeError(f"recorder {role} never became ready\n{logs.stdout[-1500:]}{logs.stderr[-1500:]}")

    # ── driving the bottles ───────────────────────────────────────────────

    def exec(self, role: str, argv, check: bool = True, timeout: int = 60):
        return docker("exec", "-u", "root", self.names[role], *argv, check=check, timeout=timeout)

    def client(self, role: str, *args: str, timeout: int = 60) -> dict:
        """Run lab_client.py in a container; returns its JSON result. The client
        catches network errors itself and exits 0 with ok=False, so a non-zero
        exit or no JSON line means the harness broke (traceback, bad argument,
        container gone): that raises, it is never a refusal."""
        res = docker("exec", "-u", "root", self.names[role], "python3", "/lab/lab_client.py", *args,
                     check=False, timeout=timeout)
        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
        if res.returncode != 0 or not lines:
            raise lab_kit.HarnessError(f"client `{' '.join(args[:1])}` in {role} failed rc={res.returncode} "
                                       f"json_lines={len(lines)} {res.stderr.strip()[-300:]}")
        return json.loads(lines[-1])

    def canary_sender(self, recorder: str, tag: str):
        """A control-source send that the named recorder must record, and where."""
        a = self.addrs
        if recorder == "dns":
            return ("ctl", ("dns", "--server", a.dns4, "--qname", f"{tag}.allowed.lab"))
        target = {"sink": a.sink4, "decoy": a.decoy4, "b1": a.b1_4}[recorder]
        return ("ctl", ("tcp", "--host", target, "--port", "443", "--payload", tag))

    def prove_alive(self, names, tag: str) -> None:
        """Send `tag` through each named recorder and require it to be recorded.
        Called right before every silence check, so a recorder that died, went
        blind or lost its path since the baseline cannot read as silence."""
        for name in names:
            role, args = self.canary_sender(name, tag)
            self.client(role, *args)
        deadline, dead = time.monotonic() + CANARY_TIMEOUT, list(names)
        while dead and time.monotonic() < deadline:
            time.sleep(0.5)
            dead = [n for n in dead
                    if not lab_kit.traffic(self.recorders[n].snapshot(), match=lab_kit.tagged(tag))]
        if dead:
            raise lab_kit.HarnessError(f"recorder(s) {dead} did not record the canary {tag}: "
                                       f"blind or dead, so silence would be meaningless")
        lab_kit.log("lab", f"canary {tag} seen by {list(names)}")

    def mac(self, role: str) -> str:
        out = docker("inspect", "-f", f"{{{{(index .NetworkSettings.Networks \"{self.net}\").MacAddress}}}}",
                     self.names[role]).stdout.strip()
        if not out:
            raise RuntimeError(f"no MAC for {role}")
        return out

    # ── what the probes ask of the lab ────────────────────────────────────

    def marks(self) -> dict:
        return {name: rec.mark() for name, rec in self.recorders.items()}

    def settle(self) -> None:
        time.sleep(lab_kit.SETTLE_SECONDS)

    @property
    def ignore_sources(self):
        """Control-only sources: baseline traffic from ctl is never probe traffic."""
        return (self.addrs.ctl4, self.addrs.ctl6)

    def bottle_resolver(self) -> str:
        """The address a bottle's DNS queries go to. No gateway: the lab upstream
        itself (so F records what a stub would). Gateway variant: the gateway's `.1`."""
        return self.addrs.dns4

    def gateway_containers(self) -> list:
        """Gateway container names; empty until child 09 builds the variant."""
        return []

    def gateway_exec(self, name: str, argv):
        """Run a command in a gateway container; raises LookupError when there is none."""
        raise LookupError(f"no gateway exists in the {self.variant} variant ({name})")


def lab_requested() -> bool:
    return bool(os.environ.get("DJINN_CONTAINMENT_IMAGE"))


def require_lab(image: str, probe=docker_available) -> None:
    """Gate for the lab class: raise (never skip) when the lab was requested but can't run."""
    if not image:
        raise AssertionError("DJINN_CONTAINMENT_IMAGE is not set")
    if not probe():
        raise AssertionError("docker is unavailable but the containment lab was requested via "
                             "DJINN_CONTAINMENT_IMAGE; refusing to skip")


def main(argv=None):  # pragma: no cover - manual debugging aid
    lab = Lab(os.environ["DJINN_CONTAINMENT_IMAGE"])
    try:
        lab.start()
        print(json.dumps({"names": lab.names, "addrs": lab.addrs.__dict__}, indent=2))
    finally:
        lab.stop()


if __name__ == "__main__":
    sys.exit(main())
