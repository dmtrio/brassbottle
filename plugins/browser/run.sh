#!/bin/bash
# plugins/browser/run.sh <container> [brave|chrome]
# Per-container watchable agent browser + MCP bridge. Start from the repo root:
#
#   ./service.sh browser <container>          # Brave if installed, else Chrome
#   ./service.sh browser <container> chrome
#
# See plugins/browser/README.md and launch.py for ports, profiles, and TMPDIR.

set -e

: "${BASE_PATH:?run this launcher via ./service.sh browser (it resolves BASE_PATH)}"
command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR: python3 not found on PATH (the launcher is Python)"; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$SCRIPT_DIR/launch.py" "$@"
