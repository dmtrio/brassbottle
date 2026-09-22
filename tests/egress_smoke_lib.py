#!/usr/bin/env python3
"""Operator-run Phase A+B egress smoke helpers (host-side).

End-to-end checks for the egress broker, NFLOG reader, transparent broker
live paths, and invariants. Invoked by tests/egress.smoke.sh; unit-tested in
tests/test_egress_smoke_lib.py.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.error import URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import egress_broker_host as broker  # noqa: E402
import egress_store as store  # noqa: E402

Status = Literal["pass", "fail", "skip"]
DEFAULT_HTTPS_HOST = "docs.stripe.com"
DEFAULT_HTTP_HOST = "neverssl.com"
DEFAULT_FAST_GITHUB = "api.github.com"
DEFAULT_FAST_NPM = "registry.npmjs.org"
DEFAULT_PG_IP = "192.0.2.55"
DEFAULT_PG_PORT = 5432
DEFAULT_COALESCE_HOST = "www.example.com"
DEFAULT_SPOOF_HOST = "example.org"
BROKER_PORT = broker.DEFAULT_PORT
POLL_INTERVAL_SECONDS = 0.5
REQUEST_WAIT_SECONDS = 20.0


@dataclass
class CheckResult:
    """One named smoke-check outcome."""

    name: str
    status: Status
    detail: str = ""


@dataclass
class SmokeSummary:
    """Aggregate pass / fail / skip counts."""

    results: list[CheckResult] = field(default_factory=list)

    def record(self, name: str, status: Status, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))

    def pass_(self, name: str, detail: str = "") -> None:
        self.record(name, "pass", detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.record(name, "fail", detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.record(name, "skip", detail)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.results if item.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "fail")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.status == "skip")

    def exit_code(self) -> int:
        return 1 if self.failed else 0


def is_inside_container() -> bool:
    """True when this process appears to run inside a container."""
    if Path("/.dockerenv").is_file():
        return True
    cgroup = Path("/proc/1/cgroup")
    if cgroup.is_file():
        text = cgroup.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(docker|containerd|kubepods)", text):
            return True
    return False


def is_mac_host() -> bool:
    return sys.platform == "darwin"


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def resolve_base_path() -> Path:
    return broker.resolve_base_path(os.environ.get("DJINN_HOME", ""))


def egress_log_root(base_path: Path) -> Path:
    """The egress root (the name predates the store; it is the same root
    the legacy log lived under, and the store's database lives there)."""
    return broker.resolve_egress_root(base_path)


def open_store(base_path: Path) -> store.EgressStore:
    return store.EgressStore(egress_log_root(base_path))


def fold_open_requests(base_path: Path) -> dict[str, store.RequestRow]:
    """Open rows keyed by request_id (the store is the queue)."""
    db = open_store(base_path)
    try:
        rows = db.list_open()
    finally:
        db.shutdown()
    return {row.request_id: row for row in rows}


def count_events(
    base_path: Path,
    *,
    kind: str,
    host: str | None = None,
    port: int | None = None,
    request_id: str | None = None,
    when: datetime | None = None,
) -> int:
    """Count events of one kind, optionally narrowed by host/port (from the
    event's JSON fields) or by request id.

    `when` is accepted for call-site compatibility and ignored: the events
    table has no months.
    """
    del when
    db = open_store(base_path)
    try:
        if request_id is not None:
            events = db.events_for(request_id)
        else:
            events = db.events(kind=kind)
    finally:
        db.shutdown()
    total = 0
    for event in events:
        if event.kind != kind:
            continue
        fields = event.fields or {}
        if host is not None and fields.get("host") != host:
            continue
        if port is not None and fields.get("port") != port:
            continue
        total += 1
    return total


def count_hits_for_request(base_path: Path, request_id: str, when: datetime | None = None) -> int:
    """Hits recorded for one request — the row's own hit_count."""
    del when
    db = open_store(base_path)
    try:
        row = db.get(request_id)
    finally:
        db.shutdown()
    return row.hit_count if row is not None else 0


def find_open_request(
    open_requests: dict[str, store.RequestRow],
    *,
    host: str | None = None,
    port: int | None = None,
) -> store.RequestRow | None:
    for row in open_requests.values():
        if port is not None and row.port != port:
            continue
        if host is not None and row.host != host:
            continue
        return row
    return None


def wait_for_open_request(
    base_path: Path,
    *,
    host: str | None = None,
    port: int | None = None,
    timeout: float = REQUEST_WAIT_SECONDS,
    poll: float = POLL_INTERVAL_SECONDS,
) -> store.RequestRow | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        req = find_open_request(fold_open_requests(base_path), host=host, port=port)
        if req is not None:
            return req
        time.sleep(poll)
    return None


def broker_host_env_set(env_value: str) -> bool:
    """The bottle knows the compose singleton's static djinn-net address —
    what init-firewall.sh scopes the 8816 ACCEPT to, and what the NFLOG
    reader and request-egress dial. 8816 left HOST_MCP_PORTS when the broker
    became the ./djinn egress service.
    """
    return bool((env_value or "").strip())


def container_name_for_bottle(bottle: str, prefix: str = "djinn-") -> str:
    short = bottle.removeprefix(prefix)
    return f"{prefix}{short}"


def list_running_bottle_containers(prefix: str = "djinn-") -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"name=^{prefix}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def docker_inspect_env(container: str, key: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Config.Env}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    prefix = f"{key}="
    for entry in entries:
        if isinstance(entry, str) and entry.startswith(prefix):
            return entry[len(prefix) :]
    return None


def docker_exec(container: str, command: str, *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container, "bash", "-lc", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def broker_health_ok(host: str = "127.0.0.1", port: int = BROKER_PORT) -> bool:
    url = f"http://{host}:{port}/health"
    try:
        with urlopen(url, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, ValueError):
        return False
    return body == {"status": "ok"}


def queue_mount_violations(base_path: Path, prefix: str = "djinn-") -> list[str]:
    run_root = str((base_path / "run").resolve())
    run_prefix = f"{run_root}/"
    violations: list[str] = []
    for container in list_running_bottle_containers(prefix):
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Mounts}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            violations.append(f"{container}: docker inspect failed")
            continue
        try:
            mounts = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            violations.append(f"{container}: mount JSON unreadable")
            continue
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source", "")
            if not isinstance(source, str) or not source:
                continue
            normalized = source.rstrip("/")
            if normalized == run_root or normalized.startswith(run_prefix.rstrip("/") + "/"):
                violations.append(f"{container}: mount source {source!r} exposes run/")
    return violations


def append_spoof_allowed_line(
    base_path: Path,
    *,
    host: str,
    container: str,
) -> None:
    """Forge an allowed row directly in the database; it must not grant
    egress (the store is the audit trail, never an allow predicate)."""
    import sqlite3

    db_path = egress_log_root(base_path) / store.DB_FILENAME
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO requests"
                " (request_id, container, host, port, host_is_ip, opened_at,"
                "  last_hit_at, hit_count, status, scope, decided_at, decided_by,"
                "  decision_body)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, 1, 'allowed', 'live', ?, 'cli', ?)",
                (
                    f"smoke-spoof-{uuid.uuid4().hex[:12]}",
                    container,
                    host,
                    443,
                    store._iso_ts(None),
                    store._iso_ts(None),
                    '{"decision":"allow","scope":"live"}',
                ),
            )
    finally:
        conn.close()



