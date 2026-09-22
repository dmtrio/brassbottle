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
#   ./bin/update-agent-keys.sh mysite user GIT_TOKEN_OVERRIDE val    # the human's shell
#
# Agents: any shim agent enabled in that container (the <agent>.env files
# up.sh wrote under keys/<container>/ are the authoritative list — descriptor-
# driven, so it needs no update when agents/ gains a new agent), or 'common'
# to set the var in EVERY agent's file at once — common.env was retired in
# Phase 3, so each agent now carries one complete env file. 'user' (the
# human's user.env, the `user` git identity sourced by the image's .bashrc)
# is also a valid target — but 'common' never touches it: user.env carries
# git routing, not agent credentials, and the human's shell never received
# plugin tokens before per-identity key files existed.
#
# Git routing (GIT_HOST_TOKENS and friends) is composed per identity by
# up.sh and is never edited here: a token variable used by a host must be
# named by that host's row in the manifest's git: section. Add hosts/entries
# there, in the manifest — this script only edits the values, never the
# routing.

set -e

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../src"
. "$SRC_DIR/common.sh"     # sets BASE_PATH
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

# The valid agents for THIS container are exactly the <agent>.env files up.sh
# composed into its keys dir (derived from the manifest's enabled, MCP-capable
# agents) — not a hardcoded list that would drift when agents/ gains an agent.
KNOWN_AGENTS=""
for f in "$KEYS_PATH"/*.env; do
    [ -f "$f" ] || continue
    KNOWN_AGENTS="${KNOWN_AGENTS:+$KNOWN_AGENTS }$(basename "$f" .env)"
done
if [ "$AGENT" != common ]; then
    case " $KNOWN_AGENTS " in
        *" $AGENT "*) ;;
        *) echo "Error: agent must be one of: ${KNOWN_AGENTS// /, }, common"; exit 1 ;;
    esac
fi

if [ -z "$VAR" ]; then
    echo "Error: VAR required (e.g. OBSIDIAN_ANNOTATED_KEY)"
    exit 1
fi

# The routing variables are manifest-owned, never edited here: a token
# variable used by a host must be named by that host's row in the manifest's
# git: section. Refuse the two a hand-edit would corrupt — GIT_HOST_TOKENS
# is the routing table itself, GH_TOKEN is the CLI host's plain export that
# the per-identity composition writes (or deliberately does not).
case "$VAR" in
    GIT_HOST_TOKENS|GH_TOKEN)
        echo "Error: $VAR is routing, not a credential value — it is composed per identity from the git: section; edit git.hosts in the manifest and rerun ./up.sh" >&2
        exit 1 ;;
esac

if [ -z "$VALUE" ]; then
    printf "Value for %s (%s/%s, input hidden): " "$VAR" "$CONTAINER" "$AGENT"
    read -s VALUE
    echo ""
fi

# Set VAR=VALUE (or remove VAR when VALUE is empty) in one agent's env file,
# idempotently (drop any existing line first, mode 600 throughout). The value
# is shell-quoted (%q), like the composed key files these lines are sourced
# into — a value with spaces, quotes or $ must round-trip byte-identically
# (an unquoted append runs the value's words as commands).
set_var_in() {
    local file="$1" tmp="$1.tmp.$$"
    touch "$file"; chmod 600 "$file"
    grep -v "^$VAR=" "$file" > "$tmp" || true
    [ -n "$VALUE" ] && printf '%s=%q\n' "$VAR" "$VALUE" >> "$tmp"
    mv "$tmp" "$file"; chmod 600 "$file"
}

# common.env is retired (Plugins v2 Phase 3): each agent has one complete env
# file, so 'common' now means "every shim agent this container enables" — a
# per-agent override of a shared token, applied across all of them at once.
# user.env is NOT in the fan-out: it is the human's git identity (routing
# rows + GH_TOKEN), not an agent credential set — the human's shell never
# carried plugin tokens before per-identity key files, and 'user' stays
# targetable individually below.
if [ "$AGENT" = common ]; then
    [ -n "$KNOWN_AGENTS" ] || { echo "Error: no agent env files under $KEYS_PATH"; exit 1; }
    for a in $KNOWN_AGENTS; do
        [ "$a" = user ] && continue
        set_var_in "$KEYS_PATH/$a.env"
    done
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
