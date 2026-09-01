"""Stable, local-only runtime placement for one Work Stack authority.

Runtime artifacts are disposable coordination and projection state.  They must
never live inside the canonical authority and must not be copied to another
machine merely because the authority is synchronized there.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class RuntimeLayoutError(ValueError):
    """Content-free refusal to place runtime state unsafely."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RuntimeAuthority:
    """Resolved local runtime identity for one exact authority location."""

    workspace_uid: str
    authority_root: Path
    authority_key: str
    runtime_root: Path

    @property
    def journal_path(self) -> Path:
        return self.runtime_root / "write-journal.v2.json"

    @property
    def manifest_path(self) -> Path:
        return self.runtime_root / "authority-manifest.v2.json"

    @property
    def idempotency_path(self) -> Path:
        return self.runtime_root / "idempotency-ledger.v1.json"

    @property
    def projection_state_path(self) -> Path:
        return self.runtime_root / "projection-state.json"


def _normalized(path: Path | str) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise RuntimeLayoutError("PATH_REQUIRED")
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeLayoutError("PATH_INVALID") from error


def _contains(parent: Path, child: Path) -> bool:
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(parent)), os.path.normcase(str(child)))
        )
        return common == os.path.normcase(str(parent))
    except ValueError:
        return False


def authority_key(authority_root: Path | str, workspace_uid: str) -> str:
    """Return a stable key bound to both logical identity and exact location."""

    if not isinstance(workspace_uid, str) or not _UUID.fullmatch(workspace_uid):
        raise RuntimeLayoutError("WORKSPACE_UID_INVALID")
    root = _normalized(authority_root)
    identity = workspace_uid.encode("ascii") + b"\0" + os.path.normcase(str(root)).encode("utf-8")
    return "authority-" + hashlib.sha256(identity).hexdigest()[:32]


def resolve_runtime_authority(
    authority_root: Path | str,
    runtime_base: Path | str,
    workspace_uid: str,
) -> RuntimeAuthority:
    """Resolve runtime paths without creating or mutating either directory."""

    authority = _normalized(authority_root)
    runtime = _normalized(runtime_base)
    if authority == runtime or _contains(authority, runtime) or _contains(runtime, authority):
        raise RuntimeLayoutError("RUNTIME_AUTHORITY_OVERLAP")
    key = authority_key(authority, workspace_uid)
    return RuntimeAuthority(workspace_uid, authority, key, runtime / key)
