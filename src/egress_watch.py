#!/usr/bin/env python3
"""egress_watch.py — interactive egress approval watcher (operator UI).

Runs the egress broker daemon in-process and presents each queued request to
the operator via a terminal prompt (and on macOS, a parallel dialog).
Stdlib only; host-side (macOS and Linux).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from egress_broker_host import (
    CONFIG_FILENAME,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PORT,
    LOCK_FILENAME,
    TOKEN_FILENAME,
    DaemonAlreadyRunning,
    DaemonLock,
    EgressBroker,
    EgressBrokerHTTPServer,
    _load_config,
    _load_token,
    _repo_root,
    _stale_sweep_loop,
    resolve_base_path,
    resolve_egress_root,
)
from egress_log import EgressLog

LOG = logging.getLogger(__name__)

InputFn = Callable[[str], str]
PopenFactory = Callable[..., subprocess.Popen[str]]


@dataclass(frozen=True)
class RequestDetails:
    """Operator-facing fields for one open egress request."""

    request_id: str
    container: str
    host: str
    port: int
    hit_count: int
    uid: int | None = None
    comm: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OperatorChoice:
    """Terminal or dialog selection for one request."""

    action: str  # allow_live | allow_manifest | deny | skip


def allow_prompt_line(host: str) -> str:
    """Question line stating zone/subdomain coverage semantics."""
    return f"Allow {host} (and everything under it)?"


def format_request_block(details: RequestDetails) -> str:
    """Render the per-request summary shown before the prompt."""
    lines = [
        f"Container: {details.container}",
        f"Host:port: {details.host}:{details.port}",
    ]
    if details.uid is not None or details.comm is not None:
        uid = details.uid if details.uid is not None else "?"
        comm = details.comm if details.comm is not None else "?"
        lines.append(f"Process: uid={uid} comm={comm}")
    lines.append(f"Request id: {details.request_id}")
    lines.append(f"Hits: {details.hit_count}")
    if details.reason:
        lines.append(f"Reason: {details.reason}")
    lines.append("")
    lines.append(allow_prompt_line(details.host))
    lines.append("")
    lines.append(
        "[a] allow (live only)   "
        "[p] allow + persist to manifest   "
        "[d] deny   "
        "[s] skip"
    )
    return "\n".join(lines)


def parse_terminal_choice(raw: str) -> OperatorChoice | None:
    """Map one keypress to an operator choice; None means re-prompt."""
    key = raw.strip().lower()
    if not key:
        return None
    ch = key[0]
    if ch == "a":
        return OperatorChoice("allow_live")
    if ch == "p":
        return OperatorChoice("allow_manifest")
    if ch == "d":
        return OperatorChoice("deny")
    if ch == "s":
        return OperatorChoice("skip")
    return None


def build_osascript_argv(
    *,
    title: str,
    message: str,
    hostname: str,
) -> list[str]:
    """Build an osascript argv list with untrusted fields passed only after --."""
    script_lines = [
        "on run argv",
        "set hostName to item 1 of argv",
        "set dialogMessage to item 2 of argv",
        "set dialogTitle to item 3 of argv",
        (
            'set userChoice to button returned of (display dialog dialogMessage '
            'with title dialogTitle buttons {"Deny", "Allow"} default button "Allow")'
        ),
        "return userChoice",
        "end run",
    ]
    argv = ["osascript"]
    for line in script_lines:
        argv.extend(["-e", line])
    argv.extend(["--", hostname, message, title])
    return argv


def build_dialog_message(details: RequestDetails) -> tuple[str, str]:
    """Return (title, message) for the macOS dialog."""
    title = "Egress approval"
    message = (
        f"{details.container} wants {details.host}:{details.port}\n"
        f"{allow_prompt_line(details.host)}"
    )
    return title, message


def _month_records(log: EgressLog, when: datetime) -> list[dict]:
    import egress_log as el  # local import keeps test patching simple

    path = el._log_path(log.root, el._month_filename(when))
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def request_details_from_log(
    log: EgressLog,
    request_id: str,
    *,
    now: datetime | None = None,
) -> RequestDetails | None:
    """Fold request metadata and hit count from the monthly audit log."""
    when = now or datetime.now(timezone.utc)
    queue = log.fold_queue(now=when)
    open_req = queue.open_requests.get(request_id)
    if open_req is None:
        return None

    meta: dict[str, object] = {}
    hit_count = 1
    for record in _month_records(log, when):
        if record.get("request_id") != request_id:
            continue
        kind = record.get("kind")
        if kind == "requested":
            for key in ("container", "host", "port", "uid", "comm", "reason"):
                if key in record:
                    meta[key] = record[key]
        elif kind == "hit":
            count = record.get("count", 1)
            if isinstance(count, int) and count > 0:
                hit_count += count

    container = meta.get("container", open_req.container)
    host = meta.get("host", open_req.host)
    port = meta.get("port", open_req.port)
    if not isinstance(container, str) or not isinstance(host, str) or not isinstance(port, int):
        return None

    uid = meta.get("uid")
    comm = meta.get("comm")
    reason = meta.get("reason")
    return RequestDetails(
        request_id=request_id,
        container=container,
        host=host,
        port=port,
        hit_count=hit_count,
        uid=uid if isinstance(uid, int) else None,
        comm=comm if isinstance(comm, str) else None,
        reason=reason if isinstance(reason, str) else None,
    )


def apply_operator_choice(
    broker: EgressBroker,
    request_id: str,
    choice: OperatorChoice,
) -> None:
    """Invoke broker.decide() for the operator choice (skip is a no-op)."""
    if choice.action == "skip":
        return
    if choice.action == "deny":
        broker.decide(request_id, "deny")
        return
    if choice.action == "allow_live":
        broker.decide(request_id, "allow", scope="live")
        return
    if choice.action == "allow_manifest":
        broker.decide(request_id, "allow", scope="manifest")
        return
    raise ValueError(f"unknown operator action {choice.action!r}")


def _dialog_choice_from_output(output: str) -> OperatorChoice | None:
    text = output.strip()
    if text == "Allow":
        return OperatorChoice("allow_live")
    if text == "Deny":
        return OperatorChoice("deny")
    return None


def _terminate_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except (OSError, ValueError):
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class EgressWatcher:
    """Poll the egress queue and prompt the operator for each open request."""

    def __init__(
        self,
        broker: EgressBroker,
        log: EgressLog,
        *,
        input_fn: InputFn | None = None,
        output: TextIO | None = None,
        poll_interval: float = 0.5,
        platform: str | None = None,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        self._broker = broker
        self._log = log
        self._input_fn = input_fn or input
        self._output = output or sys.stdout
        self._poll_interval = poll_interval
        self._platform = platform if platform is not None else sys.platform
        self._popen_factory = popen_factory or subprocess.Popen
        self._deferred: set[str] = set()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """Present at most one request; return False when the queue is idle."""
        when = self._broker.now()
        queue = self._log.fold_queue(now=when)
        open_ids = sorted(queue.open_requests)
        if not open_ids:
            self._deferred.clear()
            return False

        for request_id in open_ids:
            if request_id in self._deferred:
                continue
            details = request_details_from_log(self._log, request_id, now=when)
            if details is None:
                continue
            choice = self._prompt_operator(details)
            if choice.action == "skip":
                self._deferred.add(request_id)
                return True
            apply_operator_choice(self._broker, request_id, choice)
            self._deferred.discard(request_id)
            return True

        self._deferred.clear()
        return False

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                time.sleep(self._poll_interval)

    def _prompt_operator(self, details: RequestDetails) -> OperatorChoice:
        self._output.write(format_request_block(details) + "\n> ")
        self._output.flush()
        if self._platform == "darwin":
            return self._prompt_with_dialog(details)
        return self._prompt_terminal()

    def _prompt_terminal(self) -> OperatorChoice:
        while True:
            raw = self._input_fn("")
            choice = parse_terminal_choice(raw)
            if choice is not None:
                return choice

    def _prompt_with_dialog(self, details: RequestDetails) -> OperatorChoice:
        winner: list[OperatorChoice | None] = [None]
        decided = threading.Event()
        dialog_proc: list[subprocess.Popen[str] | None] = [None]
        lock = threading.Lock()

        def set_winner(choice: OperatorChoice) -> bool:
            with lock:
                if winner[0] is not None:
                    return False
                winner[0] = choice
                decided.set()
                return True

        def dialog_thread() -> None:
            title, message = build_dialog_message(details)
            argv = build_osascript_argv(
                title=title,
                message=message,
                hostname=details.host,
            )
            try:
                proc = self._popen_factory(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError:
                return
            dialog_proc[0] = proc
            while not decided.is_set():
                try:
                    status = proc.poll()
                except (OSError, ValueError):
                    status = 0
                if status is not None:
                    break
                time.sleep(0.05)
            if decided.is_set():
                _terminate_process(proc)
                return
            try:
                stdout = proc.stdout.read() if proc.stdout is not None else ""
            except (OSError, ValueError):
                stdout = ""
            choice = _dialog_choice_from_output(stdout)
            if choice is not None:
                set_winner(choice)

        thread = threading.Thread(target=dialog_thread, daemon=True)
        thread.start()
        while not decided.is_set():
            raw = self._input_fn("")
            choice = parse_terminal_choice(raw)
            if choice is None:
                time.sleep(0.05)
                continue
            if set_winner(choice):
                break
        _terminate_process(dialog_proc[0])
        thread.join(timeout=2)
        return winner[0] if winner[0] is not None else OperatorChoice("skip")


def run_watch(
    base_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    repo_root: Path | None = None,
    poll_interval: float = 0.5,
) -> None:
    """Acquire the singleton lock, start the broker HTTP server, and watch."""
    egress_root = resolve_egress_root(base_path)
    egress_root.mkdir(parents=True, exist_ok=True)

    token = _load_token(egress_root / TOKEN_FILENAME)
    config = _load_config(egress_root / CONFIG_FILENAME)
    hold_default = config.get("hold_seconds", DEFAULT_HOLD_SECONDS)
    if not isinstance(hold_default, int):
        hold_default = DEFAULT_HOLD_SECONDS

    lock = DaemonLock(egress_root / LOCK_FILENAME)
    lock.acquire()

    broker = EgressBroker(
        egress_root,
        repo_root=repo_root or _repo_root(),
        hold_seconds_default=hold_default,
    )
    server = EgressBrokerHTTPServer((host, port), broker, token)
    stop_event = threading.Event()
    sweep_thread = threading.Thread(
        target=_stale_sweep_loop,
        args=(broker, stop_event),
        name="egress-stale-sweep",
        daemon=True,
    )
    sweep_thread.start()
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="egress-broker-http",
        daemon=True,
    )
    server_thread.start()

    LOG.info("egress watch listening host=%s port=%d", host, server.server_address[1])
    watcher = EgressWatcher(broker, EgressLog(egress_root), poll_interval=poll_interval)
    try:
        watcher.run_forever()
    finally:
        watcher.stop()
        stop_event.set()
        server.shutdown()
        server.server_close()
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="interactive egress approval watcher (runs broker in-process)",
    )
    parser.add_argument(
        "--base-path",
        default="",
        help="djinn home (defaults to DJINN_HOME or ./.djinn)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="broker bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="broker bind port")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="seconds between queue polls when idle",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    base_path = resolve_base_path(args.base_path)
    try:
        run_watch(
            base_path,
            host=args.host,
            port=args.port,
            poll_interval=args.poll_interval,
        )
    except DaemonAlreadyRunning as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
