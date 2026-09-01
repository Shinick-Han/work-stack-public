"""Pure Wave 4 write-journal v2 contract.

The journal carries complete proposed bytes and both sides of every target CAS.
Filesystem mutation and recovery orchestration intentionally live elsewhere so
this module can be exhaustively tested without risking an authority.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .canonical import MAX_CANONICAL_INTEGER, canonical_json_bytes, canonical_sha256


JOURNAL_FORMAT = "workstack.write-journal"
JOURNAL_VERSION = 2
JOURNAL_PHASES = ("prepared", "applying", "manifest-published", "generation-published")
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_TARGET_BYTES = 32 * 1024 * 1024
MAX_TARGETS = 4096
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


class JournalV2Error(ValueError):
    """Content-free rejection of an invalid journal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


@dataclass(frozen=True)
class JournalTarget:
    action: str
    scope: str
    artifact: str
    expected_digest: str | None
    proposed_digest: str | None
    proposed_bytes: bytes | None

    @classmethod
    def replace(
        cls,
        artifact: str,
        proposed_bytes: bytes,
        *,
        expected_digest: str | None,
        scope: str = "authority",
    ) -> "JournalTarget":
        body = bytes(proposed_bytes)
        if scope not in {"authority", "runtime"}:
            raise JournalV2Error("TARGET_SCOPE_INVALID")
        _validate_artifact(artifact)
        _validate_optional_digest(expected_digest)
        if len(body) > MAX_TARGET_BYTES:
            raise JournalV2Error("TARGET_BYTE_LIMIT_EXCEEDED")
        return cls("replace", scope, artifact, expected_digest, _digest(body), body)

    @classmethod
    def delete(
        cls,
        artifact: str,
        *,
        expected_digest: str,
        scope: str = "authority",
    ) -> "JournalTarget":
        if scope not in {"authority", "runtime"}:
            raise JournalV2Error("TARGET_SCOPE_INVALID")
        _validate_artifact(artifact)
        expected = _validate_digest(expected_digest, "EXPECTED_DIGEST_INVALID")
        return cls("delete", scope, artifact, expected, None, None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "scope": self.scope,
            "artifact": self.artifact,
            "expected_digest": self.expected_digest,
            "proposed_digest": self.proposed_digest,
            "proposed_byte_count": (
                len(self.proposed_bytes) if self.proposed_bytes is not None else None
            ),
            "proposed_base64": (
                base64.b64encode(self.proposed_bytes).decode("ascii")
                if self.proposed_bytes is not None
                else None
            ),
        }


@dataclass(frozen=True)
class WriteJournalV2:
    value: Mapping[str, Any]
    canonical_bytes: bytes
    digest: str
    targets: tuple[JournalTarget, ...]


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _validate_digest(value: object, code: str = "DIGEST_INVALID") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise JournalV2Error(code)
    return value


def _validate_optional_digest(value: object) -> str | None:
    if value is None:
        return None
    return _validate_digest(value, "EXPECTED_DIGEST_INVALID")


