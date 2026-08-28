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
import queue
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
    IP_APPLY_FAILED_REASON,
    IP_REQUIRES_CIDR_REASON,
    LOCK_FILENAME,
    TOKENS_DIRNAME,
    BottleTokenStore,
    DaemonAlreadyRunning,
    DaemonLock,
    EgressBroker,
    EgressBrokerHTTPServer,
    _load_config,
    _repo_root,
    _stale_sweep_loop,
    ensure_operator_token,
    is_ip_literal,
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


def allow_prompt_line(host: str, *, host_is_ip: bool = False) -> str:
    """Question line stating zone/subdomain coverage semantics."""
    if host_is_ip:
        return (
            f"Allow traffic to {host}? "
            "(requires ALLOWED_CIDRS in the bottle manifest — not allow-egress.sh)"
        )
    return f"Allow {host} (and everything under it)?"


def format_request_block(details: RequestDetails) -> str:
    """Render the per-request summary shown before the prompt."""
    host_is_ip = is_ip_literal(details.host)
    lines = [
        f"Container: {details.container}",
        f"Host:port: {details.host}:{details.port}",
    ]
    if host_is_ip:
        lines.append("Destination: IP address (manual CIDR grant required on approve)")
    if details.uid is not None or details.comm is not None:
        uid = details.uid if details.uid is not None else "?"
        comm = details.comm if details.comm is not None else "?"
        lines.append(f"Process: uid={uid} comm={comm}")
    lines.append(f"Request id: {details.request_id}")
    lines.append(f"Hits: {details.hit_count}")
    if details.reason:
        lines.append(f"Reason: {details.reason}")
    lines.append("")
    lines.append(allow_prompt_line(details.host, host_is_ip=host_is_ip))
    lines.append("")
    if host_is_ip:
        lines.append(
            "[a] allow (records approval; add CIDR to manifest manually)   "
            "[d] deny   "
            "[s] skip"
        )
    else:
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
        'tell application "System Events"',
        "activate",
        (
            'set userChoice to button returned of (display dialog dialogMessage '
            'with title dialogTitle buttons {"Deny", "Allow"} default button "Allow" '
            'with icon caution)'
        ),
        "end tell",
        "return userChoice",
        "end run",
    ]
    argv = ["osascript"]
    for line in script_lines:
        argv.extend(["-e", line])
    argv.extend(["--", hostname, message, title])
    return argv


def build_notification_argv(
    *,
    title: str,
    message: str,
    hostname: str,
) -> list[str]:
    """osascript argv for a Notification Center banner; untrusted fields after --."""
    script_lines = [
        "on run argv",
        "set hostName to item 1 of argv",
        "set noteMessage to item 2 of argv",
        "set noteTitle to item 3 of argv",
        'display notification noteMessage with title noteTitle sound name "Ping"',
        "end run",
    ]
    argv = ["osascript"]
    for line in script_lines:
        argv.extend(["-e", line])
    argv.extend(["--", hostname, message, title])
    return argv


def build_notification_message(details: RequestDetails) -> tuple[str, str]:
    """Return (title, message) for the macOS notification banner."""
    title = "Egress approval"
    note_message = f"{details.container} wants {details.host}:{details.port}"
    if details.reason:
        note_message += f" — {details.reason}"
    return title, note_message


def build_dialog_message(details: RequestDetails) -> tuple[str, str]:
    """Return (title, message) for the macOS dialog."""
    title = "Egress approval"
    host_is_ip = is_ip_literal(details.host)
    message = (
        f"{details.container} wants {details.host}:{details.port}\n"
        f"{allow_prompt_line(details.host, host_is_ip=host_is_ip)}"
    )
    if host_is_ip:
        message += f"\n\nOn approve: {IP_APPLY_FAILED_REASON}"
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
) -> str | None:
    """Invoke broker.decide() for the operator choice (skip is a no-op).

    Returns an error reason when allow could not be applied; None on success.
    """
    if choice.action == "skip":
        return None
    if choice.action == "deny":
        broker.decide(request_id, "deny")
        return None
    if choice.action == "allow_live":
        return broker.decide(request_id, "allow", scope="live")
    if choice.action == "allow_manifest":
        return broker.decide(request_id, "allow", scope="manifest")
    raise ValueError(f"unknown operator action {choice.action!r}")


