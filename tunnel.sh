#!/bin/bash
# tunnel.sh — singleton VPN/tunnel connector operator (host-side).
#
# One connector per djinn installation, joined to djinn-net. It dials OUT to
# your VPN control plane and gives enrolled devices L3 access to the bridge —
# which is what makes the jump container and every bottle reachable from a
# phone without publishing anything on this Mac.
#
# Which connector is a src/tunnel_config.py detail (see its Provider block);
# nothing here or in ./djinn names a vendor. The exported credential names
# below are the exception, and deliberately so: they must match what the
# provider's own admin UI shows.
#
#   ./tunnel.sh start
#   ./tunnel.sh stop
#   ./tunnel.sh status
#   ./tunnel.sh logs [-f]
#
# Sources secrets.env for the provider credentials, the same way jump.sh
# sources it for SSH_AUTHORIZED_KEY.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"

# require_python3 (src/common.sh, sourced above) owns the candidate loop and
# the diagnostic — it names the pyenv/brew/Xcode-CLT causes.
require_python3 || exit 1

SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"

if [ -z "${1:-}" ]; then
    echo "Usage: ./tunnel.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f]"
    DJINN_HOME="$BASE_PATH" exec "$PYTHON3" "$SCRIPT_DIR/src/tunnel_host.py" --help
fi

# DJINN_SUBNET / DJINN_TUNNEL_IP / DJINN_TUNNEL_IMAGE are ./.env overrides, and
# common.sh sources ./.env as plain shell assignments — it exports nothing.
# tunnel_host reads os.environ, so they must be exported explicitly.
#
# `export`, NOT `exec env VAR=...`: an env prefix puts every value in the new
# process's ARGV, so the secret would be visible in the process table (and in
# /proc/<pid>/cmdline) for as long as the command runs — which for a first
# start is however long the image pull takes. jump.sh can use the env-prefix
# form because the only secret it passes is a PUBLIC key; this one cannot.
export DJINN_HOME="$BASE_PATH"
export PANGOLIN_ENDPOINT="${PANGOLIN_ENDPOINT:-}"
export NEWT_ID="${NEWT_ID:-}"
export NEWT_SECRET="${NEWT_SECRET:-}"
export DJINN_SUBNET="${DJINN_SUBNET:-}"
export DJINN_TUNNEL_IP="${DJINN_TUNNEL_IP:-}"
export DJINN_TUNNEL_IMAGE="${DJINN_TUNNEL_IMAGE:-}"

exec "$PYTHON3" "$SCRIPT_DIR/src/tunnel_host.py" "$@"
