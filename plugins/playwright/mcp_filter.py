#!/usr/bin/env python3
"""Tool-allowlist proxy in front of @playwright/mcp (stdio, newline-delimited
JSON-RPC).

@playwright/mcp has no tool allowlist of its own: every "core" tool —
including browser_run_code_unsafe, which its own description calls
RCE-equivalent — is always exposed. The host gateway this plugin replaces cut
the surface with `--tools`; this proxy does the same job here, so the bottle
gets the same 22 tools and nothing else.

    mcp_filter.py -- <server command> [args...]

Client -> server: a `tools/call` naming a tool outside ALLOWED_TOOLS is answered
here with a JSON-RPC error and never reaches the server. Server -> client: the
result of `tools/list` is stripped to ALLOWED_TOOLS, so the agent never sees
the rest. Everything else passes through byte-for-byte. Fail closed: an
unparseable client line is dropped, never forwarded.

Boundary log (stderr, so stdout stays clean JSON-RPC): child spawn, each
tools/list (tools in -> tools out), each refused call (tool name only, never
arguments), and the child's exit status with duration.
"""
import json
import subprocess
import sys
import threading
import time

# Same set as the host gateway's --tools list (plugins/gateway/run.sh), i.e.
# every browser_* tool it exposed, and no browser_run_code_unsafe.
ALLOWED_TOOLS = frozenset("""
browser_click browser_close browser_console_messages browser_drag browser_drop
browser_evaluate browser_file_upload browser_fill_form browser_handle_dialog
browser_hover browser_navigate browser_navigate_back browser_network_request
browser_network_requests browser_press_key browser_resize browser_select_option
browser_snapshot browser_tabs browser_take_screenshot browser_type
browser_wait_for
""".split())

JSONRPC_INVALID_PARAMS = -32602


def log(msg):
    print(f"playwright-filter: {msg}", file=sys.stderr, flush=True)


def _refusal(msg_id, tool):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": JSONRPC_INVALID_PARAMS,
            "message": f"Tool {tool} is not available in this bottle",
        },
    }


def filter_client_message(msg, list_ids):
    """Split one parsed client message into (forward, replies).

    `forward` is the message for the server (None if nothing to send) and
    `replies` are responses to write straight back to the client. The ids of
    tools/list requests are recorded in `list_ids` so the server's answer can
    be recognised. Batches are handled element-wise.
    """
    if isinstance(msg, list):
        forward, replies = [], []
        for item in msg:
            fwd, rep = filter_client_message(item, list_ids)
            if fwd is not None:
                forward.append(fwd)
            replies.extend(rep)
        return (forward or None), replies
    if not isinstance(msg, dict):
        return None, []
    method = msg.get("method")
    if method == "tools/list" and "id" in msg:
        list_ids.add(json.dumps(msg["id"]))
    if method == "tools/call":
        params = msg.get("params")
        tool = params.get("name") if isinstance(params, dict) else None
        if tool not in ALLOWED_TOOLS:
            log(f"refused tools/call {str(tool)[:80]!r}")
            if "id" in msg:
                return None, [_refusal(msg["id"], str(tool)[:80])]
            return None, []
    return msg, []


def filter_server_message(msg, list_ids):
    """Strip a tools/list result to ALLOWED_TOOLS; pass anything else through."""
    if isinstance(msg, list):
        return [filter_server_message(item, list_ids) for item in msg]
    if not isinstance(msg, dict) or "id" not in msg:
        return msg
    key = json.dumps(msg["id"])
    if key not in list_ids:
        return msg
    result = msg.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        list_ids.discard(key)
        before = result["tools"]
        result["tools"] = [t for t in before
                           if isinstance(t, dict) and t.get("name") in ALLOWED_TOOLS]
        log(f"tools/list: {len(before)} tools in, {len(result['tools'])} out")
    elif "error" in msg:
        list_ids.discard(key)
    return msg


def _pump_client(child, out_lock, list_ids):
    """stdin -> child, applying the allowlist. Ends the child's stdin at EOF."""
    dropped = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            dropped += 1
            log(f"dropped unparseable client line ({len(line)} bytes)")
            continue
        forward, replies = filter_client_message(msg, list_ids)
        for reply in replies:
            with out_lock:
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()
        if forward is not None:
            try:
                # Re-serialised only when we changed something; otherwise the
                # original line is forwarded untouched.
                child.stdin.write(line if forward is msg else json.dumps(forward) + "\n")
                child.stdin.flush()
            except (BrokenPipeError, ValueError):
                return
    try:
        child.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    if dropped:
        log(f"client closed; {dropped} unparseable line(s) dropped")


def run(argv):
    started = time.monotonic()
    log(f"start: {argv[0]} ({len(ALLOWED_TOOLS)} tools allowed)")
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, bufsize=1)
    list_ids = set()
    out_lock = threading.Lock()
    threading.Thread(target=_pump_client, args=(child, out_lock, list_ids),
                     daemon=True).start()
    for line in child.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            # Not JSON-RPC (stray server output): keep stdout clean.
            log(f"dropped non-JSON server line ({len(line)} bytes)")
            continue
        out = filter_server_message(msg, list_ids)
        with out_lock:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    code = child.wait()
    log(f"exit: status {code} after {time.monotonic() - started:.1f}s")
    return code


def main(args):
    if "--" not in args or args.index("--") == len(args) - 1:
        print(f"usage: {sys.argv[0]} -- <server command> [args...]", file=sys.stderr)
        return 2
    return run(args[args.index("--") + 1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