def format_apply_failure(reason: str) -> str:
    """Operator-facing line when allow-egress.sh did not install a rule."""
    if reason == IP_REQUIRES_CIDR_REASON:
        return (
            "Egress rule NOT applied: destination is an IP address — "
            f"{IP_APPLY_FAILED_REASON}"
        )
    return f"Egress rule NOT applied ({reason}) — request remains open for retry"


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
        notify_runner: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._broker = broker
        self._log = log
        self._input_fn = input_fn or input
        self._output = output or sys.stdout
        self._poll_interval = poll_interval
        self._platform = platform if platform is not None else sys.platform
        self._popen_factory = popen_factory or subprocess.Popen
        self._notify_runner = notify_runner
        self._deferred: set[str] = set()
        self._stop = threading.Event()

    def _run_notification(self, argv: list[str], *, request_id: str = "") -> None:
        LOG.info("egress watch notification dispatch request_id=%s", request_id)
        start = time.monotonic()
        status: str | int
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            status = result.returncode
        except OSError as exc:
            LOG.warning("egress watch notification failed reason=%s", exc)
            status = "error"
        except subprocess.TimeoutExpired:
            LOG.warning("egress watch notification failed reason=%s", "timeout")
            status = "timeout"
        duration_ms = int((time.monotonic() - start) * 1000)
        LOG.info(
            "egress watch notification done request_id=%s status=%s duration_ms=%d",
            request_id,
            status,
            duration_ms,
        )

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
            apply_error = apply_operator_choice(self._broker, request_id, choice)
            if apply_error is not None:
                self._output.write(format_apply_failure(apply_error) + "\n")
                self._output.flush()
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
        choice_queue: queue.Queue[OperatorChoice] = queue.Queue(maxsize=1)
        dialog_proc: list[subprocess.Popen[str] | None] = [None]
        resolved = threading.Event()

        def terminal_thread() -> None:
            while True:
                raw = self._input_fn("")
                choice = parse_terminal_choice(raw)
                if choice is not None:
                    try:
                        choice_queue.put_nowait(choice)
                    except queue.Full:
                        pass
                    return

        def notification_thread() -> None:
            # Independent of the dialog: a stalled Notification Center must
            # never delay the dialog, so this runs on its own thread and is
            # never joined (daemon; it logs its own completion).
            note_title, note_message = build_notification_message(details)
            notify_argv = build_notification_argv(
                title=note_title,
                message=note_message,
                hostname=details.host,
            )
            try:
                if self._notify_runner is not None:
                    self._notify_runner(notify_argv)
                else:
                    self._run_notification(notify_argv, request_id=details.request_id)
            except Exception as exc:
                LOG.warning("egress watch notification failed reason=%s", exc)

        def dialog_thread() -> None:
            title, message = build_dialog_message(details)
            # A terminal answer can land before this thread gets scheduled;
            # spawning the dialog then would orphan it on screen.
            if resolved.is_set():
                LOG.info(
                    "egress watch dialog skipped request_id=%s reason=already_resolved",
                    details.request_id,
                )
                return
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
            # Re-check after publishing the handle: either the main thread sees
            # the process and terminates it, or this thread sees `resolved`.
            if resolved.is_set():
                _terminate_process(proc)
                return
            try:
                stdout = proc.stdout.read() if proc.stdout is not None else ""
            except (OSError, ValueError):
                stdout = ""
            choice = _dialog_choice_from_output(stdout)
            if choice is not None:
                try:
                    choice_queue.put_nowait(choice)
                except queue.Full:
                    pass

        terminal = threading.Thread(target=terminal_thread, daemon=True)
        notification = threading.Thread(
            target=notification_thread,
            name="egress-notify",
            daemon=True,
        )
        dialog = threading.Thread(target=dialog_thread, daemon=True)
        terminal.start()
        notification.start()
        dialog.start()
        try:
            choice = choice_queue.get()
        except Exception:
            choice = OperatorChoice("skip")
        resolved.set()
        _terminate_process(dialog_proc[0])
        terminal.join(timeout=2)
        dialog.join(timeout=2)
        return choice


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

    tokens_dir = egress_root / TOKENS_DIRNAME
    tokens_dir.mkdir(parents=True, exist_ok=True)
    token_store = BottleTokenStore(tokens_dir)
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
    operator_token = ensure_operator_token(egress_root)
    server = EgressBrokerHTTPServer((host, port), broker, token_store, operator_token)
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

    # Written to the output stream, not the logger: with logging at WARNING the
    # watcher is otherwise completely silent while idle, which reads as hung.
    # This is the operator's only confirmation that approvals will reach them.
    sys.stdout.write(
        f"Watching for egress requests on {host}:{server.server_address[1]}\n"
        f"  queue:  {egress_root}\n"
        f"  keys:   [a] allow (live)  [p] allow + persist  [d] deny  [s] skip\n"
        f"  Ctrl-C to stop. -v for boundary logs.\n\n"
    )
    sys.stdout.flush()

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
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v for boundary logs (INFO), -vv for reads too (DEBUG)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args_ns = parser.parse_args(argv)
    args = args_ns

    # WARNING, not INFO. This is an interactive operator UI that polls the queue
    # roughly twice a second, and the daemon it runs in-process logs boundaries
    # on every read — at INFO those bury the approval prompts the operator is
    # here to answer. Diagnostics move to -v; the prompts themselves are written
    # to the output stream, not the logger, so they are unaffected either way.
    level = logging.DEBUG if getattr(args_ns, "verbose", 0) > 1 else (
        logging.INFO if getattr(args_ns, "verbose", 0) == 1 else logging.WARNING
    )
    logging.basicConfig(level=level, format="%(message)s")
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