def load_agent_files(repo_root: Path) -> dict[str, dict]:
    agents: dict[str, dict] = {}
    agents_dir = repo_root / "agents"
    if not agents_dir.is_dir():
        return agents
    for path in sorted(agents_dir.glob("*/agent.yml")):
        result = subprocess.run(
            ["yq", "-o=json", "-I=0", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            agents[path.parent.name] = payload
    return agents


def derive_kill_switch_ports(repo_root: Path) -> tuple[bool, dict[str, str], str]:
    sys.path.insert(0, str(repo_root / "src"))
    try:
        import manifest as manifest_mod  # noqa: WPS433
    except ImportError as exc:
        return False, {}, str(exc)
    agent_files = load_agent_files(repo_root)
    if not agent_files:
        return False, {}, "no agent descriptors found under agents/"
    try:
        derived = manifest_mod.derive(
            {"capabilities": {"egress_broker": False}},
            {},
            agent_files,
            os.environ,
        )
    except manifest_mod.ManifestError as exc:
        return False, {}, str(exc)
    return True, dict(derived), ""


def container_has_nflog_rule(container: str) -> bool:
    result = docker_exec(container, "iptables -S OUTPUT 2>/dev/null | grep -F 'NFLOG' || true")
    return "NFLOG" in result.stdout


def approve_domain(
    repo_root: Path,
    bottle: str,
    host: str,
    *,
    save: str = "none",
) -> subprocess.CompletedProcess[str]:
    script = repo_root / "bin" / "allow-egress.sh"
    return subprocess.run(
        [str(script), bottle, host, "--save", save],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )


def grant_test_cidr(container: str, cidr: str) -> subprocess.CompletedProcess[str]:
    return docker_exec(container, f"ipset add allowed-domains {cidr}")


def curl_https(container: str, host: str, *, connect_timeout: int = 3) -> int:
    result = docker_exec(
        container,
        f"curl -sS -o /dev/null --connect-timeout {connect_timeout} "
        f"https://{host}/ >/dev/null 2>&1; echo $?",
    )
    text = result.stdout.strip().splitlines()
    if not text:
        return 1
    try:
        return int(text[-1])
    except ValueError:
        return 1


def nc_probe(container: str, host: str, port: int, *, timeout_seconds: int = 2) -> int:
    result = docker_exec(
        container,
        f"timeout {timeout_seconds} bash -lc 'cat < /dev/null > /dev/tcp/{host}/{port}' "
        f"2>/dev/null; echo $?",
    )
    text = result.stdout.strip().splitlines()
    if not text:
        return 1
    try:
        return int(text[-1])
    except ValueError:
        return 1


def resolve_ip(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def reverse_dns_name(ip: str) -> str | None:
    try:
        host, _, _ = socket.gethostbyaddr(ip)
    except OSError:
        return None
    return host or None


def normalized_rdns_host(ip: str) -> str | None:
    rdns = reverse_dns_name(ip)
    if not rdns:
        return None
    try:
        return broker.normalize_host(rdns)
    except ValueError:
        return None


def run_preflight(
    summary: SmokeSummary,
    *,
    bottle: str,
    base_path: Path,
    prefix: str,
    broker_host: str = "127.0.0.1",
) -> str | None:
    """Return the docker container name when preflight passes enough to continue."""
    if not docker_available():
        summary.skip("docker present", "docker not installed or daemon unreachable")
        return None
    summary.pass_("docker present", "")

    container = container_name_for_bottle(bottle, prefix)
    running = list_running_bottle_containers(prefix)
    if container not in running:
        summary.fail(
            "bottle running",
            f"{container} not in running bottles: {', '.join(running) or '(none)'}",
        )
        return None
    summary.pass_("bottle running", container)

    if broker_health_ok(broker_host, BROKER_PORT):
        summary.pass_("broker reachable on 8816", f"http://{broker_host}:{BROKER_PORT}/health")
    else:
        summary.fail(
            "broker reachable on 8816",
            "start the broker: ./djinn egress start, then retry",
        )
        return None

    token = docker_inspect_env(container, "EGRESS_BROKER_TOKEN")
    if token:
        summary.pass_("EGRESS_BROKER_TOKEN set", f"{len(token)} chars")
    else:
        summary.fail("EGRESS_BROKER_TOKEN set", f"missing on {container}")
        return None

    broker_host = docker_inspect_env(container, "EGRESS_BROKER_HOST") or ""
    if broker_host_env_set(broker_host):
        summary.pass_("EGRESS_BROKER_HOST set (djinn-net broker address)", broker_host)
    else:
        summary.fail(
            "EGRESS_BROKER_HOST set",
            f"got {broker_host!r} — re-run ./djinn up with the egress service derived",
        )
        return None

    return container


def run_smoke(
    bottle: str,
    *,
    base_path: Path | None = None,
    repo_root: Path | None = None,
    https_host: str = DEFAULT_HTTPS_HOST,
    pg_ip: str = DEFAULT_PG_IP,
    pg_port: int = DEFAULT_PG_PORT,
    coalesce_host: str = DEFAULT_COALESCE_HOST,
    spoof_host: str = DEFAULT_SPOOF_HOST,
    prefix: str = "djinn-",
    broker_host: str = "127.0.0.1",
    kill_switch_container: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SmokeSummary:
    summary = SmokeSummary()
    base_path = (base_path or resolve_base_path()).expanduser().resolve()
    repo_root = (repo_root or REPO_ROOT).resolve()
    normalized_https = broker.normalize_host(https_host)

    container = run_preflight(
        summary,
        bottle=bottle,
        base_path=base_path,
        prefix=prefix,
        broker_host=broker_host,
    )
    if container is None:
        return summary

    # ── 2. Blocked :443 notifies ───────────────────────────────────────────
    before_ids = set(fold_open_requests(base_path))
    rc = curl_https(container, normalized_https)
    if rc != 0:
        summary.pass_(
            "blocked :443 connection fails fast",
            f"curl exit {rc} (expected)",
        )
    else:
        summary.fail("blocked :443 connection fails fast", f"curl exit {rc}")

    https_req = None
    deadline = time.monotonic() + REQUEST_WAIT_SECONDS
    while time.monotonic() < deadline:
        for request_id, req in fold_open_requests(base_path).items():
            if request_id in before_ids or req.port != 443:
                continue
            https_req = req
            break
        if https_req is not None:
            break
        sleep(POLL_INTERVAL_SECONDS)

    if https_req is None:
        summary.fail(
            "blocked :443 notifies",
            f"no open queue entry for {normalized_https}:443",
        )
    elif https_req.host == normalized_https:
        summary.pass_("blocked :443 notifies", f"request {https_req.request_id}")
    else:
        summary.fail(
            "blocked :443 notifies",
            f"queue host {https_req.host!r} != {normalized_https!r}",
        )

    # ── 3. Blocked :5432 via NFLOG ───────────────────────────────────────────
    pg_rc = nc_probe(container, pg_ip, pg_port)
    if pg_rc != 0:
        summary.pass_(
            "blocked :5432 connection fails fast",
            f"probe exit {pg_rc} (expected)",
        )
    else:
        summary.fail("blocked :5432 connection fails fast", f"probe exit {pg_rc}")

    expected_pg_hosts = {pg_ip}
    rdns_host = normalized_rdns_host(pg_ip)
    if rdns_host:
        expected_pg_hosts.add(rdns_host)

    pg_req = None
    deadline = time.monotonic() + REQUEST_WAIT_SECONDS
    while time.monotonic() < deadline:
        for req in fold_open_requests(base_path).values():
            if req.port != pg_port:
                continue
            if req.host in expected_pg_hosts:
                pg_req = req
                break
        if pg_req is not None:
            break
        sleep(POLL_INTERVAL_SECONDS)

    if pg_req is None:
        summary.fail(
            "blocked :5432 notifies via NFLOG",
            f"no open queue entry for {pg_ip}:{pg_port}",
        )
    else:
        detail = f"request {pg_req.request_id} host={pg_req.host!r} port={pg_req.port}"
        if rdns_host and pg_req.host == rdns_host:
            detail += f" (rDNS {rdns_host!r})"
        summary.pass_("blocked :5432 notifies via NFLOG", detail)

    # ── 4. Approve closes the loop ───────────────────────────────────────────
    if https_req is not None:
        approve = approve_domain(repo_root, bottle, normalized_https)
        if approve.returncode == 0:
            summary.pass_(
                "approve :443 request",
                f"allow-egress.sh applied for {normalized_https}",
            )
        else:
            summary.fail(
                "approve :443 request",
                (approve.stderr or approve.stdout or "allow-egress failed").strip(),
            )
        sleep(1.0)
        retry_rc = curl_https(container, normalized_https, connect_timeout=5)
        if retry_rc == 0:
            summary.pass_("retry :443 succeeds after approve", normalized_https)
        else:
            summary.fail("retry :443 succeeds after approve", f"curl exit {retry_rc}")

    if pg_req is not None:
        cidr = f"{pg_ip}/32"
        grant = grant_test_cidr(container, cidr)
        if grant.returncode == 0:
            summary.pass_("approve :5432 IP grant", f"ipset add {cidr}")
        else:
            summary.fail(
                "approve :5432 IP grant",
                (grant.stderr or grant.stdout or "ipset add failed").strip(),
            )
        sleep(1.0)
        retry_pg = nc_probe(container, pg_ip, pg_port, timeout_seconds=3)
        if retry_pg == 0:
            summary.pass_("retry :5432 succeeds after grant", f"{pg_ip}:{pg_port}")
        else:
            summary.fail("retry :5432 succeeds after grant", f"probe exit {retry_pg}")

    # ── 5. Queue is never mounted ────────────────────────────────────────────
    violations = queue_mount_violations(base_path, prefix)
    if violations:
        summary.fail(
            "invariant: queue not mounted",
            "; ".join(violations),
        )
    else:
        summary.pass_(
            "invariant: queue not mounted",
            f"no {base_path}/run/ mount on running bottles",
        )

    # ── 6. Log is not an allow list ──────────────────────────────────────────
    spoof_norm = broker.normalize_host(spoof_host)
    append_spoof_allowed_line(base_path, host=spoof_norm, container=bottle)
    before_spoof = count_events(base_path, kind="requested", host=spoof_norm)
    spoof_rc = curl_https(container, spoof_norm)
    if spoof_rc != 0:
        summary.pass_(
            "spoof allowed line does not open egress",
            f"curl exit {spoof_rc}",
        )
    else:
        summary.fail("spoof allowed line does not open egress", "curl succeeded unexpectedly")

    sleep(1.0)
    after_spoof = count_events(base_path, kind="requested", host=spoof_norm)
    if after_spoof > before_spoof:
        summary.pass_(
            "invariant: log is not an allow list",
            f"fresh request filed for {spoof_norm}",
        )
    else:
        summary.fail(
            "invariant: log is not an allow list",
            f"no new requested event for {spoof_norm}",
        )

    # ── 7. Coalescing ────────────────────────────────────────────────────────
    coalesce_norm = broker.normalize_host(coalesce_host)
    before_coalesce = count_events(
        base_path,
        kind="requested",
        host=coalesce_norm,
        port=443,
    )
    docker_exec(
        container,
        f"for i in $(seq 1 25); do "
        f"curl -sS -o /dev/null --connect-timeout 1 https://{coalesce_norm}/ "
        f"2>/dev/null || true; done",
        timeout=60.0,
    )
    sleep(2.0)
    after_coalesce = count_events(
        base_path,
        kind="requested",
        host=coalesce_norm,
        port=443,
    )
    new_requests = after_coalesce - before_coalesce
    open_coalesce = find_open_request(
        fold_open_requests(base_path),
        host=coalesce_norm,
        port=443,
    )
    hit_count = open_coalesce.hit_count if open_coalesce else 0
    # One filing opens the row; the retry loop coalesces onto it — the
    # retries are counted on the row (hit_count), not as new requests.
    if new_requests == 1 and hit_count >= 2:
        summary.pass_(
            "coalescing",
            f"requested +{new_requests}, row hit_count={hit_count}",
        )
    else:
        summary.fail(
            "coalescing",
            f"requested +{new_requests}, row hit_count={hit_count}"
            " (want 1 request, retries counted on the row)",
        )

    # ── 8. Kill switch ───────────────────────────────────────────────────────
    ok, derived, message = derive_kill_switch_ports(repo_root)
    broker_host = derived.get("EGRESS_BROKER_HOST", "")
    enable_broker = derived.get("ENABLE_EGRESS_BROKER", "")
    if ok and enable_broker == "false" and not broker_host:
        summary.pass_(
            "kill switch: manifest derives no broker address",
            f"EGRESS_BROKER_HOST={broker_host!r} ENABLE_EGRESS_BROKER={enable_broker!r}",
        )
    else:
        summary.fail(
            "kill switch: manifest derives no broker address",
            message if not ok else f"EGRESS_BROKER_HOST={broker_host!r} ENABLE_EGRESS_BROKER={enable_broker!r}",
        )

    if kill_switch_container:
        if container_has_nflog_rule(kill_switch_container):
            summary.fail(
                "kill switch: no NFLOG rule",
                f"{kill_switch_container} still has NFLOG",
            )
        else:
            summary.pass_(
                "kill switch: no NFLOG rule",
                f"{kill_switch_container} OUTPUT has no NFLOG",
            )
        ports = docker_inspect_env(kill_switch_container, "EGRESS_BROKER_HOST") or ""
        if ports:
            summary.fail("kill switch: no broker-address grant", ports)
        else:
            summary.pass_("kill switch: no broker-address grant", "(empty)")
    else:
        summary.skip(
            "kill switch: live broker-address/NFLOG on disabled bottle",
            "set EGRESS_SMOKE_KILL_BOTTLE to a running bottle with "
            "capabilities.egress_broker: false",
        )

    from egress_smoke_phase_b import run_smoke_phase_b

    run_smoke_phase_b(
        summary,
        bottle=bottle,
        container=container,
        base_path=base_path,
        repo_root=repo_root,
        https_host=https_host,
        kill_switch_container=kill_switch_container,
        sleep=sleep,
    )

    return summary


def format_summary(summary: SmokeSummary) -> str:
    lines = ["── egress smoke"]
    for item in summary.results:
        mark = {"pass": "✓", "fail": "✗", "skip": "~"}[item.status]
        suffix = f" — {item.detail}" if item.detail else ""
        lines.append(f"  {mark} {item.name}{suffix}")
    lines.append("")
    lines.append(
        f"summary: {summary.passed} passed, {summary.failed} failed, "
        f"{summary.skipped} skipped"
    )
    if summary.failed:
        lines.append("FAILED")
    else:
        lines.append("OK")
    return "\n".join(lines)


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase A+B egress operator smoke test (host-side)",
    )
    parser.add_argument(
        "bottle",
        nargs="?",
        default=os.environ.get("EGRESS_SMOKE_BOTTLE", ""),
        help="bottle manifest name (default: EGRESS_SMOKE_BOTTLE)",
    )
    parser.add_argument(
        "--base-path",
        default=os.environ.get("DJINN_HOME", ""),
        help="djinn home (default: DJINN_HOME or ./.djinn)",
    )
    parser.add_argument(
        "--https-host",
        default=os.environ.get("EGRESS_SMOKE_HTTPS_HOST", DEFAULT_HTTPS_HOST),
    )
    parser.add_argument(
        "--pg-ip",
        default=os.environ.get("EGRESS_SMOKE_PG_IP", DEFAULT_PG_IP),
    )
    parser.add_argument(
        "--kill-switch-bottle",
        default=os.environ.get("EGRESS_SMOKE_KILL_BOTTLE", ""),
        help="optional bottle with capabilities.egress_broker: false",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if is_inside_container():
        print("SKIP: egress smoke must run on the Mac host, not inside a container")
        return 0
    if not is_mac_host():
        print("SKIP: egress smoke requires a Mac host with Docker Desktop")
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    bottle = args.bottle.strip()
    if not bottle:
        running = list_running_bottle_containers()
        if len(running) == 1:
            bottle = running[0].removeprefix("djinn-")
        else:
            print(
                "SKIP: pass a bottle name or set EGRESS_SMOKE_BOTTLE "
                f"(running: {', '.join(running) or 'none'})",
                file=sys.stderr,
            )
            return 0

    base_path = broker.resolve_base_path(args.base_path)
    kill_container = ""
    if args.kill_switch_bottle.strip():
        kill_container = container_name_for_bottle(args.kill_switch_bottle.strip())

    summary = run_smoke(
        bottle,
        base_path=base_path,
        https_host=args.https_host,
        pg_ip=args.pg_ip,
        kill_switch_container=kill_container or None,
    )
    print(format_summary(summary))
    return summary.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
