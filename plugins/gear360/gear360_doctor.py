#!/usr/bin/env python3
"""Report which parts of the gear360 toolchain are present in this image.

The install's source steps are deliberately non-fatal (a broken upstream must
not brick every container's build), so a missing tool is a realistic state.
This is how you find out. Exits non-zero if anything is missing or broken.
"""
import glob
import os
import shutil
import sys

# Every executable the pipeline shells out to, and what it is for.
COMMANDS = [
    ("ffmpeg", "frame extract / re-encode"),
    ("ffprobe", "resolution probe"),
    ("cmake", "build tooling"),
    ("nona", "Hugin remapper (photos)"),
    ("enblend", "Hugin blender (photos)"),
    ("enfuse", "Hugin exposure fusion (photos)"),
    ("exiftool", "photo metadata"),
    ("spatialmedia", "360 metadata injection"),
    ("fisheyeStitcher", "the stitcher itself"),
]

# Present-but-useless is worse than absent: these run, print nothing and exit 0.
CMAKE_ARTIFACTS = ("CMakeFiles", "CompilerId")

GRID_GLOB = "/usr/share/fisheye-stitcher/grid_*"
WRAPPERS = ["stitch-gear360.sh", "gear360pano.sh"]


def main() -> int:
    ok = True

    for name, purpose in COMMANDS:
        path = shutil.which(name)
        if not path:
            print(f"  MISSING {name:<16} {purpose}")
            ok = False
            continue

        # A CMake build tree is full of probe binaries. If the PATH symlink
        # caught one, `which` succeeds and everything looks fine while nothing
        # works — so check what it actually resolves to.
        real = os.path.realpath(path)
        if any(marker in real for marker in CMAKE_ARTIFACTS):
            print(f"  BROKEN  {name:<16} → {real}")
            print(f"          that is a CMake artifact, not {name}")
            ok = False
        else:
            print(f"  ok      {name:<16} {purpose}")

    grids = sorted(glob.glob(GRID_GLOB))
    if grids:
        print(f"  ok      {'grid':<16} {', '.join(os.path.basename(g) for g in grids)}")
    else:
        print(f"  MISSING {'grid':<16} no calibration grid in /usr/share/fisheye-stitcher")
        ok = False

    for wrapper in WRAPPERS:
        if shutil.which(wrapper):
            print(f"  ok      {wrapper}")
        else:
            print(f"  absent  {wrapper}")

    print()
    if ok:
        print("gear360: toolchain complete")
    else:
        print("gear360: INCOMPLETE — see plugins/gear360/README.md")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