def _validate_artifact(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise JournalV2Error("ARTIFACT_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise JournalV2Error("ARTIFACT_INVALID")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise JournalV2Error("CREATED_AT_INVALID")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise JournalV2Error("CREATED_AT_INVALID") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JournalV2Error("CREATED_AT_INVALID")
    return value


def _generation(value: object, code: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CANONICAL_INTEGER:
        raise JournalV2Error(code)
    return value


def _target_from_dict(value: object) -> JournalTarget:
    fields = {
        "action", "scope", "artifact", "expected_digest", "proposed_digest",
        "proposed_byte_count", "proposed_base64",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise JournalV2Error("TARGET_INVALID")
    scope = value["scope"]
    if scope not in {"authority", "runtime"}:
        raise JournalV2Error("TARGET_SCOPE_INVALID")
    artifact = _validate_artifact(value["artifact"])
    expected = _validate_optional_digest(value["expected_digest"])
    action = value["action"]
    if action == "delete":
        if expected is None or any(
            value[field] is not None
            for field in ("proposed_digest", "proposed_byte_count", "proposed_base64")
        ):
            raise JournalV2Error("DELETE_TARGET_INVALID")
        return JournalTarget(action, scope, artifact, expected, None, None)
    if action != "replace":
        raise JournalV2Error("TARGET_ACTION_INVALID")
    proposed = _validate_digest(value["proposed_digest"], "PROPOSED_DIGEST_INVALID")
    count = value["proposed_byte_count"]
    if type(count) is not int or not 0 <= count <= MAX_TARGET_BYTES:
        raise JournalV2Error("TARGET_BYTE_COUNT_INVALID")
    encoded = value["proposed_base64"]
    if not isinstance(encoded, str):
        raise JournalV2Error("TARGET_BASE64_INVALID")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise JournalV2Error("TARGET_BASE64_INVALID") from error
    if len(body) != count or _digest(body) != proposed:
        raise JournalV2Error("TARGET_CONTENT_MISMATCH")
    return JournalTarget(action, scope, artifact, expected, proposed, body)


def _validate_header(value: object) -> dict[str, Any]:
    fields = {
        "format", "schema_version", "workspace_uid", "operation_id", "created_at",
        "phase", "base_generation", "proposed_generation", "base_manifest_digest",
        "proposed_manifest_digest", "targets",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise JournalV2Error("JOURNAL_FIELDS_INVALID")
    if value["format"] != JOURNAL_FORMAT or value["schema_version"] != JOURNAL_VERSION:
        raise JournalV2Error("JOURNAL_VERSION_UNSUPPORTED")
    if not isinstance(value["workspace_uid"], str) or not _UUID.fullmatch(value["workspace_uid"]):
        raise JournalV2Error("WORKSPACE_UID_INVALID")
    if not isinstance(value["operation_id"], str) or not _OPERATION_ID.fullmatch(value["operation_id"]):
        raise JournalV2Error("OPERATION_ID_INVALID")
    _timestamp(value["created_at"])
    if not isinstance(value["phase"], str) or value["phase"] not in JOURNAL_PHASES:
        raise JournalV2Error("PHASE_INVALID")
    return value


def _validate_generation(value: Mapping[str, Any]) -> None:
    base = _generation(value["base_generation"], "BASE_GENERATION_INVALID")
    proposed = _generation(value["proposed_generation"], "PROPOSED_GENERATION_INVALID")
    if proposed != base + 1:
        raise JournalV2Error("GENERATION_TRANSITION_INVALID")
    _validate_digest(value["base_manifest_digest"], "BASE_MANIFEST_DIGEST_INVALID")
    _validate_digest(value["proposed_manifest_digest"], "PROPOSED_MANIFEST_DIGEST_INVALID")


def _validate_targets(value: object) -> tuple[JournalTarget, ...]:
    raw_targets = value
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= MAX_TARGETS:
        raise JournalV2Error("TARGET_ROSTER_INVALID")
    targets = tuple(_target_from_dict(target) for target in raw_targets)
    identities = tuple((target.scope, target.artifact) for target in targets)
    if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
        raise JournalV2Error("TARGET_ROSTER_INVALID")
    return targets


def _validated_value(value: object) -> tuple[dict[str, Any], tuple[JournalTarget, ...]]:
    validated = _validate_header(value)
    _validate_generation(validated)
    return validated, _validate_targets(validated["targets"])


def build_write_journal(
    *,
    workspace_uid: str,
    operation_id: str,
    created_at: str,
    base_generation: int,
    base_manifest_digest: str,
    proposed_manifest_digest: str,
    targets: Iterable[JournalTarget],
    phase: str = "prepared",
) -> WriteJournalV2:
    ordered = tuple(sorted(tuple(targets), key=lambda item: (item.scope, item.artifact)))
    value = {
        "format": JOURNAL_FORMAT,
        "schema_version": JOURNAL_VERSION,
        "workspace_uid": workspace_uid,
        "operation_id": operation_id,
        "created_at": created_at,
        "phase": phase,
        "base_generation": base_generation,
        "proposed_generation": base_generation + 1,
        "base_manifest_digest": base_manifest_digest,
        "proposed_manifest_digest": proposed_manifest_digest,
        "targets": [target.as_dict() for target in ordered],
    }
    validated, parsed_targets = _validated_value(value)
    body = canonical_json_bytes(validated)
    if len(body) > MAX_JOURNAL_BYTES:
        raise JournalV2Error("JOURNAL_BYTE_LIMIT_EXCEEDED")
    return WriteJournalV2(validated, body, canonical_sha256(validated), parsed_targets)


def parse_write_journal(body: bytes, *, maximum: int = MAX_JOURNAL_BYTES) -> WriteJournalV2:
    if not isinstance(body, bytes) or len(body) > maximum:
        raise JournalV2Error("JOURNAL_BYTE_LIMIT_EXCEEDED")
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        _DuplicateKeyError,
    ) as error:
        raise JournalV2Error("JOURNAL_JSON_INVALID") from error
    validated, targets = _validated_value(value)
    canonical = canonical_json_bytes(validated)
    if canonical != body:
        raise JournalV2Error("JOURNAL_CANONICAL_BYTES_REQUIRED")
    return WriteJournalV2(validated, canonical, canonical_sha256(validated), targets)


def advance_journal_phase(journal: WriteJournalV2, phase: str) -> WriteJournalV2:
    if phase not in JOURNAL_PHASES:
        raise JournalV2Error("PHASE_INVALID")
    current = JOURNAL_PHASES.index(str(journal.value["phase"]))
    requested = JOURNAL_PHASES.index(phase)
    if requested < current:
        raise JournalV2Error("PHASE_REGRESSION")
    value = dict(journal.value)
    value["phase"] = phase
    validated, targets = _validated_value(value)
    body = canonical_json_bytes(validated)
    return WriteJournalV2(validated, body, canonical_sha256(validated), targets)
