#!/usr/bin/env python3
"""egress_service.py — the egress broker and admin page as one compose project.

`djinn egress start` renders $DJINN_HOME/egress/docker-compose.yml and drives
`docker compose -p djinn-egress`, the same shape the jump singleton uses
(src/jump_host.py): two `restart: unless-stopped` services from one small
image, so no terminal has to stay open for a bottle to file an egress request.
The broker joins djinn-net at a static address (offset 3 in
djinn_net_addr.SINGLETON_OFFSETS) and the compose-private egress-backend
network; the admin joins egress-backend only, so no bottle has a route to it,
and publishes 127.0.0.1:8817 on the host.

The broker container mounts the host's docker socket so bin/allow-egress.sh
(exec'd from inside the broker) can docker exec into bottles and edit
manifests; DOCKER_HOST selects which socket is mounted (unix only — a TCP
docker endpoint is a different transport and is refused by name). Token files
(operator.token, admin.key) are created HOST-SIDE before the containers come
up, so they stay operator-owned and `djinn egress url` can read them.

Stdlib only; host-side. Docker command output is boundary-logged the way
jump_host._run logs it: exact argv, duration, exit code.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ensure_net  # noqa: E402
import djinn_net_addr  # noqa: E402
from admin_daemon import (  # noqa: E402
    DEFAULT_PORT as ADMIN_DEFAULT_PORT,
    ensure_admin_key,
    read_admin_key,
    session_url,
)
from egress_broker_host import (  # noqa: E402
    DEFAULT_PORT as BROKER_DEFAULT_PORT,
    LOCK_FILENAME,
    ensure_operator_token,
    resolve_egress_root,
)
from egress_notify import SECRETS_FILENAME, read_secrets_env  # noqa: E402

EGRESS_ACTIONS_URL_ENV = "EGRESS_ACTIONS_URL"
DOCKER_HOST_ENV = "DOCKER_HOST"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
IN_CONTAINER_SOCKET = "/var/run/docker.sock"

COMPOSE_PROJECT = "djinn-egress"
COMPOSE_DIRNAME = "egress"
COMPOSE_FILENAME = "docker-compose.yml"
IMAGE_TAG = "djinn-egress:local"
SERVICE_BROKER = "broker"
SERVICE_ADMIN = "admin"
CONTAINER_BROKER = "djinn-egress-broker"
CONTAINER_ADMIN = "djinn-egress-admin"
BACKEND_NETWORK = "egress-backend"

# `djinn egress start` waits this long for the broker's /health to answer
# before reporting the admin URL anyway (the containers are up either way;
# the wait only decides whether the start line reads ok or degraded).
HEALTH_WAIT_SECONDS = 15.0
HEALTH_POLL_SECONDS = 0.5
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

DJINN_CONTAINER_MARKER = "DJINN_CONTAINER"
PYTHON_ENTRY = "/opt/brassbottle/src"


class EgressServiceError(Exception):
    """Operator-facing egress service error."""


class DockerCommandMissing(Exception):
    """Docker CLI absent from PATH — boundary error already logged."""

    exit_code = 127


DOCKER_MISSING_EXIT = DockerCommandMissing.exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_base_path(explicit: str | None, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser()
    home = (env.get("DJINN_HOME") or "").strip()
    if home:
        return Path(home)
    return _repo_root() / ".djinn"


def resolve_bottles_path(base_path: Path, env: dict[str, str] | None = None) -> Path:
    """Where manifests live, matching src/common.sh's resolution: an explicit
    BOTTLES_PATH wins, else $DJINN_HOME/bottles."""
    env = os.environ if env is None else env
    raw = (env.get("BOTTLES_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return base_path / "bottles"


def paths(base_path: Path) -> dict[str, Path]:
    base = base_path.expanduser().resolve()
    return {
        "base": base,
        "egress_root": base / "run" / "egress",
        "compose_dir": base / COMPOSE_DIRNAME,
        "compose_file": base / COMPOSE_DIRNAME / "docker-compose.yml",
    }


def resolve_docker_socket(env: dict[str, str] | None = None) -> str:
    """The host socket path to mount into the broker container.

    DOCKER_HOST unset → /var/run/docker.sock; `unix://<path>` → <path>;
    anything else (tcp, ssh, npipe) is refused by name: allow-egress.sh speaks
    the docker CLI, which supports those transports, but a TCP daemon is a
    different trust boundary this service does not mount or configure.
    """
    env = os.environ if env is None else env
    value = (env.get(DOCKER_HOST_ENV) or "").strip()
    if not value:
        return IN_CONTAINER_SOCKET
    if value.startswith("unix://"):
        path = value[len("unix://"):]
        return path or IN_CONTAINER_SOCKET
    scheme = value.split("://", 1)[0] or value
    raise EgressServiceError(
        f"DOCKER_HOST {value!r} is not a unix socket (scheme {scheme!r}) — "
        f"the egress service mounts a local socket at {IN_CONTAINER_SOCKET}; "
        f"unset DOCKER_HOST or point it at unix://<path>"
    )


def _socket_mount(sock: str, target: str) -> str:
    # json.dumps gives a correctly escaped double-quoted scalar; $$-escaping
    # afterwards because compose interpolates ${VAR} and $VAR in file contents
    # (same as the jump overlay's volume scalars).
    return json.dumps(f"{sock}:{target}").replace("$", "$$")


def _env_scalar(value: str) -> str:
    return json.dumps(value).replace("$", "$$")


def build_compose_spec(
    *,
    base_path: Path,
    bottles_path: Path,
    egress_ip: str,
    docker_socket: str,
    actions_url: str | None,
    repo_root: Path | None = None,
) -> dict:
    """The compose project as data — tests pin this dict literally.

    Both services run the same image; the admin holds the operator token and
    the session key and must not share a process (or a network) with the
    thing bottles reach, hence two services on two networks.
    """
    repo = repo_root or _repo_root()
    broker_env: dict[str, str] = {
        "DJINN_HOME": str(base_path),
        "BOTTLES_PATH": str(bottles_path),
        "DJINN_CONTAINER": "1",
        "EGRESS_ADMIN_URL": f"http://127.0.0.1:{ADMIN_DEFAULT_PORT}",
    }
    if actions_url:
        broker_env["EGRESS_ACTIONS_URL"] = actions_url
    return {
        "networks": {
            "djinn-net": {"name": djinn_net_addr.NETWORK_NAME, "external": True},
            "egress-backend": {"internal": True},
        },
        "services": {
            SERVICE_BROKER: {
                "build": {"context": str(repo), "dockerfile": "egress/Dockerfile"},
                "image": IMAGE_TAG,
                "container_name": CONTAINER_BROKER,
                "restart": "unless-stopped",
                "networks": {
                    "egress-backend": None,
                    "djinn-net": {"ipv4_address": egress_ip},
                },
                "ports": [f"127.0.0.1:{BROKER_DEFAULT_PORT}:{BROKER_DEFAULT_PORT}"],
                "volumes": [
                    f"{docker_socket}:{IN_CONTAINER_SOCKET}",
                    f"{base_path}:{base_path}",
                    f"{bottles_path}:{bottles_path}",
                ],
                "environment": broker_env,
                "command": [
                    "python3",
                    f"{PYTHON_ENTRY}/egress_broker_host.py",
                    "--base-path",
                    str(base_path),
                    "--bind-any",
                    "--port",
                    str(BROKER_DEFAULT_PORT),
                    "--advertise",
                    f"127.0.0.1:{BROKER_DEFAULT_PORT}",
                ],
            },
            SERVICE_ADMIN: {
                "build": {"context": str(repo), "dockerfile": "egress/Dockerfile"},
                "image": IMAGE_TAG,
                "container_name": CONTAINER_ADMIN,
                "restart": "unless-stopped",
                "networks": {"egress-backend": None},
                "ports": [f"127.0.0.1:{ADMIN_DEFAULT_PORT}:{ADMIN_DEFAULT_PORT}"],
                "volumes": [f"{base_path}:{base_path}"],
                "environment": {
                    "DJINN_HOME": str(base_path),
                    "DJINN_CONTAINER": "1",
                    "EGRESS_BROKER_URL": f"http://{SERVICE_BROKER}:{BROKER_DEFAULT_PORT}",
                },
                "command": [
                    "python3",
                    f"{PYTHON_ENTRY}/admin_daemon.py",
                    "--bind-any",
                    "--port",
                    str(ADMIN_DEFAULT_PORT),
                ],
                "depends_on": [SERVICE_BROKER],
            },
        },
    }


_SAFE_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _scalar(value: object) -> str:
    """One YAML scalar. Bare when unambiguous (identifiers, dotted IPs,
    restart policies), json.dumps-quoted otherwise (paths with spaces, `#`,
    `$`, URLs with colons), with $$-escaping because compose interpolates
    ${VAR} in file contents."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _SAFE_SCALAR_RE.fullmatch(text) or _IPV4_RE.fullmatch(text):
        return text
    return json.dumps(text).replace("$", "$$")

