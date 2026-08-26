#!/usr/bin/env python3
"""Phase B live egress broker smoke checks (host-side)."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import egress_broker_host as broker
import egress_smoke_lib as smoke


def operator_token_path(base_path: Path) -> Path:
    return smoke.egress_log_root(base_path) / broker.OPERATOR_TOKEN_FILENAME


def load_operator_token(base_path: Path) -> str | None:
    path = operator_token_path(base_path)
    if not path.is_file():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def watcher_process_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "egress_watch.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def post_decide_allow(
    *,
    bottle: str,
    host: str,
    operator_token: str,
    broker_host: str = "127.0.0.1",
    port: int = smoke.BROKER_PORT,
) -> tuple[int, dict[str, Any] | None]:
    url = f"http://{broker_host}:{port}/decide"
    payload = json.dumps(
        {
            "container": bottle,
            "host": host,
            "decision": "allow",
            "scope": "live",
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {operator_token}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body if isinstance(body, dict) else None
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
            body = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            body = None
        return exc.code, body if isinstance(body, dict) else None
    except (OSError, URLError, json.JSONDecodeError, ValueError):
        return 0, None


def post_decide_with_bottle_token(
    *,
    bottle: str,
    host: str,
    bottle_token: str,
    broker_host: str = "127.0.0.1",
    port: int = smoke.BROKER_PORT,
) -> int:
    url = f"http://{broker_host}:{port}/decide"
    payload = json.dumps(
        {
            "container": bottle,
            "host": host,
            "decision": "allow",
            "scope": "live",
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bottle_token}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (OSError, URLError):
        return 0


def curl_http_response(
    container: str,
    host: str,
    *,
    include_headers: bool = False,
    connect_timeout: int = 10,
) -> tuple[int, str]:
    flag = "-i" if include_headers else "-sS"
    result = smoke.docker_exec(
        container,
        f"curl {flag} --connect-timeout {connect_timeout} http://{host}/ 2>&1; "
        f"printf '\\n__RC__%s' $?",
        timeout=float(connect_timeout + 15),
    )
    text = result.stdout
    if "__RC__" not in text:
        return 1, text
    body, _, rc_text = text.rpartition("__RC__")
    try:
        return int(rc_text.strip()), body
    except ValueError:
        return 1, body


def curl_https_verbose(container: str, host: str, *, connect_timeout: int = 5) -> str:
    result = smoke.docker_exec(
        container,
        f"curl -vk --connect-timeout {connect_timeout} https://{host}/ 2>&1 || true",
        timeout=float(connect_timeout + 15),
    )
    return result.stdout


def nat_redirect_packet_count(container: str) -> int | None:
    result = smoke.docker_exec(
        container,
        "iptables -t nat -L OUTPUT -v -n -x 2>/dev/null | "
        "awk '/REDIRECT/ && /dports 80,443/ {print $1; exit}'",
    )
    text = result.stdout.strip().splitlines()
    if not text:
        return None
    try:
        return int(text[-1])
    except ValueError:
        return None


def container_proxy_env_clean(container: str) -> tuple[bool, str]:
    result = smoke.docker_exec(
        container,
        "for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY no_proxy; do "
        'if [ -n "${!v:-}" ]; then echo "$v=${!v}"; fi; done',
    )
    leaks = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return len(leaks) == 0, ", ".join(leaks)


def container_broker_listening(container: str) -> bool:
    result = smoke.docker_exec(
        container,
        "ss -ltn 2>/dev/null | awk '$4 ~ /:3128$/ {found=1} END {exit !found}'",
    )
    return result.returncode == 0


def container_has_redirect_rule(container: str) -> bool:
    result = smoke.docker_exec(
        container,
        "iptables -t nat -S OUTPUT 2>/dev/null | grep -F 'REDIRECT' | grep -F 'dports 80,443' || true",
    )
    return "REDIRECT" in result.stdout


def curl_timed_https(
    container: str,
    host: str,
    *,
    connect_timeout: int = 5,
) -> tuple[int, float]:
    result = smoke.docker_exec(
        container,
        "start=$(date +%s.%N); "
        f"curl -sS -o /dev/null --connect-timeout {connect_timeout} https://{host}/ "
        "2>/dev/null; rc=$?; end=$(date +%s.%N); "
        "python3 -c 'import sys; s,e,rc=map(float,sys.argv[1:]); print(f\"{rc:.0f} {e-s:.3f}\")' "
        "$start $end $rc",
        timeout=float(connect_timeout + 20),
    )
    parts = result.stdout.strip().split()
    if len(parts) < 2:
        return 1, 999.0
    try:
        return int(float(parts[0])), float(parts[1])
    except ValueError:
        return 1, 999.0


def run_smoke_phase_b(
    summary: smoke.SmokeSummary,
    *,
    bottle: str,
    container: str,
    base_path: Path,
    repo_root: Path,
    https_host: str = smoke.DEFAULT_HTTPS_HOST,
    http_host: str = smoke.DEFAULT_HTTP_HOST,
    broker_host: str = "127.0.0.1",
    kill_switch_container: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    normalized_https = broker.normalize_host(https_host)
    normalized_http = broker.normalize_host(http_host)
    short_bottle = bottle.removeprefix("djinn-")

    rc, body = curl_http_response(container, normalized_http)
    if rc == 0 and "pending operator approval" in body:
        summary.pass_(":80 body instructional", normalized_http)
    else:
        summary.fail(":80 body instructional", f"curl rc={rc}")

    req_id_match = re.search(r"request id ([0-9a-f]{8})", body)
    request_id = req_id_match.group(1) if req_id_match else ""
    if re.search(r"request id unknown", body, re.I):
        summary.fail(":80 body request id", 'body contains literal request id "unknown"')
    elif req_id_match:
        summary.pass_(":80 body request id", request_id)
    else:
        summary.fail(":80 body request id", "no request id in body")

    _, headers = curl_http_response(container, normalized_http, include_headers=True)
    header_blob = headers.split("\r\n\r\n", 1)[0] if "\r\n\r\n" in headers else headers
    header_checks = [
        ("503", "HTTP/1.1 503" in header_blob),
        (
            "reason phrase",
            f"Egress pending approval: {normalized_http}" in header_blob
            and "(req " in header_blob,
        ),
        ("Retry-After", "Retry-After:" in header_blob),
        ("X-Djinn-Egress", "X-Djinn-Egress: pending" in header_blob),
        ("X-Djinn-Egress-Host", f"X-Djinn-Egress-Host: {normalized_http}" in header_blob),
        (
            "X-Djinn-Egress-Request",
            bool(request_id) and f"X-Djinn-Egress-Request: {request_id}" in header_blob,
        ),
    ]
    failed_headers = [name for name, ok in header_checks if not ok]
    if failed_headers:
        summary.fail(":80 headers", ", ".join(failed_headers))
    else:
        summary.pass_(":80 headers", f"503 + Djinn headers (req {request_id})")

    summary.skip(
        ":80 deny returns 403",
        "requires interactive ./djinn allow --watch deny (not automatable here)",
    )

    # Version-sensitive strings observed on OpenSSL 3 in this image:
    #   error:0A000419:SSL routines::tlsv1 alert access denied
    #   TLS alert, access denied (561)
    verbose = curl_https_verbose(container, normalized_https)
    if "access denied" in verbose.lower():
        summary.pass_(":443 TLS access denied alert", "matched access denied")
    else:
        summary.fail(":443 TLS access denied alert", "expected TLS access denied alert")

    sni_req = smoke.wait_for_open_request(
        base_path,
        host=normalized_https,
        port=443,
        timeout=5.0,
    )
    if sni_req is None:
        summary.fail("SNI naming", f"no open :443 request for {normalized_https}")
    elif broker.is_ip_literal(sni_req.host):
        summary.fail("SNI naming", f"filed host is IP {sni_req.host!r}")
    else:
        summary.pass_("SNI naming", sni_req.host)

    operator_token = load_operator_token(base_path)
    if not operator_token:
        summary.skip(
            "approve mid-hold splices",
            "no operator token — start ./djinn allow --watch on the host",
        )
    elif not watcher_process_running():
        summary.skip(
            "approve mid-hold splices",
            "no ./djinn allow --watch process detected on host",
        )
    else:
        hold_host = broker.normalize_host(f"smoke-hold-{uuid.uuid4().hex[:8]}.example.com")
        outcome: dict[str, Any] = {"rc": 1, "output": ""}

        def _held_curl() -> None:
            rc, output = curl_http_response(container, hold_host, connect_timeout=60)
            outcome["rc"] = rc
            outcome["output"] = output

        thread = threading.Thread(target=_held_curl, daemon=True)
        thread.start()
        hold_req = smoke.wait_for_open_request(
            base_path,
            host=hold_host,
            port=80,
            timeout=smoke.REQUEST_WAIT_SECONDS,
        )
        if hold_req is None:
            summary.fail("approve mid-hold splices", f"no open request for {hold_host}")
        else:
            status, _body = post_decide_allow(
                bottle=short_bottle,
                host=hold_host,
                operator_token=operator_token,
                broker_host=broker_host,
            )
            thread.join(timeout=smoke.REQUEST_WAIT_SECONDS)
            if status == 200 and outcome["rc"] == 0 and "pending operator approval" not in outcome["output"]:
                summary.pass_(
                    "approve mid-hold splices",
                    f"original request completed (req {hold_req.request_id})",
                )
            else:
                summary.fail(
                    "approve mid-hold splices",
                    f"decide status={status} curl rc={outcome['rc']}",
                )

    direct_host = broker.normalize_host(f"smoke-fast-{uuid.uuid4().hex[:8]}.example.com")
    approve = smoke.approve_domain(repo_root, short_bottle, direct_host)
    if approve.returncode != 0:
        summary.fail(
            "fast retry after approve",
            (approve.stderr or approve.stdout or "allow-egress failed").strip(),
        )
    else:
        sleep(1.0)
        rc, elapsed = curl_timed_https(container, direct_host, connect_timeout=8)
        if rc == 0 and elapsed < 8.0:
            summary.pass_("fast retry after approve", f"curl ok in {elapsed:.2f}s")
        else:
            summary.fail("fast retry after approve", f"rc={rc} elapsed={elapsed:.2f}s")

    before_redirect = nat_redirect_packet_count(container)
    github_rc = smoke.curl_https(container, smoke.DEFAULT_FAST_GITHUB, connect_timeout=5)
    npm_rc = smoke.curl_https(container, smoke.DEFAULT_FAST_NPM, connect_timeout=5)
    after_redirect = nat_redirect_packet_count(container)
    if github_rc == 0 and npm_rc == 0:
        summary.pass_("fast path hosts reachable", f"{smoke.DEFAULT_FAST_GITHUB}, {smoke.DEFAULT_FAST_NPM}")
    else:
        summary.fail(
            "fast path hosts reachable",
            f"github rc={github_rc} npm rc={npm_rc}",
        )
    if before_redirect is not None and after_redirect is not None and after_redirect == before_redirect:
        summary.pass_("fast path REDIRECT counter stable", f"packets {before_redirect}")
    elif before_redirect is None or after_redirect is None:
        summary.skip("fast path REDIRECT counter stable", "could not read nat counters")
    else:
        summary.fail(
            "fast path REDIRECT counter stable",
            f"before={before_redirect} after={after_redirect}",
        )

    clean, leaks = container_proxy_env_clean(container)
    if clean:
        summary.pass_("no proxy env leak", "http(s)_proxy and NO_PROXY unset")
    else:
        summary.fail("no proxy env leak", leaks)

    impersonation_host = broker.normalize_host(f"smoke-spoof-{uuid.uuid4().hex[:8]}.example.com")
    spoof_py = (
        "import socket, threading, time\n"
        "def serve():\n"
        "    s = socket.socket()\n"
        "    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    s.bind(('127.0.0.1', 3128))\n"
        "    s.listen(1)\n"
        "    s.settimeout(8)\n"
        "    try:\n"
        "        c, _ = s.accept()\n"
        "        c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok')\n"
        "        c.close()\n"
        "    except OSError:\n"
        "        pass\n"
        "    finally:\n"
        "        s.close()\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
        "time.sleep(0.3)\n"
    )
    smoke.docker_exec(
        container,
        "pkill -f '/usr/local/lib/djinn/egress_broker.py' || true; sleep 0.5",
    )
    impersonation = smoke.docker_exec(
        container,
        "su -s /bin/bash coder -c "
        + json.dumps(
            f"python3 -c {json.dumps(spoof_py)} & "
            f"sleep 0.6; curl -sS --connect-timeout 3 http://{impersonation_host}/; "
            "printf '\\n__RC__%s' $?"
        ),
        timeout=20.0,
    )
    imp_text = impersonation.stdout
    if "__RC__" in imp_text:
        imp_body, _, imp_rc_text = imp_text.rpartition("__RC__")
        try:
            imp_rc = int(imp_rc_text.strip())
        except ValueError:
            imp_rc = 1
    else:
        imp_body, imp_rc = imp_text, 1
    if imp_rc == 0 and imp_body.strip() == "ok":
        summary.pass_(
            "security: impersonation",
            "fake :3128 listener answered locally; no real egress",
        )
    else:
        summary.fail(
            "security: impersonation",
            f"expected fake ok body, got rc={imp_rc} body={imp_body[:80]!r}",
        )
    smoke.docker_exec(
        container,
        "pkill -f 'python3 -c' || true; "
        "PYTHONPATH=/usr/local/lib/djinn python3 /usr/local/lib/djinn/egress_broker.py --supervise "
        "</dev/null >/tmp/egress-broker-restart.log 2>&1 & sleep 1",
        timeout=15.0,
    )

    bottle_token = smoke.docker_inspect_env(container, "EGRESS_BROKER_TOKEN") or ""
    if not bottle_token:
        summary.fail("security: bottle cannot self-approve", "missing EGRESS_BROKER_TOKEN")
    else:
        status = post_decide_with_bottle_token(
            bottle=short_bottle,
            host=normalized_https,
            bottle_token=bottle_token,
            broker_host=broker_host,
        )
        if status in (401, 403):
            summary.pass_("security: bottle cannot self-approve", f"HTTP {status}")
        else:
            summary.fail(
                "security: bottle cannot self-approve",
                f"POST /decide unexpectedly returned HTTP {status}",
            )

    if kill_switch_container:
        if container_has_redirect_rule(kill_switch_container):
            summary.fail(
                "kill switch: no REDIRECT rule",
                f"{kill_switch_container} still has REDIRECT",
            )
        else:
            summary.pass_(
                "kill switch: no REDIRECT rule",
                f"{kill_switch_container} nat table clean",
            )
        if container_broker_listening(kill_switch_container):
            summary.fail(
                "kill switch: broker not listening",
                f"{kill_switch_container} still listens on :3128",
            )
        else:
            summary.pass_(
                "kill switch: broker not listening",
                f"{kill_switch_container} :3128 closed",
            )
    else:
        summary.skip(
            "kill switch: live broker/REDIRECT on disabled bottle",
            "set EGRESS_SMOKE_KILL_BOTTLE to a running bottle with "
            "capabilities.egress_broker: false",
        )
