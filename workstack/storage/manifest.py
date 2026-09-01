"""Deterministic runtime authority manifests for read-only SSOT v4 inputs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .canonical import (
    CANONICAL_JSON_FORMAT,
    MAX_CANONICAL_INTEGER,
    canonical_json_bytes,
    canonical_sha256,
)
from .layout import RECORD_KINDS, STREAM_KINDS
from .reader import ReadArtifact, StorageReadError, V4ReadResult, read_v4


MANIFEST_FORMAT = "workstack.runtime-authority-manifest"
MANIFEST_VERSION = 2
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class V4ManifestError(ValueError):
    """A content-free refusal to construct an authority manifest."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4Manifest:
    """Immutable canonical bytes plus their exact digest."""

    canonical_bytes: bytes
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_bytes.decode("utf-8"))


def _reader_fingerprint(result: V4ReadResult) -> str:
    records = {
        kind: sorted((dict(value) for value in result.records.get(kind, ())), key=lambda item: item["uid"])
        for kind in RECORD_KINDS
    }
    streams = {
        kind: sorted(
            (dict(value) for value in result.streams.get(kind, ())),
            key=lambda item: (item["sequence"], item["event_uid"]),
        )
        for kind in STREAM_KINDS
    }
    artifacts = sorted((asdict(value) for value in result.artifacts), key=lambda item: item["artifact"])
    return canonical_sha256(
        {
            "store": dict(result.store),
            "workspace": dict(result.workspace),
            "records": records,
            "streams": streams,
            "artifacts": artifacts,
        }
    )


def _verified_result(result: V4ReadResult) -> V4ReadResult:
    try:
        current = read_v4(result.root)
        if _reader_fingerprint(result) != _reader_fingerprint(current):
            raise V4ManifestError("READER_RESULT_INCONSISTENT")
    except V4ManifestError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, StorageReadError) as error:
        raise V4ManifestError("READER_RESULT_INCONSISTENT") from error
    return current


def _artifact_catalog(result: V4ReadResult) -> dict[str, ReadArtifact]:
    catalog: dict[str, ReadArtifact] = {}
    for artifact in result.artifacts:
        if artifact.artifact in catalog or not _SHA256.fullmatch(artifact.sha256):
            raise V4ManifestError("ARTIFACT_ROSTER_INVALID")
        if artifact.byte_count < 0 or artifact.item_count < 0:
            raise V4ManifestError("ARTIFACT_ROSTER_INVALID")
        catalog[artifact.artifact] = artifact
    return catalog


def _consume_artifact(
    catalog: dict[str, ReadArtifact],
    artifact: str,
    category: str,
    kind: str,
) -> ReadArtifact:
    value = catalog.pop(artifact, None)
    if value is None or value.category != category or value.kind != kind:
        raise V4ManifestError("ARTIFACT_ROSTER_INVALID")
    return value


def _metadata(result: V4ReadResult, catalog: dict[str, ReadArtifact]) -> dict[str, str]:
    store_artifact = _consume_artifact(catalog, "store.json", "metadata", "store")
    workspace_artifact = _consume_artifact(catalog, "workspace.json", "metadata", "workspace")
    if store_artifact.item_count != 1 or workspace_artifact.item_count != 1:
        raise V4ManifestError("ARTIFACT_ROSTER_INVALID")
    return {
        "store_digest": store_artifact.sha256,
        "workspace_digest": workspace_artifact.sha256,
    }


def _record_roster(
    result: V4ReadResult, catalog: dict[str, ReadArtifact]
) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind in RECORD_KINDS:
        for value in sorted(result.records.get(kind, ()), key=lambda item: str(item.get("uid", ""))):
            uid = value.get("uid")
            revision = value.get("revision")
            if not isinstance(uid, str) or uid in seen or type(revision) is not int:
                raise V4ManifestError("RECORD_ROSTER_INVALID")
            artifact_name = f"records/{kind}/{uid[:2]}/{uid}.json"
            artifact = _consume_artifact(catalog, artifact_name, "record", kind)
            if artifact.item_count != 1:
                raise V4ManifestError("RECORD_ROSTER_INVALID")
            seen.add(uid)
            roster.append(
                {
                    "artifact": artifact_name,
                    "digest": artifact.sha256,
                    "kind": kind,
                    "revision": revision,
                    "uid": uid,
                    "value_digest": canonical_sha256(dict(value)),
                }
            )
    return roster


