"""Read-only, format-specific metadata for an inactive normalized authority.

This storage-layer facade is intentionally injected into desktop profile
inspection only when a composition root opts in.  Keeping the dependency
direction explicit prevents the desktop layer from importing physical storage
modules and keeps released v3 profile testing unchanged.
"""

from __future__ import annotations

from pathlib import Path

from .manifest import build_v4_manifest
from .reader import read_v4
from .validation import validate_storage_path


def inspect_inactive_v4_authority(root: Path | str) -> dict[str, object]:
    """Return content-free metadata after a full read-only v4 verification."""

    report = validate_storage_path(root)
    if not report.valid or report.format_version != 4:
        raise ValueError("V4_AUTHORITY_INVALID")
    result = read_v4(root)
    manifest = build_v4_manifest(result)
    if result.store.get("schema_version") != 4:
        raise ValueError("V4_AUTHORITY_SCHEMA_UNSUPPORTED")
    return {
        "authority_manifest_digest": manifest.digest,
        "capabilities": {
            "read": True,
            "write": False,
            "migrate": False,
            "projection": True,
        },
        "schema_version": 4,
        "storage_format": "v4",
        "workspace_uid": result.workspace_uid,
    }
