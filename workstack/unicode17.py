"""Pinned Unicode Standard 17.0.0 normalization for docking snapshots."""

from __future__ import annotations

try:
    import unicodedata2 as _unicode
except ImportError as error:  # pragma: no cover - exercised by installation smoke tests
    raise RuntimeError(
        "Work Stack snapshot export requires unicodedata2==17.0.0"
    ) from error


UNICODE_DATA_VERSION = _unicode.unidata_version
if UNICODE_DATA_VERSION != "17.0.0":  # pragma: no cover - fail closed on drift
    raise RuntimeError(
        "Work Stack snapshot export requires Unicode data 17.0.0, found {}".format(
            UNICODE_DATA_VERSION
        )
    )


def normalize_nfc(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Unicode normalization input must be a string")
    return _unicode.normalize("NFC", value)
