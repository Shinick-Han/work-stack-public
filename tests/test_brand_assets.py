"""T-0007 conformance for the generated brand artifacts.

Every assertion decodes the committed bytes independently. Nothing here reads a
constant back out of the generator and compares it with itself: the ICO
container is parsed by hand, each frame's PNG is decompressed through ``zlib``,
and the pixels are sampled directly.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_PATH = ROOT / "desktop" / "python-webview-shell" / "assets" / "brand-mark.v1.json"
DESKTOP_ASSETS = ROOT / "desktop" / "python-webview-shell" / "assets"
FRONTEND_ASSETS = ROOT / "frontend" / "src" / "assets"
SVG_NAME = "WorkStack-Mark-Lime-v2.svg"
ICO_NAME = "WorkStack-Mark-Lime-v2.ico"

SPEC = importlib.util.spec_from_file_location(
    "generate_brand_assets_test", ROOT / "scripts" / "generate_brand_assets.py"
)
assert SPEC is not None and SPEC.loader is not None
GENERATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def geometry() -> dict:
    return json.loads(GEOMETRY_PATH.read_text(encoding="utf-8"))


def ico_frames(payload: bytes) -> list[tuple[int, int, int, bytes]]:
    """Parse the container by hand: (width, height, bit_count, payload)."""

    reserved, kind, count = struct.unpack_from("<HHH", payload, 0)
    assert reserved == 0 and kind == 1, "not an ICO container"
    frames = []
    for index in range(count):
        entry = 6 + 16 * index
        width, height, colors, _r, planes, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", payload, entry
        )
        frames.append((
            width or 256,
            height or 256,
            bits,
            payload[offset:offset + size],
        ))
        assert colors == 0, "a 32-bit frame declares no palette"
        assert planes == 1
    return frames


def png_dimensions(payload: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit_depth, color_type) straight from IHDR."""

    assert payload.startswith(PNG_SIGNATURE), "frame is not PNG-backed"
    length, tag = struct.unpack_from(">I4s", payload, 8)
    assert tag == b"IHDR" and length == 13
    width, height, depth, color_type = struct.unpack_from(">IIBB", payload, 16)
    return width, height, depth, color_type


def png_pixels(payload: bytes) -> tuple[int, list[list[tuple[int, int, int, int]]]]:
    """Decode an RGBA PNG that uses filter type 0 on every row."""

    width, height, depth, color_type = png_dimensions(payload)
    assert (depth, color_type) == (8, 6), "frames must be 8-bit RGBA"
    idat = bytearray()
    offset = 8
    while offset < len(payload):
        length, tag = struct.unpack_from(">I4s", payload, offset)
        body = payload[offset + 8:offset + 8 + length]
        if tag == b"IDAT":
            idat += body
        offset += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    rows = []
    for index in range(height):
        start = index * (stride + 1)
        assert raw[start] == 0, "unexpected PNG row filter"
        row = raw[start + 1:start + 1 + stride]
        rows.append([tuple(row[i:i + 4]) for i in range(0, stride, 4)])
    return width, rows


class BrandGeometryTest(unittest.TestCase):
    def test_geometry_document_is_the_frozen_contract(self) -> None:
        document = geometry()
        self.assertEqual(document["canvas"], 256)
        self.assertEqual(document["colors"], {"accent": "#B8F24B", "ink": "#12150D"})
        self.assertEqual(
            document["plate"], {"x": 4, "y": 4, "width": 248, "height": 248, "radius": 60}
        )
        self.assertEqual(document["bars"], [
            {"x": 72, "y": 88, "width": 28, "height": 80, "radius": 14},
            {"x": 116, "y": 60, "width": 28, "height": 136, "radius": 14},
            {"x": 160, "y": 80, "width": 28, "height": 100, "radius": 14},
        ])
        self.assertEqual(document["ico_sizes"], EXPECTED_SIZES)


class BrandSvgTest(unittest.TestCase):
    def test_both_svg_copies_are_byte_identical(self) -> None:
        desktop = (DESKTOP_ASSETS / SVG_NAME).read_bytes()
        frontend = (FRONTEND_ASSETS / SVG_NAME).read_bytes()
        self.assertEqual(desktop, frontend)

    def test_svg_carries_the_geometry_and_only_the_fixed_colors(self) -> None:
        document = geometry()
        text = (DESKTOP_ASSETS / SVG_NAME).read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 256 256"', text)
        self.assertEqual(text.count(document["colors"]["accent"]), 1)
        self.assertEqual(text.count(document["colors"]["ink"]), len(document["bars"]))
        self.assertNotIn("#FFFF00", text.upper())
        for rect in [document["plate"], *document["bars"]]:
            self.assertIn(
                f'x="{rect["x"]}" y="{rect["y"]}" width="{rect["width"]}" '
                f'height="{rect["height"]}" rx="{rect["radius"]}"',
                text,
            )


class BrandIcoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = (DESKTOP_ASSETS / ICO_NAME).read_bytes()
        self.frames = ico_frames(self.payload)

    def test_exactly_the_nine_declared_sizes_are_present(self) -> None:
        self.assertEqual([width for width, _h, _b, _p in self.frames], EXPECTED_SIZES)
        for width, height, bits, _payload in self.frames:
            self.assertEqual(width, height)
            self.assertEqual(bits, 32)

    def test_every_frame_is_png_backed_with_matching_dimensions(self) -> None:
        for width, _height, _bits, payload in self.frames:
            self.assertTrue(payload.startswith(PNG_SIGNATURE))
            decoded_width, decoded_height, depth, color_type = png_dimensions(payload)
            self.assertEqual((decoded_width, decoded_height), (width, width))
            self.assertEqual((depth, color_type), (8, 6))

    def test_corners_are_transparent_and_the_mark_fills_the_frame(self) -> None:
        for width, _height, _bits, payload in self.frames:
            size, rows = png_pixels(payload)
            self.assertEqual(size, width)
            for y, x in ((0, 0), (0, size - 1), (size - 1, 0), (size - 1, size - 1)):
                self.assertEqual(rows[y][x][3], 0, f"corner is opaque at size {size}")
            occupied_rows = [y for y, row in enumerate(rows) if any(p[3] > 0 for p in row)]
            occupied_columns = [
                x for x in range(size) if any(rows[y][x][3] > 0 for y in range(size))
            ]
            height_ratio = (occupied_rows[-1] - occupied_rows[0] + 1) / size
            width_ratio = (occupied_columns[-1] - occupied_columns[0] + 1) / size
            self.assertGreaterEqual(height_ratio, 0.94, f"height ratio at {size}")
            self.assertGreaterEqual(width_ratio, 0.94, f"width ratio at {size}")

    def test_interior_samples_are_the_exact_lime_and_ink(self) -> None:
        document = geometry()
        accent = GENERATOR.rgba(document["colors"]["accent"])[:3]
        ink = GENERATOR.rgba(document["colors"]["ink"])[:3]
        payload = dict((width, body) for width, _h, _b, body in self.frames)[256]
        _size, rows = png_pixels(payload)
        # Middle of the tall centre bar, and a plate point clear of every bar.
        self.assertEqual(rows[128][130][:3], ink)
        self.assertEqual(rows[128][130][3], 255)
        self.assertEqual(rows[30][128][:3], accent)
        self.assertEqual(rows[30][128][3], 255)

    def test_no_pixel_is_pure_yellow(self) -> None:
        for width, _height, _bits, payload in self.frames:
            _size, rows = png_pixels(payload)
            for row in rows:
                for pixel in row:
                    self.assertNotEqual(pixel[:3], (255, 255, 0), f"pure yellow at {width}")


class BrandGeneratorDeterminismTest(unittest.TestCase):
    def test_regeneration_is_byte_identical_and_matches_the_committed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            loaded = GENERATOR.Geometry.load()
            first = GENERATOR.artifacts(loaded, {"desktop": root / "a", "frontend": root / "a"})
            second = GENERATOR.artifacts(loaded, {"desktop": root / "b", "frontend": root / "b"})
            self.assertEqual(
                sorted(payload for payload in first.values()),
                sorted(payload for payload in second.values()),
            )
            by_name = {path.name: payload for path, payload in first.items()}
            self.assertEqual(by_name[SVG_NAME], (DESKTOP_ASSETS / SVG_NAME).read_bytes())
            self.assertEqual(by_name[ICO_NAME], (DESKTOP_ASSETS / ICO_NAME).read_bytes())

    def test_check_mode_reports_up_to_date_without_writing(self) -> None:
        before = {
            path: path.read_bytes()
            for path in (
                DESKTOP_ASSETS / SVG_NAME,
                DESKTOP_ASSETS / ICO_NAME,
                FRONTEND_ASSETS / SVG_NAME,
            )
        }

        self.assertEqual(GENERATOR.main(["--check"]), 0)

        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
