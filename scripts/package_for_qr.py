#!/usr/bin/env python3
"""Create a deterministic ZIP and reversible Base45 frame payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
import zlib
from pathlib import Path


ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
VALUE = {character: index for index, character in enumerate(ALPHABET)}
SKIP_PARTS = {".git", ".venv", "__pycache__", "venv"}
SKIP_SUFFIXES = {".pyc", ".zip", ".zst"}


def base45_encode(data: bytes) -> str:
    output: list[str] = []
    index = 0
    while index + 1 < len(data):
        value = data[index] * 256 + data[index + 1]
        output.extend((
            ALPHABET[value % 45],
            ALPHABET[(value // 45) % 45],
            ALPHABET[value // (45 * 45)],
        ))
        index += 2
    if index < len(data):
        output.extend((ALPHABET[data[index] % 45], ALPHABET[data[index] // 45]))
    return "".join(output)


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


def source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path


def create_zip(root: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in source_files(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            output.writestr(info, path.read_bytes())


def make_frames(archive: Path, frame_dir: Path, chunk_size: int) -> dict:
    if frame_dir.exists() and any(frame_dir.iterdir()):
        raise ValueError("frame directory must be empty")
    frame_dir.mkdir(parents=True, exist_ok=True)
    raw = archive.read_bytes()
    digest = hashlib.sha256(raw).hexdigest().upper()
    chunks = [raw[offset:offset + chunk_size] for offset in range(0, len(raw), chunk_size)]
    safe_name = "".join(character if character in ALPHABET else "-" for character in archive.name.upper())
    for index, chunk in enumerate(chunks, start=1):
        checksum = "{:08X}".format(zlib.crc32(chunk) & 0xFFFFFFFF)
        header = "SQR1:{}:{}:{:04d}:{:04d}:{}:".format(
            safe_name, digest, index, len(chunks), checksum
        )
        (frame_dir / "frame-{:04d}.txt".format(index)).write_text(
            header + base45_encode(chunk), encoding="ascii"
        )
    manifest = {
        "format": "SQR1",
        "archive": archive.name,
        "archive_bytes": len(raw),
        "sha256": digest,
        "chunk_bytes": chunk_size,
        "frames": len(chunks),
    }
    (frame_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def restore(frame_dir: Path, output: Path) -> None:
    parsed = []
    for path in sorted(frame_dir.glob("frame-*.txt")):
        parts = path.read_text(encoding="ascii").split(":", 6)
        if len(parts) != 7 or parts[0] != "SQR1":
            raise ValueError("invalid frame: {}".format(path.name))
        _, _name, digest, index, total, checksum, encoded = parts
        chunk = base45_decode(encoded)
        if "{:08X}".format(zlib.crc32(chunk) & 0xFFFFFFFF) != checksum:
            raise ValueError("checksum mismatch: {}".format(path.name))
        parsed.append((int(index), int(total), digest, chunk))
    if not parsed:
        raise ValueError("no frames")
    parsed.sort()
    total, digest = parsed[0][1], parsed[0][2]
    if len(parsed) != total or [item[0] for item in parsed] != list(range(1, total + 1)):
        raise ValueError("frame sequence mismatch")
    if any(item[1] != total or item[2] != digest for item in parsed):
        raise ValueError("frame set mismatch")
    raw = b"".join(item[3] for item in parsed)
    if hashlib.sha256(raw).hexdigest().upper() != digest:
        raise ValueError("archive digest mismatch")
    output.write_bytes(raw)


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("root", type=Path)
    arguments.add_argument("archive", type=Path)
    arguments.add_argument("frame_dir", type=Path)
    arguments.add_argument("--chunk-bytes", type=int, default=1450)
    options = arguments.parse_args()
    root, archive, frame_dir = options.root.resolve(), options.archive.resolve(), options.frame_dir.resolve()
    create_zip(root, archive)
    manifest = make_frames(archive, frame_dir, options.chunk_bytes)
    restored = archive.with_name("restored-" + archive.name)
    restore(frame_dir, restored)
    if restored.read_bytes() != archive.read_bytes():
        raise ValueError("text frame round trip mismatch")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
