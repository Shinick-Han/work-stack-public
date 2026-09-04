"""Deterministic Work Stack brand asset generator.

One frozen geometry document, ``desktop/python-webview-shell/assets/brand-mark.v1.json``,
is the single source for every produced artifact:

* an SVG written byte-identically to the desktop assets and to ``frontend/src/assets``;
* ``WorkStack-Mark-Lime-v2.ico`` containing PNG-backed 32-bit RGBA frames at
  16, 20, 24, 32, 40, 48, 64, 128 and 256 pixels.

Only the standard library is used, so the output does not depend on an image
library version: the rasterizer is an analytic rounded-rectangle coverage
sampler and the PNG encoder is ``zlib`` at a fixed level with fixed filtering.
Running the generator twice therefore produces identical bytes.

``--check`` is read-only: it regenerates in memory and reports whether the
committed files already match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_PATH = REPOSITORY_ROOT / "desktop" / "python-webview-shell" / "assets" / "brand-mark.v1.json"
DESKTOP_ASSETS = REPOSITORY_ROOT / "desktop" / "python-webview-shell" / "assets"
FRONTEND_ASSETS = REPOSITORY_ROOT / "frontend" / "src" / "assets"
SVG_NAME = "WorkStack-Mark-Lime-v2.svg"
ICO_NAME = "WorkStack-Mark-Lime-v2.ico"

# Supersampling factor per axis. 4 keeps 16 px frames smooth while staying
# exactly reproducible in integer arithmetic.
SUPERSAMPLE = 4


class Geometry:
    """The frozen document, validated enough to fail loudly on a bad edit."""

    def __init__(self, document: dict) -> None:
        self.canvas = int(document["canvas"])
        self.accent = str(document["colors"]["accent"])
        self.ink = str(document["colors"]["ink"])
        self.plate = dict(document["plate"])
        self.bars = [dict(bar) for bar in document["bars"]]
        self.ico_sizes = [int(size) for size in document["ico_sizes"]]
        if self.canvas <= 0 or not self.bars or not self.ico_sizes:
            raise ValueError("brand geometry is incomplete")
        for value in (self.accent, self.ink):
            if len(value) != 7 or not value.startswith("#"):
                raise ValueError(f"brand color is not #rrggbb: {value}")

    @classmethod
    def load(cls, path: Path = GEOMETRY_PATH) -> "Geometry":
        return cls(json.loads(path.read_text(encoding="utf-8")))


def rgba(color: str) -> tuple[int, int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), 255)


def _inside_rounded_rect(px: float, py: float, rect: dict) -> bool:
    x, y = float(rect["x"]), float(rect["y"])
    width, height = float(rect["width"]), float(rect["height"])
    radius = min(float(rect["radius"]), width / 2, height / 2)
    if not (x <= px <= x + width and y <= py <= y + height):
        return False
    left, right = x + radius, x + width - radius
    top, bottom = y + radius, y + height - radius
    cx = left if px < left else right if px > right else px
    cy = top if py < top else bottom if py > bottom else py
    if cx == px and cy == py:
        return True
    return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius


def _scaled(rect: dict, scale: float) -> dict:
    return {key: float(value) * scale for key, value in rect.items()}


def render_rgba(geometry: Geometry, size: int) -> bytes:
    """Render one frame as raw RGBA rows, antialiased by supersampling."""

    scale = size / geometry.canvas
    plate = _scaled(geometry.plate, scale)
    bars = [_scaled(bar, scale) for bar in geometry.bars]
    accent = rgba(geometry.accent)
    ink = rgba(geometry.ink)
    step = 1.0 / SUPERSAMPLE
    offset = step / 2.0
    samples = SUPERSAMPLE * SUPERSAMPLE
    out = bytearray(size * size * 4)
    for row in range(size):
        for column in range(size):
            plate_hits = 0
            ink_hits = 0
            for sy in range(SUPERSAMPLE):
                py = row + offset + sy * step
                for sx in range(SUPERSAMPLE):
                    px = column + offset + sx * step
                    if not _inside_rounded_rect(px, py, plate):
                        continue
                    plate_hits += 1
                    if any(_inside_rounded_rect(px, py, bar) for bar in bars):
                        ink_hits += 1
            index = (row * size + column) * 4
            if plate_hits == 0:
                continue
            alpha = round(255 * plate_hits / samples)
            lime_hits = plate_hits - ink_hits
            if plate_hits:
                red = round((accent[0] * lime_hits + ink[0] * ink_hits) / plate_hits)
                green = round((accent[1] * lime_hits + ink[1] * ink_hits) / plate_hits)
                blue = round((accent[2] * lime_hits + ink[2] * ink_hits) / plate_hits)
            else:  # pragma: no cover - guarded by the plate_hits check above
                red = green = blue = 0
            out[index] = red
            out[index + 1] = green
            out[index + 2] = blue
            out[index + 3] = alpha
    return bytes(out)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(pixels: bytes, size: int) -> bytes:
    """Minimal deterministic RGBA PNG: fixed filter 0 and fixed zlib level."""

    stride = size * 4
    raw = bytearray()
    for row in range(size):
        raw.append(0)
        raw += pixels[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def encode_ico(frames: Sequence[tuple[int, bytes]]) -> bytes:
    """ICO container whose every entry is a PNG payload."""

    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    directory = bytearray()
    payloads = bytearray()
    offset = 6 + 16 * count
    for size, payload in frames:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(payload),
            offset + len(payloads),
        )
        payloads += payload
    return header + bytes(directory) + bytes(payloads)


def build_svg(geometry: Geometry) -> bytes:
    def rounded(rect: dict, fill: str) -> str:
        return (
            '  <rect x="{x}" y="{y}" width="{width}" height="{height}" '
            'rx="{radius}" ry="{radius}" fill="{fill}" />'
        ).format(fill=fill, **{key: int(rect[key]) for key in ("x", "y", "width", "height", "radius")})

    canvas = geometry.canvas
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {0}" '
        'width="{0}" height="{0}" role="img" aria-label="Work Stack">'.format(canvas),
        rounded(geometry.plate, geometry.accent),
    ]
    lines += [rounded(bar, geometry.ink) for bar in geometry.bars]
    lines.append("</svg>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_ico(geometry: Geometry) -> bytes:
    return encode_ico([
        (size, encode_png(render_rgba(geometry, size), size)) for size in geometry.ico_sizes
    ])


def artifacts(geometry: Geometry, roots: dict[str, Path]) -> dict[Path, bytes]:
    svg = build_svg(geometry)
    return {
        roots["desktop"] / SVG_NAME: svg,
        roots["frontend"] / SVG_NAME: svg,
        roots["desktop"] / ICO_NAME: build_ico(geometry),
    }


def _roots(output: Path | None) -> dict[str, Path]:
    if output is None:
        return {"desktop": DESKTOP_ASSETS, "frontend": FRONTEND_ASSETS}
    return {"desktop": output / "desktop", "frontend": output / "frontend"}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing anything")
    parser.add_argument("--output", type=Path, default=None, help="write into this directory instead")
    parser.add_argument("--print-hashes", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    geometry = Geometry.load()
    produced = artifacts(geometry, _roots(arguments.output))

    if arguments.check:
        stale = []
        for path, payload in produced.items():
            if not path.is_file() or path.read_bytes() != payload:
                stale.append(path)
        for path, payload in sorted(produced.items()):
            print(f"{hashlib.sha256(payload).hexdigest()}  {path}")
        if stale:
            for path in stale:
                print(f"STALE {path}", file=sys.stderr)
            return 1
        print("brand assets are up to date")
        return 0

    for path, payload in produced.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if arguments.print_hashes:
            print(f"{hashlib.sha256(payload).hexdigest()}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
