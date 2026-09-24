"""png_pixels counts exact-colour pixels, through every PNG scanline filter."""
import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import png_pixels  # noqa: E402

RED, GREEN, WHITE = (255, 0, 0), (0, 200, 0), (255, 255, 255)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def encode(pixels: list[list[tuple[int, ...]]], filters: list[int], colour_type: int = 2) -> bytes:
    """A PNG of `pixels`, scanline y stored with filter type filters[y % len(filters)]."""
    bpp = 3 if colour_type == 2 else 4
    raw, prev = bytearray(), bytes(len(pixels[0]) * bpp)
    for y, row in enumerate(pixels):
        line = bytes(channel for px in row for channel in px)
        kind = filters[y % len(filters)]
        raw.append(kind)
        for i, x in enumerate(line):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            predictor = (0, a, b, (a + b) // 2, _paeth(a, b, c))[kind]
            raw.append((x - predictor) & 255)
        prev = line
    header = struct.pack(">IIBBBBB", len(pixels[0]), len(pixels), 8, colour_type, 0, 0, 0)
    return (png_pixels._SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw)))
            + _chunk(b"IEND", b""))


class CountPixelsTests(unittest.TestCase):
    def grid(self, extra=()):
        return [[RED if (x, y) in {(1, 0), (3, 2), (0, 3), (4, 4), *extra} else (WHITE if (x + y) % 2 else GREEN)
                 for x in range(6)] for y in range(5)]

    def test_counts_exactly_the_red_pixels_through_each_filter_type(self):
        for kind in range(5):
            with self.subTest(filter=kind):
                self.assertEqual(png_pixels.count_pixels(encode(self.grid(), [kind]), RED), 4)

    def test_mixed_filters_and_rgba(self):
        rgba = [[px + (255,) for px in row] for row in self.grid()]
        self.assertEqual(png_pixels.count_pixels(encode(rgba, [0, 1, 2, 3, 4], colour_type=6), RED), 4)

    def test_a_near_red_pixel_is_not_red(self):
        near = [[(254, 0, 0), (255, 1, 0), (255, 0, 1), RED]]
        self.assertEqual(png_pixels.count_pixels(encode(near, [0]), RED), 1)

    def test_an_image_with_no_red_counts_zero(self):
        self.assertEqual(png_pixels.count_pixels(encode([[WHITE] * 3] * 3, [4]), RED), 0)

    def test_rejects_what_it_cannot_decode(self):
        with self.assertRaises(ValueError):
            png_pixels.count_pixels(b"not a png", RED)

    def test_agrees_with_pillow_when_installed(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        import io
        png = encode(self.grid(), [4])
        pixels = Image.open(io.BytesIO(png)).convert("RGB").tobytes()
        theirs = sum(1 for i in range(0, len(pixels), 3) if tuple(pixels[i:i + 3]) == RED)
        self.assertEqual(theirs, png_pixels.count_pixels(png, RED))


if __name__ == "__main__":
    unittest.main()
