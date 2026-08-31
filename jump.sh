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
# Unlike backup.sh this sources secrets.env, but only as a COMPATIBILITY SEED:
# the operator's keys now live one-per-line in $DJINN_HOME/jump/authorized_keys
# (public keys are not secrets, and a file takes multiple keys without any
# shell quoting). SSH_AUTHORIZED_KEY populates that file when it does not
# exist yet; after that the file wins. See jump_config.resolve_authorized_keys.

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
    echo "Usage: ./jump.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f] | pubkey"
    exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" --help
fi

# DJINN_SUBNET / DJINN_JUMP_IP / DJINN_JUMP_MOSH_PORTS are documented as
# ./.env overrides, and src/common.sh sources ./.env as plain shell
# assignments — it exports nothing. up.sh gets away with reading DJINN_SUBNET
# as a shell variable because it passes it to ensure_net.py as an ARGUMENT;
# jump_host reads os.environ, so they must be forwarded explicitly here or the
# derivation silently falls back to the defaults and emits an address on the
# wrong bridge.
exec env DJINN_HOME="$BASE_PATH" SSH_AUTHORIZED_KEY="${SSH_AUTHORIZED_KEY:-}" \
    DJINN_SUBNET="${DJINN_SUBNET:-}" \
    DJINN_JUMP_IP="${DJINN_JUMP_IP:-}" \
    DJINN_JUMP_MOSH_PORTS="${DJINN_JUMP_MOSH_PORTS:-}" \
    "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" "$@"
