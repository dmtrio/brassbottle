## Gear 360 stitching

This container has the SM-C200 dual-fisheye → equirectangular toolchain baked in
at `/opt/gear360`, with binaries on `PATH`. Source footage is unstitched dual
fisheye; every deliverable is stitched equirectangular.

**Run `gear360-doctor` first.** The build step that produces `fisheyeStitcher`
and the calibration grid is allowed to fail without failing the image build, so
a missing tool is a realistic state, not an impossible one. It exits non-zero
and names what is absent. Do not work around a `MISSING` result by improvising a
different tool — report it.

### Where files go

| Path | Use |
|---|---|
| `/artifacts/in` | source footage, copied in from the Mac |
| `/artifacts/out` | finished stitched output — Mac-visible |
| `/workspace/scratch` | intermediate frames, temp files |

`/artifacts` is a bind mount to the host. It is fine for reading one source MP4
and writing one result, and **bad** for the tens of thousands of small PNG
writes a frame-extract pass produces — that will crawl. Keep every intermediate
step in `/workspace/scratch`, which is a named volume, and copy only the final
file to `/artifacts/out`.

### Commands

`stitch-gear360.sh` handles **any even 2:1 dual-fisheye resolution**, including
this camera's 2560x1280 — the plugin patches upstream at image build. Use it as
the single entry point; it does frames, stitch, join, audio and 360 metadata.

Always pass **`-a -l`**. Both work at every resolution, and `-a` (refine
alignment) is the single biggest seam-quality lever — measured on real footage
it removes most of the visible boundary band. Neither is gated on resolution.

`gear360-watch` **upscales to 3840x1920 by default** before stitching anything
smaller, and that is the single biggest quality difference on this camera —
measured on real 2560x1280 footage the seam goes from clearly visible to
essentially invisible. The alignment constants are empirical for 3840x1920 and
only approximate when scaled down, which is why. Input already at 3840x1920 is
passed through untouched, so it costs nothing there.

Do not pass `--no-upscale` unless the user asks for speed over quality, and say
what they are trading. When stitching by hand rather than through the watcher,
upscale first — keep the original filename, and stage it in /workspace/scratch,
never /artifacts.

```bash
# video — 2560x1280 and 3840x1920 both work
stitch-gear360.sh -a -l /artifacts/in/CLIP.MP4 /artifacts/out/CLIP_360.mp4

# photos (Hugin-based)
gear360pano.sh /artifacts/in/PHOTO.JPG

# drop-folder ingest: stitch everything in /artifacts/in
gear360-watch --once -a -l   # drain the backlog and exit
gear360-watch -a -l          # keep watching (polls every 10s)
```

### Resolution

Both SM-C200 video modes work through `stitch-gear360.sh`: 3840x1920 and
2560x1280. Do **not** upscale 2560x1280 to get past a resolution check — there
is no longer one to get past, and upscaling costs two resampling passes and
2.25x the pixels for nothing.

This relies on build-time patches to upstream (`patch_upstream.py`). Stock
upstream fails on 2560x1280 two ways: an out-of-bounds `cv::Rect` abort, and —
more insidiously — a silently **empty** output file, because `cv::VideoWriter`
discards frames whose size does not match the one it was opened with, and the
computed size is one pixel off. Symptoms of the patches not applying:

- `(-215:Assertion failed) 0 <= roi.x` — the crash case.
- A stitch that "succeeds" but whose output has no video stream, or an
  intermediate AVI of a few KB. Check `ffprobe` for `nb_frames` and a real
  `pix_fmt`; `unspecified pixel format` means an empty file.

In either case say the patches did not apply during the image build. Do not
work around it by upscaling, and do not report a stitch as successful on exit
code alone — confirm the output actually has a video stream.

`stitch-gear360.sh` needs **original SM-C200 filenames**; a renamed file needs
`--force`. Passing several inputs concatenates them into one output — do not do
that unless asked, since it is not obvious afterwards which clips went in.

### The watcher

`gear360-watch` is the drop-folder ingest. Prefer it over hand-rolling a loop:
it already handles the things a naive loop gets wrong — partial copies still
arriving from the SD card, re-stitching files it already did, two jobs running
at once, and half-written output appearing in `/artifacts/out`.

- Use `--once` when the user wants a batch drained now; use the bare form only
  when they have asked for continuous watching, since it does not return.
- Start it in the background (`gear360-watch -a -l &`) if you need to keep
  working, and tell the user it is running. Do not leave a foreground watcher
  blocking the session.
- Only one can run at a time — a second exits immediately by design. If you get
  that message, a watcher is already up; do not try to work around the lock.
- It polls (every 10s), so a newly dropped file takes a few seconds to be
  noticed. That is expected, not a fault.
- On failure it writes `/artifacts/failed/<file>.log` and will not retry that
  file. Read the log and report the cause; clear the marker in
  `/artifacts/.gear360/failed/` only once the cause is actually fixed.

### Working rules

- **Stitch one short test clip before any batch.** A full clip is minutes of
  compute per file; a bad flag choice discovered after 40 files is expensive.
- **Never delete anything in `/artifacts/in`.** It is the copy off the SD card
  and may be the only one.
- Always verify metadata after stitching — `python3 -m spatialmedia <file>`
  should report spherical XML. Without it the result plays as a flat warped
  rectangle and looks like a stitching failure when it is not.
- You cannot visually judge a 360 result from a frame grab; a still of
  equirectangular output looks distorted even when correct. Report seam or
  exposure concerns and let the user view it in a real 360 player rather than
  concluding the stitch failed.
- Visible seams are **not** fixable with `-a` at 2560x1280 (it is ignored
  there). The static MLS lens calibration is applied at every resolution via a
  build-time grid rescale; if seams look badly misaligned, check the build log
  for `gear360: MLS grid rescaled to ...` — its absence at a non-3840x1920
  resolution means the calibration is not being applied.
- `ffmpeg`'s `v360=dfisheye:equirect` filter is a quick structural check only.
  It has no per-lens calibration, so it is not a substitute for the stitcher
  and its output should not be delivered as a result.
