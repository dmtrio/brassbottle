#!/usr/bin/env python3
"""egress_watch.py — interactive egress approval watcher (operator UI).

Runs the egress broker daemon in-process and presents each queued request to
the operator via a terminal prompt (and on macOS, a parallel dialog).
Stdlib only; host-side (macOS and Linux).
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from egress_broker_host import (
    CONFIG_FILENAME,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PORT,
    DENYLIST_PERSIST_FAILED_REASON,
    IP_APPLY_FAILED_REASON,
    IP_REQUIRES_CIDR_REASON,
    LOCK_FILENAME,
    TOKENS_DIRNAME,
    BottleTokenStore,
    DaemonAlreadyRunning,
    DaemonLock,
    EgressBroker,
    EgressBrokerHostError,
    EgressBrokerHTTPServer,
    _connect_host_for_bind,
    _load_config,
    _repo_root,
    _stale_sweep_loop,
    ensure_operator_token,
    is_ip_literal,
    remove_daemon_endpoint,
    resolve_base_path,
    resolve_egress_root,
    undeny_hint,
    write_daemon_endpoint,
)
from egress_denylist import DenyEntry, DenyList
from egress_log import (
    EgressLog,
    RequestDetails,
    request_details_for_ids,
    request_details_from_log,
)

from egress_notify import (
    NtfyNotifier,
    allow_prompt_line,
    load_ntfy_settings,
    ntfy_server_hostname,
)

LOG = logging.getLogger(__name__)

InputFn = Callable[[str], str]
PopenFactory = Callable[..., subprocess.Popen[str]]


@dataclass(frozen=True)
class OperatorChoice:
    """Terminal or dialog selection for one request."""

    action: str  # allow_live | allow_manifest | deny | deny_bottle | deny_global | skip


# Shared by both branches of format_request_block below (finding: cleanup)
# — the IP and non-IP prompts differ only in their [a]/[p] lead-in, so the
# [d]/[D]/[G]/[s] tail must stay in exactly one place or the two branches
# can silently drift apart.
_DENY_KEYS_LINE = (
    "[d] deny   "
    "[D] deny always: this host, this bottle   "
    "[G] deny always: this host, all bottles   "
    "[s] skip"
)


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
            + _DENY_KEYS_LINE
        )
    else:
        lines.append(
            "[a] allow (live only)   "
            "[p] allow + persist to manifest   " + _DENY_KEYS_LINE
        )
    return "\n".join(lines)


DIALOG_TITLE = "Egress approval"


def _request_summary(details: RequestDetails) -> str:
    return f"{details.container} wants {details.host}:{details.port}"


def _osascript_argv(script_lines: list[str], *untrusted: str) -> list[str]:
    """osascript argv: one -e per script line, untrusted fields only after --."""
    argv = ["osascript"]
    for line in script_lines:
        argv.extend(["-e", line])
    if untrusted:
        argv.append("--")
        argv.extend(untrusted)
    return argv


def parse_terminal_choice(raw: str) -> OperatorChoice | None:
    """Map one keypress to an operator choice; None means re-prompt.

    d/D is the only case-sensitive pair — there is no lowercase g: `d` is
    the narrow, reversible action (deny this one, once); `D` is the wider,
    harder-to-undo "deny always: this host, this bottle"; `G` (uppercase
    only) is wider still, "deny always: this host, all bottles". a/p/s stay
    case-insensitive since they have no such split.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    ch = stripped[0]
    if ch in ("a", "A"):
        return OperatorChoice("allow_live")
    if ch in ("p", "P"):
        return OperatorChoice("allow_manifest")
    if ch == "d":
        return OperatorChoice("deny")
    if ch == "D":
        return OperatorChoice("deny_bottle")
    if ch == "G":
        return OperatorChoice("deny_global")
    if ch in ("s", "S"):
        return OperatorChoice("skip")
    return None


def build_osascript_argv(
    *,
    title: str,
    message: str,
) -> list[str]:
    """Build an osascript argv list with untrusted fields passed only after --."""
    script_lines = [
        "on run argv",
        "set dialogMessage to item 1 of argv",
        "set dialogTitle to item 2 of argv",
        "activate",
        (
            'set userChoice to button returned of (display dialog dialogMessage '
            'with title dialogTitle buttons {"Deny", "Deny always", "Allow"} '
            'default button "Deny" with icon caution)'
        ),
        "return userChoice",
        "end run",
    ]
    return _osascript_argv(script_lines, message, title)


