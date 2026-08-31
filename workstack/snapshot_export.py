"""Read-only snapshot artifact and exclusive file delivery helpers."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .snapshot import (
    build_snapshot,
    canonical_snapshot_bytes,
    snapshot_digest,
    validate_snapshot_bytes,
)


OMITTED_PLANNING_FIELDS = (
    "objectives",
    "dependencies",
    "subtasks",
    "notes",
    "tags",
)


@dataclass(frozen=True)
class SnapshotArtifact:
    snapshot: dict[str, Any]
    canonical_bytes: bytes
    digest: str
    filename: str
    omissions: tuple[str, ...] = OMITTED_PLANNING_FIELDS


def create_snapshot_artifact(
    workspace_uid: str, task: dict[str, Any], planning_status: str
) -> SnapshotArtifact:
    snapshot = build_snapshot(workspace_uid, task, planning_status)
    canonical = canonical_snapshot_bytes(snapshot)
    digest = snapshot_digest(canonical)
    reparsed = validate_snapshot_bytes(canonical, digest)
    if canonical_snapshot_bytes(reparsed) != canonical:
        raise RuntimeError("canonical snapshot round-trip mismatch")
    return SnapshotArtifact(
        snapshot=snapshot,
        canonical_bytes=canonical,
        digest=digest,
        filename="{}.workstack-task.json".format(snapshot["planning_task_uid"]),
    )


def write_snapshot_file(destination: Path | str, canonical_bytes: bytes) -> Path:
    """Publish exact bytes without overwriting and remove every partial temporary."""

    path = Path(destination).resolve()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".",
        suffix=".workstack-export.tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)
