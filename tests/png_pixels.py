"""Count exact-colour pixels in a PNG with the standard library only (no Pillow in CI)."""
from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {2: 3, 6: 4}   # colour type -> bytes per pixel at 8 bits: RGB, RGBA


def _chunks(png: bytes):
    if not png.startswith(_SIGNATURE):
        raise ValueError("not a PNG")
    pos = len(_SIGNATURE)
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        yield png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        pos += 12 + length


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def decode_rows(png: bytes) -> tuple[int, int, int, list[bytearray]]:
    """(width, height, bytes per pixel, unfiltered scanlines) of an 8-bit, non-interlaced RGB or RGBA PNG."""
    header, data = None, bytearray()
    for kind, body in _chunks(png):
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            data += body
    if header is None:
        raise ValueError("PNG has no IHDR")
    width, height, depth, colour, _compression, _filter, interlace = header
    if depth != 8 or colour not in _CHANNELS or interlace != 0:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {colour}, interlace {interlace})")
    bpp = _CHANNELS[colour]
    stride = width * bpp
    raw = zlib.decompress(bytes(data))
    if len(raw) != height * (stride + 1):
        raise ValueError(f"PNG data is {len(raw)} bytes, expected {height * (stride + 1)}")
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up = prev[i]
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                line[i] = (line[i] + _paeth(left, up, prev[i - bpp] if i >= bpp else 0)) & 255
            elif kind != 0:
                raise ValueError(f"bad PNG filter type {kind}")
        rows.append(line)
        prev = line
    return width, height, bpp, rows


def count_pixels(png: bytes, rgb: tuple[int, int, int]) -> int:
    """How many pixels are exactly `rgb` (alpha ignored)."""
    _width, _height, bpp, rows = decode_rows(png)
    target = bytes(rgb)
    return sum(1 for row in rows for i in range(0, len(row), bpp) if row[i:i + 3] == target)
