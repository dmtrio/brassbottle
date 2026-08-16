#!/usr/bin/env bash
# Baked at image build by plugin.yml (`bash /opt/plugins/gear360/install.sh`).
# Network is unrestricted here — the container firewall is a runtime thing.
#
# Failure policy: this runs inside EVERY djinn image build, so only the apt
# step is allowed to fail the build. The three source steps are independently
# guarded — a broken upstream must not brick the build of containers that have
# nothing to do with 360 video, and a compile failure must not also cost us the
# wrapper scripts and calibration grid, which do not depend on it.
# `gear360-doctor` reports what actually made it in.
set -uo pipefail

GEAR=/opt/gear360
HERE="$(cd "$(dirname "$0")" && pwd)"

warn() { echo "[gear360] WARNING: $* — run gear360-doctor in the container" >&2; }

sudo mkdir -p "$GEAR"
sudo chown "$(id -u):$(id -g)" "$GEAR"

# ── 1. System packages (strict) ──────────────────────────────────────────────
#   libopencv-dev                 fisheyeStitcher's only hard dependency
#   ffmpeg                        frame extract / re-encode
#   hugin-tools + enblend/enfuse  gear360pano (photos)
#   exiftool/imagemagick/bc       gear360pano shell-script deps
set -e
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    cmake \
    libopencv-dev \
    ffmpeg \
    libncurses-dev \
    hugin-tools enblend enfuse \
    imagemagick libimage-exiftool-perl bc
sudo rm -rf /var/lib/apt/lists/*
set +e

# ── 2. spatialmedia (360 metadata injection) ─────────────────────────────────
# stitch-gear360 shells out to a `spatialmedia` executable. PyPI is the happy
# path; Google's repo is the fallback. Either way, guarantee the executable.
spatialmedia_install() {
    command -v spatialmedia >/dev/null 2>&1 && return 0
    pip3 install --break-system-packages spatialmedia \
        || git clone --depth 1 https://github.com/google/spatial-media.git "$GEAR/spatial-media"
    command -v spatialmedia >/dev/null 2>&1 && return 0

    local body
    if python3 -c 'import spatialmedia' 2>/dev/null; then
        body='exec python3 -m spatialmedia "$@"'
    elif [ -d "$GEAR/spatial-media/spatialmedia" ]; then
        body="exec python3 $GEAR/spatial-media/spatialmedia \"\$@\""
    else
        return 1
    fi
    printf '#!/bin/sh\n%s\n' "$body" | sudo tee /usr/local/bin/spatialmedia >/dev/null
    sudo chmod +x /usr/local/bin/spatialmedia
}
spatialmedia_install || warn "spatialmedia unavailable (metadata injection will fail)"

# ── 3. Sources ───────────────────────────────────────────────────────────────
# NOTE on the guard pattern used by these three steps: `set -e` inside a
# function invoked as `f || warn` is IGNORED for the entire body (POSIX: errexit
# is off for any command of an AND-OR list but the last) and then leaks to the
# top level. A subshell body inherits the same suppression, so it does not help
# either. Every fallible command therefore propagates explicitly with
# `|| return 1`.
clone_sources() {
    local repo name
    for repo in drNoob13/fisheyeStitcher bilde2910/stitch-gear360 ultramango/gear360pano; do
        name="${repo##*/}"
        [ -d "$GEAR/$name" ] && continue
        git clone --depth 1 "https://github.com/$repo.git" "$GEAR/$name" || return 1
    done
}
clone_sources || warn "could not clone the stitching repos"

# ── 3b. Patch upstream ───────────────────────────────────────────────────────
# Eight fixes: non-3840x1920 support, the empty-AVI writer bug, NTSC frame-rate
# truncation, the wrapper's resolution gate and dead -a/-l flags, and audio
# passthrough. Non-fatal — an unpatched build still stitches native 3840x1920
# video, so a patch that no longer applies must narrow what works rather than
# cost us the toolchain. See patch_upstream.py for what each one does.
python3 "$HERE/patch_upstream.py" "$GEAR/fisheyeStitcher" "$GEAR/stitch-gear360" \
    || warn "some upstream patches did not apply (see above)"

