#!/bin/bash
# plugins/gateway/run.sh — serve the 'coding' MCP profile (Playwright only)
# on localhost:8811 for dev containers (RFC 03 tier 1). This is the gateway
# plugin's host-side launcher; start it from the repo root with:
#
#   ./service.sh gateway
#
# Run it in tmux, or wrap in a launchd plist for boot persistence.
# Token: MCP_GATEWAY_TOKEN in ~/djinn/secrets.env (self-generated on
# first run); up.sh composes it into containers whose bottle lists the
# gateway plugin.
#
# Security posture:
# - Binds localhost only (containers reach it via Docker Desktop's
#   host.docker.internal forwarding; nothing is exposed to the LAN).
# - Bearer token required (401 without it).
# - MCPGODEBUG disables only the SDK's Host-header rebinding check, which
#   is redundant here: a rebinding page can never present the Bearer token.
# - Tool allowlist: Playwright browser_* only — no gateway management
#   tools (also disable globally: docker mcp feature disable dynamic-tools),
#   and no browser_run_code_unsafe.

set -e

# BASE_PATH (the djinn home) is resolved by service.sh — which sources
# src/common.sh once, at the repo root — and handed to this launcher in the
# environment. So this script needs no path arithmetic of its own; start it via:
#   ./service.sh gateway
: "${BASE_PATH:?run this launcher via ./service.sh gateway (it resolves BASE_PATH)}"
SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] || { mkdir -p "$(dirname "$SECRETS_FILE")"; touch "$SECRETS_FILE"; chmod 600 "$SECRETS_FILE"; }
. "$SECRETS_FILE"
if [ -z "${MCP_GATEWAY_TOKEN:-}" ]; then
    MCP_GATEWAY_TOKEN=$(openssl rand -hex 24)
    echo "MCP_GATEWAY_TOKEN=$MCP_GATEWAY_TOKEN" >> "$SECRETS_FILE"
    echo "Generated MCP_GATEWAY_TOKEN in $SECRETS_FILE"
fi

TOOLS="browser_click,browser_close,browser_console_messages,browser_drag"
TOOLS="$TOOLS,browser_drop,browser_evaluate,browser_file_upload,browser_fill_form"
TOOLS="$TOOLS,browser_handle_dialog,browser_hover,browser_navigate,browser_navigate_back"
TOOLS="$TOOLS,browser_network_request,browser_network_requests,browser_press_key"
TOOLS="$TOOLS,browser_resize,browser_select_option,browser_snapshot,browser_tabs"
TOOLS="$TOOLS,browser_take_screenshot,browser_type,browser_wait_for"

exec env \
    MCPGODEBUG=disablelocalhostprotection=1 \
    MCP_GATEWAY_AUTH_TOKEN="$MCP_GATEWAY_TOKEN" \
    docker mcp gateway run \
        --profile coding \
        --transport streaming \
        --port 8811 \
        --tools "$TOOLS"
