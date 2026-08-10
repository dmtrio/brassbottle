#!/usr/bin/env python3
"""Patch the vendored upstream sources at image build.

Four fixes across two projects, applied to the clones under /opt/gear360 before
fisheyeStitcher is compiled. Each is an exact-match text replacement that fails
loudly rather than applying partially, so an upstream change is reported instead
of silently producing a subtly wrong build.

fisheyeStitcher (src/fisheye_stitcher.cpp, app/stitch.cpp)
  1. conditional MLS deform      — non-3840x1920 support
  2. scaled alignment constants  — non-3840x1920 support
  3. frame-rate truncation       — output ran 1.59% slow on NTSC sources

stitch-gear360 (stitch-gear360.sh)
  4. audio passthrough           — output was silent by construction

Usage: patch_upstream.py <fisheyeStitcher-dir> <stitch-gear360-dir>
Exit code is the number of patches that could not be applied (0 = all good).
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "PATCHED-BY-GEAR360"

# repo-relative path -> list of (description, exact find, replace)
PATCHES: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "fisheyeStitcher": {
        "src/fisheye_stitcher.cpp": [
            (
                "rescale the MLS grid to this resolution",
                """    mls_map_x.copyTo(m_mls_map_x);
    mls_map_y.copyTo(m_mls_map_y);""",
                f"""    // {MARKER}: the MLS grid is the STATIC LENS CALIBRATION that aligns the
    // right fisheye to the left. Upstream applies it on every frame regardless
    // of refine alignment (which only adds a template-matching pass on top), so
    // skipping it is what makes seams visibly misaligned.
    //
    // grid_xd_yd_3840x1920.yml.gz is sized for that frame's right-hand crop and
    // holds ABSOLUTE pixel coordinates. cv::remap takes dst's size from the map,
    // so at any other resolution an unscaled grid both returns a wrong-sized Mat
    // and samples the wrong places. Resize the maps to this frame's crop
    // (m_ws x m_hd-2, exactly what deform() is handed) and scale their values by
    // the same factor. At 3840x1920 the sizes already match and this is a no-op.
    {{
        const cv::Size mls_target(m_ws, m_hd - 2);
        if( mls_target.width > 0 && mls_target.height > 0
            && !mls_map_x.empty() && mls_map_x.size() != mls_target )
        {{
            const double sx = static_cast<double>(mls_target.width)
                              / static_cast<double>(mls_map_x.cols);
            const double sy = static_cast<double>(mls_target.height)
                              / static_cast<double>(mls_map_x.rows);
            cv::resize(mls_map_x, mls_map_x, mls_target, 0, 0, cv::INTER_LINEAR);
            cv::resize(mls_map_y, mls_map_y, mls_target, 0, 0, cv::INTER_LINEAR);
            mls_map_x *= sx;
            mls_map_y *= sy;
            std::cout << "gear360: MLS grid rescaled to " << mls_target << "\\n";
        }}
    }}
    mls_map_x.copyTo(m_mls_map_x);
    mls_map_y.copyTo(m_mls_map_y);""",
            ),
            (
                "resolution-scaled alignment constants",
                """    uint16_t p_wid     = 55;
    uint16_t p_x1      = 90 - 15;
    uint16_t p_x2      = 1780 - 5;
    uint16_t p_x1_ref  = 2 * crop;
    uint16_t row_start = 590;
    uint16_t row_end   = 1320;""",
                f"""    // {MARKER}: these are empirical for 3840x1920 and are used to build ROIs
    // BEFORE m_enb_refine_align is checked, so they must stay in bounds at every
    // resolution. Scale with the image; at 1920 the factor is 1.0 and the values
    // are unchanged from upstream.
    const double gear360_scale = static_cast<double>(m_ws) / 1920.0;
    uint16_t p_wid     = static_cast<uint16_t>(55 * gear360_scale);
    uint16_t p_x1      = static_cast<uint16_t>((90 - 15) * gear360_scale);
    uint16_t p_x2      = static_cast<uint16_t>((1780 - 5) * gear360_scale);
    uint16_t p_x1_ref  = 2 * crop;
    uint16_t row_start = static_cast<uint16_t>(590 * gear360_scale);
    uint16_t row_end   = static_cast<uint16_t>(1320 * gear360_scale);""",
            ),
        ],
        "app/stitch.cpp": [
            (
                "frame-rate truncation",
                "    int frame_fps    = VCap.get(CV_CAP_PROP_FPS);",
                f"""    // {MARKER}: VideoCapture::get() returns a double. Assigning it to an int
    // truncates toward zero, so every NTSC-family rate is stamped onto the
    // output AVI a whole frame slow (59.94->59, 29.97->29, 23.976->23) and the
    // result plays ~1.59% long. VideoWriter::open() takes fps as a double.
    double frame_fps = VCap.get(CV_CAP_PROP_FPS);""",
            ),
            (
                "reopen the writer at the real frame size",
                """        // Encoding
        VOut << pano;""",
                f"""        // {MARKER}: cv::VideoWriter SILENTLY DISCARDS any frame whose size
        // differs from the size it was opened with. The writer above is opened
        // with a COMPUTED size (Wd x Hd) that only happens to match what
        // stitch() returns at 3840x1920 — at 2560x1280 every frame was thrown
        // away, producing a structurally valid but EMPTY avi (correct header,
        // zero packets, exit code 0). Downstream that surfaces as "unspecified
        // pixel format" on probe and "video:0kB" on remux. Reopen at the real
        // size on the first frame; the printed line is the cheapest proof that
        // frames are actually reaching the file.
        if( count == 0 )
        {{
            if( pano.size() != cv::Size(Wd, Hd) )
            {{
                VOut.release();
                VOut.open( video_out_name, cv::VideoWriter::fourcc('X','2','6','4'),
                           frame_fps, pano.size() );
                if( !VOut.isOpened() )
                {{
                    CV_Error_(cv::Error::StsBadArg,
                              ("Error opening video: %s", video_out_name.c_str()));
                }}
            }}
            std::cout << "Output: " << pano.size() << " @ " << frame_fps
                      << " fps\\n";
        }}
        // Encoding
        VOut << pano;""",
            ),
        ],
    },
    "stitch-gear360": {
        "stitch-gear360.sh": [
            (
                "accept any 2:1 dual-fisheye resolution",
                """    if [ "$resolution" != "3840x1920" ]; then
        echo_fail
        echo_err "Error: '$1' is not 3840x1920 pixels."
        echo "This video file is not supported. Cannot continue."
        exit 1
    fi""",
                f"""    # {MARKER}: upstream hard-codes the SM-C200's 3840x1920 mode, but the
    # camera also shoots 2560x1280 and the stitcher derives its geometry from
    # the frame size at runtime. Accept any even 2:1 dual-fisheye frame.
    res_w="${{resolution%x*}}"
    res_h="${{resolution#*x}}"
    if [ -z "$res_w" ] || [ -z "$res_h" ] \\
       || [ $((res_w % 2)) -ne 0 ] || [ $((res_h % 2)) -ne 0 ] \\
       || [ "$res_w" -ne $((res_h * 2)) ]; then
        echo_fail
        echo_err "Error: '$1' is $resolution, not an even 2:1 dual-fisheye frame."
        echo "This video file is not supported. Cannot continue."
        exit 1
    fi""",
            ),
            (
                "pass real stitcher flag names with numeric values",
                """    fisheyeStitcher \\
        --out_dir "${CACHE_DIR}" \\
        --img_nm "${i}" \\
        --video_path "${current}" \\
        --mls_map_path "${MLS_MAP_PATH}" \\
        --enb_lc "${USE_LC}" \\
        --enb_ra "${USE_RA}" \\
        --mode video""",
                f"""    # {MARKER}: upstream passes --enb_lc / --enb_ra, but the stitcher's options
    # are --enb_light_compen / --enb_refine_align and it parses their values with
    # atoi() — so the strings "true"/"false" both evaluated to 0 and neither
    # feature ever ran, whatever -a / -l were set to. Pass the real names with
    # numeric values. Refine alignment additionally needs the 3840x1920 MLS grid,
    # so it stays off at any other size regardless of -a.
    cur_res=$(ffprobe -v error -select_streams v:0 \\
        -show_entries stream=width,height -of csv=s=x:p=0 "$current")
    LC_NUM=0; [ "$USE_LC" = "true" ] && LC_NUM=1
    RA_NUM=0
    if [ "$USE_RA" = "true" ]; then
        if [ "$cur_res" = "3840x1920" ]; then
            RA_NUM=1
        else
            echo_err "Warning: -a needs 3840x1920 (this is $cur_res); ignoring it."
        fi
    fi
    fisheyeStitcher \\
        --out_dir "${{CACHE_DIR}}" \\
        --img_nm "${{i}}" \\
        --video_path "${{current}}" \\
        --mls_map_path "${{MLS_MAP_PATH}}" \\
        --enb_light_compen "$LC_NUM" \\
        --enb_refine_align "$RA_NUM" \\
        --mode video""",
            ),
            (
                "queue source files for audio",
                """    echo "file '${CACHE_DIR}/${i}_blend_video.avi'" >> "${CACHE_DIR}/ffmpeg-queue.txt"
