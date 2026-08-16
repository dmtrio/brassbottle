#!/bin/bash
# update-agent-keys.sh — TEMPORARY override of an MCP credential for one
# agent in one container. Takes effect the NEXT time that agent starts (the
# shims read ~/.agent-keys at process launch) — no container restart needed.
#
# WARNING: ~/djinn/keys/<container>/ is DERIVED output — the next
# ./djinn up <container> wipes and recomposes it from ~/djinn/secrets.env
# and the manifest. Make DURABLE changes there instead; use this script only
# for quick between-runs experiments.
#
# Usage:
#   ./bin/update-agent-keys.sh <container> <agent|common> <VAR> [value]
#   ./bin/update-agent-keys.sh <container>                       # list keys
#
# Examples:
#   ./bin/update-agent-keys.sh mysite claude OBSIDIAN_ANNOTATED_KEY   # prompts
#   ./bin/update-agent-keys.sh mysite pi OBSIDIAN_ANNOTATED_KEY      # pi's own key
#   ./bin/update-agent-keys.sh mysite common MCP_GATEWAY_TOKEN       # all agents
#
# Agents: claude, pi, gemini, cursor-agent, codex (or 'common' to set the var
# in EVERY agent's file at once — common.env was retired in Phase 3, so each
# agent now carries one complete env file).

set -e

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../src/common.sh"   # sets BASE_PATH
CONTAINER="$1"
AGENT="$2"
VAR="$3"
VALUE="$4"

if [ -z "$CONTAINER" ]; then
    echo "Usage: ./bin/update-agent-keys.sh <container> <agent|common> <VAR> [value]"
    exit 1
fi

KEYS_PATH="$BASE_PATH/keys/$CONTAINER"
if [ ! -d "$KEYS_PATH" ]; then
    echo "Error: no keys dir at $KEYS_PATH (container never applied via ./up.sh?)"
    exit 1
fi

# List mode
if [ -z "$AGENT" ]; then
    echo "Key files for $CONTAINER (values hidden):"
    for f in "$KEYS_PATH"/*.env; do
        [ -f "$f" ] || continue
        echo "  $(basename "$f" .env):"
        cut -d= -f1 "$f" | sed 's/^/    /'
    done
    exit 0
fi

case "$AGENT" in
    claude|pi|gemini|cursor-agent|codex|common) ;;
    *) echo "Error: agent must be one of: claude, pi, gemini, cursor-agent, codex, common"; exit 1 ;;
esac

if [ -z "$VAR" ]; then
    echo "Error: VAR required (e.g. OBSIDIAN_ANNOTATED_KEY)"
    exit 1
fi

if [ -z "$VALUE" ]; then
    printf "Value for %s (%s/%s, input hidden): " "$VAR" "$CONTAINER" "$AGENT"
    read -s VALUE
    echo ""
fi

# Set VAR=VALUE (or remove VAR when VALUE is empty) in one agent's env file,
# idempotently (drop any existing line first, mode 600 throughout).
set_var_in() {
    local file="$1" tmp="$1.tmp.$$"
    touch "$file"; chmod 600 "$file"
    grep -v "^$VAR=" "$file" > "$tmp" || true
    [ -n "$VALUE" ] && echo "$VAR=$VALUE" >> "$tmp"
    mv "$tmp" "$file"; chmod 600 "$file"
}

# common.env is retired (Plugins v2 Phase 3): each agent has one complete env
# file, so 'common' now means "every shim agent" — a per-agent override of a
# shared token, applied across all of them at once. SHIM_AGENTS must match the
# Dockerfile shim loop and up.sh.
SHIM_AGENTS="claude pi gemini cursor-agent codex"
if [ "$AGENT" = common ]; then
    for a in $SHIM_AGENTS; do set_var_in "$KEYS_PATH/$a.env"; done
    TARGET="all agents"
else
    set_var_in "$KEYS_PATH/$AGENT.env"
    TARGET="$AGENT"
fi

if [ -n "$VALUE" ]; then
    echo "✓ $VAR set for $CONTAINER/$TARGET — applies on next start"
else
    echo "✓ $VAR removed for $CONTAINER/$TARGET"
fi
