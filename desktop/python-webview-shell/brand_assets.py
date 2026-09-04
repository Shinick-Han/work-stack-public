"""Package-owned brand asset access.

Every path here is resolved relative to this module, so the mark travels with
the packaged desktop shell and is never read from the user's profile, the
install root or the current working directory.

A missing generated asset is reported as such. Callers fall back to plain text,
never to a stale icon file and never by repairing a user file.
"""

from __future__ import annotations

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
GEOMETRY_PATH = ASSETS_DIR / "brand-mark.v1.json"
MARK_SVG_PATH = ASSETS_DIR / "WorkStack-Mark-Lime-v2.svg"
MARK_ICO_PATH = ASSETS_DIR / "WorkStack-Mark-Lime-v2.ico"

# The SVG is a small, fully known artifact this package generates itself.
MAX_MARK_BYTES = 64 * 1024


class BrandAssetMissing(RuntimeError):
    """The packaged mark is absent or unreadable. Never silently substituted."""


def mark_svg_path() -> Path:
    return MARK_SVG_PATH


def mark_ico_path() -> Path:
    return MARK_ICO_PATH


def has_mark_ico() -> bool:
    return MARK_ICO_PATH.is_file()


def read_mark_svg() -> str:
    """Return the packaged SVG markup, bounded and without an XML declaration."""

    # Read at most one byte past the limit. Checking the size after an
    # unbounded read would already have allocated whatever was on disk, so the
    # bound has to be applied to the read itself rather than to the result.
    try:
        with open(MARK_SVG_PATH, "rb") as handle:
            raw = handle.read(MAX_MARK_BYTES + 1)
    except OSError as error:
        raise BrandAssetMissing("the packaged Work Stack mark is unavailable") from error
    if len(raw) > MAX_MARK_BYTES:
        raise BrandAssetMissing("the packaged Work Stack mark is larger than expected")
    try:
        markup = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise BrandAssetMissing("the packaged Work Stack mark is not valid UTF-8") from error
    if not markup.startswith("<svg") or not markup.endswith("</svg>"):
        raise BrandAssetMissing("the packaged Work Stack mark is not a bare SVG element")
    return markup


def inline_mark_markup(css_class: str = "mark") -> str:
    """Inline SVG for an HTML surface, decorative and bounded.

    Falls back to an empty accent tile rather than the literal ``|||`` glyph or
    any other stale drawing when the packaged asset is missing.
    """

    try:
        markup = read_mark_svg()
    except BrandAssetMissing:
        return f'<div class="{css_class}" aria-hidden="true"></div>'
    return f'<div class="{css_class}" aria-hidden="true">{markup}</div>'