done""",
                f"""    echo "file '${{CACHE_DIR}}/${{i}}_blend_video.avi'" >> "${{CACHE_DIR}}/ffmpeg-queue.txt"
    # {MARKER}: keep a parallel queue of the SOURCE files so the join below can
    # take their audio. fisheyeStitcher reads video through OpenCV, which has no
    # audio support at all, so its AVI is video-only and the original audio has
    # no other path to the output.
    echo "file '$(readlink -f "$current")'" >> "${{CACHE_DIR}}/ffmpeg-src-queue.txt"
done""",
            ),
            (
                "map source audio into the join",
                """ffmpeg \\
    -f concat \\
    -safe 0 \\
    -i "${CACHE_DIR}/ffmpeg-queue.txt" \\
    -c copy "${CACHE_DIR}/ffmpeg-output.mp4\"""",
                f"""# {MARKER}: second concat input supplies the source audio. -map 1:a? keeps it
# optional so a silent source still joins. Audio is re-encoded because
# concatenating AAC across separate files with -c copy is unreliable.
ffmpeg \\
    -f concat \\
    -safe 0 \\
    -i "${{CACHE_DIR}}/ffmpeg-queue.txt" \\
    -f concat \\
    -safe 0 \\
    -i "${{CACHE_DIR}}/ffmpeg-src-queue.txt" \\
    -map 0:v -map 1:a? \\
    -c:v copy -c:a aac \\
    "${{CACHE_DIR}}/ffmpeg-output.mp4\"""",
            ),
        ],
    },
}


