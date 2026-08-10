"""Unit tests for the gear360 plugin's Python tools.

Covers the two things that have actually broken in practice:

  1. patch_upstream.py matches upstream source by exact string. A whitespace
     difference silently reduced a patch to "0 matches" once already, so every
     patch is exercised against a fixture, and a mutated fixture must be
     REPORTED rather than skipped quietly.
  2. gear360_watch.py's bookkeeping — result naming, retiring finished sources,
     dedupe, the partial-copy guard, failure quarantine — all of which decide
     whether footage is safe.

Nothing here builds the toolchain or runs ffmpeg; the stitcher itself is stubbed
so these stay fast and offline.

LIMITATION worth knowing: the fixtures below are copies of what the patcher
EXPECTS, not of upstream, and the two get edited together — so they cannot
detect upstream drift. Only a real clone can. That check is one command:

    docker run --rm -v "$PWD:/s:ro" ubuntu:24.04 bash -c '
      apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        ca-certificates git python3 >/dev/null
      git clone --depth 1 -q https://github.com/drNoob13/fisheyeStitcher.git /tmp/fs
      git clone --depth 1 -q https://github.com/bilde2910/stitch-gear360.git /tmp/sg
      python3 /s/patch_upstream.py /tmp/fs /tmp/sg'

Run it if a build starts warning that patches did not apply.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# The tests live next to the code they cover; tests/test_gear360.py loads them
# into the repo-wide suite.
PLUGIN = Path(__file__).parent
sys.path.insert(0, str(PLUGIN))

import gear360_watch  # noqa: E402
import patch_upstream  # noqa: E402


# ── Fixtures: verbatim slices of upstream that the patches anchor on ─────────
# Kept byte-exact (including the trailing spaces upstream has) so a drift in
# patch_upstream.py's expectations fails here rather than in an image build.
STITCHER_CPP = """    cv::Mat mls_map_x, mls_map_y;
    cv::FileStorage fs(m_map_path, cv::FileStorage::READ);
    if( fs.isOpened())
    {
        fs["Xd"] >> mls_map_x;
        fs["Yd"] >> mls_map_y;
        fs.release();
    }
    mls_map_x.copyTo(m_mls_map_x);
    mls_map_y.copyTo(m_mls_map_y);

    cv::Mat rightImg_crop, rightImg_mls_deformed;
    rightImg_crop = right_unwarped(cv::Rect(int(m_wd / 2) - (W_in / 2), 0,
                                   W_in, m_hd - 2));
    rightImg_mls_deformed = deform(rightImg_crop);

    uint16_t p_wid     = 55;
    uint16_t p_x1      = 90 - 15;
    uint16_t p_x2      = 1780 - 5;
    uint16_t p_x1_ref  = 2 * crop;
    uint16_t row_start = 590;
    uint16_t row_end   = 1320;
    uint16_t p_x2_ref  = m_ws - 2 * crop + 1;
"""

STITCH_CPP = """    // Video output
    int frame_fps    = VCap.get(CV_CAP_PROP_FPS);
    int frame_width  = VCap.get(CV_CAP_PROP_FRAME_WIDTH);

        // Encoding
        VOut << pano;

        count++;
"""

WRAPPER_SH = """#!/bin/bash
# Trimmed but syntactically complete: test_patched_wrapper_is_valid_bash runs
# `bash -n` over the patched result, so the fixture must parse on its own.
INPUTS=("$@")
CACHE_DIR=/tmp/cache
validate_input() {
    resolution=$(ffprobe -v error -select_streams v:0 -show_entries \\
        stream=width,height -of csv=s=x:p=0 "$1")
    if [ "$resolution" != "3840x1920" ]; then
        echo_fail
        echo_err "Error: '$1' is not 3840x1920 pixels."
        echo "This video file is not supported. Cannot continue."
        exit 1
    fi
    echo_ok
}

