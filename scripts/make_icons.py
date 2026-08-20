"""Generate the app icons, with no image library in the dependency list.

A PNG is a header, a couple of chunks and a zlib stream, so writing one
directly is cheaper than taking on Pillow for four flat images. The mark is
the same one the sidebar and favicon use: an accent tile with a terminal G.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"

ACCENT = (0x3D, 0xDB, 0xD9)
INK = (0x05, 0x08, 0x0C)

# 5x7 terminal G, the same shape the brand mark draws.
GLYPH = [
    "01110",
    "10001",
    "10000",
    "10111",
    "10001",
    "10001",
    "01110",
]


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + _chunk(b"IHDR", header)
                     + _chunk(b"IDAT", zlib.compress(raw, 9))
                     + _chunk(b"IEND", b""))


def icon(size: int, *, maskable: bool = False) -> list[list[tuple[int, int, int]]]:
    """Accent tile, dark glyph.

    A maskable icon gets cropped to whatever shape the launcher prefers, so the
    glyph is drawn smaller to stay inside the 80% safe zone.
    """
    grid = [[ACCENT for _ in range(size)] for _ in range(size)]

    cell = size // (14 if maskable else 10)
    gw, gh = len(GLYPH[0]) * cell, len(GLYPH) * cell
    x0, y0 = (size - gw) // 2, (size - gh) // 2

    for gy, row in enumerate(GLYPH):
        for gx, on in enumerate(row):
            if on != "1":
                continue
            for y in range(y0 + gy * cell, y0 + (gy + 1) * cell):
                for x in range(x0 + gx * cell, x0 + (gx + 1) * cell):
                    grid[y][x] = INK
    return grid


def main() -> int:
    WEB.mkdir(parents=True, exist_ok=True)
    written = []
    for name, size, maskable in (("icon-192.png", 192, False),
                                 ("icon-512.png", 512, False),
                                 ("icon-maskable-512.png", 512, True),
                                 ("apple-touch-icon.png", 180, True)):
        path = WEB / name
        write_png(path, icon(size, maskable=maskable))
        written.append(f"{name} ({path.stat().st_size} bytes)")
    print("wrote " + ", ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
