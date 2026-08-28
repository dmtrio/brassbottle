#!/bin/bash
# backup.sh — singleton artifact backup operator (host-side).
#
# One Dockerized restic service per djinn installation. Mounts aggregate
# artifacts/ and browser-tmp/ read-only; repository and credentials live only
# under $BASE_PATH/backups and are never mounted into bottle containers.
#
#   ./backup.sh start
#   ./backup.sh stop
#   ./backup.sh status
#   ./backup.sh logs [-f]
#   ./backup.sh snapshots
#   ./backup.sh check
#   ./backup.sh restore <snapshot> --target <host-path>
#   ./backup.sh browser start|stop|status|logs|url

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"

# require_python3 (src/common.sh, sourced above) owns the candidate loop and
# the diagnostic; backup.sh only decides what to do on failure (exit 1).
require_python3 || exit 1

if [ -z "${1:-}" ]; then
    echo "Usage: ./backup.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f] | snapshots | check | restore <id> --target <path>"
    echo "  browser start | stop | status | logs [-f] | url"
    exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/backup_host.py" --help
fi

exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/backup_host.py" "$@"
