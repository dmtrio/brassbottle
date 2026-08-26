#!/usr/bin/env python3
"""Minimal stdio MCP server for egress request/check tools (stdlib only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

for _lib in (
    Path("/usr/local/lib/djinn"),
    Path(__file__).resolve().parents[2] / "src",
):
    if _lib.is_dir() and str(_lib) not in sys.path:
        sys.path.insert(0, str(_lib))

from egress_request import (  # noqa: E402
    check_hosts,
    format_check_results,
    format_request_results,
    request_hosts,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "djinn-egress"
SERVER_VERSION = "1.0.0"


def _tool_schema(
    name: str,
    description: str,
    *,
    required: list[str],
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


TOOLS = [
    _tool_schema(
        "request_egress",
        (
            "File egress approval for one or more hosts and block until the "
            "operator allows, denies, or the hold window expires."
        ),
        required=["hosts"],
        properties={
            "hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hostnames to request (optionally host:port).",
            },
            "reason": {
                "type": "string",
                "description": "Why this egress is needed (shown to the operator).",
            },
        },
    ),
    _tool_schema(
        "check_egress",
        (
            "Probe whether hosts are already allowed via the container ipset "
            "(no filing, no operator prompt)."
        ),
        required=["hosts"],
        properties={
            "hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hostnames to probe (optionally host:port).",
            },
        },
    ),
]


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    payload = sys.stdin.buffer.read(length)
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("message must be a JSON object")
    return parsed


def _write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _respond(request_id: Any, result: Any) -> None:
    _write_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def _respond_error(request_id: Any, code: int, message: str) -> None:
    _write_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _tool_text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _call_request_egress(arguments: dict[str, Any]) -> dict[str, Any]:
    hosts = arguments.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("hosts must be a non-empty array of strings")
    if not all(isinstance(item, str) and item.strip() for item in hosts):
        raise ValueError("hosts must contain non-empty strings")
    reason = arguments.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    results, code = request_hosts(hosts, reason=reason or None)
    summary = format_request_results(results)
    if code != 0:
        summary += f"\nexit_code={code}"
    return _tool_text(summary)


def _call_check_egress(arguments: dict[str, Any]) -> dict[str, Any]:
    hosts = arguments.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("hosts must be a non-empty array of strings")
    if not all(isinstance(item, str) and item.strip() for item in hosts):
        raise ValueError("hosts must contain non-empty strings")
    results = check_hosts(hosts)
    return _tool_text(format_check_results(results))


def _handle_request(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str):
        return
    if params is not None and not isinstance(params, dict):
        params = {}

    if method == "initialize":
        _respond(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        _respond(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = (params or {}).get("name")
        arguments = (params or {}).get("arguments")
        if not isinstance(name, str):
            _respond_error(request_id, -32602, "tool name is required")
            return
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            _respond_error(request_id, -32602, "arguments must be an object")
            return
        try:
            if name == "request_egress":
                result = _call_request_egress(arguments)
            elif name == "check_egress":
                result = _call_check_egress(arguments)
            else:
                _respond_error(request_id, -32601, f"unknown tool: {name}")
                return
        except (RuntimeError, ValueError) as exc:
            _respond_error(request_id, -32000, str(exc))
            return
        _respond(request_id, result)
        return

    if request_id is not None:
        _respond_error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        if message.get("id") is None:
            _handle_request(message)
            continue
        try:
            _handle_request(message)
        except Exception as exc:  # pragma: no cover - last-resort protocol guard
            _respond_error(message.get("id"), -32603, str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
