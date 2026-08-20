#!/bin/bash
# service.sh <name> [args...] — start a plugin's host-side service on this Mac.
#
# Some plugins are backed by a service that runs on the HOST, not in a
# container: the gateway (Playwright MCP), proxyman (traffic capture bridge),
# and browser (a watchable desktop browser). Each ships a run.sh in its plugin
# directory; this dispatcher starts it so you never need the path:
#
#   ./service.sh gateway          # execs plugins/gateway/run.sh
#   ./service.sh browser chrome   # extra args are forwarded to run.sh
#   ./service.sh                  # lists plugins that ship a host service
#
# Run each in its own tmux window (or wrap in a launchd plist for boot
# persistence) and leave it running; containers reach it via the port its
# plugin.yml declares.
#
# This is DELIBERATELY separate from ./up.sh: up.sh recreates a container
# (docker compose up --build), which would kill a running agent — starting a
# host service must never carry that risk, nor sit one typo away from it.
# service.sh never touches docker.
#
# service.sh is the one place that knows the djinn home: it sources
# src/common.sh (once, at the repo root — no ../ path arithmetic) to resolve
# BASE_PATH and hands it to the launcher in the environment, so each run.sh
# needs zero path knowledge of its own. common.sh is sourced LATE (just before
# exec), so the usage/list/error paths above it never depend on ./.env —
# mirroring bin/allow-egress.sh.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGINS_DIR="$SCRIPT_DIR/plugins"

list_services() {
    echo "Plugins with a host service (run.sh):"
    local found=0 r
    for r in "$PLUGINS_DIR"/*/run.sh; do
        [ -e "$r" ] || continue
        found=1
        printf '  %s\n' "$(basename "$(dirname "$r")")"
    done
    [ "$found" = 1 ] || echo "  (none)"
}

NAME="${1:-}"
if [ -z "$NAME" ]; then
    echo "Usage: ./service.sh <name> [args...]   (starts plugins/<name>/run.sh on this host)"
    list_services
    exit 1
fi
# Same charset as bottle.py's plugin-name rule (letters, digits, _, -). The
# name is interpolated into a filesystem path below, so reject anything with a
# slash or '..' before it can escape plugins/.
case "$NAME" in
    *[!A-Za-z0-9_-]*)
        echo "Error: invalid plugin name '$NAME' (allowed: letters, digits, underscore, dash)."
        exit 1 ;;
esac
shift

if [ ! -d "$PLUGINS_DIR/$NAME" ]; then
    echo "Error: no plugin named '$NAME' (plugins/$NAME/ does not exist)."
    list_services
    exit 1
fi

RUN="$PLUGINS_DIR/$NAME/run.sh"
if [ ! -f "$RUN" ]; then
    echo "Error: plugin '$NAME' has no host service (no plugins/$NAME/run.sh)."
    echo "It's a container-side (serena) or remote (obsidian-annotated) plugin —"
    echo "nothing to start on the host."
    list_services
    exit 1
fi

# Resolve the djinn home here and hand it down — the launcher takes it from
# the environment rather than computing a path to common.sh itself.
# BOTTLES_PATH rides along for the same reason: common.sh resolves it (from
# ./.env, the env, or the per-setup default), and a launcher that reads a
# container bottle must see the SAME directory up.sh does. Without the export
# it is only a shell variable here, and a ./.env override would be silently
# ignored one process down.
. "$SCRIPT_DIR/src/common.sh"   # sets BASE_PATH, BOTTLES_PATH
export BASE_PATH BOTTLES_PATH
exec bash "$RUN" "$@"
