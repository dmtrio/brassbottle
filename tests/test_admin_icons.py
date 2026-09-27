"""The admin's icon set is a Ledger: every icon is derived from one SVG master, and this Gate fails
when the master changes without `python3 admin/ui/scripts/render_icons.py` being re-run, or when a
committed icon no longer matches icons.json. It never re-renders (screenshots are not byte-stable
across Chromium versions), so it needs no browser.

It also decodes the PNGs (stdlib zlib): the apple-touch and maskable icons are fully opaque, and the
maskable icon's pixels outside the central 80 % circle are the tile colour, so a platform's mask
never cuts the bottle. The last class tests render_icons.py's own helpers.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "admin" / "ui"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(UI / "scripts"))

import png_pixels  # noqa: E402
import render_icons  # noqa: E402

MASTER = UI / "icons" / "app-icon.svg"
LEDGER = UI / "icons" / "icons.json"
PUBLIC = UI / "public"

# The set, kinds and dimensions the manifest, index.html and PLN promise. Literal on purpose: a
# wrong `size` in icons.json must not pass by agreeing with itself.
EXPECTED = {
    "icon-192.png": ("any", 192),
    "icon-512.png": ("any", 512),
    "icon-maskable-512.png": ("maskable", 512),
    "apple-touch-icon.png": ("apple-touch", 180),
    "favicon-32.png": ("favicon", 32),
    "favicon-16.png": ("favicon", 16),
    "favicon.svg": ("favicon", 1024),
}
TILE = (0x3f, 0x40, 0x41)
TOLERANCE = 6


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


class LedgerGate(unittest.TestCase):
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))

    def test_the_master_is_the_one_the_icons_were_rendered_from(self):
        self.assertEqual(
            digest(MASTER), self.ledger["master"]["sha256"],
            "icons/app-icon.svg changed since the icons were rendered: run "
            "`python3 admin/ui/scripts/render_icons.py` in admin/ui and commit the result")

    def test_the_ledger_lists_exactly_the_promised_icons(self):
        listed = {o["name"]: (o["kind"], o["size"]) for o in self.ledger["outputs"]}
        self.assertEqual(listed, EXPECTED)

    def test_public_holds_no_icon_the_ledger_does_not_list(self):
        icons = {p.name for p in PUBLIC.iterdir() if p.suffix in (".png", ".svg")}
        self.assertEqual(icons, set(EXPECTED), "an icon in public/ outside the ledger (or one missing from it)")

    def test_every_output_matches_its_hash(self):
        for entry in self.ledger["outputs"]:
            with self.subTest(icon=entry["name"]):
                path = PUBLIC / entry["name"]
                self.assertTrue(path.is_file(), f"{entry['name']} is missing from public/")
                self.assertEqual(digest(path), entry["sha256"], f"{entry['name']} was edited or is stale")

    def test_every_png_has_its_literal_dimensions(self):
        for name, (_kind, size) in EXPECTED.items():
            if name.endswith(".png"):
                with self.subTest(icon=name):
                    self.assertEqual(png_size(PUBLIC / name), (size, size))

    def test_the_svg_favicon_is_the_rounded_variant_of_the_master(self):
        self.assertEqual((PUBLIC / "favicon.svg").read_text(encoding="utf-8"),
                         render_icons.rounded_svg(MASTER.read_text(encoding="utf-8")))


def pixels(name: str) -> tuple[int, int, int, list[bytearray]]:
    return png_pixels.decode_rows((PUBLIC / name).read_bytes())


class Opacity(unittest.TestCase):
    def assert_opaque(self, name: str) -> None:
        _w, _h, bpp, rows = pixels(name)
        if bpp == 3:
            return   # no alpha channel at all
        translucent = sum(1 for row in rows for i in range(3, len(row), 4) if row[i] != 255)
        self.assertEqual(translucent, 0, f"{name} has {translucent} pixels that are not fully opaque")

    def test_apple_touch_is_fully_opaque(self):
        self.assert_opaque("apple-touch-icon.png")

    def test_apple_touch_has_no_alpha_channel(self):
        self.assertEqual(pixels("apple-touch-icon.png")[2], 3)

    def test_maskable_is_fully_opaque(self):
        self.assert_opaque("icon-maskable-512.png")

    def test_the_rounded_icon_keeps_a_transparent_margin(self):
        _w, _h, bpp, rows = pixels("icon-512.png")
        self.assertEqual(bpp, 4)
        self.assertEqual(rows[0][3], 0, "the corner of the rounded icon is not transparent")


class MaskableSafeZone(unittest.TestCase):
    def test_outside_the_central_80_percent_circle_it_is_the_tile_colour(self):
        width, height, bpp, rows = pixels("icon-maskable-512.png")
        centre, radius = width / 2, width * 0.8 / 2
        outside = off = 0
        for y, row in enumerate(rows):
            for x in range(width):
                if (x + 0.5 - centre) ** 2 + (y + 0.5 - centre) ** 2 <= radius ** 2:
                    continue
                outside += 1
                px = row[x * bpp:x * bpp + 3]
                if any(abs(px[c] - TILE[c]) > TOLERANCE for c in range(3)):
                    off += 1
        self.assertGreater(outside, 40_000, "the test looked at too few pixels")
        self.assertEqual(off, 0, f"{off} of {outside} pixels outside the safe circle are not the tile colour")

    def test_the_bottle_is_inside_the_circle(self):
        """The other half: the circle is not just empty tile, the bottle's brass is in it."""
        width, _h, bpp, rows = pixels("icon-maskable-512.png")
        brass = sum(1 for row in rows for x in range(width)
                    if row[x * bpp] > 0x90 and row[x * bpp + 2] < 0xa0 and row[x * bpp] - row[x * bpp + 2] > 40)
        self.assertGreater(brass, 20_000)


