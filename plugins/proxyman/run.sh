#!/bin/bash
# plugins/proxyman/run.sh — expose Proxyman's stdio MCP server over
# streamable HTTP for dev containers (RFC 03 tier 2: direct host MCP).
# This is the proxyman plugin's host-side launcher; start it from the repo
# root with:  ./service.sh proxyman
#
# Proxyman's MCP is a stdio binary inside the app bundle, so containers
# can't reach it without a Mac-side bridge. This uses the Node mcp-proxy,
# chosen over supergateway because it can bind localhost-only AND require
# an API key on inbound requests.
#
# Run in tmux or wrap in launchd. Proxyman.app must be running.
# Key: PROXYMAN_BRIDGE_KEY in ~/djinn/secrets.env (self-generated on
# first run). Containers get access by listing the `proxyman` plugin in the
# manifest — up.sh grants port 8813, composes the key, and generates the
# .mcp.json entry.

set -e

PROXYMAN_MCP="/Applications/Setapp/Proxyman.app/Contents/MacOS/mcp-server"
[ -x "$PROXYMAN_MCP" ] || PROXYMAN_MCP="/Applications/Proxyman.app/Contents/MacOS/mcp-server"
[ -x "$PROXYMAN_MCP" ] || { echo "ERROR: Proxyman mcp-server binary not found"; exit 1; }

# BASE_PATH (the djinn home) is resolved by service.sh — which sources
# src/common.sh once, at the repo root — and handed to this launcher in the
# environment. So this script needs no path arithmetic of its own; start it via:
#   ./service.sh proxyman
: "${BASE_PATH:?run this launcher via ./service.sh proxyman (it resolves BASE_PATH)}"
SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] || { mkdir -p "$(dirname "$SECRETS_FILE")"; touch "$SECRETS_FILE"; chmod 600 "$SECRETS_FILE"; }
. "$SECRETS_FILE"
if [ -z "${PROXYMAN_BRIDGE_KEY:-}" ]; then
    PROXYMAN_BRIDGE_KEY=$(openssl rand -hex 24)
    echo "PROXYMAN_BRIDGE_KEY=$PROXYMAN_BRIDGE_KEY" >> "$SECRETS_FILE"
    echo "Generated PROXYMAN_BRIDGE_KEY in $SECRETS_FILE"
fi

exec npx -y mcp-proxy \
    --host 127.0.0.1 \
    --port 8813 \
    --server stream \
    --apiKey "$PROXYMAN_BRIDGE_KEY" \
    -- "$PROXYMAN_MCP"
