"""Bounded probe vocabulary shared by the streamed remote provisioning collector.

This module holds the collector's failure type, its clipped text bounds, the
Linux POSIX path / owner / interpreter-version / digest shapes and the exact
``provision-facts`` argv contract. It imports nothing outside the standard
library so it can be streamed to ``python -I -B -`` alongside the collector.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


MAX_DETAIL_LENGTH = 256
MAX_CODE_LENGTH = 64
MAX_PRODUCT_VERSION_LENGTH = 64
MAX_PROTOCOL_VERSION = 1_000_000
COLLECTOR_COMMAND = "provision-facts"
SCRATCH_FLAG = "--capability-scratch-parent"
OWNER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
POSIX_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
LINK_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_LINK_TARGET_LENGTH = 4096
PYTHON_VERSION_PATTERN = re.compile(
    r"^3\.([0-9]|[1-9][0-9])(?:\.([0-9]|[1-9][0-9]{0,2}))?$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProbeError(RuntimeError):
    """Bounded probe failure that must not echo raw stderr or paths."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = _clip_text(code, MAX_CODE_LENGTH)
        self.detail = _clip_text(detail, MAX_DETAIL_LENGTH)
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def _clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


def _admitted_link_target(value: object) -> str | None:
    """Return the readlink payload the collector is willing to follow, else None.

    The alphabet matches the configured POSIX path shape, the payload is bounded,
    and empty, doubled or trailing separators and control characters are refused.
    The bare root ``/`` is a legitimate target and is admitted.
    """

    if type(value) is not str or not value or len(value) > MAX_LINK_TARGET_LENGTH:
        return None
    if not LINK_TARGET_PATTERN.fullmatch(value):
        return None
    if "//" in value or (value != "/" and value.endswith("/")):
        return None
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        return None
    return value


def _link_target_segments(value: object, prefix: Sequence[str]) -> list[str] | None:
    """Resolve one bounded readlink target against the symlink's own directory.

    A Linux alias is routinely relative (``/u -> remote``, ``/srv/u -> ../remote``)
    or the root itself (``/u -> /``); those are ordinary configured targets, not
    reparse ambiguity. ``prefix`` is the already-walked, symlink-free component
    list of the directory holding the link, so a relative target continues from
    it and an absolute target restarts from the root. ``.`` is dropped and ``..``
    pops one component, stopping at the root exactly as POSIX ``/..`` does.

    Returns the resolved component list, or None when the target is not a
    bounded POSIX name the collector is willing to follow. Following the target
    is all this decides: the leaf-symlink refusal, the hop bound and the
    re-resolution stability check stay with the caller.
    """

    target = _admitted_link_target(value)
    if target is None:
        return None
    resolved = [] if target.startswith("/") else list(prefix)
    for segment in target.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(segment)
    return resolved


def _is_posix(value: object) -> bool:
    if type(value) is not str or not POSIX_PATH_PATTERN.fullmatch(value):
        return False
    if value == "/" or value.endswith("/"):
        return False
    segments = value.split("/")[1:]
    return bool(segments) and not any(segment in {"", ".", ".."} for segment in segments)


def _posix_arg(value: object) -> str:
    if not _is_posix(value):
        raise ProbeError("INVALID_PROBE", "path is not a valid Linux POSIX path")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise ProbeError("INVALID_PROBE", "path is not a valid Linux POSIX path")
    return value


def _owner_arg(value: object) -> str:
    if type(value) is not str or not OWNER_PATTERN.fullmatch(value):
        raise ProbeError("INVALID_PROBE", "owner is not a POSIX user name")
    return value


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def parse_collector_argv(argv: Sequence[str]) -> tuple[str, str, str, str | None]:
    if len(argv) not in {7, 9}:
        raise ProbeError("INVALID_PROBE", "malformed argv")
    if (
        argv[0] != COLLECTOR_COMMAND
        or argv[1] != "--install-root"
        or argv[3] != "--data-root"
        or argv[5] != "--owner"
    ):
        raise ProbeError("INVALID_PROBE", "malformed argv")
    install_root = _posix_arg(argv[2])
    data_root = _posix_arg(argv[4])
    owner = _owner_arg(argv[6])
    if _overlap(install_root, data_root):
        raise ProbeError("INVALID_PROBE", "install and data roots must be separate")
    scratch = None
    if len(argv) == 9:
        if argv[7] != SCRATCH_FLAG:
            raise ProbeError("INVALID_PROBE", "malformed argv")
        scratch = _posix_arg(argv[8])
        if scratch == install_root or scratch == data_root or scratch.startswith(data_root + "/"):
            raise ProbeError("INVALID_PROBE", "scratch parent must be the application parent")
    return install_root, data_root, owner, scratch
