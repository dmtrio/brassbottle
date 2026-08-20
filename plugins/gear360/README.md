# gear360

Samsung Gear 360 (**SM-C200**) dual-fisheye → equirectangular stitching, baked
into the image. Photos and video.

The camera writes unstitched dual-fisheye files to its SD card (video: 3840x1920
MP4). Samsung's ActionDirector is dead — its activation server is offline — so
stitching has to happen locally. This plugin bakes the open-source replacement.

| Piece | What it does |
|---|---|
| [`fisheyeStitcher`](https://github.com/drNoob13/fisheyeStitcher) | C++/OpenCV stitcher calibrated for SM-C200 3840x1920, ~70-90ms/frame |
| [`stitch-gear360`](https://github.com/bilde2910/stitch-gear360) | video wrapper: extract frames → stitch → re-encode → inject metadata |
| [`gear360pano`](https://github.com/ultramango/gear360pano) | Hugin-based path, better for **photos** |
| `spatialmedia` | injects the spherical-360 metadata players look for |
| ffmpeg, Hugin, enblend/enfuse, exiftool | the surrounding media toolchain |

Sources are cloned and built at image build into `/opt/gear360`; binaries and
wrapper scripts land on `PATH`. The calibration grid goes to
`/usr/share/fisheye-stitcher/`, which is where `stitch-gear360` hardcodes it.

## Not a server

No `mcp:`, no `secrets:`, no `run.sh`, no `host_port:` — this is the binary-only
plugin shape (like [`ngrok`](../ngrok/README.md), minus the secret). Nothing needs runtime
network either: stitching is pure local compute, so there is no `egress:` grant.

**Consequence worth knowing:** listing `gear360` in a bottle's `plugins:` has
exactly one effect — `AGENTS.md` is merged into that container's agent rules.
The install bakes into **every** djinn image regardless, because the
Dockerfile's bake loop globs all of `plugins/*/plugin.yml` and does not consult
any bottle. See "Cost" below.

## Layout

`plugin.yml`'s `install:` is one line. The work lives in sibling files, because
`COPY plugins /opt/plugins` puts the whole directory in the image — so these
stay ordinary scripts you can read, lint and run directly rather than a wall of
bash embedded in a YAML string.

| File | Runs | Does |
|---|---|---|
| `install.sh` | image build | apt, sources, patches, compile, `PATH` wiring |
| `patch_upstream.py` | image build | 8 exact-match patches to the vendored clones |
| `gear360_doctor.py` | runtime (`gear360-doctor`) | reports what actually made it in |
| `gear360_watch.py` | runtime (`gear360-watch`) | drop-folder ingest |
| `test_gear360.py` | host | 34 unit tests; auto-discovered by the repo suite |
| `LOG.md` | — | change history: what broke, what was patched, why |
| `patch_upstream.py` note | — | a file is patched **all-or-nothing**; a partial match leaves it untouched |

The two runtime tools are **symlinked** from `/opt/plugins/gear360/`, not
copied, so there is one copy in the image and one place to edit. Tests live
beside the code and run either from this directory
(`python3 -m unittest discover`) or as part of the repo suite, which picks up
any `plugins/*/test_*.py` automatically. They are excluded from the image.

## Cost

This plugin adds OpenCV, Hugin and ffmpeg to every container's image.

- **Disk** is worse than it looks. The bake `RUN` sits *after* the per-agent
  install layers, whose build args differ per container, so the parent layer
  differs and each distinct agent-toolset combination gets its own copy of
  OpenCV + Hugin + ffmpeg rather than sharing one. Moving the apt step above
  the agent installs would fix that.
- **Rebuild time** is the real tax. `COPY plugins /opt/plugins` invalidates
  whenever *any* `plugin.yml` changes, which re-runs the whole bake loop — so
  every future plugin edit costs an extra couple of minutes on every
  container's next `djinn up`.

If that becomes annoying, the escape hatch is to move this directory out of
`plugins/` and back to a hand-run script; nothing else depends on it.

## Failure behaviour

The apt step is **strict** — Ubuntu archives are reliable, and a failure there
is a genuine problem. Every package name is verified present on `ubuntu:24.04`.

The three source steps — clone, install grid + wrappers, compile — are
**independently guarded and non-fatal**. They run inside every image build, so
an upstream repo that moves or stops compiling would otherwise brick the build
of containers that have nothing to do with 360 video. They are separate on
purpose: the grid and the Hugin photo wrappers do not depend on the compile, so
a broken stitcher must not also cost you those. Each warns and you find out
with:

```bash
gear360-doctor        # reports every component; exits non-zero if incomplete
```

Run that first if anything behaves oddly. `MISSING fisheyeStitcher` or
`MISSING calibration grid` means the build step failed — check the `[gear360]
WARNING` line in the image build output.

`BROKEN fisheyeStitcher → …CMakeFiles…` means the `PATH` symlink caught a CMake
probe binary (`CMakeDetermineCompilerABI_CXX.bin`, `CompilerIdC/a.out`) instead
of the real target. Those run, print nothing and exit 0, so the symptom is a
command that appears installed and silently does nothing. The install block
resolves the binary by target name (upstream sets `RUNTIME_OUTPUT_DIRECTORY` to
`build/bin`) and refuses to link anything under `CMakeFiles/`; the doctor check
exists because this failure is otherwise invisible.

### The OpenCV 4 build flags

Upstream targets OpenCV 3 and still uses C-API constants that OpenCV 4's C++
headers removed — `CV_INTER_LINEAR` and `CV_TM_CCORR_NORMED` in `src/`,
`CV_CAP_PROP_*` in `app/stitch.cpp`. Ubuntu 24.04 ships OpenCV 4.6, so
**fisheyeStitcher does not compile as-is**; a plain `cmake --build` fails.

Both legacy headers are still shipped and still define those constants, so
`install.sh` force-includes them rather than patching upstream source, which
would rot on the next upstream change:

```
-I/usr/include/opencv4
-include opencv2/imgproc/types_c.h
-include opencv2/videoio/legacy/constants_c.h
```

The `-I` is not redundant. CMake runs its own compiler probes before OpenCV's
include directories are applied, and without it the force-includes fail to
resolve and **configure** dies rather than the build.

### Defensive bits

Two things could not be verified at authoring time, so both discover rather
than assume:

- the `spatialmedia` PyPI package name (falls back to cloning
  `google/spatial-media` and shimming an executable)
- the calibration grid filename (globs `grid_*.yml*` out of the repo rather
  than assuming `grid_xd_yd_3840x1920.yml.gz`)

## Usage

```yaml
# containers/<name>.yml
plugins: [gear360]
memory: 8g          # OpenCV compile + ffmpeg encode want headroom
```

Then, in the container:

```bash
gear360-doctor

# video — any even 2:1 dual-fisheye MP4 (3840x1920 and 2560x1280 both work).
# Needs ORIGINAL SM-C200 filenames; renamed files need --force. Multiple
# inputs concatenate into one output.
#   -a  refine alignment (use it — biggest seam-quality lever)
#   -l  light compensation
stitch-gear360.sh -a -l /artifacts/in/CLIP.MP4 /artifacts/out/CLIP_stitched.mp4

# photos
gear360pano.sh /artifacts/in/PHOTO.JPG
```

## Watching a folder

`gear360-watch` polls the input folder and stitches whatever lands in it —
`.MP4` via `stitch-gear360.sh`, `.JPG`/`.JPEG` via `gear360pano.sh`, everything
else ignored. Test on a couple of files first, then leave it running:

```bash
gear360-watch --once -a -l    # drain the current backlog and exit
gear360-watch -a -l           # then watch forever (Ctrl-C, or TERM, to stop)
gear360-watch -h              # all flags
```

It is **polling, not inotify** — host-side writes to a Docker Desktop bind mount
don't reliably deliver inotify events into the container, so a file dropped on
the Mac may never fire one. Default interval is 10s (`-n`).

Behaviour worth knowing:

- **One job at a time**, enforced by an `flock` on the state dir. A second
  `gear360-watch` exits rather than racing the first. A clip is minutes of
  pegged CPU; concurrency would only thrash.
- **Partial copies are skipped.** A file whose size/mtime is still moving is
  left alone and retried next cycle — otherwise a large MP4 still coming off
  the SD card would get stitched half-written.
- **Output is atomic.** Results are built in scratch and moved into the output
  folder only on success, so a partial result never appears there.
- **Upscaling to 3840x1920 is the default** for anything smaller and 2:1 — it
  is the biggest seam-quality lever on this camera (see "Seam quality").
  `--no-upscale` opts out. Input already at 3840x1920 passes through untouched.
- **Finished sources are moved to `<input>/complete`**, so the input folder
  shows only what is left to do. Moved, never deleted or rewritten — that
  folder may be the only copy off the SD card. Name collisions are suffixed.
- **Results are named `<source>_stitched.<ext>`**, including photos, whose
  own output name from `gear360pano` is not trusted.
- **Failures are not retried.** A `.log` lands in `/artifacts/failed/` and the
  file is skipped until you clear its marker.

State lives in `/artifacts/.gear360/`:

| Path | Meaning |
|---|---|
| `done/<file>` | fingerprint of a completed file — delete to re-stitch |
| `failed/<file>` | a failure — delete to retry after fixing the cause |
| `logs/<file>.log` | full output of the last attempt, success or not |
| `lock` | `flock` target; one watcher per state dir |

Replacing a file with a different one of the same name changes its fingerprint,
so it gets picked up again automatically.

No-build sanity check, if you want to confirm the footage before trusting the
stitcher (inferior — no per-lens calibration):

```bash
ffmpeg -i /artifacts/in/CLIP.MP4 \
  -vf v360=dfisheye:equirect:ih_fov=195:iv_fov=195 -t 5 /artifacts/out/smoke.mp4
```

## Validation

1. Stitch one short clip before committing to a batch.
2. Confirm the metadata took: `python3 -m spatialmedia <file>` should report
   spherical XML. Without it, players show a flat warped rectangle.
3. Check the seams at the lens boundaries (the vertical centre lines). If they
   show, confirm `-a -l` were passed and that the input was upscaled — see
   "Seam quality".
4. QuickLook will **not** reframe 360 video. Use VLC, or a private YouTube
   upload, to judge the result.

## Upstream patches

For the story behind each of these — how they were found and what the symptoms
looked like — see [`LOG.md`](LOG.md).

`install.sh` applies `patch_upstream.py` to the vendored clones before building.
Eight fixes across three files; each needs a unique exact match or it is
reported and skipped, never half-applied.

### fisheyeStitcher — `src/fisheye_stitcher.cpp`

1. **MLS grid rescaled to the frame.** The grid is the *static lens
   calibration* that aligns the right fisheye to the left; upstream applies it
   on every frame regardless of refine alignment, which only adds a
   template-matching pass on top. `grid_xd_yd_3840x1920.yml.gz` is sized for
   that frame's right-hand crop and holds **absolute pixel coordinates**, and
   `cv::remap` takes its output size from the map — so at any other resolution
   an unscaled grid both returned a wrong-sized Mat and sampled the wrong
   places. The maps are now resized to `m_ws x (m_hd-2)` with their values
   scaled by the same factor. At 3840x1920 the sizes already match and it is a
   no-op. **Skipping the deform instead of rescaling it produces visibly
   misaligned seams** — it removes the calibration entirely.
2. **Resolution-scaled alignment constants.** The reference/template crops use
   values commented in-source as empirical for 3840x1920, and they build ROIs
   *before* `m_enb_refine_align` is checked — so `row_end = 1320` overran a
   2560-wide input's ~1180px height and aborted with an out-of-bounds
   `cv::Rect`. Now scaled by `m_ws / 1920.0`; the factor is exactly 1.0 at
   3840x1920, so that path is unchanged.

### fisheyeStitcher — `app/stitch.cpp`

3. **Frame-rate truncation.** `int frame_fps = VCap.get(CV_CAP_PROP_FPS)`
   truncated a `double`, stamping every NTSC rate a whole frame slow
   (59.94→59, 29.97→29, 23.976→23) — output ran ~1.59% long. Now `double`.
4. **Writer reopened at the real frame size.** The writer was opened from a
   computed `Wd x Hd`, but `cv::VideoWriter` **silently discards** frames whose
   size differs. At 2560x1280 the computation gives 1180 while `stitch()`
   returns 1179 — one pixel — so all frames were dropped and the result was a
   structurally valid but **empty** AVI: right header, zero packets, exit 0.
   Downstream that appears as `unspecified pixel format` on probe, `video:0kB`
   on remux, and `spatialmedia` seeing a single track. Now reopened at
   `pano.size()` on frame 0, and the real size is printed.

### stitch-gear360 — `stitch-gear360.sh`

5. **2:1 resolution gate.** Upstream hard-coded `!= "3840x1920"`. The gate was
   always wrapper-level — the stitcher derives geometry from the frame at
   runtime — so it now accepts any even 2:1 dual-fisheye frame.
6. **Real flag names, numeric values.** The wrapper passed `--enb_lc` /
   `--enb_ra`, but the stitcher's options are `--enb_light_compen` /
   `--enb_refine_align`, and it parses values with `atoi()` while the wrapper
   passed the strings `"true"`/`"false"` (`atoi("true") == 0`). **Both features
   had never run, whatever `-a`/`-l` were set to.** Both are now passed
   correctly at every resolution.
7. **Source files queued for audio.** fisheyeStitcher reads video through
   OpenCV, which has no audio support, so its AVI is video-only.
8. **Source audio mapped into the join.** The join took only the AVI queue and
   never referenced the source again — audio could not reach the output by any
   mapping. Now a second concat input with `-map 0:v -map 1:a?` (optional, so
   silent sources still join) and `-c:a aac`.

Verified in a clean `ubuntu:24.04` at both resolutions: 2560x1280 produces
2362x1178 @ 60000/1001 with 180 frames, video + audio streams, and
`Spherical = true / ProjectionType = equirectangular`; 3840x1920 is unchanged.

### Seam quality

Measured on real SM-C200 footage, best last:

| | seam |
|---|---|
| native 2560x1280, `-l` | obvious hazy band; clouds break across it |
| native 2560x1280, `-a -l` | band largely gone, faint line remains |
| upscaled to 3840x1920, `-a -l` | essentially seamless |

**Use `-a`.** It works at any resolution now that the grid is rescaled and the
alignment constants scale with the frame, and it is the single biggest quality
lever. It is not gated on resolution.

Upscaling to 3840x1920 first still wins, because the alignment constants are
empirical for that size and only approximate when scaled. It costs 2.25x the
pixels through the stitcher — the watcher does it by default; `--no-upscale`
trades the quality back:

```bash
# by hand — keep the ORIGINAL filename, the wrapper checks it
ffmpeg -i /artifacts/in/CLIP.MP4 -vf scale=3840:1920:flags=lanczos \
       -c:v libx264 -crf 16 -preset medium -c:a copy /workspace/scratch/CLIP.MP4
stitch-gear360.sh -a -l /workspace/scratch/CLIP.MP4 /artifacts/out/CLIP_stitched.mp4

# or let the watcher do it — upscaling is the DEFAULT
gear360-watch --once -a -l
gear360-watch --once -a -l --no-upscale   # faster, visibly worse seams
```

Upscaling stages the enlarged copy inside the scratch work dir under the
original filename, stitches from it, and names the result after the *original*
source. Anything already 3840x1920, or not 2:1, is passed through untouched; a
failed upscale falls back to stitching natively rather than failing the file.

## Notes

- Docker removes most of the friction in the original macOS plan: the
  CMake/OpenCV path hunting, BSD-vs-GNU `sed`/`getopt` differences, and the
  `/usr/share/fisheye-stitcher` path all stop being problems on Ubuntu, which
  is what upstream tests against.
- Everything is pinned to `--depth 1` on each repo's default branch. Rebuilding
  the image can therefore pick up upstream changes; `gear360-doctor` is how you
  notice if one breaks.