# ── 4. Grid + wrapper scripts ────────────────────────────────────────────────
# Deliberately BEFORE the compile: neither depends on it, and an unbuildable
# stitcher should not also cost us the Hugin photo path.
# stitch-gear360 hardcodes /usr/share/fisheye-stitcher/grid_*.yml.gz — ship
# whatever grid the repo carries rather than assuming the filename.
install_assets() {
    sudo mkdir -p /usr/share/fisheye-stitcher || return 1
    find "$GEAR/fisheyeStitcher" \( -name 'grid_*.yml.gz' -o -name 'grid_*.yml' \) \
        -exec sudo cp -n {} /usr/share/fisheye-stitcher/ \; || return 1
    ls /usr/share/fisheye-stitcher/grid_* >/dev/null 2>&1 || {
        echo "no calibration grid found in the fisheyeStitcher checkout" >&2
        return 1
    }
    local s
    for s in "$GEAR/stitch-gear360"/*.sh "$GEAR/gear360pano"/*.sh; do
        [ -f "$s" ] || continue
        chmod +x "$s" || return 1
        sudo ln -sf "$s" "/usr/local/bin/$(basename "$s")" || return 1
    done
}
install_assets || warn "could not install the calibration grid / wrapper scripts"

# ── 5. Build fisheyeStitcher ─────────────────────────────────────────────────
build_stitcher() {
    # Upstream targets OpenCV 3 and still uses C-API constants that OpenCV 4's
    # C++ headers dropped: CV_INTER_LINEAR / CV_TM_CCORR_NORMED (imgproc) and
    # CV_CAP_PROP_* (videoio). Ubuntu 24.04 ships OpenCV 4.6, so it will not
    # build as-is. Both legacy headers are still shipped and still define those
    # constants, so force-including them fixes the whole class of error without
    # patching upstream source (which would rot on every upstream change).
    #
    # -I/usr/include/opencv4 is required for the force-includes to resolve
    # during CMake's own compiler probes, which run before OpenCV's include
    # directories are applied — without it, configure fails.
    cmake -S "$GEAR/fisheyeStitcher" -B "$GEAR/fisheyeStitcher/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CXX_STANDARD=17 \
        -DCMAKE_CXX_FLAGS="-I/usr/include/opencv4 \
-include opencv2/imgproc/types_c.h \
-include opencv2/videoio/legacy/constants_c.h" || return 1
    cmake --build "$GEAR/fisheyeStitcher/build" -j"$(nproc)" || return 1

    # Resolve by TARGET NAME, never "first executable in build/". A CMake build
    # tree is full of probe binaries (CompilerIdC/a.out,
    # CMakeDetermineCompilerABI_CXX.bin) that run, print nothing and exit 0 — so
    # a wrong pick looks exactly like a working install that does nothing.
    # Upstream sets RUNTIME_OUTPUT_DIRECTORY to build/bin.
    local bin=""
    local cand
    for cand in "$GEAR/fisheyeStitcher/build/bin/fisheyeStitcher" \
                "$GEAR/fisheyeStitcher/build/fisheyeStitcher"; do
        [ -x "$cand" ] && { bin="$cand"; break; }
    done
    [ -n "$bin" ] || bin="$(find "$GEAR/fisheyeStitcher/build" -type f -name fisheyeStitcher \
                              -perm -u+x -not -path '*/CMakeFiles/*' -print -quit)"
    [ -n "$bin" ] || { echo "no fisheyeStitcher binary in the build tree" >&2; return 1; }
    sudo ln -sf "$bin" /usr/local/bin/fisheyeStitcher || return 1
}
build_stitcher || warn "fisheyeStitcher failed to build"

# ── 6. Our own tools ─────────────────────────────────────────────────────────
# Symlinked, not copied: /opt/plugins is baked into the image, so the scripts
# are already there and stay editable in one place.
sudo ln -sf "$HERE/gear360_doctor.py" /usr/local/bin/gear360-doctor
sudo ln -sf "$HERE/gear360_watch.py"  /usr/local/bin/gear360-watch
sudo chmod +x "$HERE/gear360_doctor.py" "$HERE/gear360_watch.py"

exit 0
