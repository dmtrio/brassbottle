#!/bin/bash
# newt.sh — singleton Pangolin Newt connector operator (host-side).
#
# One Newt per djinn installation, joined to djinn-net. It dials OUT to your
# Pangolin instance and gives Olm-enrolled devices L3 access to the bridge —
# which is what makes the jump container and every bottle reachable from a
# phone without publishing anything on this Mac.
#
#   ./newt.sh start
#   ./newt.sh stop
#   ./newt.sh status
#   ./newt.sh logs [-f]
#
# Sources secrets.env for PANGOLIN_ENDPOINT / NEWT_ID / NEWT_SECRET, the same
# way jump.sh sources it for SSH_AUTHORIZED_KEY.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"

# require_python3 (src/common.sh, sourced above) owns the candidate loop and
# the diagnostic — it names the pyenv/brew/Xcode-CLT causes.
require_python3 || exit 1

SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"

if [ -z "${1:-}" ]; then
    echo "Usage: ./newt.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f]"
    exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/newt_host.py" --help
fi

# DJINN_SUBNET / DJINN_NEWT_IP / DJINN_NEWT_IMAGE are ./.env overrides, and
# common.sh sources ./.env as plain shell assignments — it exports nothing.
# newt_host reads os.environ, so they must be forwarded explicitly.
exec env DJINN_HOME="$BASE_PATH" \
    PANGOLIN_ENDPOINT="${PANGOLIN_ENDPOINT:-}" \
    NEWT_ID="${NEWT_ID:-}" \
    NEWT_SECRET="${NEWT_SECRET:-}" \
    DJINN_SUBNET="${DJINN_SUBNET:-}" \
    DJINN_NEWT_IP="${DJINN_NEWT_IP:-}" \
    DJINN_NEWT_IMAGE="${DJINN_NEWT_IMAGE:-}" \
    "$PYTHON3" "$SCRIPT_DIR/src/newt_host.py" "$@"
