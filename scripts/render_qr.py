#!/usr/bin/env python3
"""Render SQR1 text frames as QR PNG files and a contact sheet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def load_image_dependencies():
    try:
        import qrcode
        from PIL import Image, ImageDraw
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "QR image tooling is optional. Install it with "
            "'python -m pip install --require-hashes -r requirements-qr-windows.txt'."
        ) from error
    return qrcode, Image, ImageDraw


def make_contact_sheet(rendered, output: Path, Image, ImageDraw) -> None:
    thumb = 220
    label = 28
    columns = 4
    rows = (len(rendered) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb, rows * (thumb + label)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (path, version) in enumerate(rendered):
        image = Image.open(path).convert("RGB")
        image.thumbnail((thumb - 16, thumb - 16), Image.Resampling.NEAREST)
        column, row = index % columns, index // columns
        left = column * thumb + (thumb - image.width) // 2
        top = row * (thumb + label) + 8
        sheet.paste(image, (left, top))
        draw.text((column * thumb + 8, row * (thumb + label) + thumb), "{} · V{}".format(path.stem, version), fill="black")
    sheet.save(output, format="PNG", optimize=True)


def render(frame_dir: Path, output_dir: Path, box_size: int) -> int:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    qrcode, Image, ImageDraw = load_image_dependencies()
    rendered = []
    for path in sorted(frame_dir.glob("frame-*.txt")):
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_Q,
            box_size=box_size,
            border=4,
        )
        qr.add_data(path.read_text(encoding="ascii"))
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        output = output_dir / (path.stem + ".png")
        image.save(output, format="PNG", optimize=True)
        rendered.append((output, qr.version))
        print("{} {}x{} version={}".format(output.name, image.width, image.height, qr.version))
    if not rendered:
        raise ValueError("no text frames")
    make_contact_sheet(rendered, output_dir / "contact-sheet.png", Image, ImageDraw)
    return len(rendered)


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("frame_dir", type=Path)
    arguments.add_argument("output_dir", type=Path)
    arguments.add_argument("--box-size", type=int, default=8)
    options = arguments.parse_args()
    print("rendered {} QR images".format(render(options.frame_dir.resolve(), options.output_dir.resolve(), options.box_size)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
