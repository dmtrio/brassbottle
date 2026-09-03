#!/usr/bin/env python3
"""herdr_notify.py — event-driven notification daemon for herdr agent state transitions.

PLN - herdr adoption P2: replacement for the tmux silence hook (tmux-notify.sh)
when remote.shell is herdr. Subscribes to herdr's local socket API and pushes
ntfy notifications on agent state transitions (blocked, done, idle) when no TUI
client is attached.

Configuration via environment (argparse overrides for paths in tests):
  NTFY_URL (required; empty = no-op exit 0)
  NTFY_TOPIC (default: djinn-agents)
  NTFY_TOKEN (optional bearer token)
  CONTAINER_NAME (default: container)
  HERDR_SOCKET (default: ~/.config/herdr/herdr.sock)
  HERDR_CLIENT_SOCKET (default: ~/.config/herdr/herdr-client.sock)
  HERDR_NOTIFY_STATES (default: blocked,done,idle)
  HERDR_NOTIFY_COOLDOWN_SECONDS (default: 30)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator
from urllib.request import Request

LOG = logging.getLogger(__name__)

# Constants
NTFY_TIMEOUT_SECONDS = 5.0
STARTUP_BACKOFF_INITIAL = 1.0
STARTUP_BACKOFF_MAX = 30.0


@dataclass(frozen=True)
class Transition:
    """A detected agent status transition for a pane."""

    pane_id: str
    agent: str
    prev_status: str
    new_status: str
    workspace_id: str


def parse_states(states_str: str) -> list[str]:
    """Parse HERDR_NOTIFY_STATES comma-separated list."""
    if not states_str.strip():
        return []
    return [s.strip() for s in states_str.split(",") if s.strip()]


def count_attached_clients(proc_net_unix_text: str, client_socket_path: str) -> int:
    """Count attached herdr TUI clients from /proc/net/unix.

    Connected sockets have St=03 (connected); listener is St=01.
    One attached TUI = one 03 line for the client socket.
    """
    count = 0
    for line in proc_net_unix_text.splitlines():
        # Columns: Num RefCount Protocol Flags Type St Inode Path
        # Line format: "0000000000000000: 00000002 00000000 00010000 0001 01 12345 /path"
        parts = line.split()
        if len(parts) < 8:
            continue
        # parts[0] = "Num:" or "0000000000000000:"
        # parts[1] = RefCount
        # parts[2] = Protocol
        # parts[3] = Flags
        # parts[4] = Type
        # parts[5] = St
        # parts[6] = Inode
        # parts[7...] = Path
        st = parts[5]
        path = " ".join(parts[7:])  # Handle paths with spaces
        if path == client_socket_path and st == "03":
            count += 1
    return count


def seed_from_snapshot(snapshot: dict[str, Any]) -> dict[str, str]:
    """Extract pane_id -> agent_status mapping from a session snapshot.

    Args:
        snapshot: The "snapshot" dict from session.snapshot response.

    Returns:
        Dict of {pane_id: agent_status}.
    """
    panes: dict[str, str] = {}
    for pane in snapshot.get("panes", []):
        pane_id = pane.get("pane_id")
        status = pane.get("agent_status", "unknown")
        if pane_id:
            panes[pane_id] = status
    return panes


def should_notify(
    prev_status: str, new_status: str, notify_states: list[str]
) -> bool:
    """Check if a status transition should trigger a notification.

    Semantics:
      - idle only notifies if prev was working or blocked (not unknown→idle)
      - blocked and done notify on any transition into them
      - other statuses do not notify
    """
    if new_status not in notify_states:
        return False
    if new_status == "idle":
        # idle only when coming from active states
        return prev_status in ("working", "blocked")
    # blocked and done notify on any transition
    return True


def context_lines(text: str, n: int = 3) -> str:
    """Extract last n non-blank lines, truncated to 200 chars each.

    Returns fallback string if no non-blank lines found.
    """
    if not text:
        return "<no recent output>"
    lines = []
    for line in text.splitlines():
        if line.strip():
            lines.append(line[:200])  # Truncate to 200 chars
    if not lines:
        return "<no recent output>"
    return "\n".join(lines[-n:])


def build_payload(
    *,
    pane_id: str,
    agent: str,
    status: str,
    workspace_label_or_id: str,
    context: str,
    topic: str,
    container_name: str,
) -> dict[str, Any]:
    """Build the ntfy JSON payload for an agent state transition.

    Args:
        pane_id: The pane identifier (e.g., "w1:p1").
        agent: The agent name (e.g., "claude") or "agent" if unknown.
        status: The new status (blocked, done, idle).
        workspace_label_or_id: Workspace label or id for context.
        context: The last lines of pane output (or fallback).
        topic: The ntfy topic.
        container_name: The container name for the title prefix.

    Returns:
        Dict ready for JSON encoding and POST to ntfy.
    """
    priority = 4 if status == "blocked" else 3
    return {
        "topic": topic,
        "title": f"djinn-{container_name}: {agent} {status}",
        "message": f"{workspace_label_or_id} · {pane_id}\n{context}",
        "priority": priority,
        "tags": ["robot"],
    }


class PaneTracker:
    """Tracks pane state and detects transitions.

    Maintains a map of pane_id -> agent_status and records the last time
    each pane was pushed for cooldown suppression.
    """

    def __init__(self) -> None:
        self._panes: dict[str, str] = {}  # pane_id -> agent_status
        self._last_push: dict[str, float] = {}  # pane_id -> time.monotonic()
        self._needs_resubscribe = False

    def adopt(self, pane_id: str, agent_status: str) -> bool:
        """Record a newly discovered pane.

        Args:
            pane_id: The pane identifier.
            agent_status: The initial status.

        Returns:
            True if this pane is new (not previously known).
        """
        is_new = pane_id not in self._panes
        self._panes[pane_id] = agent_status
        return is_new

    def forget(self, pane_id: str) -> None:
        """Forget a closed pane."""
        self._panes.pop(pane_id, None)
        self._last_push.pop(pane_id, None)

    def apply_status(
        self,
        pane_id: str,
        agent: str,
        agent_status: str,
        workspace_id: str,
    ) -> Transition | None:
        """Process a status change for a pane.

        If the pane is unknown, it is adopted. If the status changed,
        return a Transition; otherwise return None.

        Args:
            pane_id: The pane identifier.
            agent: The agent name.
            agent_status: The new agent status.
            workspace_id: The workspace id.

        Returns:
            Transition if status changed, else None.
        """
        prev_status = self._panes.get(pane_id, "unknown")
        if pane_id not in self._panes:
            self._panes[pane_id] = agent_status
            # No transition: new pane is adopted but not notified
            return None
        if prev_status == agent_status:
            return None
        self._panes[pane_id] = agent_status
        return Transition(
            pane_id=pane_id,
            agent=agent,
            prev_status=prev_status,
            new_status=agent_status,
            workspace_id=workspace_id,
        )

    def record_push(self, pane_id: str) -> None:
        """Record that a notification was pushed for this pane."""
        self._last_push[pane_id] = time.monotonic()

    def time_since_push(self, pane_id: str) -> float:
        """Return seconds since last push, or infinity if never pushed."""
        if pane_id not in self._last_push:
            return float("inf")
        return time.monotonic() - self._last_push[pane_id]

    def is_known(self, pane_id: str) -> bool:
        """Check if a pane is already tracked."""
        return pane_id in self._panes

    def mark_resubscribe_needed(self) -> None:
        """Mark that the subscription list is stale (new pane appeared)."""
        self._needs_resubscribe = True

    def needs_resubscribe(self) -> bool:
        """Check if a re-subscribe is needed."""
        return self._needs_resubscribe

    def clear_resubscribe_flag(self) -> None:
        """Clear the re-subscribe flag after re-subscribing."""
        self._needs_resubscribe = False


class HerdrClient:
    """Low-level herdr socket client.

    Implements the wire protocol: each request opens a fresh connection,
    except for events.subscribe which streams events on a single connection.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a single request and return the response.

        Opens a fresh connection for each request (per herdr protocol).

        Args:
            method: The method name (e.g., "session.snapshot").
            params: The parameters dict.

        Returns:
            The response dict (result or error).

        Raises:
            OSError: If socket operations fail.
            json.JSONDecodeError: If response is not valid JSON.
        """
        request_id = str(int(time.time() * 1000000))
        request_obj = {
            "id": request_id,
            "method": method,
            "params": params,
        }
        request_text = json.dumps(request_obj, separators=(",", ":")) + "\n"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._socket_path)
            sock.sendall(request_text.encode("utf-8"))
            response_text = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_text += chunk
        finally:
            sock.close()

        response_text_str = response_text.decode("utf-8").strip()
        if not response_text_str:
            raise OSError("herdr server closed connection without response")
        response = json.loads(response_text_str)
        return response

    def subscribe(
        self, subscriptions: list[dict[str, Any]]
    ) -> Generator[dict[str, Any], None, None]:
        """Subscribe to herdr events and stream event envelopes.

        Yields event envelopes from the server until the stream closes.

        Args:
            subscriptions: List of subscription specs.

        Yields:
            Event envelope dicts with "event" and "data" keys.

        Raises:
            OSError: If socket operations fail.
            json.JSONDecodeError: If an event is not valid JSON.
        """
        request_id = str(int(time.time() * 1000000))
        request_obj = {
            "id": request_id,
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        request_text = json.dumps(request_obj, separators=(",", ":")) + "\n"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._socket_path)
            sock.sendall(request_text.encode("utf-8"))

            # Read the subscription_started response
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("herdr server closed connection during subscribe")
                buffer += chunk

            line_end = buffer.find(b"\n")
            response = json.loads(buffer[:line_end].decode("utf-8"))
            buffer = buffer[line_end + 1 :]
            if "error" in response:
                # e.g. a pane_id that no longer exists: the server answers with
                # an error and would otherwise leave us waiting on a stream
                # that never starts.
                raise OSError(
                    "herdr subscribe rejected: "
                    f"{response['error'].get('message', 'unknown error')}"
                )

            # Stream events until closed
            while True:
                while b"\n" not in buffer:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return
                    buffer += chunk

                line_end = buffer.find(b"\n")
                event_line = buffer[:line_end].decode("utf-8")
                buffer = buffer[line_end + 1 :]
                event = json.loads(event_line)
                yield event
        finally:
            sock.close()


class NtfySink:
    """Send ntfy notifications."""

    def __init__(
        self,
        ntfy_url: str,
        topic: str,
        token: str | None = None,
        *,
        urlopen: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self._ntfy_url = ntfy_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._urlopen = urlopen

    def send(self, payload: dict[str, Any]) -> str:
        """Send a notification payload to ntfy.

        Args:
            payload: The ntfy payload dict.

        Returns:
            Status string ("ok" or error code/type).
        """
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(
            f"{self._ntfy_url}/",
            data=body,
            headers=headers,
            method="POST",
        )

        LOG.info("herdr notify dispatch bytes=%d", len(body))
        started = time.monotonic()
        status: str
        try:
            with self._urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
                status = str(getattr(response, "status", getattr(response, "code", "ok")))
        except urllib.error.HTTPError as exc:
            status = str(exc.code)
            LOG.warning("herdr notify failed reason=HTTPError status=%s", exc.code)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            status = "timeout" if isinstance(exc, socket.timeout) else "error"
            LOG.warning(
                "herdr notify failed reason=%s",
                exc.__class__.__name__,
            )
        LOG.info(
            "herdr notify done status=%s duration_ms=%d",
            status,
            int((time.monotonic() - started) * 1000),
        )
        return status


def _adopt_new_pane(
    tracker: PaneTracker, pane_id: str, agent_status: str, event_kind: str
) -> None:
    """Record a pane first seen through a broadcast event.

    Global subscriptions replay history on subscribe, so an event for a pane
    the snapshot already covered is replay and is ignored. A genuinely new
    pane is not covered by the per-pane status subscription yet, so the
    cycle must end and re-subscribe.
    """
    if tracker.is_known(pane_id):
        LOG.debug("herdr notify replay pane_id=%s event=%s", pane_id, event_kind)
        return
    tracker.adopt(pane_id, agent_status)
    LOG.info(
        "herdr notify new_pane pane_id=%s agent_status=%s event=%s",
        pane_id,
        agent_status,
        event_kind,
    )
    tracker.mark_resubscribe_needed()


def _handle_status_change(
    *,
    client: HerdrClient,
    sink: NtfySink,
    tracker: PaneTracker,
    data: dict[str, Any],
    workspace_labels: dict[str, str],
    herdr_client_socket: str,
    proc_net_unix_path: str,
    ntfy_topic: str,
    container_name: str,
    notify_states: list[str],
    cooldown_seconds: float,
) -> None:
    """Decide on, and possibly dispatch, one agent status change."""
    pane_id = data.get("pane_id")
    agent_status = data.get("agent_status")
    agent = data.get("agent") or "agent"
    workspace_id = data.get("workspace_id", "")
    if not pane_id or not agent_status:
        return

    transition = tracker.apply_status(pane_id, agent, agent_status, workspace_id)
    if transition is None:
        LOG.debug("herdr notify pane_id=%s status=%s no_transition", pane_id, agent_status)
        return
    LOG.debug(
        "herdr notify transition pane_id=%s prev=%s new=%s",
        pane_id,
        transition.prev_status,
        transition.new_status,
    )
    if not should_notify(transition.prev_status, transition.new_status, notify_states):
        LOG.debug(
            "herdr notify pane_id=%s status=%s not_in_notify_states",
            pane_id,
            transition.new_status,
        )
        return

    time_since = tracker.time_since_push(pane_id)
    if time_since < cooldown_seconds:
        LOG.info(
            "herdr notify suppressed pane_id=%s reason=cooldown seconds_remaining=%.1f",
            pane_id,
            cooldown_seconds - time_since,
        )
        return

    try:
        proc_net_unix_text = Path(proc_net_unix_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        LOG.warning(
            "herdr notify failed to read /proc/net/unix reason=%s",
            exc.__class__.__name__,
        )
        proc_net_unix_text = ""
    attached = count_attached_clients(proc_net_unix_text, herdr_client_socket)
    if attached > 0:
        LOG.info(
            "herdr notify suppressed pane_id=%s reason=client_attached count=%d",
            pane_id,
            attached,
        )
        return

    try:
        response = client.request(
            "pane.read",
            {"pane_id": pane_id, "source": "visible", "format": "text", "strip_ansi": True},
        )
        pane_text = response.get("result", {}).get("text", "")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        LOG.warning(
            "herdr notify failed to read pane_id=%s reason=%s",
            pane_id,
            exc.__class__.__name__,
        )
        pane_text = ""

    payload = build_payload(
        pane_id=pane_id,
        agent=agent,
        status=transition.new_status,
        workspace_label_or_id=workspace_labels.get(workspace_id, workspace_id or "workspace"),
        context=context_lines(pane_text),
        topic=ntfy_topic,
        container_name=container_name,
    )
    status = sink.send(payload)
    tracker.record_push(pane_id)
    LOG.info(
        "herdr notify sent pane_id=%s agent=%s status=%s sink_status=%s",
        pane_id,
        agent,
        transition.new_status,
        status,
    )


def run_once(
    *,
    herdr_socket: str,
    herdr_client_socket: str,
    proc_net_unix_path: str,
    ntfy_url: str,
    ntfy_topic: str,
    ntfy_token: str | None,
    container_name: str,
    notify_states: list[str],
    cooldown_seconds: float,
    urlopen: Callable[..., object] = urllib.request.urlopen,
    tracker: PaneTracker | None = None,
) -> str:
    """Run one subscription cycle: snapshot, subscribe, process events.

    Returns why the cycle ended: "resubscribe" (a pane appeared that the
    per-pane subscription list does not cover — call again right away),
    "stream_closed" (the server closed or errored the stream — wait for it
    to come back), or "snapshot_failed" (could not even seed — same).

    `tracker` carries pane statuses and cooldowns across cycles; a fresh one
    is created when omitted (tests).
    """
    client = HerdrClient(herdr_socket)
    sink = NtfySink(ntfy_url, ntfy_topic, ntfy_token, urlopen=urlopen)
    if tracker is None:
        tracker = PaneTracker()
    tracker.clear_resubscribe_flag()
    workspace_labels: dict[str, str] = {}

    try:
        response = client.request("session.snapshot", {})
        snapshot = response.get("result", {}).get("snapshot", {})
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        LOG.error("herdr notify failed to get snapshot reason=%s", exc.__class__.__name__)
        return "snapshot_failed"

    initial_panes = seed_from_snapshot(snapshot)
    for pane_id, status in initial_panes.items():
        tracker.adopt(pane_id, status)
    for ws in snapshot.get("workspaces", []):
        ws_id = ws.get("workspace_id")
        label = ws.get("label")
        if ws_id and label:
            workspace_labels[ws_id] = label

    subscriptions: list[dict[str, Any]] = [
        {"type": "pane.agent_status_changed", "pane_id": pane_id} for pane_id in initial_panes
    ]
    subscriptions.extend(
        [
            {"type": "pane.created"},
            {"type": "pane.agent_detected"},
            {"type": "pane.closed"},
            {"type": "pane.exited"},
        ]
    )
    LOG.info("herdr notify subscribed panes=%d", len(initial_panes))

    try:
        for event_envelope in client.subscribe(subscriptions):
            event_kind = event_envelope.get("event")
            data = event_envelope.get("data", {})

            if event_kind in ("pane_created", "pane.created"):
                pane_data = data.get("pane", {})
                pane_id = pane_data.get("pane_id") or data.get("pane_id")
                if pane_id:
                    _adopt_new_pane(
                        tracker, pane_id, pane_data.get("agent_status", "unknown"), event_kind
                    )
            elif event_kind in ("pane_agent_detected", "pane.agent_detected"):
                pane_id = data.get("pane_id")
                if pane_id:
                    _adopt_new_pane(tracker, pane_id, "unknown", event_kind)
            elif event_kind in ("pane_closed", "pane.closed", "pane_exited", "pane.exited"):
                pane_id = data.get("pane_id")
                if pane_id:
                    tracker.forget(pane_id)
                    LOG.debug("herdr notify pane_gone pane_id=%s event=%s", pane_id, event_kind)
            elif event_kind in ("pane_agent_status_changed", "pane.agent_status_changed"):
                _handle_status_change(
                    client=client,
                    sink=sink,
                    tracker=tracker,
                    data=data,
                    workspace_labels=workspace_labels,
                    herdr_client_socket=herdr_client_socket,
                    proc_net_unix_path=proc_net_unix_path,
                    ntfy_topic=ntfy_topic,
                    container_name=container_name,
                    notify_states=notify_states,
                    cooldown_seconds=cooldown_seconds,
                )

            # Checked after every event, not before the next one: a lone
            # pane_created must end the cycle now, not once something else
            # happens to arrive.
            if tracker.needs_resubscribe():
                LOG.info("herdr notify resubscribe_needed=true closing stream")
                return "resubscribe"
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("herdr notify subscribe failed reason=%s", exc.__class__.__name__)
        return "stream_closed"

    LOG.info("herdr notify stream closed by server")
    return "stream_closed"


def wait_for_server(herdr_socket: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Block until the herdr API socket answers ping.

    The herdr server starts on the first login, so waiting minutes at boot is
    normal; it also goes away across `herdr server stop` / upgrades.
    """
    backoff = STARTUP_BACKOFF_INITIAL
    while True:
        try:
            response = HerdrClient(herdr_socket).request("ping", {})
            version = response.get("result", {}).get("version", "unknown")
            LOG.info("herdr notify connected version=%s", version)
            return
        except (OSError, json.JSONDecodeError) as exc:
            LOG.info(
                "herdr notify waiting reason=%s backoff=%0.1f",
                exc.__class__.__name__,
                backoff,
            )
            sleep(backoff)
            backoff = min(backoff * 2, STARTUP_BACKOFF_MAX)


