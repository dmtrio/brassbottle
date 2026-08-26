#!/bin/bash
# tests/egress.smoke.sh — operator-run Phase A+B egress end-to-end smoke test.
# Needs a Mac host with Docker Desktop, a running bottle, and the host broker
# listening on 8816 (e.g. ./djinn allow --watch). Cannot run in CI or inside
# a container — the Python driver skips with a clear message in those cases.

# SC2015 (`A && pass || fail` is not if-else): N/A — this file delegates to Python.
# shellcheck disable=SC2148

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$SCRIPT_DIR"

command -v python3 >/dev/null || {
    echo "SKIP: python3 not installed"
    exit 0
}

exec python3 tests/egress_smoke_lib.py "$@"