def _events_by_segment(
    result: V4ReadResult,
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    sequences: set[int] = set()
    event_uids: set[str] = set()
    for kind in STREAM_KINDS:
        for event in result.streams.get(kind, ()):
            sequence = event.get("sequence")
            event_uid = event.get("event_uid")
            created_at = event.get("created_at")
            if type(sequence) is not int or not isinstance(event_uid, str):
                raise V4ManifestError("STREAM_ROSTER_INVALID")
            if sequence in sequences or event_uid in event_uids:
                raise V4ManifestError("STREAM_ROSTER_INVALID")
            if not isinstance(created_at, str) or len(created_at) < 7:
                raise V4ManifestError("STREAM_ROSTER_INVALID")
            sequences.add(sequence)
            event_uids.add(event_uid)
            grouped.setdefault((kind, created_at[:7]), []).append(event)
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        raise V4ManifestError("STREAM_SEQUENCE_INVALID")
    return grouped


def _verified_event_digests(result: V4ReadResult) -> dict[str, str]:
    ordered = sorted(
        (event for kind in STREAM_KINDS for event in result.streams.get(kind, ())),
        key=lambda item: int(item["sequence"]),
    )
    chain_enabled = any("event_digest" in event or "previous_event_digest" in event for event in ordered)
    previous: str | None = None
    digests: dict[str, str] = {}
    for event in ordered:
        digest, claimed = _verified_event_digest(event, chain_enabled, previous)
        digests[str(event["event_uid"])] = digest
        previous = claimed if isinstance(claimed, str) else None
    return digests


def _verified_event_digest(
    event: Mapping[str, Any], chain_enabled: bool, previous: str | None
) -> tuple[str, Any]:
    candidate = dict(event)
    claimed = candidate.pop("event_digest", None)
    actual = canonical_sha256(candidate)
    if chain_enabled and (claimed != actual or event.get("previous_event_digest") != previous):
        raise V4ManifestError("STREAM_DIGEST_CHAIN_INVALID")
    digest = actual if chain_enabled else canonical_sha256(dict(event))
    return digest, claimed


def _segment_entry(
    artifact: ReadArtifact,
    events: list[Mapping[str, Any]],
    event_digests: Mapping[str, str],
) -> dict[str, Any]:
    ordered = sorted(events, key=lambda event: int(event["sequence"]))
    first = int(ordered[0]["sequence"]) if ordered else None
    last = int(ordered[-1]["sequence"]) if ordered else None
    if artifact.item_count != len(ordered):
        raise V4ManifestError("STREAM_SEGMENT_METADATA_INVALID")
    if artifact.first_sequence != first or artifact.last_sequence != last:
        raise V4ManifestError("STREAM_SEGMENT_METADATA_INVALID")
    head = ordered[-1] if ordered else None
    return {
        "artifact": artifact.artifact,
        "digest": artifact.sha256,
        "event_count": len(ordered),
        "first_sequence": first,
        "head_digest": event_digests[str(head["event_uid"])] if head else None,
        "head_event_uid": str(head["event_uid"]) if head else None,
        "kind": artifact.kind,
        "last_sequence": last,
        "segment": Path(artifact.artifact).stem,
        "value_digest": canonical_sha256([dict(event) for event in ordered]),
    }


def _stream_roster(
    result: V4ReadResult, catalog: dict[str, ReadArtifact]
) -> list[dict[str, Any]]:
    grouped = _events_by_segment(result)
    event_digests = _verified_event_digests(result)
    roster: list[dict[str, Any]] = []
    stream_artifacts = sorted(
        (value for value in tuple(catalog.values()) if value.category == "stream"),
        key=lambda value: value.artifact,
    )
    for artifact in stream_artifacts:
        if artifact.kind not in STREAM_KINDS:
            raise V4ManifestError("STREAM_ROSTER_INVALID")
        expected = f"streams/{artifact.kind}/{Path(artifact.artifact).name}"
        if artifact.artifact != expected:
            raise V4ManifestError("STREAM_ROSTER_INVALID")
        consumed = _consume_artifact(catalog, artifact.artifact, "stream", artifact.kind)
        roster.append(
            _segment_entry(
                consumed,
                grouped.pop((artifact.kind, Path(artifact.artifact).stem), []),
                event_digests,
            )
        )
    if grouped:
        raise V4ManifestError("STREAM_SEGMENT_MISSING")
    return roster


def _semantic_task_baselines(
    result: V4ReadResult,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_entries = {
        str(record["uid"]): record for record in records if record["kind"] == "tasks"
    }
    facts: dict[str, list[Mapping[str, Any]]] = {}
    for event in result.streams.get("planning-status", ()):
        facts.setdefault(str(event.get("task_uid")), []).append(event)
    event_digests = _verified_event_digests(result)
    baselines: list[dict[str, Any]] = []
    for uid in sorted(task_entries):
        ordered = sorted(facts.get(uid, ()), key=lambda event: int(event["sequence"]))
        if not ordered:
            raise V4ManifestError("TASK_SEMANTIC_BASELINE_MISSING")
        head = ordered[-1]
        task = task_entries[uid]
        if head.get("task_display_id") is None or head.get("status") is None:
            raise V4ManifestError("TASK_SEMANTIC_BASELINE_INVALID")
        baselines.append({
            "planning_head_digest": event_digests[str(head["event_uid"])],
            "planning_head_event_uid": str(head["event_uid"]),
            "planning_revision": int(head["new_revision"]),
            "record_revision": int(task["revision"]),
            "record_value_digest": str(task["value_digest"]),
            "status": str(head["status"]),
            "task_display_id": str(head["task_display_id"]),
            "task_uid": uid,
        })
    if set(facts) != set(task_entries):
        raise V4ManifestError("TASK_SEMANTIC_BASELINE_INVALID")
    return baselines


def _manifest_core(result: V4ReadResult) -> dict[str, Any]:
    store = result.store
    workspace_uid = store.get("workspace_uid")
    if (
        store.get("format") != "workstack.ssot"
        or store.get("schema_version") != 4
        or store.get("schema_set") != "workstack.ssot.v4"
        or result.workspace.get("uid") != workspace_uid
        or result.workspace.get("workspace_uid") != workspace_uid
    ):
        raise V4ManifestError("AUTHORITY_HEADER_INVALID")
    catalog = _artifact_catalog(result)
    metadata = _metadata(result, catalog)
    records = _record_roster(result, catalog)
    streams = _stream_roster(result, catalog)
    semantic_task_baselines = _semantic_task_baselines(result, records)
    if catalog:
        raise V4ManifestError("ARTIFACT_ROSTER_INVALID")
    return {
        "canonical_json": CANONICAL_JSON_FORMAT,
        "format": MANIFEST_FORMAT,
        "metadata": metadata,
        "record_count": len(records),
        "records": records,
        "semantic_task_baselines": semantic_task_baselines,
        "schema_set": "workstack.ssot.v4",
        "store_format": "workstack.ssot",
        "store_schema_version": 4,
        "stream_event_count": result.event_count,
        "streams": streams,
        "version": MANIFEST_VERSION,
        "workspace_uid": workspace_uid,
    }


def build_v4_manifest(
    result: V4ReadResult,
    *,
    generation: int | None = None,
) -> V4Manifest:
    """Build a v2 runtime or migration-candidate manifest from a stable read."""

    if generation is not None and (
        type(generation) is not int or not 0 <= generation <= MAX_CANONICAL_INTEGER
    ):
        raise V4ManifestError("GENERATION_INVALID")
    verified = _verified_result(result)
    value = _manifest_core(verified)
    if generation is None:
        value["candidate_digest"] = canonical_sha256(value)
    else:
        value["generation"] = generation
    body = canonical_json_bytes(value)
    return V4Manifest(body, canonical_sha256(value))


def construct_v4_manifest(
    root: Path | str,
    *,
    generation: int | None = None,
) -> V4Manifest:
    """Read one v4 authority and construct its manifest without writing it."""

    try:
        result = read_v4(root)
    except (OSError, ValueError, StorageReadError) as error:
        raise V4ManifestError("AUTHORITY_READ_FAILED") from error
    return build_v4_manifest(result, generation=generation)