def _emit(node: object, indent: int = 0) -> list[str]:
    """A tiny YAML emitter for the compose shape above (dicts of dicts/lists
    of scalars only). Ambiguous strings are quoted via json.dumps, so paths with
    spaces, `#`, or `$` survive verbatim."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            rendered_key = _scalar(key)
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{rendered_key}:")
                lines.extend(_emit(value, indent + 1))
            elif value is None:
                lines.append(f"{pad}{rendered_key}:")
            else:
                lines.append(f"{pad}{rendered_key}: {_scalar(value)}")
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(_emit(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:  # pragma: no cover — build_compose_spec never emits a bare scalar
        lines.append(f"{pad}{_scalar(node)}")
    return lines


def render_compose_yaml(spec: dict) -> str:
    return "\n".join(_emit(spec)) + "\n"


def write_compose_file(base_path: Path, spec: dict) -> Path:
    """Write the compose file atomically (tmp + os.replace) — a partially
    written file read by a concurrent `status` must not parse as a broken
    project."""
    p = paths(base_path)
    p["compose_dir"].mkdir(parents=True, exist_ok=True)
    target = p["compose_file"]
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(render_compose_yaml(spec), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise EgressServiceError(f"cannot write compose file {target}: {exc}") from exc
    return target


def _compose_cmd(base_path: Path, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--project-directory",
        str(_repo_root()),
        "-f",
        str(paths(base_path)["compose_file"]),
        *args,
    ]


def _run(
    cmd: list[str],
    *,
    boundary: str | None = None,
    started: float | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
    except FileNotFoundError as exc:
        print(
            f"egress {boundary or 'run'} error reason=docker-not-found",
            file=sys.stderr,
        )
        raise DockerCommandMissing(str(exc)) from exc
    if boundary is not None and started is not None:
        print(
            f"egress {boundary} done duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}"
        )
    return result


def _services_running(base_path: Path) -> tuple[bool, bool]:
    """(broker, admin) actually RUNNING — same shape as
    jump_host._service_running: `ps --status running` only."""
    result = _run(
        _compose_cmd(base_path, "ps", "--status", "running", "--services"),
        boundary="status",
        check=False,
        capture_output=True,
    )
    names = set((result.stdout or "").split())
    return SERVICE_BROKER in names, SERVICE_ADMIN in names


def _live_subnet() -> ipaddress.IPv4Network | None:
    """The subnet djinn-net actually has, or None if it cannot be read.

    Same reasoning as jump_host._live_subnet: ensure_net only WARNS when a
    pre-existing bridge disagrees with DJINN_SUBNET and still returns 0, so
    the desired value is not a safe basis for a static address.
    """
    try:
        raw = ensure_net.network_subnet(djinn_net_addr.NETWORK_NAME)
    except Exception as exc:  # noqa: BLE001 — boundary: never fail start on this
        print(f"egress ensure-net warn reason=subnet-unreadable detail={exc}")
        return None
    if not raw:
        return None
    try:
        return ipaddress.IPv4Network(raw, strict=True)
    except ValueError:
        print(f"egress ensure-net warn reason=subnet-unparseable value={raw}")
        return None


def _ensure_network() -> int:
    """Create/validate djinn-net before compose runs — same call up.sh makes
    (src/ensure_net.py owns create/verify); the subnet is an argument, not
    inherited from the environment."""
    subnet = str(djinn_net_addr.resolve_subnet())
    try:
        return _run(
            [sys.executable, str(_repo_root() / "src" / "ensure_net.py"), subnet],
            boundary="ensure-net",
            check=False,
        ).returncode
    except DockerCommandMissing:
        raise


def _lock_held(egress_root: Path) -> bool:
    """True when a live process holds the daemon flock (a running
    `./djinn allow --watch`, or the dockerized broker itself). flock cannot
    see across the check's own process boundary either way — acquiring and
    immediately releasing proves nobody else holds it."""
    path = egress_root / LOCK_FILENAME
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False  # unreadable → let compose tell us what is wrong
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return False


def _probe_health(port: int, path: str = "/health") -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_PROBE_TIMEOUT_SECONDS) as resp:
            resp.read(1)
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_broker_health(base_path: Path) -> bool:
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    while True:
        if _probe_health(BROKER_DEFAULT_PORT):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(HEALTH_POLL_SECONDS)


def _resolve_bottles_env(env: dict[str, str], base_path: Path) -> Path:
    return resolve_bottles_path(base_path, env)


def _actions_url(env: dict[str, str], base_path: Path) -> str | None:
    """EGRESS_ACTIONS_URL: process environment first, else secrets.env — this
    module is the only reader of that name from secrets.env; the broker itself
    reads only its environment."""
    value = (env.get(EGRESS_ACTIONS_URL_ENV) or "").strip()
    if not value:
        from_file = read_secrets_env(
            base_path / SECRETS_FILENAME, (EGRESS_ACTIONS_URL_ENV,)
        )
        value = from_file.get(EGRESS_ACTIONS_URL_ENV, "").strip()
    return value or None


def _ensure_host_side_state(egress_root: Path) -> None:
    """Create run/egress and its secrets BEFORE the containers come up.

    The service containers run as root in-container; anything they created
    first would be root-owned and unreadable to the operator's own host-side
    commands (`djinn egress url` needs admin.key; notify and the denylist CLI
    need operator.token). Pre-creating keeps ownership with the operator and
    costs the containers nothing (root reads anything).
    """
    egress_root.mkdir(parents=True, exist_ok=True)
    (egress_root / "tokens").mkdir(parents=True, exist_ok=True)
    ensure_operator_token(egress_root)
    ensure_admin_key(egress_root)


def cmd_start(base_path: Path) -> int:
    started = time.monotonic()
    env = os.environ
    print(f"egress start begin base={base_path}")
    try:
        docker_socket = resolve_docker_socket(env)
        bottles_path = _resolve_bottles_env(env, base_path)
        egress_root = resolve_egress_root(base_path)
    except EgressServiceError as exc:
        print(f"egress start error reason={exc}", file=sys.stderr)
        return 1
    if _lock_held(egress_root):
        broker_running, _admin_running = _services_running(base_path)
        if broker_running:
            print("egress start note reason=lock-held-by-broker-service (idempotent restart)")
        else:
            print(
                "egress start error reason=daemon-lock-held detail=a running "
                "`./djinn allow --watch` holds the egress daemon lock — stop it, "
                "or run ./djinn egress stop first",
                file=sys.stderr,
            )
            return 1
    try:
        rc = _ensure_network()
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if rc != 0:
        print(
            f"egress start error reason=djinn-net-unavailable exit_code={rc}",
            file=sys.stderr,
        )
        return rc
    live = _live_subnet()
    if live is not None and live != djinn_net_addr.resolve_subnet(env):
        print(
            f"egress start warn reason=subnet-drift live={live} "
            f"desired={djinn_net_addr.resolve_subnet(env)} — using the live bridge"
        )
    try:
        egress_ip = djinn_net_addr.resolve_egress_ip(env=env, subnet=live)
        actions_url = _actions_url(env, base_path)
        _ensure_host_side_state(egress_root)
        spec = build_compose_spec(
            base_path=base_path,
            bottles_path=bottles_path,
            egress_ip=egress_ip,
            docker_socket=docker_socket,
            actions_url=actions_url,
        )
        write_compose_file(base_path, spec)
    except (EgressServiceError, ValueError) as exc:
        print(f"egress start error reason={exc}", file=sys.stderr)
        return 1
    try:
        result = _run(
            _compose_cmd(base_path, "up", "-d", "--build"),
            boundary="up",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    if result.returncode != 0:
        print(
            f"egress start error duration={time.monotonic() - started:.2f}s "
            f"exit_code={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode
    healthy = _wait_for_broker_health(base_path)
    try:
        key = read_admin_key(egress_root)
        admin_link = session_url(key)
    except RuntimeError as exc:
        print(f"egress start warn reason={exc}", file=sys.stderr)
        admin_link = ""
    print(
        f"egress start ok project={COMPOSE_PROJECT} ip={egress_ip} "
        f"broker_health={'ok' if healthy else 'pending'}"
    )
    print("")
    print(f"  Broker:  http://127.0.0.1:{BROKER_DEFAULT_PORT}")
    if admin_link:
        print("  Open the admin page from this URL (one page-load signs you in):")
        print(f"    {admin_link}")
    else:
        print(f"  Admin:   http://127.0.0.1:{ADMIN_DEFAULT_PORT} — run ./djinn egress url")
    print("")
    return 0


def cmd_stop(base_path: Path) -> int:
    compose_file = paths(base_path)["compose_file"]
    started = time.monotonic()
    if not compose_file.exists():
        print(f"egress stop not-configured project={COMPOSE_PROJECT}")
        return 0
    try:
        result = _run(
            _compose_cmd(base_path, "down"),
            boundary="down",
            started=started,
            check=False,
        )
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    return result.returncode


def cmd_status(base_path: Path) -> int:
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        print(f"egress status not-configured project={COMPOSE_PROJECT}")
        return 1
    try:
        broker_running, admin_running = _services_running(base_path)
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT
    print(
        f"egress status broker={'running' if broker_running else 'stopped'} "
        f"admin={'running' if admin_running else 'stopped'} "
        f"project={COMPOSE_PROJECT}"
    )
    print(
        f"egress broker health={'ok' if _probe_health(BROKER_DEFAULT_PORT) else 'unreachable'} "
        f"port={BROKER_DEFAULT_PORT}"
    )
    print(
        f"egress admin health={'ok' if _probe_health(ADMIN_DEFAULT_PORT, '/health') else 'unreachable'} "
        f"port={ADMIN_DEFAULT_PORT}"
    )
    return 0 if (broker_running and admin_running) else 1


def cmd_logs(base_path: Path, follow: bool) -> int:
    compose_file = paths(base_path)["compose_file"]
    if not compose_file.exists():
        raise EgressServiceError(
            "the egress service is not configured — run: ./djinn egress start"
        )
    args = ["logs"]
    if follow:
        args.append("-f")
    try:
        return _run(_compose_cmd(base_path, *args), boundary="logs", check=False).returncode
    except DockerCommandMissing:
        return DOCKER_MISSING_EXIT


def cmd_url(base_path: Path) -> int:
    try:
        key = read_admin_key(resolve_egress_root(base_path))
    except RuntimeError as exc:
        print(f"egress url error reason={exc}", file=sys.stderr)
        return 1
    print(session_url(key))
    return 0


def cmd_ip(env: dict[str, str] | None = None) -> int:
    """Print the egress broker's static bridge address — one line, nothing
    else. up.sh-style consumers read stdout as a bare value, so warnings
    (subnet drift) go to stderr for this one call, like jump_host.cmd_ip."""
    env = os.environ if env is None else env
    with contextlib.redirect_stdout(sys.stderr):
        try:
            raw = ensure_net.network_subnet(djinn_net_addr.NETWORK_NAME)
        except Exception:  # noqa: BLE001 — an unreadable bridge falls back
            raw = None
        subnet = None
        if raw:
            try:
                subnet = ipaddress.IPv4Network(raw, strict=True)
            except ValueError:
                subnet = None
    print(djinn_net_addr.resolve_egress_ip(env=env, subnet=subnet))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="djinn egress",
        description="Egress broker + admin page as a docker compose singleton.",
    )
    parser.add_argument("--base-path", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    sub.add_parser("start", help="build and start the broker and admin containers")
    sub.add_parser("stop", help="stop and remove both containers")
    sub.add_parser("status", help="report whether both services are running")
    logs = sub.add_parser("logs", help="show egress service logs")
    logs.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("url", help="print the admin session URL")
    sub.add_parser("ip", help="print the broker's static djinn-net address")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    try:
        if args.command == "start":
            return cmd_start(base_path)
        if args.command == "stop":
            return cmd_stop(base_path)
        if args.command == "status":
            return cmd_status(base_path)
        if args.command == "logs":
            return cmd_logs(base_path, args.follow)
        if args.command == "url":
            return cmd_url(base_path)
        if args.command == "ip":
            return cmd_ip()
    except (EgressServiceError, DockerCommandMissing) as exc:
        exit_code = getattr(exc, "exit_code", 1)
        if exit_code != 1:
            return exit_code
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