def build_notification_argv(
    *,
    title: str,
    message: str,
) -> list[str]:
    """osascript argv for a Notification Center banner; untrusted fields after --."""
    script_lines = [
        "on run argv",
        "set noteMessage to item 1 of argv",
        "set noteTitle to item 2 of argv",
        'display notification noteMessage with title noteTitle sound name "Ping"',
        "end run",
    ]
    return _osascript_argv(script_lines, message, title)


def build_notification_message(details: RequestDetails) -> tuple[str, str]:
    """Return (title, message) for the macOS notification banner."""
    note_message = _request_summary(details)
    if details.reason:
        note_message += f" — {details.reason}"
    return DIALOG_TITLE, note_message


DENY_ALWAYS_SCOPE_LINE = (
    "Deny always = this host, this bottle (persistent; undo with ./djinn undeny)"
)


def build_dialog_message(details: RequestDetails) -> tuple[str, str]:
    """Return (title, message) for the macOS dialog."""
    host_is_ip = is_ip_literal(details.host)
    message = (
        f"{_request_summary(details)}\n"
        f"{allow_prompt_line(details.host, host_is_ip=host_is_ip)}\n\n"
        f"{DENY_ALWAYS_SCOPE_LINE}"
    )
    if host_is_ip:
        message += f"\n\nOn approve: {IP_APPLY_FAILED_REASON}"
    return DIALOG_TITLE, message


def apply_operator_choice(
    broker: EgressBroker,
    request_id: str,
    choice: OperatorChoice,
    details: RequestDetails,
) -> tuple[str | None, DenyEntry | None]:
    """Invoke broker.decide()/persist_deny() for the operator choice.

    Returns (error, persisted_entry). error is set when an allow could not
    be applied, or a persistent deny's write failed (unchanged single-value
    contract otherwise — format_apply_failure renders either case).
    persisted_entry is set only for deny_bottle/deny_global on success, so
    the caller can print an acknowledgment naming what was written.
    """
    if choice.action == "skip":
        return None, None
    if choice.action == "deny":
        broker.decide(request_id, "deny")
        return None, None
    if choice.action in ("deny_bottle", "deny_global"):
        # Routed through persist_deny (not decide(request_id, ..., scope=...)
        # directly): it writes the entry for details.host regardless of
        # what else is open, then sweeps every open request it now covers —
        # this one included — across every container for deny_global, just
        # details.container for deny_bottle. See EgressBroker.persist_deny.
        scope = "bottle" if choice.action == "deny_bottle" else "global"
        # details.reason is the requesting AGENT's justification for why it
        # wants the host — not the operator's. persist_deny's `reason` is an
        # operator free-text field (kept distinct from denylist_zone/scope
        # in the audit — see decide()'s docstring); passing the agent's text
        # through as if the operator wrote it would misattribute it in the
        # audit log, so this always passes reason=None from the watcher.
        result = broker.persist_deny(
            details.host,
            scope,
            container=details.container,
            reason=None,
            trigger_request_id=request_id,
        )
        return result.error, result.entry
    if choice.action == "allow_live":
        return broker.decide(request_id, "allow", scope="live"), None
    if choice.action == "allow_manifest":
        return broker.decide(request_id, "allow", scope="manifest"), None
    raise ValueError(f"unknown operator action {choice.action!r}")


def format_apply_failure(reason: str) -> str:
    """Operator-facing line when allow-egress.sh did not install a rule, or
    a scope=bottle|global deny's denylist write itself failed."""
    if reason == IP_REQUIRES_CIDR_REASON:
        return (
            "Egress rule NOT applied: destination is an IP address — "
            f"{IP_APPLY_FAILED_REASON}"
        )
    if reason == DENYLIST_PERSIST_FAILED_REASON:
        return (
            f"Denied once; deny-list entry NOT written ({reason}) — "
            "check $DJINN_HOME/run/egress"
        )
    return f"Egress rule NOT applied ({reason}) — request remains open for retry"


def format_denylist_ack(entry: DenyEntry) -> str:
    """Operator-facing acknowledgment after ANY persistent deny (watcher
    D/G, or /decide with scope!=once) actually writes an entry."""
    return (
        f"Deny-list entry written: zone={entry.zone} scope={entry.scope}"
        f" — undo with: {undeny_hint(entry.zone, entry.scope)}"
    )


def format_denylist_status(denylist: DenyList) -> str:
    """One-line deny-list status for the startup banner and each prompt
    block (finding #8b): a corrupt denylist.json must never fail silently —
    entries stop being applied (every request resumes prompting, exactly as
    if the deny list were empty) and this is the one place that says so,
    right where the operator is already looking."""
    entries = denylist.load()
    if denylist.corrupt is not None:
        return f"Deny list: CORRUPT ({denylist.corrupt}) — entries NOT applied, prompts will resume"
    return f"Deny list: {len(entries)} entries"


