#!/bin/bash
# down.sh <name> [--purge] — stop and remove a container.
# Default keeps the workspace volume (code) — ./up.sh <name> restores the
# container around it. --purge also deletes the volume and derived keys;
# the bottle, secrets.env, and artifacts/<name>/ always survive.

set -e

NAME="$1"
[ -n "$NAME" ] || { echo "Usage: ./down.sh <name> [--purge]"; exit 1; }

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/src/common.sh"   # sets BASE_PATH + DJINN_CTR_PREFIX
CNAME="$DJINN_CTR_PREFIX$NAME"

if [ "$2" = "--purge" ]; then
    # IMAGE_TAG passed so compose can resolve (and we can remove) the
    # per-container image djinn:<name>, otherwise it orphans on disk.
    IMAGE_TAG="$NAME" docker compose -p "$CNAME" down -v
    docker image rm "djinn:$NAME" 2>/dev/null || true
    rm -rf "$BASE_PATH/keys/$NAME"
    # The generated plugin-volume overlay (up.sh writes it per container) is
    # derived state like keys/, so a purge collects it too.
    rm -f "$BASE_PATH/compose/$NAME.plugins.yml"
    echo "Purged $CNAME (container, volume, image, derived keys). Kept: bottle, secrets.env, artifacts/$NAME/"
else
    docker compose -p "$CNAME" down
    echo "Stopped $CNAME (workspace volume kept — ./djinn up $NAME to restore)"
fi
