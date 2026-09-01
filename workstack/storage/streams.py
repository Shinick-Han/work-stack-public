"""Pure staged replacement primitives for normalized v4 event streams."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256
from .contracts import StorageContractError, require_valid_by_format
from .layout import STREAM_KINDS


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORMAT_BY_KIND = {
    "activity": "workstack.activity-event",
    "planning-status": "workstack.planning-status-event",
    "worklog": "workstack.worklog-event",
}
_CHAIN_FIELDS = {"sequence", "previous_event_digest", "event_digest"}


class V4StreamStagingError(ValueError):
    """A content-free refusal to stage one or more stream replacements."""

    def __init__(self, code: str, artifact: str = "") -> None:
        super().__init__(code if not artifact else f"{code}: {artifact}")
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class StagedStreamWrite:
    """One complete intended NDJSON segment state for journal v2."""

    artifact: str
    kind: str
    segment: str
    expected_digest: str | None
    intended_digest: str
    body: bytes
    first_sequence: int
    last_sequence: int
    head_event_uid: str
    head_digest: str
    appended_events: tuple[Mapping[str, Any], ...]


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _body(events: Sequence[Mapping[str, Any]]) -> bytes:
    return b"\n".join(canonical_json_bytes(dict(event)) for event in events) + b"\n"


def _artifact(kind: str, created_at: str) -> str:
    return f"streams/{kind}/{created_at[:7]}.ndjson"


def _time_key(value: str) -> dt.datetime:
    try:
        if len(value) == 10:
            return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise V4StreamStagingError("EVENT_TIMESTAMP_INVALID") from error


def _validate_event(kind: str, event: Mapping[str, Any]) -> None:
    if kind not in STREAM_KINDS:
        raise V4StreamStagingError("UNKNOWN_STREAM_KIND")
    if not isinstance(event, Mapping):
        raise V4StreamStagingError("EVENT_OBJECT_REQUIRED")
    value = dict(event)
    if value.get("format") != _FORMAT_BY_KIND[kind]:
        raise V4StreamStagingError("STREAM_KIND_MISMATCH")
    try:
        require_valid_by_format(value)
        canonical_json_bytes(value)
    except (CanonicalJsonError, StorageContractError) as error:
        raise V4StreamStagingError("EVENT_CONTRACT_INVALID") from error


def _ordered_existing(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[tuple[str, Mapping[str, Any]]]:
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for kind, events in streams.items():
        if kind not in STREAM_KINDS:
            raise V4StreamStagingError("UNKNOWN_STREAM_KIND")
        for event in events:
            _validate_event(kind, event)
            entries.append((kind, event))
    entries.sort(key=lambda item: int(item[1]["sequence"]))
    if [event["sequence"] for _, event in entries] != list(range(1, len(entries) + 1)):
        raise V4StreamStagingError("STREAM_SEQUENCE_INVALID")
    seen_uids: set[str] = set()
    previous: str | None = None
    for _, event in entries:
        uid = str(event["event_uid"])
        if uid in seen_uids:
            raise V4StreamStagingError("DUPLICATE_EVENT_UID")
        seen_uids.add(uid)
        candidate = dict(event)
        claimed = candidate.pop("event_digest", None)
        if not isinstance(claimed, str) or not _SHA256.fullmatch(claimed):
            raise V4StreamStagingError("STREAM_DIGEST_CHAIN_REQUIRED")
        expected_previous = candidate.get("previous_event_digest")
        if expected_previous != previous:
            raise V4StreamStagingError("STREAM_DIGEST_CHAIN_INVALID")
        if claimed != canonical_sha256(candidate):
            raise V4StreamStagingError("STREAM_DIGEST_CHAIN_INVALID")
        previous = claimed
    return entries


def _segment_events(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for kind, event in entries:
        grouped.setdefault(_artifact(kind, str(event["created_at"])), []).append(event)
    return grouped


def _last_event_time(
    existing: Sequence[tuple[str, Mapping[str, Any]]],
) -> dt.datetime | None:
    if not existing:
        return None
    return max(_time_key(str(event["created_at"])) for _, event in existing)


def _prepare_additions(
    existing: Sequence[tuple[str, Mapping[str, Any]]],
    additions: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    last_time = _last_event_time(existing)
    previous_digest = str(existing[-1][1]["event_digest"]) if existing else None
    seen_uids = {str(event["event_uid"]) for _, event in existing}
    next_sequence = len(existing) + 1
    staged: list[tuple[str, dict[str, Any]]] = []
    for kind, source in additions:
        if any(field in source for field in _CHAIN_FIELDS):
            raise V4StreamStagingError("CHAIN_FIELD_OWNED_BY_WRITER")
        event = copy.deepcopy(dict(source))
        event["sequence"] = next_sequence
        if previous_digest is not None:
            event["previous_event_digest"] = previous_digest
        _validate_event(kind, event)
        event_uid = str(event["event_uid"])
        if event_uid in seen_uids:
            raise V4StreamStagingError("DUPLICATE_EVENT_UID")
        event_time = _time_key(str(event["created_at"]))
        if last_time is not None and event_time < last_time:
            raise V4StreamStagingError("LATE_EVENT_REJECTED")
        event["event_digest"] = canonical_sha256(event)
        _validate_event(kind, event)
        staged.append((kind, event))
        seen_uids.add(event_uid)
        previous_digest = str(event["event_digest"])
        last_time = event_time
        next_sequence += 1
    return staged


def _expected_segment_digest(
    artifact: str,
    prior: Sequence[Mapping[str, Any]],
    supplied: str | None,
) -> str | None:
    if not prior:
        if supplied is not None:
            raise V4StreamStagingError("NEW_SEGMENT_EXPECTATION_INVALID", artifact)
        return None
    actual = _sha256(_body(prior))
    if not isinstance(supplied, str) or not _SHA256.fullmatch(supplied):
        raise V4StreamStagingError("EXPECTED_DIGEST_REQUIRED", artifact)
    if supplied != actual:
        raise V4StreamStagingError("STALE_STREAM_DIGEST", artifact)
    return supplied


def _staged_segment_write(
    artifact: str,
    prior: Sequence[Mapping[str, Any]],
    appended: Sequence[Mapping[str, Any]],
    supplied_digest: str | None,
) -> StagedStreamWrite:
    expected_digest = _expected_segment_digest(artifact, prior, supplied_digest)
    intended_events = [*prior, *appended]
    body = _body(intended_events)
    head = intended_events[-1]
    return StagedStreamWrite(
        artifact=artifact,
        kind=artifact.split("/")[1],
        segment=artifact.rsplit("/", 1)[1][:-7],
        expected_digest=expected_digest,
        intended_digest=_sha256(body),
        body=body,
        first_sequence=int(intended_events[0]["sequence"]),
        last_sequence=int(head["sequence"]),
        head_event_uid=str(head["event_uid"]),
        head_digest=str(head["event_digest"]),
        appended_events=tuple(copy.deepcopy(appended)),
    )


def stage_stream_appends(
    current_streams: Mapping[str, Sequence[Mapping[str, Any]]],
    additions: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    current_artifact_digests: Mapping[str, str],
) -> tuple[StagedStreamWrite, ...]:
    """Assign global sequence/digests and stage full active-segment replacements.

    ``additions`` are ordered command facts without chain-owned fields.  The
    caller supplies digests captured by its consistent baseline read; a later
    write session repeats those comparisons against physical targets.
    """

    if not additions:
        return ()
    existing = _ordered_existing(current_streams)
    existing_by_artifact = _segment_events(existing)
    staged = _prepare_additions(existing, additions)
    additions_by_artifact = _segment_events(staged)
    return tuple(
        _staged_segment_write(
            artifact,
            existing_by_artifact.get(artifact, ()),
            additions_by_artifact[artifact],
            current_artifact_digests.get(artifact),
        )
        for artifact in sorted(additions_by_artifact)
    )