def _dialog_choice_from_output(output: str) -> OperatorChoice | None:
    text = output.strip()
    if text == "Allow":
        return OperatorChoice("allow_live")
    if text == "Deny always":
        # osascript's three-button ceiling: "always" here means this bottle
        # only. Global denylist entries stay a terminal/CLI-only gesture,
        # deliberately the harder one (PLN "Operator surfaces").
        return OperatorChoice("deny_bottle")
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
        self._notified: set[str] = set()
        self._stop = threading.Event()

    def _run_notification(
        self,
        argv: list[str],
        *,
        request_id: str = "",
        run_fn: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        LOG.info("egress watch notification dispatch request_id=%s", request_id)
        start = time.monotonic()
        status: str | int
        try:
            result = run_fn(
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
            self._notified.clear()
            return False

        # Forget state for requests that are no longer open, so a host that is
        # deferred, decided, and then re-filed prompts again — and neither set
        # grows without bound across a long watch.
        still_open = set(open_ids)
        self._deferred &= still_open
        self._notified &= still_open

        # ONE pass over the log for the whole poll. Asking for details per
        # request would make each poll O(open requests x log size) — with a
        # backlogged queue and a large monthly log that delays the very
        # prompt and banners this loop exists to deliver.
        details_by_id = request_details_for_ids(
            self._log, open_ids, queue=queue, now=when
        )

        # Announce EVERY open request as soon as it appears. This used to live
        # inside _prompt_with_dialog, which meant the banner for request B did
        # not fire until the operator had answered request A — and an
        # unanswerable request (an IP literal) suppressed every banner behind
        # it indefinitely.
        for request_id in open_ids:
            if request_id in self._notified:
                continue
            details = details_by_id.get(request_id)
            if details is None:
                continue
            self._notified.add(request_id)
            self._notify_operator_banner(details)

        for request_id in open_ids:
            if request_id in self._deferred:
                continue
            details = details_by_id.get(request_id)
            if details is None:
                continue
            choice = self._prompt_operator(details)
            if choice.action == "skip":
                self._deferred.add(request_id)
                return True
            try:
                apply_error, persisted_entry = apply_operator_choice(
                    self._broker, request_id, choice, details
                )
            except EgressBrokerHostError as exc:
                # finding #4: e.g. a request whose container is literally
                # "global" hitting D/G — persist_deny's own
                # validate_bottle_scope call raises before writing anything,
                # so nothing was decided: the request stays open (it will be
                # re-prompted on the next poll) rather than the watcher
                # crashing on an uncaught exception.
                self._output.write(f"Error: {exc}\n")
                self._output.flush()
                self._deferred.discard(request_id)
                return True
            if apply_error is not None:
                self._output.write(format_apply_failure(apply_error) + "\n")
                self._output.flush()
                if apply_error == IP_REQUIRES_CIDR_REASON:
                    # decide() logs apply_failed and leaves the request OPEN,
                    # because an IP literal can only be granted by editing the
                    # manifest. Without this the next 0.5s poll re-prompts the
                    # same id, forever: neither [a] nor [s] could clear an IP
                    # request. Defer it — the operator has been told what to
                    # edit, and the request is answerable again after a
                    # restart or once it is decided some other way.
                    self._deferred.add(request_id)
                    self._output.flush()
                    return True
            if persisted_entry is not None:
                self._output.write(format_denylist_ack(persisted_entry) + "\n")
                self._output.flush()
            self._deferred.discard(request_id)
            return True

        # Every open request is deferred: go quiet and let the poll idle.
        # This used to clear _deferred first, which made [s] last exactly one
        # 0.5s poll — the skipped request was re-prompted immediately and the
        # operator could not get out of it. Deferrals are released instead
        # when the queue drains, at the top of this method.
        return False

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                time.sleep(self._poll_interval)

    def _notify_operator_banner(self, details: RequestDetails) -> None:
        """Fire the macOS Notification Center banner for one open request.

        Runs on its own daemon thread and is never joined: a stalled
        Notification Center must not hold up the poll loop or the prompt.
        Non-darwin hosts have no banner to show, so this is a no-op there
        (ntfy push is dispatched daemon-side at file time, independent of
        this watcher — see EgressBroker._dispatch_notifier).
        """
        if self._platform != "darwin":
            return

        def run() -> None:
            note_title, note_message = build_notification_message(details)
            notify_argv = build_notification_argv(
                title=note_title,
                message=note_message,
            )
            try:
                if self._notify_runner is not None:
                    self._notify_runner(notify_argv)
                else:
                    self._run_notification(notify_argv, request_id=details.request_id)
            except Exception as exc:
                LOG.warning("egress watch notification failed reason=%s", exc)

        threading.Thread(target=run, name="egress-notify", daemon=True).start()

    def _prompt_operator(self, details: RequestDetails) -> OperatorChoice:
        self._output.write(format_denylist_status(self._broker.denylist) + "\n")
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
                try:
                    raw = self._input_fn("")
                except EOFError:
                    LOG.info("egress watch terminal input closed")
                    return
                choice = parse_terminal_choice(raw)
                if choice is not None:
                    try:
                        choice_queue.put_nowait(choice)
                    except queue.Full:
                        pass
                    return

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
            LOG.info("egress watch dialog spawn request_id=%s", details.request_id)
            argv = build_osascript_argv(title=title, message=message)
            start = time.monotonic()
            try:
                proc = self._popen_factory(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as exc:
                LOG.warning(
                    "egress watch dialog spawn failed request_id=%s reason=%s",
                    details.request_id,
                    exc,
                )
                return
            dialog_proc[0] = proc
            # Re-check after publishing the handle: either the main thread sees
            # the process and terminates it, or this thread sees `resolved`.
            if resolved.is_set():
                _terminate_process(proc)
                LOG.info(
                    "egress watch dialog done request_id=%s status=terminated duration_ms=%d",
                    details.request_id,
                    int((time.monotonic() - start) * 1000),
                )
                return
            try:
                stdout = proc.stdout.read() if proc.stdout is not None else ""
            except (OSError, ValueError):
                stdout = ""
            try:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
            except (OSError, ValueError):
                stderr = ""
            status: str | int | None
            try:
                status = proc.wait()
            except (OSError, ValueError):
                status = getattr(proc, "returncode", None)
            duration_ms = int((time.monotonic() - start) * 1000)
            LOG.info(
                "egress watch dialog done request_id=%s status=%s duration_ms=%d",
                details.request_id,
                status,
                duration_ms,
            )
            choice = _dialog_choice_from_output(stdout)
            if choice is not None:
                try:
                    choice_queue.put_nowait(choice)
                except queue.Full:
                    pass
            else:
                if "User canceled" in stderr:
                    LOG.info(
                        "egress watch dialog dismissed request_id=%s",
                        details.request_id,
                    )
                else:
                    first_line = stderr.splitlines()[0] if stderr else ""
                    LOG.warning(
                        "egress watch dialog gave no answer request_id=%s status=%s stderr=%s",
                        details.request_id,
                        status,
                        first_line[:120],
                    )

        terminal = threading.Thread(target=terminal_thread, daemon=True)
        dialog = threading.Thread(target=dialog_thread, daemon=True)
        terminal.start()
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

    operator_token = ensure_operator_token(egress_root)
    settings = load_ntfy_settings(
        base_path,
        os.environ,
        broker_host=host,
        broker_port=port,
        operator_token=operator_token,
    )
    notifier = None
    if settings is not None:
        ntfy_notifier = NtfyNotifier(settings)
        notifier = ntfy_notifier.send_async

    broker = EgressBroker(
        egress_root,
        repo_root=repo_root or _repo_root(),
        hold_seconds_default=hold_default,
        notifier=notifier,
    )
    server = EgressBrokerHTTPServer((host, port), broker, token_store, operator_token)
    # DaemonLock is already held above, so only one watcher ever writes this.
    write_daemon_endpoint(egress_root, host, server.server_address[1])
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
    listen_url = f"http://{_connect_host_for_bind(host)}:{server.server_address[1]}"

    if settings is not None:
        notify_line = (
            f"  notify: ntfy → {ntfy_server_hostname(settings.url)} "
            f"(actions {'on' if settings.broker_url else 'off'} — bind {host})\n"
        )
    else:
        notify_line = (
            "  notify: terminal only (set NTFY_URL in secrets.env for push)\n"
        )

    # Written to the output stream, not the logger: with logging at WARNING the
    # watcher is otherwise completely silent while idle, which reads as hung.
    # This is the operator's only confirmation that approvals will reach them.
    sys.stdout.write(
        f"Watching for egress requests on {host}:{server.server_address[1]}\n"
        f"  queue:  {egress_root}\n"
        f"  listen: {listen_url}\n"
        f"  {format_denylist_status(broker.denylist)}\n"
        f"{notify_line}"
        f"  keys:   [a] allow (live)  [p] allow + persist  [d] deny  [D] deny always (bottle)  [G] deny always (global)  [s] skip\n"
        f"  D/G are uppercase on purpose: they write a deny-list entry (undo: ./djinn undeny)\n"
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
        remove_daemon_endpoint(egress_root)
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
