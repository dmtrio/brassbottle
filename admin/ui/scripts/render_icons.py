#!/usr/bin/env python3
"""render_icons.py: derive every app icon from the one SVG master.

    python3 scripts/render_icons.py          (from admin/ui; needs Playwright's Chromium)

Reads icons/app-icon.svg (1024x1024, full-bleed) and writes, into public/:

  icon-192.png, icon-512.png    purpose "any": the master at 824/1024 inside a transparent
                                1024 canvas, clipped to a rounded rect of radius 185 (the
                                macOS icon grid), rendered at 192 and 512
  icon-maskable-512.png         purpose "maskable": the full-bleed master; the bottle sits in
                                the central 80 % circle, so a platform mask never cuts it
  apple-touch-icon.png          180x180, full-bleed, RGB with no alpha channel (Safari
                                rounds the corners itself)
  favicon-32.png, favicon-16.png  the rounded variant
  favicon.svg                   the rounded variant as SVG

and icons/icons.json: the master's sha256 plus each output's kind, size and sha256. That
file makes the committed icons a Ledger: tests/test_admin_icons.py fails when the master
changes without a re-run, or an output no longer matches. Screenshots are not byte-stable
across Chromium versions, so CI compares against the committed outputs and never re-renders.

Each size is rendered from the vector at that size, not downscaled from a 1024 raster.
Progress goes to stderr, one `stage=` line per step.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import time
import zlib
from pathlib import Path

UI = Path(__file__).resolve().parents[1]
ROOT = UI.parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import png_pixels  # noqa: E402  (the one PNG decoder in this repo)

MASTER = UI / "icons" / "app-icon.svg"
LEDGER = UI / "icons" / "icons.json"
PUBLIC = UI / "public"

CANVAS = 1024
TILE = 824          # the rounded tile's side on the 1024 grid
TILE_RADIUS = 185   # its corner radius on the same grid

# name -> (kind, size, source): the ledger's outputs, in the order they are listed.
OUTPUTS = {
    "icon-192.png": ("any", 192, "rounded"),
    "icon-512.png": ("any", 512, "rounded"),
    "icon-maskable-512.png": ("maskable", 512, "full-bleed"),
    "apple-touch-icon.png": ("apple-touch", 180, "opaque"),
    "favicon-32.png": ("favicon", 32, "rounded"),
    "favicon-16.png": ("favicon", 16, "rounded"),
    "favicon.svg": ("favicon", CANVAS, "svg"),
}


def log(message: str) -> None:
    print(f"render_icons: {message}", file=sys.stderr, flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rounded_svg(master: str) -> str:
    """The master scaled to TILE/CANVAS inside a transparent CANVAS square, clipped to the rounded tile."""
    margin = (CANVAS - TILE) // 2
    root, inner = re.subn(
        r"<svg\b[^>]*>",
        f'<svg x="{margin}" y="{margin}" width="{TILE}" height="{TILE}" viewBox="0 0 {CANVAS} {CANVAS}">',
        master, count=1)
    if inner != 1 or "</svg>" not in root:
        raise ValueError("the master has no <svg> root")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" width="{CANVAS}" height="{CANVAS}">'
        f'<defs><clipPath id="icon-tile"><rect x="{margin}" y="{margin}" width="{TILE}" height="{TILE}" '
        f'rx="{TILE_RADIUS}"/></clipPath></defs><g clip-path="url(#icon-tile)">{root}</g></svg>\n'
    )


def sized(svg: str, size: int) -> str:
    """The same SVG with its root width and height set to `size`."""
    out, count = re.subn(
        r"(<svg\b[^>]*?)\swidth=\"\d+\"\s+height=\"\d+\"", rf'\1 width="{size}" height="{size}"', svg, count=1)
    if count != 1:
        raise ValueError("the svg root has no width/height pair to resize")
    return out


def strip_alpha(png: bytes) -> bytes:
    """An RGB copy of an RGBA PNG, refusing one that is not fully opaque."""
    width, height, bpp, rows = png_pixels.decode_rows(png)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for i in range(0, len(row), bpp):
            if bpp == 4 and row[i + 3] != 255:
                raise ValueError("the image is not opaque; dropping alpha would change it")
            raw += row[i:i + 3]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def ledger(master: bytes, outputs: dict[str, bytes]) -> dict:
    """The icons.json body: the master's hash and each output's kind, size and hash."""
    return {
        "master": {"path": "icons/app-icon.svg", "sha256": sha256(master)},
        "outputs": [
            {"name": name, "kind": OUTPUTS[name][0], "size": OUTPUTS[name][1], "sha256": sha256(outputs[name])}
            for name in OUTPUTS
        ],
    }


def screenshot(browser, svg: str, size: int, transparent: bool) -> bytes:
    page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
    try:
        page.set_content(
            f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;background:transparent}}'
            f'svg{{display:block}}</style>{sized(svg, size)}')
        return page.screenshot(omit_background=transparent, clip={"x": 0, "y": 0, "width": size, "height": size})
    finally:
        page.close()


def render(browser) -> tuple[bytes, dict[str, bytes]]:
    """The master's raw bytes and every output's bytes, keyed by name, rendered with an
    already-launched `browser`. Split out of `main` so a caller that already has Playwright's
    Chromium running (tests/admin_ui_playwright.py check 195, the Ledger-regeneration check) can
    reuse it instead of launching a second one."""
    master_bytes = MASTER.read_bytes()
    master = master_bytes.decode("utf-8")
    log(f"stage=read master={MASTER.relative_to(ROOT)} bytes={len(master_bytes)} sha256={sha256(master_bytes)[:12]}")
    rounded = rounded_svg(master)
    outputs: dict[str, bytes] = {"favicon.svg": rounded.encode("utf-8")}
    for name, (kind, size, source) in OUTPUTS.items():
        if source == "svg":
            continue
        t0 = time.monotonic()
        svg = rounded if source == "rounded" else master
        png = screenshot(browser, svg, size, transparent=source == "rounded")
        if source == "opaque":
            png = strip_alpha(png)
        outputs[name] = png
        log(f"stage=render name={name} kind={kind} size={size} bytes={len(png)} ms={int((time.monotonic() - t0) * 1000)}")
    return master_bytes, outputs


def main() -> int:
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            master_bytes, outputs = render(browser)
        finally:
            browser.close()
    for name, body in outputs.items():
        (PUBLIC / name).write_bytes(body)
    LEDGER.write_text(json.dumps(ledger(master_bytes, outputs), indent=2) + "\n", encoding="utf-8")
    log(f"stage=done outputs={len(outputs)} ledger={LEDGER.relative_to(ROOT)} ms={int((time.monotonic() - started) * 1000)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