def patch_file(path: Path, patches: list[tuple[str, str, str]]) -> int:
    """Apply every patch to one file. Returns the number that failed."""
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"gear360 patch: cannot read {path}: {exc}", file=sys.stderr)
        return len(patches)

    if MARKER in text:
        print(f"gear360 patch: {path.name}: already applied")
        return 0

    failures = 0
    for description, find, replace in patches:
        count = text.count(find)
        if count != 1:
            print(
                f"gear360 patch: FAILED — '{description}' matched {count} times "
                f"in {path.name} (expected 1). Upstream changed; update "
                f"plugins/gear360/patch_upstream.py.",
                file=sys.stderr,
            )
            failures += 1
            continue
        text = text.replace(find, replace)

    if failures == len(patches):
        return failures

    path.write_text(text)
    applied = len(patches) - failures
    print(f"gear360 patch: {path.name}: applied {applied}/{len(patches)}")
    return failures


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <fisheyeStitcher-dir> <stitch-gear360-dir>",
              file=sys.stderr)
        return 2

    roots = {"fisheyeStitcher": Path(sys.argv[1]), "stitch-gear360": Path(sys.argv[2])}
    failures = 0
    for repo, files in PATCHES.items():
        for rel, patches in files.items():
            failures += patch_file(roots[repo] / rel, patches)

    if failures:
        print(f"gear360 patch: {failures} patch(es) did not apply", file=sys.stderr)
    return failures


if __name__ == "__main__":
    sys.exit(main())
