from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from generated.theme_tokens import THEME_TOKENS


THEME_STATE_FILE = "desktop-theme.json"
THEME_STATE_SCHEMA_VERSION = 1
MAX_THEME_STATE_BYTES = 256


def normalize_theme(theme: str) -> str:
    return theme if theme in THEME_TOKENS else "dark"


def theme_color(theme: str, token: str) -> str:
    normalized = normalize_theme(theme)
    value = THEME_TOKENS[normalized].get(token)
    if not isinstance(value, str) or not value.startswith("#") or len(value) not in {4, 5, 7, 9}:
        raise ValueError(f"invalid native theme token {normalized}.{token}: {value!r}")
    return value


def theme_rgb(theme: str, token: str) -> tuple[int, int, int]:
    value = theme_color(theme, token).removeprefix("#")
    if len(value) in {3, 4}:
        value = "".join(character * 2 for character in value)
    if len(value) == 8:
        value = value[:6]
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def theme_colorref(theme: str, token: str) -> int:
    """Return the generated token as a Win32 COLORREF value."""

    red, green, blue = theme_rgb(theme, token)
    return red | (green << 8) | (blue << 16)


def load_persisted_theme(state_root: Path) -> str:
    """Read the bounded, exact desktop theme document or fail safely to dark."""

    path = state_root / THEME_STATE_FILE
    try:
        if path.is_symlink() or not path.is_file():
            return "dark"
        with path.open("rb") as source:
            encoded = source.read(MAX_THEME_STATE_BYTES + 1)
    except OSError:
        return "dark"
    if not encoded or len(encoded) > MAX_THEME_STATE_BYTES:
        return "dark"
    try:
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return "dark"
    selected_theme = document.get("theme") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "theme"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != THEME_STATE_SCHEMA_VERSION
        or not isinstance(selected_theme, str)
        or selected_theme not in THEME_TOKENS
    ):
        return "dark"
    return selected_theme


def persist_theme(state_root: Path, theme: str) -> str:
    """Atomically persist a normalized theme in the strict desktop document."""

    normalized = normalize_theme(theme)
    state_root.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            {"schema_version": THEME_STATE_SCHEMA_VERSION, "theme": normalized},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_THEME_STATE_BYTES:
        raise ValueError("desktop theme document exceeds its storage bound")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{THEME_STATE_FILE}.",
            suffix=".tmp",
            dir=state_root,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, state_root / THEME_STATE_FILE)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return normalized


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
