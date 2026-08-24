#!/bin/bash
# tests/compose-context.test.sh — guards the compose build context against the
# root-reorg regression: the compose files live in compose/, so compose derives
# the project directory from the first -f file unless up.sh pins it. When that
# pin is missing, `context: .` resolves to compose/ and the build dies with
# "failed to read dockerfile: open Dockerfile: no such file or directory".
# Static checks only — no docker needed.

# SC2015 (`A && pass || fail` is not if-else): intentional — pass() is a bare
# echo and cannot fail, so the || arm only runs when the check fails.
# shellcheck disable=SC2015

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$SCRIPT_DIR"

command -v yq >/dev/null || { echo "SKIP: yq not installed"; exit 0; }

FAILURES=0
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ✓ $1"; }

echo "── up.sh pins the compose project directory"
grep -q -- '--project-directory "$SCRIPT_DIR"' up.sh \
    && pass "up.sh passes --project-directory \$SCRIPT_DIR" \
    || fail "up.sh must pass --project-directory \"\$SCRIPT_DIR\" (else context resolves to compose/)"

echo "── build context resolves from the repo root"
CONTEXT="$(yq -r '.services.djinn.build.context // ""' compose/docker-compose.local.yml)"
DOCKERFILE="$(yq -r '.services.djinn.build.dockerfile // "Dockerfile"' compose/docker-compose.local.yml)"
[ -n "$CONTEXT" ] \
    && pass "local compose declares a build context ($CONTEXT)" \
    || fail "local compose is missing services.djinn.build.context"
[ -f "$SCRIPT_DIR/$CONTEXT/$DOCKERFILE" ] \
    && pass "$CONTEXT/$DOCKERFILE exists relative to the repo root" \
    || fail "$CONTEXT/$DOCKERFILE does not exist relative to the repo root"

echo "── PID 1 reaps orphans (init: true → tini)"
INIT="$(yq -r '.services.djinn.init // ""' compose/docker-compose.local.yml)"
[ "$INIT" = "true" ] \
    && pass "local compose sets services.djinn.init: true" \
    || fail "local compose must set services.djinn.init: true (else orphaned children accumulate as zombies)"

echo ""
if [ "$FAILURES" -gt 0 ]; then
    echo "FAILED: $FAILURES check(s)"
    exit 1
fi
echo "all checks passed"
