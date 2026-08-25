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

if [ -z "${PYTHON3:-}" ]; then
    for cand in /usr/bin/python3 python3; do
        if "$cand" -c '' 2>/dev/null; then PYTHON3="$cand"; break; fi
    done
fi
[ -n "${PYTHON3:-}" ] && "$PYTHON3" -c '' 2>/dev/null \
    || { echo "Error: no working python3" >&2; exit 1; }

if [ -z "${1:-}" ]; then
    echo "Usage: ./backup.sh <cmd> [args...]"
    echo "  start | stop | status | logs [-f] | snapshots | check | restore <id> --target <path>"
    echo "  browser start | stop | status | logs [-f] | url"
    exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/backup_host.py" --help
fi

exec env DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/backup_host.py" "$@"
