# gear360 — change log

Why this plugin does what it does. Every entry was found the hard way on
real SM-C200 footage; if behaviour surprises you, the reason is probably
here. Newest first.

Ships inside the image at `/opt/plugins/gear360/LOG.md`, so it is readable
from a container as well as from the repo.

Newest first. Everything here was found the hard way on real SM-C200 footage —
if behaviour surprises you, the reason is probably below.

**2026-08-10 — photo output collected properly.** `gear360pano` writes to
`html/data` relative to the *script*, not the working directory, so every photo
was stitching fine and then being reported `no output produced`. It is now
given an explicit `-o <work dir>`.

**2026-08-10 — upscaling to 3840x1920 became the default.** On 2560x1280
footage it is the difference between a clearly visible seam and essentially
none. Input already at 3840x1920 is untouched, so it only fires where it helps.
`--no-upscale` opts out.

**2026-08-10 — refine alignment (`-a`) ungated.** It had been forced off below
3840x1920 while the alignment constants were unscaled. They scale now, so `-a`
runs at any resolution and measurably tightens the seam. Always pass it.

**2026-08-10 — sources retire to `<input>/complete`; results are
`<source>_stitched.<ext>`.** Previously results were `_360` and sources stayed
in place. Sources are moved, never deleted.

**2026-08-09 — the MLS grid is rescaled, not skipped.** An earlier fix made
`deform()` conditional on refine alignment, on the mistaken belief it belonged
to that feature. It does not: it is the static lens calibration applied to
every frame, and skipping it is what made seams misalign badly. The grid is now
resized to the frame with its values scaled to match.

**2026-08-09 — the empty-AVI bug.** `cv::VideoWriter` silently discards frames
whose size differs from the size it was opened with, and the computed size was
one pixel off at 2560x1280 (1180 vs 1179). Every frame was dropped, producing a
valid-looking empty file with exit code 0. The writer is now reopened at the
real frame size. **This is why you must never call a stitch successful on exit
code alone** — check the output actually has a video stream.

**2026-08-09 — `-a` and `-l` had never worked.** The wrapper passed
`--enb_lc`/`--enb_ra` (the stitcher's options are
`--enb_light_compen`/`--enb_refine_align`) with the strings `"true"`/`"false"`,
parsed via `atoi()` to `0`. Both features were off in every stitch this wrapper
had ever produced.

**2026-08-09 — audio, frame rate, resolution gate.** Output was silent by
construction (the source was never an input to the muxing stage); NTSC rates
were truncated by an `int` assignment, running output ~1.59% long; and the
2560x1280 gate was wrapper-level, not a stitcher limit.