class RenderHelpers(unittest.TestCase):
    MASTER_TEXT = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
                   '<rect width="1024" height="1024" fill="#3f4041"/></svg>')

    def test_rounded_svg_is_the_master_on_an_824_tile_clipped_at_radius_185(self):
        svg = render_icons.rounded_svg(self.MASTER_TEXT)
        root = ET.fromstring(svg)
        ns = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(root.get("viewBox"), "0 0 1024 1024")
        clip = root.find(".//s:clipPath/s:rect", ns)
        self.assertEqual({k: clip.get(k) for k in ("x", "y", "width", "height", "rx")},
                         {"x": "100", "y": "100", "width": "824", "height": "824", "rx": "185"})
        nested = root.find("./s:g/s:svg", ns)
        self.assertEqual({k: nested.get(k) for k in ("x", "y", "width", "height", "viewBox")},
                         {"x": "100", "y": "100", "width": "824", "height": "824", "viewBox": "0 0 1024 1024"})
        self.assertEqual(nested.find("s:rect", ns).get("fill"), "#3f4041")

    def test_rounded_svg_refuses_a_master_without_a_root(self):
        with self.assertRaises(ValueError):
            render_icons.rounded_svg("<g/>")

    def test_sized_sets_only_the_root_size(self):
        svg = render_icons.sized(render_icons.rounded_svg(self.MASTER_TEXT), 32)
        root = ET.fromstring(svg)
        self.assertEqual((root.get("width"), root.get("height"), root.get("viewBox")), ("32", "32", "0 0 1024 1024"))

    def test_sized_refuses_a_root_with_no_width_height_pair(self):
        # A reordered or missing width/height attribute pair must not silently pass through
        # un-resized (which would render the maskable/apple icons at 1024 clipped to a corner).
        svg = '<svg xmlns="http://www.w3.org/2000/svg" height="1024" width="1024" viewBox="0 0 1024 1024"/>'
        with self.assertRaises(ValueError):
            render_icons.sized(svg, 32)

    def _png(self, alpha: int) -> bytes:
        import zlib

        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        raw = b"".join(b"\x00" + bytes([10, 20, 30, alpha]) * 2 for _ in range(2))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    def test_strip_alpha_keeps_the_colour_and_drops_the_channel(self):
        width, height, bpp, rows = png_pixels.decode_rows(render_icons.strip_alpha(self._png(255)))
        self.assertEqual((width, height, bpp), (2, 2, 3))
        self.assertEqual(bytes(rows[0]), bytes([10, 20, 30, 10, 20, 30]))

    def test_strip_alpha_refuses_a_translucent_image(self):
        with self.assertRaises(ValueError):
            render_icons.strip_alpha(self._png(128))

    def test_ledger_lists_each_output_in_order_with_its_hash(self):
        outputs = {name: name.encode() for name in render_icons.OUTPUTS}
        body = render_icons.ledger(b"master", outputs)
        self.assertEqual(body["master"]["sha256"], hashlib.sha256(b"master").hexdigest())
        self.assertEqual([o["name"] for o in body["outputs"]], list(EXPECTED))
        self.assertEqual(body["outputs"][3], {"name": "apple-touch-icon.png", "kind": "apple-touch", "size": 180,
                                              "sha256": hashlib.sha256(b"apple-touch-icon.png").hexdigest()})


if __name__ == "__main__":
    unittest.main()