for ((i = 0; i < ${#INPUTS[@]}; i++)); do
    current="${INPUTS[$i]}"
    validate_input "$current"
    fisheyeStitcher \\
        --out_dir "${CACHE_DIR}" \\
        --img_nm "${i}" \\
        --video_path "${current}" \\
        --mls_map_path "${MLS_MAP_PATH}" \\
        --enb_lc "${USE_LC}" \\
        --enb_ra "${USE_RA}" \\
        --mode video
    echo "file '${CACHE_DIR}/${i}_blend_video.avi'" >> "${CACHE_DIR}/ffmpeg-queue.txt"
done

ffmpeg \\
    -f concat \\
    -safe 0 \\
    -i "${CACHE_DIR}/ffmpeg-queue.txt" \\
    -c copy "${CACHE_DIR}/ffmpeg-output.mp4"
"""

FIXTURES = {
    "fisheyeStitcher/src/fisheye_stitcher.cpp": STITCHER_CPP,
    "fisheyeStitcher/app/stitch.cpp": STITCH_CPP,
    "stitch-gear360/stitch-gear360.sh": WRAPPER_SH,
}


def make_tree(root: Path) -> tuple[Path, Path]:
    for rel, body in FIXTURES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root / "fisheyeStitcher", root / "stitch-gear360"


def run_patcher(fs: Path, sg: Path) -> int:
    return subprocess.call(
        [sys.executable, str(PLUGIN / "patch_upstream.py"), str(fs), str(sg)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class PatchUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fs, self.sg = make_tree(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_every_patch_applies_to_pristine_upstream(self):
        self.assertEqual(run_patcher(self.fs, self.sg), 0)

    def test_marker_written_to_each_file(self):
        run_patcher(self.fs, self.sg)
        for rel in FIXTURES:
            body = (Path(self.tmp.name) / rel).read_text()
            self.assertIn(patch_upstream.MARKER, body, rel)

    def test_idempotent(self):
        self.assertEqual(run_patcher(self.fs, self.sg), 0)
        after_first = {rel: (Path(self.tmp.name) / rel).read_text() for rel in FIXTURES}
        self.assertEqual(run_patcher(self.fs, self.sg), 0)
        for rel, text in after_first.items():
            self.assertEqual((Path(self.tmp.name) / rel).read_text(), text, rel)

    def test_fps_truncation_fixed(self):
        run_patcher(self.fs, self.sg)
        body = (self.fs / "app/stitch.cpp").read_text()
        self.assertIn("double frame_fps", body)
        self.assertNotIn("int frame_fps", body)

    def test_writer_reopened_at_real_size(self):
        # The empty-AVI bug: frames silently discarded on a size mismatch.
        run_patcher(self.fs, self.sg)
        body = (self.fs / "app/stitch.cpp").read_text()
        self.assertIn("pano.size()", body)
        self.assertIn("VOut.release()", body)

    def test_mls_grid_is_rescaled_not_skipped(self):
        # The seam regression: skipping deform() drops the static lens
        # calibration entirely. It must be rescaled, and still applied.
        run_patcher(self.fs, self.sg)
        body = (self.fs / "src/fisheye_stitcher.cpp").read_text()
        self.assertIn("mls_target", body)
        self.assertIn("cv::resize(mls_map_x", body)
        self.assertIn("rightImg_mls_deformed = deform(rightImg_crop);", body)
        self.assertNotIn("m_enb_refine_align ? deform", body)

    def test_alignment_constants_scale(self):
        run_patcher(self.fs, self.sg)
        body = (self.fs / "src/fisheye_stitcher.cpp").read_text()
        self.assertIn("gear360_scale", body)
        # The bare literals that overran a 2560-wide frame must be gone.
        self.assertNotIn("row_end   = 1320;", body)

    def test_wrapper_accepts_any_2to1_resolution(self):
        run_patcher(self.fs, self.sg)
        body = (self.sg / "stitch-gear360.sh").read_text()
        self.assertNotIn('"$resolution" != "3840x1920"', body)
        self.assertIn("res_w", body)

    def test_wrapper_uses_real_flag_names(self):
        run_patcher(self.fs, self.sg)
        body = (self.sg / "stitch-gear360.sh").read_text()
        self.assertIn("--enb_light_compen", body)
        self.assertIn("--enb_refine_align", body)
        self.assertNotIn('--enb_lc "${USE_LC}"', body)

    def test_refine_alignment_not_resolution_gated(self):
        # -a measurably tightens the seam at 2560x1280 once the grid is
        # rescaled and the constants scale, so it must not be forced off.
        run_patcher(self.fs, self.sg)
        body = (self.sg / "stitch-gear360.sh").read_text()
        self.assertIn('RA_NUM=0; [ "$USE_RA" = "true" ] && RA_NUM=1', body)
        self.assertNotIn("ignoring it", body)

    def test_wrapper_join_maps_source_audio(self):
        run_patcher(self.fs, self.sg)
        body = (self.sg / "stitch-gear360.sh").read_text()
        self.assertIn("ffmpeg-src-queue.txt", body)
        self.assertIn("-map 0:v -map 1:a?", body)

    def test_patched_wrapper_is_valid_bash(self):
        run_patcher(self.fs, self.sg)
        self.assertEqual(
            subprocess.call(["bash", "-n", str(self.sg / "stitch-gear360.sh")]), 0
        )

    def test_upstream_drift_is_reported_not_skipped(self):
        # The failure mode that already bit us: a patch quietly matching nothing.
        target = self.fs / "app/stitch.cpp"
        target.write_text(target.read_text().replace(
            "int frame_fps    = VCap.get(CV_CAP_PROP_FPS);",
            "int frame_fps = VCap.get(CV_CAP_PROP_FPS);  // reformatted upstream",
        ))
        self.assertNotEqual(run_patcher(self.fs, self.sg), 0)

    def test_drift_in_one_file_does_not_block_the_others(self):
        target = self.sg / "stitch-gear360.sh"
        target.write_text("# upstream rewritten entirely\n")
        run_patcher(self.fs, self.sg)
        self.assertIn(patch_upstream.MARKER,
                      (self.fs / "src/fisheye_stitcher.cpp").read_text())


# ── Watcher ──────────────────────────────────────────────────────────────────
class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.args = argparse.Namespace(
            input=str(self.root / "in"), output=str(self.root / "out"),
            failed=str(self.root / "failed"), scratch=str(self.root / "scratch"),
            complete=None, state=str(self.root / "state"),
            interval=1, settle=0, once=True, force=False, passthrough=[],
            # Off in the fixture so process() tests don't shell out to ffprobe;
            # the real CLI default (on) is asserted by
            # test_upscale_is_on_by_default.
            upscale=False,
        )
        self.w = gear360_watch.Watcher(self.args)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _src(self, name: str = "360_0300.MP4") -> Path:
        p = self.w.in_dir / name
        p.write_bytes(b"data")
        return p

    def test_complete_dir_defaults_under_input(self):
        self.assertEqual(self.w.complete_dir, self.w.in_dir / "complete")
        self.assertTrue(self.w.complete_dir.is_dir())

    def test_video_output_named_stitched(self):
        src = self._src()
        cmd = self.w.build_command(src, self.root / "work")
        self.assertTrue(cmd[-1].endswith("360_0300_stitched.mp4"), cmd[-1])
        self.assertNotIn("_360.mp4", cmd[-1])

    def test_force_flag_passed_through(self):
        self.args.force = True
        cmd = self.w.build_command(self._src(), self.root / "work")
        self.assertIn("--force", cmd)

    def test_photos_dispatch_to_hugin_path(self):
        src = self._src("360_0301.JPG")
        self.assertEqual(self.w.build_command(src, self.root / "w")[0],
                         "gear360pano.sh")

    def test_photos_get_an_explicit_output_dir(self):
        # gear360pano writes to html/data relative to the script, not to cwd.
        # Without -o the stitch succeeds and the harvest finds nothing, which
        # the watcher then reports as "no output produced".
        work = self.root / "w"
        cmd = self.w.build_command(self._src("360_0301.JPG"), work)
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], str(work))

    def test_non_media_is_not_dispatched(self):
        self.assertIsNone(
            self.w.build_command(self._src("notes.txt"), self.root / "w"))

    def test_upscale_is_on_by_default(self):
        # Asserted against the real CLI, not a hand-built Namespace.
        help_text = subprocess.check_output(
            [sys.executable, str(PLUGIN / "gear360_watch.py"), "--help"], text=True)
        self.assertIn("--no-upscale", help_text)
        self.assertNotIn("--upscale ", help_text)

    def test_no_upscale_uses_the_source_untouched(self):
        src = self._src()
        self.assertEqual(self.w.prepare_source(src, self.root / "work"), src)

    def test_photos_are_never_upscaled(self):
        src = self._src("360_0301.JPG")
        self.assertEqual(self.w.prepare_source(src, self.root / "work"), src)

    def _run_process(self, src, stub):
        """Drive the real process() with a stubbed stitcher on PATH."""
        binpath = self.root / "bin"
        binpath.mkdir(exist_ok=True)
        script = binpath / "stitch-gear360.sh"
        script.write_text(stub)
        script.chmod(0o755)
        (binpath / "gear360pano.sh").write_bytes(script.read_bytes())
        (binpath / "gear360pano.sh").chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{binpath}:{old}"
        try:
            return self.w.process(src)
        finally:
            os.environ["PATH"] = old

    def test_staged_upscale_is_not_shipped_as_a_result(self):
        # Exercises the real harvest in process(), not a copy of its filter: a
        # 3840x1920 un-stitched intermediate must never reach out/.
        src = self._src()
        stub = ('#!/bin/sh\n'
                'out=$(eval echo \\${$#})\n'
                'mkdir -p "$(dirname "$out")/upscaled"\n'
                'echo staged > "$(dirname "$out")/upscaled/360_0300.MP4"\n'
                'echo real > "$out"\n')
        self.assertTrue(self._run_process(src, stub))
        self.assertEqual([p.name for p in sorted(self.w.out_dir.iterdir())],
                         ["360_0300_stitched.mp4"])

    def test_success_publishes_retires_and_marks_done(self):
        src = self._src()
        stub = '#!/bin/sh\nout=$(eval echo \\${$#})\necho result > "$out"\n'
        self.assertTrue(self._run_process(src, stub))
        self.assertTrue((self.w.out_dir / "360_0300_stitched.mp4").exists())
        self.assertFalse(src.exists(), "source should have been retired")
        self.assertTrue((self.w.complete_dir / "360_0300.MP4").exists())
        self.assertTrue((self.w.done / "360_0300.MP4").exists())
        self.assertFalse(any(self.w.out_dir.glob(".tmp-*")), "staging left behind")

    def test_failure_quarantines_and_leaves_the_source_alone(self):
        src = self._src()
        self.assertFalse(self._run_process(src, '#!/bin/sh\nexit 3\n'))
        self.assertTrue(src.exists(), "source must not be retired on failure")
        self.assertTrue((self.w.failed / "360_0300.MP4").exists())
        self.assertTrue((self.w.failed_dir / "360_0300.MP4.log").exists())
        self.assertEqual(list(self.w.out_dir.iterdir()), [])

    def test_tool_exiting_zero_with_no_output_is_a_failure(self):
        # The empty-AVI lesson: a clean exit code is not evidence of success.
        src = self._src()
        self.assertFalse(self._run_process(src, '#!/bin/sh\nexit 0\n'))
        self.assertTrue((self.w.failed / "360_0300.MP4").exists())

    def test_results_never_overwrite_an_earlier_stitch(self):
        # SM-C200 names repeat across cards; a second 360_0300 must not silently
        # replace the first one's output.
        (self.w.out_dir / "360_0300_stitched.mp4").write_bytes(b"first card")
        stub = '#!/bin/sh\nout=$(eval echo \\${$#})\necho second > "$out"\n'
        self._run_process(self._src(), stub)
        self.assertEqual((self.w.out_dir / "360_0300_stitched.mp4").read_bytes(),
                         b"first card")
        self.assertTrue((self.w.out_dir / "360_0300_stitched_1.mp4").exists())

    def test_our_own_output_is_not_restitched(self):
        (self.w.in_dir / "360_0300_stitched.mp4").write_bytes(b"result")
        self.w.scan()
        self.assertEqual(list(self.w.out_dir.iterdir()), [])

    def test_retire_moves_source_into_complete(self):
        src = self._src()
        self.w.retire(src)
        self.assertFalse(src.exists())
        self.assertTrue((self.w.complete_dir / "360_0300.MP4").exists())

    def test_retire_never_clobbers_an_existing_name(self):
        (self.w.complete_dir / "360_0300.MP4").write_bytes(b"older")
        self.w.retire(self._src())
        self.assertEqual((self.w.complete_dir / "360_0300.MP4").read_bytes(), b"older")
        self.assertTrue((self.w.complete_dir / "360_0300_1.MP4").exists())

    def test_fingerprint_changes_when_file_grows(self):
        src = self._src()
        first = gear360_watch.fingerprint(src)
        src.write_bytes(b"data-and-more")
        self.assertNotEqual(first, gear360_watch.fingerprint(src))

    def test_already_handled_only_for_matching_fingerprint(self):
        src = self._src()
        fp = gear360_watch.fingerprint(src)
        (self.w.done / src.name).write_text(fp)
        self.assertTrue(self.w.already_handled(src, fp))
        self.assertFalse(self.w.already_handled(src, "999 999"))

    def test_failed_files_are_not_retried(self):
        src = self._src()
        fp = gear360_watch.fingerprint(src)
        (self.w.failed / src.name).write_text(fp)
        self.assertTrue(self.w.already_handled(src, fp))


if __name__ == "__main__":
    unittest.main()