def main(argv: list[str] | None = None) -> None:
    """Main entry point: startup loop with ping retry, then run_once cycles."""
    parser = argparse.ArgumentParser(
        description="herdr event-driven notification daemon"
    )
    parser.add_argument("--socket", default=None, help="herdr socket path override")
    parser.add_argument(
        "--client-socket", default=None, help="herdr client socket path override"
    )
    parser.add_argument(
        "--proc-net-unix", default=None, help="/proc/net/unix path override"
    )
    args = parser.parse_args(argv)

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s herdr-notify %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Load config from environment
    ntfy_url = os.environ.get("NTFY_URL", "").strip()
    if not ntfy_url:
        LOG.info("herdr notify disabled reason=NTFY_URL_empty")
        sys.exit(0)

    ntfy_topic = os.environ.get("NTFY_TOPIC", "djinn-agents").strip()
    ntfy_token = os.environ.get("NTFY_TOKEN", "").strip() or None
    container_name = os.environ.get("CONTAINER_NAME", "container").strip()
    states_str = os.environ.get("HERDR_NOTIFY_STATES", "blocked,done,idle").strip()
    notify_states = parse_states(states_str)

    try:
        cooldown_seconds = float(
            os.environ.get("HERDR_NOTIFY_COOLDOWN_SECONDS", "30")
        )
    except ValueError:
        cooldown_seconds = 30.0

    # Resolve socket paths
    home = Path.home()
    herdr_socket = args.socket or str(home / ".config" / "herdr" / "herdr.sock")
    herdr_client_socket = args.client_socket or str(
        home / ".config" / "herdr" / "herdr-client.sock"
    )
    proc_net_unix_path = args.proc_net_unix or "/proc/net/unix"

    def _stop(signum: int, _frame: object) -> None:
        LOG.info("herdr notify stopping signal=%d", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    LOG.info(
        "herdr notify starting socket=%s states=%s cooldown_s=%.0f",
        herdr_socket,
        ",".join(notify_states),
        cooldown_seconds,
    )
    tracker = PaneTracker()
    wait_for_server(herdr_socket)
    last_cycle = 0.0
    while True:
        # A re-subscribe (new pane) restarts immediately, capped at one per
        # second; anything else means the server went away, so go back to
        # the ping/backoff wait rather than hammering a dead socket.
        elapsed = time.monotonic() - last_cycle
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        last_cycle = time.monotonic()
        try:
            reason = run_once(
                herdr_socket=herdr_socket,
                herdr_client_socket=herdr_client_socket,
                proc_net_unix_path=proc_net_unix_path,
                ntfy_url=ntfy_url,
                ntfy_topic=ntfy_topic,
                ntfy_token=ntfy_token,
                container_name=container_name,
                notify_states=notify_states,
                cooldown_seconds=cooldown_seconds,
                tracker=tracker,
            )
        except Exception as exc:  # never die on one bad cycle
            LOG.warning("herdr notify cycle failed reason=%s", exc.__class__.__name__)
            reason = "cycle_failed"
        if reason != "resubscribe":
            wait_for_server(herdr_socket)


if __name__ == "__main__":
    main()
