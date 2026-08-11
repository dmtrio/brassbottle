#!/bin/bash
# down.sh <name> [--purge] — stop and remove a container.
# Default keeps the workspace volume (code) — ./up.sh <name> restores the
# container around it. --purge also deletes the volume and derived keys;
# the manifest, secrets.env, and artifacts/<name>/ always survive.

set -e

NAME="$1"
[ -n "$NAME" ] || { echo "Usage: ./down.sh <name> [--purge]"; exit 1; }

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/src/common.sh"   # sets BASE_PATH

if [ "$2" = "--purge" ]; then
    # IMAGE_TAG passed so compose can resolve (and we can remove) the
    # per-container image dev-agent:<name>, otherwise it orphans on disk.
    IMAGE_TAG="$NAME" docker compose -p "dev-agent-$NAME" down -v
    docker image rm "dev-agent:$NAME" 2>/dev/null || true
    rm -rf "$BASE_PATH/keys/$NAME"
    # The generated plugin-volume overlay (up.sh writes it per container) is
    # derived state like keys/, so a purge collects it too.
    rm -f "$BASE_PATH/compose/$NAME.plugins.yml"
    echo "Purged dev-agent-$NAME (container, volume, image, derived keys). Kept: manifest, secrets.env, artifacts/$NAME/"
else
    docker compose -p "dev-agent-$NAME" down
    echo "Stopped dev-agent-$NAME (workspace volume kept — ./up.sh $NAME to restore)"
fi
