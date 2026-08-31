#!/usr/bin/env python3
"""Decode SQR1 PNG frames and restore the original archive."""

from __future__ import annotations

import argparse
import hashlib
import sys
import zlib
from pathlib import Path

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
VALUE = {character: index for index, character in enumerate(ALPHABET)}


def base45_decode(text: str) -> bytes:
    output = bytearray()
    index = 0
    while index < len(text):
        width = 3 if len(text) - index >= 3 else 2
        if width == 3:
            value = VALUE[text[index]] + VALUE[text[index + 1]] * 45 + VALUE[text[index + 2]] * 2025
            if value > 65535:
                raise ValueError("invalid Base45 triplet")
            output.extend(divmod(value, 256))
        else:
            value = VALUE[text[index]] + VALUE[text[index + 1]] * 45
            if value > 255:
                raise ValueError("invalid Base45 pair")
            output.append(value)
        index += width
    return bytes(output)


def decode(path: Path):
    try:
        import zxingcpp
        from PIL import Image
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "QR image tooling is optional. Install it with "
            "'python -m pip install --require-hashes -r requirements-qr-windows.txt'."
        ) from error
    result = zxingcpp.read_barcode(Image.open(path).convert("RGB"))
    if result is None or not result.text:
        raise ValueError("QR decode failed: {}".format(path.name))
    parts = result.text.split(":", 6)
    if len(parts) != 7 or parts[0] != "SQR1":
        raise ValueError("invalid frame: {}".format(path.name))
    _, archive_name, digest, index, total, checksum, encoded = parts
    chunk = base45_decode(encoded)
    if "{:08X}".format(zlib.crc32(chunk) & 0xFFFFFFFF) != checksum:
        raise ValueError("chunk checksum mismatch: {}".format(path.name))
    return archive_name, digest, int(index), int(total), chunk


def restore(image_dir: Path, output: Path) -> None:
    frames = [decode(path) for path in sorted(image_dir.glob("frame-*.png"))]
    if not frames:
        raise ValueError("no QR images")
    name, digest, _index, total, _chunk = frames[0]
    if len(frames) != total:
        raise ValueError("missing frames")
    if any(frame[0] != name or frame[1] != digest or frame[3] != total for frame in frames):
        raise ValueError("frame set mismatch")
    frames.sort(key=lambda frame: frame[2])
    if [frame[2] for frame in frames] != list(range(1, total + 1)):
        raise ValueError("frame sequence mismatch")
    raw = b"".join(frame[4] for frame in frames)
    actual = hashlib.sha256(raw).hexdigest().upper()
    if actual != digest:
        raise ValueError("archive digest mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print("restored {} bytes to {}".format(len(raw), output))
    print("sha256 {}".format(actual))


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("image_dir", type=Path)
    arguments.add_argument("output", type=Path)
    options = arguments.parse_args()
    restore(options.image_dir.resolve(), options.output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
