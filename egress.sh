#!/bin/bash
# egress.sh — singleton egress service operator (host-side).
#
# Brings the egress broker and the admin daemon up as `restart: unless-stopped`
# containers in one compose project (src/egress_service.py renders and drives
# it), so no terminal has to stay open for a bottle to file an egress request.
#
#   ./egress.sh start
#   ./egress.sh stop
#   ./egress.sh status
#   ./egress.sh logs [-f]
#   ./egress.sh url
#   ./egress.sh ip

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"

# require_python3 (src/common.sh, sourced above) owns the candidate loop and
# the diagnostic — it names the pyenv/brew/Xcode-CLT causes, which is exactly
# the failure this hits on a Mac host.
require_python3 || exit 1

SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"

if [ -z "${1:-}" ]; then
    echo "Usage: ./egress.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f] | url | ip"
    exit 1
fi

# DJINN_SUBNET / DJINN_EGRESS_IP are documented ./.env overrides, and
# src/common.sh sources ./.env as plain shell assignments — it exports
# nothing. The Python side reads os.environ, so they must be forwarded
# explicitly here or the address silently falls back to the default subnet
# (the same forwarding jump.sh does for DJINN_JUMP_IP). DOCKER_HOST likewise:
# it decides which socket gets mounted into the broker container. Secrets
# sourced above populate EGRESS_ACTIONS_URL when the operator set it there;
# an environment value wins because the assignment below is a no-op then.
exec env DJINN_HOME="$BASE_PATH" \
    BOTTLES_PATH="${BOTTLES_PATH:-}" \
    DJINN_SUBNET="${DJINN_SUBNET:-}" \
    DJINN_EGRESS_IP="${DJINN_EGRESS_IP:-}" \
    DOCKER_HOST="${DOCKER_HOST:-}" \
    EGRESS_ACTIONS_URL="${EGRESS_ACTIONS_URL:-}" \
    "$PYTHON3" "$SCRIPT_DIR/src/egress_service.py" "$@"
