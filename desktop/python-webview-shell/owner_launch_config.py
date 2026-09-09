"""Trusted operator launch-path helper for the Windows owner process.

Reads only the optional local config.json field ``knowledge_drivers_config``.
Absence leaves graph-serve argv unchanged. A present value must already be an
absolute Windows path string; it is forwarded unmodified. This is not a
browser, env, or webview boundary, and the registry file is not opened here.
"""

from __future__ import annotations

from collections.abc import Mapping


FIELD = "knowledge_drivers_config"
FLAG = "--knowledge-drivers-config"
REFUSAL = "Work Stack knowledge drivers configuration path is invalid."
_DRIVE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CONTROL = frozenset(chr(code) for code in range(32)) | {"\x7f"}


class OwnerLaunchConfigError(RuntimeError):
    """Content-free refusal; the rejected value is never in the message."""


def _has_control(text: str) -> bool:
    return any(character in _CONTROL for character in text)


def _is_drive_absolute(text: str) -> bool:
    return (
        len(text) >= 3
        and text[0] in _DRIVE_LETTERS
        and text[1] == ":"
        and text[2] in "\\/"
    )


def _is_unc_body(text: str) -> bool:
    if not text or text[0] in "\\/":
        return False
    parts = text.replace("/", "\\").split("\\")
    return len(parts) >= 2 and parts[0] != "" and parts[1] != ""


def is_windows_absolute_owner_path(value: object) -> bool:
    """True only for a non-empty Windows drive or UNC absolute path string."""

    if not isinstance(value, str) or not value or _has_control(value):
        return False
    if value.startswith("\\\\?\\"):
        rest = value[4:]
        if rest[:4].upper() == "UNC\\":
            return _is_unc_body(rest[4:])
        return _is_drive_absolute(rest)
    if value.startswith("\\\\"):
        return _is_unc_body(value[2:])
    return _is_drive_absolute(value)


def knowledge_drivers_config_argv(config: Mapping[str, object]) -> tuple[str, ...]:
    """Return ``()`` when the field is absent; otherwise flag plus raw path."""

    if FIELD not in config:
        return ()
    value = config[FIELD]
    if not is_windows_absolute_owner_path(value):
        raise OwnerLaunchConfigError(REFUSAL)
    return (FLAG, value)


def graph_serve_argv(
    python_path: str,
    entry_path: str,
    data_path: str,
    port: int,
    config: Mapping[str, object],
) -> list[str]:
    """Bundled runtime ``graph serve`` argv, with the optional config flag."""

    argv = [
        python_path,
        entry_path,
        "--data-dir",
        data_path,
        "graph",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    argv.extend(knowledge_drivers_config_argv(config))
    return argv
