#!/usr/bin/env python3
"""egress_notify.py — ntfy push notifications for egress approval requests."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

LOG = logging.getLogger(__name__)

NTFY_DEFAULT_TOPIC = "djinn-agents"
NTFY_TIMEOUT_SECONDS = 5.0
NTFY_PRIORITY = 4
SECRETS_FILENAME = "secrets.env"
NTFY_ENV_NAMES = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN")


@dataclass(frozen=True)
class EgressNotification:
    request_id: str
    container: str
    host: str
    port: int
    host_is_ip: bool = False
    uid: int | None = None
    comm: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class NtfySettings:
    url: str
    topic: str
    token: str | None = None
    broker_url: str | None = None
    operator_token: str | None = None


def allow_prompt_line(host: str, *, host_is_ip: bool = False) -> str:
    """Question line stating zone/subdomain coverage semantics."""
    if host_is_ip:
        return (
            f"Allow traffic to {host}? "
            "(requires ALLOWED_CIDRS in the bottle manifest — not allow-egress.sh)"
        )
    return f"Allow {host} (and everything under it)?"


def read_secrets_env(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Parse named KEY=value entries from a secrets.env-style file."""
    if not path.is_file():
        return {}
    wanted = set(names)
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _bind_unreachable_reason(host: str) -> str | None:
    """Why action buttons cannot target this bind address; None when they can."""
    if host == "localhost":
        return "loopback"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname: assume the device can resolve it
    if addr.is_loopback:
        return "loopback"
    if addr.is_unspecified:
        return "unspecified"  # 0.0.0.0 / :: is not an address a phone can dial
    return None


def ntfy_server_hostname(url: str) -> str:
    """Return the hostname from an ntfy origin URL (for logging only)."""
    return urlparse(url).hostname or ""


def load_ntfy_settings(
    base_path: Path,
    env: Mapping[str, str],
    *,
    broker_host: str,
    broker_port: int,
    operator_token: str,
) -> NtfySettings | None:
    """Load ntfy publish settings; return None when push is disabled."""
    secrets_path = base_path / SECRETS_FILENAME
    from_file = read_secrets_env(secrets_path, NTFY_ENV_NAMES)

    def _value(name: str) -> str:
        return (env.get(name) or from_file.get(name) or "").strip()

    ntfy_url = _value("NTFY_URL")
    if not ntfy_url:
        return None

    if any(c in ntfy_url for c in ('#', '"', "'")):
        LOG.warning("egress notify ntfy settings invalid reason=bad_url")
        return None

    ntfy_url = ntfy_url.rstrip("/")
    topic = _value("NTFY_TOPIC") or NTFY_DEFAULT_TOPIC
    token = _value("NTFY_TOKEN") or None

    broker_url: str | None = None
    op_token: str | None = None
    unreachable = _bind_unreachable_reason(broker_host)
    if unreachable is None:
        broker_url = f"http://{broker_host}:{broker_port}"
        op_token = operator_token
    else:
        LOG.info("egress notify actions off reason=bind_%s", unreachable)

    return NtfySettings(
        url=ntfy_url,
        topic=topic,
        token=token,
        broker_url=broker_url,
        operator_token=op_token,
    )


def _action_body(
    *,
    container: str,
    host: str,
    decision: str,
    scope: str | None = None,
    reason: str | None = None,
) -> str:
    body: dict[str, str] = {
        "container": container,
        "host": host,
        "decision": decision,
    }
    if scope is not None:
        body["scope"] = scope
    if reason is not None:
        body["reason"] = reason
    return json.dumps(body, separators=(",", ":"))


def _http_action(
    *,
    label: str,
    broker_url: str,
    operator_token: str,
    body: str,
) -> dict[str, object]:
    return {
        "action": "http",
        "label": label,
        "method": "POST",
        "url": f"{broker_url}/decide",
        "headers": {
            "Authorization": f"Bearer {operator_token}",
            "Content-Type": "application/json",
        },
        "clear": True,
        "body": body,
    }


def build_ntfy_payload(n: EgressNotification, s: NtfySettings) -> dict:
    """Build the JSON body for an ntfy publish request."""
    message = allow_prompt_line(n.host, host_is_ip=n.host_is_ip)
    if n.reason:
        message += f"\nreason: {n.reason}"
    if n.uid is not None or n.comm is not None:
        uid = n.uid if n.uid is not None else "?"
        comm = n.comm if n.comm is not None else "?"
        message += f"\nprocess: uid={uid} comm={comm}"
    message += f"\nreq {n.request_id}"

    payload: dict[str, object] = {
        "topic": s.topic,
        "title": f"egress: {n.container} → {n.host}:{n.port}",
        "message": message,
        "priority": NTFY_PRIORITY,
        "tags": ["warning"] if n.host_is_ip else ["lock"],
    }

    if s.broker_url and s.operator_token:
        actions: list[dict[str, object]] = []
        if not n.host_is_ip:
            actions.append(
                _http_action(
                    label="Allow",
                    broker_url=s.broker_url,
                    operator_token=s.operator_token,
                    body=_action_body(
                        container=n.container,
                        host=n.host,
                        decision="allow",
                        scope="live",
                    ),
                )
            )
            actions.append(
                _http_action(
                    label="Allow + persist",
                    broker_url=s.broker_url,
                    operator_token=s.operator_token,
                    body=_action_body(
                        container=n.container,
                        host=n.host,
                        decision="allow",
                        scope="manifest",
                    ),
                )
            )
        actions.append(
            _http_action(
                label="Deny",
                broker_url=s.broker_url,
                operator_token=s.operator_token,
                body=_action_body(
                    container=n.container,
                    host=n.host,
                    decision="deny",
                    reason="denied from notification",
                ),
            )
        )
        payload["actions"] = actions[:3]

    return payload


class NtfyNotifier:
    """Send egress approval notifications via ntfy."""

    def __init__(
        self,
        settings: NtfySettings,
        *,
        urlopen: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self._settings = settings
        self._urlopen = urlopen

    def send(self, n: EgressNotification) -> None:
        payload = build_ntfy_payload(n, self._settings)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._settings.token:
            headers["Authorization"] = f"Bearer {self._settings.token}"
        request = urllib.request.Request(
            f"{self._settings.url}/",
            data=body,
            headers=headers,
            method="POST",
        )
        LOG.info(
            "egress notify dispatch request_id=%s bytes=%d",
            n.request_id,
            len(body),
        )
        started = time.monotonic()
        status: str
        try:
            with self._urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
                status = str(getattr(response, "status", getattr(response, "code", "ok")))
        except urllib.error.HTTPError as exc:
            status = str(exc.code)
            LOG.warning(
                "egress notify failed request_id=%s reason=HTTPError %d",
                n.request_id,
                exc.code,
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            status = "timeout" if isinstance(exc, socket.timeout) else "error"
            LOG.warning(
                "egress notify failed request_id=%s reason=%s",
                n.request_id,
                exc.__class__.__name__,
            )
        LOG.info(
            "egress notify done request_id=%s status=%s duration_ms=%d",
            n.request_id,
            status,
            int((time.monotonic() - started) * 1000),
        )

    def send_async(self, n: EgressNotification) -> threading.Thread:
        thread = threading.Thread(
            target=self.send,
            args=(n,),
            name="egress-notify",
            daemon=True,
        )
        thread.start()
        return thread
