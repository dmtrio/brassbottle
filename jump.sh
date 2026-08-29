#!/bin/bash
# jump.sh — singleton mosh jump container operator (host-side).
#
# One jump container per djinn installation. It terminates the operator's
# inbound mosh session and hops onward to bottles over djinn-net, so mosh does
# not have to live in every bottle image.
#
#   ./jump.sh start
#   ./jump.sh stop
#   ./jump.sh status
#   ./jump.sh logs [-f]
#   ./jump.sh pubkey
#
# Unlike backup.sh this sources secrets.env: the jump needs SSH_AUTHORIZED_KEY
# (the same operator public key the bottles already use) to let you in at all.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"

if [ -z "${PYTHON3:-}" ]; then
    for cand in /usr/bin/python3 python3; do
        if "$cand" -c '' 2>/dev/null; then PYTHON3="$cand"; break; fi
    done
fi
[ -n "${PYTHON3:-}" ] && "$PYTHON3" -c '' 2>/dev/null \
    || { echo "Error: no working python3" >&2; exit 1; }

SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"

if [ -z "${1:-}" ]; then
    echo "Usage: ./jump.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f] | pubkey"
    exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" --help
fi

exec env DJINN_HOME="$BASE_PATH" SSH_AUTHORIZED_KEY="${SSH_AUTHORIZED_KEY:-}" \
    "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" "$@"
