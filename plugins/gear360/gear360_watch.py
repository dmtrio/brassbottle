#!/usr/bin/env python3
"""gear360-watch — poll a drop folder and stitch whatever lands in it.

POLLING, not inotify: host-side writes to a Docker Desktop bind mount do not
reliably deliver inotify events into the container, so a file dropped on the
Mac might never fire one.

Strictly one job at a time — a clip is minutes of pegged CPU.
A source is moved into <input>/complete once it has stitched successfully, so
the input folder shows only what is left to do. Sources are never deleted or
rewritten — that folder may be the only copy off the SD card.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VIDEO_EXT = {".mp4"}
PHOTO_EXT = {".jpg", ".jpeg"}
# What counts as a result worth keeping out of the scratch dir.
RESULT_EXT = {".mp4", ".jpg", ".jpeg", ".tif", ".avi"}
# Appended to every result, so a stitched file is never mistaken for a source.
STITCHED_SUFFIX = "_stitched"

running = True


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def fingerprint(path: Path) -> str | None:
    """Size+mtime. Cheap, and enough to spot both a still-growing file and a
    replaced one that happens to reuse the same name."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_size} {st.st_mtime_ns}"


class Watcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.in_dir = Path(args.input)
        self.out_dir = Path(args.output)
        self.failed_dir = Path(args.failed)
        self.scratch = Path(args.scratch)
        self.complete_dir = (Path(args.complete) if args.complete
                             else self.in_dir / "complete")
        self.state = Path(args.state)
        self.done = self.state / "done"
        self.failed = self.state / "failed"
        self.logs = self.state / "logs"
        for d in (self.in_dir, self.out_dir, self.failed_dir, self.scratch,
                  self.complete_dir, self.done, self.failed, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    def already_handled(self, src: Path, fp: str) -> bool:
        """True if this exact file (same fingerprint) already succeeded or
        failed. A replaced file has a new fingerprint and gets picked up."""
        for marker in (self.done / src.name, self.failed / src.name):
            try:
                if marker.read_text().strip() == fp:
                    return True
            except OSError:
                continue
        return False

    def build_command(self, src: Path, work: Path) -> list[str] | None:
        ext = src.suffix.lower()
        if ext in VIDEO_EXT:
            cmd = ["stitch-gear360.sh", *self.args.passthrough]
            if self.args.force:
                cmd.append("--force")
            return [*cmd, str(src), str(work / f"{src.stem}{STITCHED_SUFFIX}.mp4")]
        if ext in PHOTO_EXT:
            # gear360pano's output naming/location is not something to assume:
            # run it inside an empty scratch dir and harvest what it produced.
            return ["gear360pano.sh", str(src)]
        return None

    def process(self, src: Path) -> bool:
        work = self.scratch / f"gear360-{os.getpid()}-{src.stem}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        logfile = self.logs / f"{src.name}.log"

        cmd = self.build_command(src, work)
        if cmd is None:
            shutil.rmtree(work, ignore_errors=True)
            return True

        kind = "video" if src.suffix.lower() in VIDEO_EXT else "photo"
        log(f"{kind}: {src.name}")

        with logfile.open("w") as fh:
            fh.write(f"$ {' '.join(cmd)}\n\n")
            fh.flush()
            rc = subprocess.call(cmd, cwd=work, stdout=fh, stderr=subprocess.STDOUT)

        produced = [p for p in sorted(work.rglob("*"))
                    if p.is_file() and p.suffix.lower() in RESULT_EXT]
        if rc == 0 and not produced:
            rc = 1
            with logfile.open("a") as fh:
                fh.write("\nno output produced\n")

        if rc == 0:
            # Built in scratch and moved only on success, so a half-written
            # result never appears in the output folder. Names are normalised
            # here rather than trusted from the tool: gear360pano picks its own.
            for n, p in enumerate(produced):
                stem = f"{src.stem}{STITCHED_SUFFIX}"
                if len(produced) > 1:
                    stem = f"{stem}_{n + 1}"
                shutil.move(str(p), str(self.out_dir / f"{stem}{p.suffix.lower()}"))
            (self.done / src.name).write_text(fingerprint(src) or "")
            (self.failed / src.name).unlink(missing_ok=True)
            (self.failed_dir / f"{src.name}.log").unlink(missing_ok=True)
            log(f"  ✓ {src.name} → {self.out_dir} ({len(produced)} file(s))")
            self.retire(src)
        else:
            (self.failed / src.name).write_text(fingerprint(src) or "")
            shutil.copyfile(logfile, self.failed_dir / f"{src.name}.log")
            log(f"  ✗ {src.name} failed (rc={rc}) — see {self.failed_dir}/{src.name}.log")

        shutil.rmtree(work, ignore_errors=True)
        return rc == 0

    def retire(self, src: Path) -> None:
        """Move a finished source into the completed folder.

        Moved, never deleted — the input folder may be the only copy off the SD
        card. A name collision is resolved by suffixing rather than clobbering,
        since SM-C200 filenames wrap around and repeat between cards.
        """
        dest = self.complete_dir / src.name
        n = 1
        while dest.exists():
            dest = self.complete_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        try:
            shutil.move(str(src), str(dest))
            log(f"    moved source → {dest}")
        except OSError as exc:
            log(f"    could not move {src.name} into {self.complete_dir}: {exc}")

    def scan(self) -> None:
        for src in sorted(self.in_dir.iterdir()):
            if not running:
                return
            if not src.is_file():
                continue
            # Filter by extension BEFORE the settle wait below — otherwise every
            # stray file in the drop folder costs `settle` seconds every cycle.
            if src.suffix.lower() not in VIDEO_EXT | PHOTO_EXT:
                continue

            fp = fingerprint(src)
            if fp is None or self.already_handled(src, fp):
                continue

            # Still being copied off the SD card? Its size/mtime will move.
            time.sleep(self.args.settle)
            if fingerprint(src) != fp:
                log(f"still copying, will retry: {src.name}")
                continue

            self.process(src)

    def run(self) -> int:
        lock = (self.state / "lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"gear360-watch: another instance is already running "
                  f"(lock: {self.state / 'lock'})", file=sys.stderr)
            return 1

        mode = ", --once" if self.args.once else ""
        log(f"watching {self.in_dir} → {self.out_dir} "
            f"(poll {self.args.interval}s{mode})")
        if subprocess.call(["gear360-doctor"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            print("gear360-watch: WARNING — toolchain incomplete, "
                  "run gear360-doctor", file=sys.stderr)

        while running:
            self.scan()
            if self.args.once or not running:
                break
            time.sleep(self.args.interval)
        log("stopped")
        return 0


def stop(signum, frame) -> None:
    global running
    running = False
    print("\ngear360-watch: stopping after current job", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="gear360-watch",
        description="Stitch Gear 360 files dropped into a folder.",
        epilog="State lives in the --state dir: done/<file> and failed/<file> "
               "hold fingerprints (delete one to re-stitch), logs/<file>.log "
               "holds the last attempt. A stitched source is moved into "
               "<input>/complete; sources are never deleted or rewritten.",
    )
    p.add_argument("-i", "--input", default="/artifacts/in")
    p.add_argument("-o", "--output", default="/artifacts/out")
    p.add_argument("--failed", default="/artifacts/failed",
                   help="where failure logs are surfaced (host-visible)")
    p.add_argument("--complete", default=None,
                   help="where successfully stitched sources are moved "
                        "(default: <input>/complete)")
    p.add_argument("-s", "--scratch", default="/workspace/scratch",
                   help="intermediates — keep this OFF /artifacts, which is a "
                        "slow bind mount")
    p.add_argument("--state", default="/artifacts/.gear360")
    p.add_argument("-n", "--interval", type=int, default=10,
                   help="seconds between polls (default: 10)")
    p.add_argument("--settle", type=int, default=3,
                   help="seconds a file must stop changing before it is "
                        "considered fully copied (default: 3)")
    p.add_argument("--once", action="store_true",
                   help="drain the current backlog, then exit")
    p.add_argument("-f", "--force", action="store_true",
                   help="pass --force to stitch-gear360.sh (for files without "
                        "original SM-C200 filenames)")
    p.add_argument("-a", dest="passthrough", action="append_const", const="-a",
                   default=[], help="refine alignment (passed through)")
    p.add_argument("-l", dest="passthrough", action="append_const", const="-l",
                   help="light compensation (passed through)")
    args = p.parse_args()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return Watcher(args).run()


if __name__ == "__main__":
    sys.exit(main())
