#!/usr/bin/env bash
# Register the baked @tinfoilsh/pi-provider package into pi at `./djinn up`.
# The hook's shell has the image's final ENV PATH (Dockerfile: ~/.agent-shims
# and ~/.fnm/aliases/default/bin first), so `pi` resolves without ~/.bashrc.
# A bottle may enable this plugin without the pi agent: say so once, exit 0.
# Idempotent (pi keeps one packages[] entry) and offline (deps were baked).
set -eu
command -v pi >/dev/null 2>&1 || { echo "tinfoil: pi is not installed in this image; nothing to register" >&2; exit 0; }
exec pi install "${1:-/opt/plugins/tinfoil/pkg/package}"
