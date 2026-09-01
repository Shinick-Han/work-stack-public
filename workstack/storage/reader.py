"""Bounded, read-only loading of normalized Work Stack SSOT v4 packages."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .canonical import CanonicalJsonError, canonical_json_bytes
from .contracts import StorageContractError, require_valid_by_format
from .layout import RECORD_KINDS, STREAM_KINDS, StreamLocation, V4Layout


_RECORD_FORMATS = {
    "captures": "workstack.capture",
    "notes": "workstack.note",
    "objectives": "workstack.objective",
    "replies": "workstack.reply",
    "tasks": "workstack.task",
}


class StorageReadError(ValueError):
    """A content-free rejection raised before untrusted content is exposed."""

    def __init__(self, code: str, artifact: str = "") -> None:
        super().__init__(code if not artifact else f"{code}: {artifact}")
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class V4ReadLimits:
    max_record_bytes: int = 1_048_576
    max_stream_line_bytes: int = 1_048_576
    max_stream_segment_bytes: int = 67_108_864
    max_total_bytes: int = 268_435_456
    max_records: int = 100_000
    max_stream_events: int = 1_000_000

    def __post_init__(self) -> None:
        if any(value < 1 for value in vars(self).values()):
            raise ValueError("read limits must be positive")


@dataclass(frozen=True)
class ReadArtifact:
    artifact: str
    category: str
    kind: str
    byte_count: int
    sha256: str
    item_count: int = 1
    first_sequence: int | None = None
    last_sequence: int | None = None


@dataclass(frozen=True)
class V4ReadResult:
    root: Path
    store: Mapping[str, Any]
    workspace: Mapping[str, Any]
    records: Mapping[str, tuple[Mapping[str, Any], ...]]
    streams: Mapping[str, tuple[Mapping[str, Any], ...]]
    artifacts: tuple[ReadArtifact, ...]

    @property
    def workspace_uid(self) -> str:
        return str(self.store["workspace_uid"])

    @property
    def record_count(self) -> int:
        return sum(len(values) for values in self.records.values())

    @property
    def event_count(self) -> int:
        return sum(len(values) for values in self.streams.values())


class _DuplicateKeyError(ValueError):
    pass


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def consume(self, count: int) -> None:
        self.used += count
        if self.used > self.limit:
            raise StorageReadError("TOTAL_BYTE_LIMIT_EXCEEDED")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _signature(details: os.stat_result) -> tuple[int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _read_bytes(path: Path, artifact: str, maximum: int, budget: _Budget) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if before.st_size > maximum:
            raise StorageReadError("ARTIFACT_BYTE_LIMIT_EXCEEDED", artifact)
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            body = source.read(maximum + 1)
        after = path.stat(follow_symlinks=False)
    except StorageReadError:
        raise
    except OSError as error:
        raise StorageReadError("ARTIFACT_UNREADABLE", artifact) from error
    if len(body) > maximum:
        raise StorageReadError("ARTIFACT_BYTE_LIMIT_EXCEEDED", artifact)
    before_signature = _signature(before)
    opened_signature = _signature(opened)
    after_signature = _signature(after)
    if before_signature != opened_signature or opened_signature != after_signature:
        raise StorageReadError("ARTIFACT_CHANGED_DURING_READ", artifact)
    budget.consume(len(body))
    return body


def _decode_object(body: bytes, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError, RecursionError) as error:
        raise StorageReadError("INVALID_JSON", artifact) from error
    if not isinstance(value, dict):
        raise StorageReadError("JSON_OBJECT_REQUIRED", artifact)
    return value


def _validate(
    value: dict[str, Any], artifact: str, expected_format: str, body: bytes
) -> None:
    if value.get("format") != expected_format:
        raise StorageReadError("FORMAT_MISMATCH", artifact)
    try:
        require_valid_by_format(value)
        canonical = canonical_json_bytes(value)
    except StorageContractError as error:
        raise StorageReadError("CONTRACT_VIOLATION", artifact) from error
    except CanonicalJsonError as error:
        raise StorageReadError("CANONICAL_JSON_VIOLATION", artifact) from error
    if canonical != body:
        raise StorageReadError("CANONICAL_JSON_BYTES_MISMATCH", artifact)


def _artifact(
    name: str,
    category: str,
    kind: str,
    body: bytes,
    *,
    item_count: int = 1,
    first_sequence: int | None = None,
    last_sequence: int | None = None,
) -> ReadArtifact:
    return ReadArtifact(
        artifact=name,
        category=category,
        kind=kind,
        byte_count=len(body),
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        item_count=item_count,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
    )


def _read_document(
    path: Path,
    artifact: str,
    expected_format: str,
    limits: V4ReadLimits,
    budget: _Budget,
) -> tuple[dict[str, Any], bytes]:
    body = _read_bytes(path, artifact, limits.max_record_bytes, budget)
    value = _decode_object(body, artifact)
    _validate(value, artifact, expected_format, body)
    return value, body


def _read_records(
    layout: V4Layout,
    limits: V4ReadLimits,
    budget: _Budget,
    workspace_uid: str,
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], list[ReadArtifact]]:
    locations = layout.record_files()
    if len(locations) > limits.max_records:
        raise StorageReadError("RECORD_COUNT_LIMIT_EXCEEDED")
    grouped: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in RECORD_KINDS}
    artifacts: list[ReadArtifact] = []
    seen: set[str] = set()
    for location in locations:
        expected = _RECORD_FORMATS[location.kind]
        value, body = _read_document(
            location.path, location.artifact, expected, limits, budget
        )
        uid = value.get("uid")
        if uid != location.uid:
            raise StorageReadError("RECORD_UID_PATH_MISMATCH", location.artifact)
        if value.get("workspace_uid") != workspace_uid:
            raise StorageReadError("WORKSPACE_UID_MISMATCH", location.artifact)
        if uid in seen:
            raise StorageReadError("DUPLICATE_RECORD_UID", location.artifact)
        seen.add(str(uid))
        grouped[location.kind].append(MappingProxyType(value))
        artifacts.append(_artifact(location.artifact, "record", location.kind, body))
    return {kind: tuple(grouped[kind]) for kind in RECORD_KINDS}, artifacts


def _stream_lines(
    location: StreamLocation,
    body: bytes,
    limits: V4ReadLimits,
) -> list[tuple[int, bytes]]:
    if body and not body.endswith(b"\n"):
        raise StorageReadError("TRUNCATED_FINAL_LINE", location.artifact)
    lines = body.splitlines()
    result: list[tuple[int, bytes]] = []
    for number, line in enumerate(lines, start=1):
        if not line:
            raise StorageReadError("MALFORMED_NDJSON_LINE", f"{location.artifact}:{number}")
        if len(line) > limits.max_stream_line_bytes:
            raise StorageReadError("STREAM_LINE_BYTE_LIMIT_EXCEEDED", location.artifact)
        result.append((number, line))
    return result


def _read_segment(
    location: StreamLocation,
    limits: V4ReadLimits,
    budget: _Budget,
    workspace_uid: str,
) -> tuple[list[Mapping[str, Any]], ReadArtifact]:
    body = _read_bytes(
        location.path,
        location.artifact,
        limits.max_stream_segment_bytes,
        budget,
    )
    events: list[Mapping[str, Any]] = []
    sequences: list[int] = []
    expected = "workstack." + location.kind + "-event"
    for number, line in _stream_lines(location, body, limits):
        line_artifact = f"{location.artifact}:{number}"
        value = _decode_object(line, line_artifact)
        _validate(value, line_artifact, expected, line)
        if value.get("workspace_uid") != workspace_uid:
            raise StorageReadError("WORKSPACE_UID_MISMATCH", line_artifact)
        created_at = value.get("created_at")
        if not isinstance(created_at, str) or not created_at.startswith(location.segment):
            raise StorageReadError("STREAM_SEGMENT_MONTH_MISMATCH", line_artifact)
        sequence = value.get("sequence")
        if type(sequence) is not int:
            raise StorageReadError("STREAM_SEQUENCE_REQUIRED", line_artifact)
        sequences.append(sequence)
        events.append(MappingProxyType(value))
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise StorageReadError("STREAM_SEGMENT_ORDER_INVALID", location.artifact)
    return events, _artifact(
        location.artifact,
        "stream",
        location.kind,
        body,
        item_count=len(events),
        first_sequence=sequences[0] if sequences else None,
        last_sequence=sequences[-1] if sequences else None,
    )


def _read_streams(
    layout: V4Layout,
    limits: V4ReadLimits,
    budget: _Budget,
    workspace_uid: str,
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], list[ReadArtifact]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in STREAM_KINDS}
    artifacts: list[ReadArtifact] = []
    event_uids: set[str] = set()
    sequences: set[int] = set()
    event_count = 0
    for location in layout.stream_files():
        events, artifact = _read_segment(location, limits, budget, workspace_uid)
        event_count += len(events)
        if event_count > limits.max_stream_events:
            raise StorageReadError("STREAM_EVENT_COUNT_LIMIT_EXCEEDED")
        for event in events:
            event_uid = str(event["event_uid"])
            sequence = int(event["sequence"])
            if event_uid in event_uids:
                raise StorageReadError("DUPLICATE_EVENT_UID", location.artifact)
            if sequence in sequences:
                raise StorageReadError("DUPLICATE_STREAM_SEQUENCE", location.artifact)
            event_uids.add(event_uid)
            sequences.add(sequence)
        grouped[location.kind].extend(events)
        artifacts.append(artifact)
    return {kind: tuple(grouped[kind]) for kind in STREAM_KINDS}, artifacts


def read_v4(
    root: Path | str,
    *,
    limits: V4ReadLimits | None = None,
) -> V4ReadResult:
    """Read and contract-check a v4 package without writing to its authority."""

    active_limits = limits or V4ReadLimits()
    budget = _Budget(active_limits.max_total_bytes)
    layout = V4Layout.open(root)
    store, store_body = _read_document(
        layout.store_path, "store.json", "workstack.ssot", active_limits, budget
    )
    workspace, workspace_body = _read_document(
        layout.workspace_path,
        "workspace.json",
        "workstack.workspace",
        active_limits,
        budget,
    )
    workspace_uid = str(store["workspace_uid"])
    if workspace.get("workspace_uid") != workspace_uid or workspace.get("uid") != workspace_uid:
        raise StorageReadError("WORKSPACE_UID_MISMATCH", "workspace.json")
    records, record_artifacts = _read_records(
        layout, active_limits, budget, workspace_uid
    )
    streams, stream_artifacts = _read_streams(
        layout, active_limits, budget, workspace_uid
    )
    metadata = [
        _artifact("store.json", "metadata", "store", store_body),
        _artifact("workspace.json", "metadata", "workspace", workspace_body),
    ]
    return V4ReadResult(
        root=layout.root,
        store=MappingProxyType(store),
        workspace=MappingProxyType(workspace),
        records=MappingProxyType(records),
        streams=MappingProxyType(streams),
        artifacts=tuple(metadata + record_artifacts + stream_artifacts),
    )
