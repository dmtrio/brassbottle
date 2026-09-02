#!/bin/bash
# down.sh <name> [--purge] — stop and remove a container.
# Default keeps the workspace volume (code) — ./up.sh <name> restores the
# container around it. --purge also deletes the volume and derived keys;
# the manifest, secrets.env, and artifacts/<name>/ always survive.

set -e

NAME="$1"
[ -n "$NAME" ] || { echo "Usage: ./down.sh <name> [--purge]"; exit 1; }

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/src/common.sh"   # sets BASE_PATH + DJINN_CTR_PREFIX
CNAME="$DJINN_CTR_PREFIX$NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [ "$2" = "--purge" ]; then
    # IMAGE_TAG passed so compose can resolve (and we can remove) the
    # per-container image djinn:<name>, otherwise it orphans on disk.
    IMAGE_TAG="$NAME" docker compose -p "$CNAME" down -v
    docker image rm "djinn:$NAME" 2>/dev/null || true
    rm -rf "$BASE_PATH/keys/$NAME"
    # The generated plugin-volume and agent-state overlays (up.sh writes them
    # per container) are derived state like keys/, so a purge collects them too.
    rm -f "$BASE_PATH/compose/$NAME.plugins.yml" "$BASE_PATH/compose/$NAME.agents.yml"
    echo "Purged $CNAME (container, volume, image, derived keys). Kept: manifest, secrets.env, artifacts/$NAME/"
else
    docker compose -p "$CNAME" down
    echo "Stopped $CNAME (workspace volume kept — ./djinn up $NAME to restore)"
fi

# Removal changes what the jump may offer. This stays host-side: the jump
# receives only its read-only registry, never a Docker socket.
if [ -f "$BASE_PATH/compose/jump.yml" ] && require_python3; then
    if ! DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" refresh; then
        echo "WARNING: jump registry refresh failed; rerun './djinn jump refresh' to retry" >&2
    fi
elif [ -f "$BASE_PATH/compose/jump.yml" ]; then
    echo "WARNING: jump registry was not refreshed (python3 unavailable)" >&2
fi
