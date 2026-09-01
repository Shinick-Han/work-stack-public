"""Pure record staging primitives for the normalized v4 writer.

This module deliberately performs no filesystem writes.  It turns one
revision- and digest-checked record mutation into immutable canonical bytes
that a later journal/write-session layer can commit.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .canonical import CanonicalJsonError, canonical_json_bytes
from .contracts import StorageContractError, require_valid_by_format
from .layout import RECORD_KINDS, StorageLayoutError, V4Layout


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORMAT_BY_KIND = {
    "captures": "workstack.capture",
    "notes": "workstack.note",
    "objectives": "workstack.objective",
    "replies": "workstack.reply",
    "tasks": "workstack.task",
}


class V4RecordStagingError(ValueError):
    """A content-free refusal to stage one record mutation."""

    def __init__(self, code: str, artifact: str = "") -> None:
        super().__init__(code if not artifact else f"{code}: {artifact}")
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class StagedRecordWrite:
    """One complete intended record-file state for journal v2."""

    artifact: str
    kind: str
    uid: str
    expected_revision: int | None
    proposed_revision: int | None
    expected_digest: str | None
    intended_digest: str | None
    body: bytes | None

    @property
    def deletes_target(self) -> bool:
        return self.body is None


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _artifact(kind: str, uid: str) -> str:
    try:
        path = V4Layout(Path(".")).record_path(kind, uid)
    except StorageLayoutError as error:
        raise V4RecordStagingError(error.code) from error
    return path.as_posix()


def _canonical_record(kind: str, value: Mapping[str, Any]) -> tuple[str, bytes]:
    if kind not in RECORD_KINDS:
        raise V4RecordStagingError("UNKNOWN_RECORD_KIND")
    if not isinstance(value, Mapping):
        raise V4RecordStagingError("RECORD_OBJECT_REQUIRED")
    detached = dict(value)
    if detached.get("format") != _FORMAT_BY_KIND[kind]:
        raise V4RecordStagingError("RECORD_KIND_MISMATCH")
    try:
        require_valid_by_format(detached)
        body = canonical_json_bytes(detached)
    except (CanonicalJsonError, StorageContractError) as error:
        raise V4RecordStagingError("RECORD_CONTRACT_INVALID") from error
    uid = detached.get("uid")
    if not isinstance(uid, str):
        raise V4RecordStagingError("RECORD_UID_INVALID")
    return _artifact(kind, uid), body


def _checked_current(
    kind: str,
    current: Mapping[str, Any],
    expected_revision: int,
    expected_digest: str,
) -> tuple[str, bytes]:
    artifact, body = _canonical_record(kind, current)
    revision = current.get("revision")
    if type(expected_revision) is not int or expected_revision < 0:
        raise V4RecordStagingError("EXPECTED_REVISION_INVALID", artifact)
    if revision != expected_revision:
        raise V4RecordStagingError("STALE_RECORD_REVISION", artifact)
    if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
        raise V4RecordStagingError("EXPECTED_DIGEST_INVALID", artifact)
    if _sha256(body) != expected_digest:
        raise V4RecordStagingError("STALE_RECORD_DIGEST", artifact)
    return artifact, body


def stage_record_put(
    kind: str,
    proposed: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None,
    expected_revision: int | None,
    expected_digest: str | None,
) -> StagedRecordWrite:
    """Stage a create or single-revision update without touching the authority."""

    artifact, body = _canonical_record(kind, proposed)
    proposed_revision = proposed.get("revision")
    if type(proposed_revision) is not int or proposed_revision < 0:
        raise V4RecordStagingError("PROPOSED_REVISION_INVALID", artifact)

    if current is None:
        if expected_revision is not None or expected_digest is not None:
            raise V4RecordStagingError("CREATE_EXPECTATION_INVALID", artifact)
        if proposed_revision != 0:
            raise V4RecordStagingError("CREATE_REVISION_INVALID", artifact)
        return StagedRecordWrite(
            artifact,
            kind,
            str(proposed["uid"]),
            None,
            proposed_revision,
            None,
            _sha256(body),
            body,
        )

    if expected_revision is None or expected_digest is None:
        raise V4RecordStagingError("UPDATE_EXPECTATION_REQUIRED", artifact)
    current_artifact, _ = _checked_current(
        kind, current, expected_revision, expected_digest
    )
    if current_artifact != artifact or current.get("uid") != proposed.get("uid"):
        raise V4RecordStagingError("RECORD_IDENTITY_CHANGED", artifact)
    if current.get("workspace_uid") != proposed.get("workspace_uid"):
        raise V4RecordStagingError("WORKSPACE_IDENTITY_CHANGED", artifact)
    if proposed_revision != expected_revision + 1:
        raise V4RecordStagingError("REVISION_DID_NOT_ADVANCE", artifact)
    return StagedRecordWrite(
        artifact,
        kind,
        str(proposed["uid"]),
        expected_revision,
        proposed_revision,
        expected_digest,
        _sha256(body),
        body,
    )


def stage_record_delete(
    kind: str,
    current: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_digest: str,
) -> StagedRecordWrite:
    """Stage a physical delete marker; domain tombstone policy remains above this layer."""

    artifact, _ = _checked_current(
        kind, current, expected_revision, expected_digest
    )
    return StagedRecordWrite(
        artifact,
        kind,
        str(current["uid"]),
        expected_revision,
        None,
        expected_digest,
        None,
        None,
    )
