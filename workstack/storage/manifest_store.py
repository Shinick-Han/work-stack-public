"""CAS persistence for the disposable v4 runtime authority manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .canonical import CANONICAL_JSON_FORMAT, canonical_json_bytes
from .layout import RECORD_KINDS, STREAM_KINDS
from .manifest import MANIFEST_FORMAT, MANIFEST_VERSION, V4Manifest


MAX_RUNTIME_MANIFEST_BYTES = 32 * 1024 * 1024
FaultHook = Callable[[str], None]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEGMENT = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")


class RuntimeManifestError(ValueError):
    """Content-free refusal to trust or publish a runtime manifest."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RuntimeManifestState:
    manifest: V4Manifest
    generation: int


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _signature(path: Path) -> tuple[int, int, int]:
    details = path.stat(follow_symlinks=False)
    return details.st_size, details.st_mtime_ns, getattr(details, "st_ino", 0)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_metadata(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"store_digest", "workspace_digest"}:
        raise RuntimeManifestError("MANIFEST_METADATA_INVALID")
    if not all(_valid_digest(item) for item in value.values()):
        raise RuntimeManifestError("MANIFEST_METADATA_INVALID")


def _validate_record_roster(value: object, expected_count: object) -> None:
    fields = {"artifact", "digest", "kind", "revision", "uid", "value_digest"}
    if not isinstance(value, list) or expected_count != len(value):
        raise RuntimeManifestError("MANIFEST_RECORD_ROSTER_INVALID")
    seen: set[tuple[str, str]] = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != fields:
            raise RuntimeManifestError("MANIFEST_RECORD_ROSTER_INVALID")
        kind, uid = record.get("kind"), record.get("uid")
        expected = f"records/{kind}/{str(uid)[:2]}/{uid}.json"
        valid = (
            kind in RECORD_KINDS
            and isinstance(uid, str)
            and _UUID.fullmatch(uid) is not None
            and record.get("artifact") == expected
            and type(record.get("revision")) is int
            and record["revision"] >= 0
            and _valid_digest(record.get("digest"))
            and _valid_digest(record.get("value_digest"))
        )
        if not valid or (kind, uid) in seen:
            raise RuntimeManifestError("MANIFEST_RECORD_ROSTER_INVALID")
        seen.add((kind, uid))


def _task_record_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record["uid"]): record
        for record in records
        if record["kind"] == "tasks"
    }


def _valid_task_baseline(
    baseline: Mapping[str, Any], task: Mapping[str, Any] | None
) -> bool:
    return bool(
        task is not None
        and baseline.get("record_revision") == task.get("revision")
        and baseline.get("record_value_digest") == task.get("value_digest")
        and isinstance(baseline.get("task_display_id"), str)
        and baseline.get("status") in {"open", "started", "done", "dropped"}
        and type(baseline.get("planning_revision")) is int
        and 0 <= baseline["planning_revision"] <= baseline["record_revision"]
        and isinstance(baseline.get("planning_head_event_uid"), str)
        and _UUID.fullmatch(baseline["planning_head_event_uid"]) is not None
        and _valid_digest(baseline.get("planning_head_digest"))
    )


def _validate_task_baselines(
    value: object, records: list[dict[str, Any]]
) -> None:
    fields = {
        "planning_head_digest", "planning_head_event_uid", "planning_revision",
        "record_revision", "record_value_digest", "status", "task_display_id",
        "task_uid",
    }
    tasks = _task_record_index(records)
    if not isinstance(value, list) or len(value) != len(tasks):
        raise RuntimeManifestError("MANIFEST_TASK_BASELINES_INVALID")
    seen: set[str] = set()
    for baseline in value:
        if not isinstance(baseline, dict) or set(baseline) != fields:
            raise RuntimeManifestError("MANIFEST_TASK_BASELINES_INVALID")
        uid = baseline.get("task_uid")
        if not isinstance(uid, str) or uid in seen or not _valid_task_baseline(
            baseline, tasks.get(uid)
        ):
            raise RuntimeManifestError("MANIFEST_TASK_BASELINES_INVALID")
        seen.add(uid)


def _valid_stream_identity(stream: Mapping[str, Any]) -> bool:
    kind, segment = stream.get("kind"), stream.get("segment")
    count = stream.get("event_count")
    expected = f"streams/{kind}/{segment}.ndjson"
    return (
        kind in STREAM_KINDS
        and isinstance(segment, str)
        and _SEGMENT.fullmatch(segment) is not None
        and stream.get("artifact") == expected
        and type(count) is int
        and count >= 0
        and _valid_digest(stream.get("digest"))
        and _valid_digest(stream.get("value_digest"))
    )


def _valid_stream_head(stream: Mapping[str, Any]) -> bool:
    count = stream["event_count"]
    first, last = stream.get("first_sequence"), stream.get("last_sequence")
    head_uid, head_digest = stream.get("head_event_uid"), stream.get("head_digest")
    if count == 0:
        return first is None and last is None and head_uid is None and head_digest is None
    return (
        type(first) is int
        and type(last) is int
        and 1 <= first <= last
        and isinstance(head_uid, str)
        and _UUID.fullmatch(head_uid) is not None
        and _valid_digest(head_digest)
    )


def _valid_stream_entry(stream: dict[str, Any]) -> bool:
    if not _valid_stream_identity(stream):
        return False
    return _valid_stream_head(stream)


def _validate_stream_roster(value: object, expected_count: object) -> None:
    fields = {
        "artifact", "digest", "event_count", "first_sequence", "head_digest",
        "head_event_uid", "kind", "last_sequence", "segment", "value_digest",
    }
    if not isinstance(value, list) or type(expected_count) is not int or expected_count < 0:
        raise RuntimeManifestError("MANIFEST_STREAM_ROSTER_INVALID")
    if any(not isinstance(stream, dict) or set(stream) != fields for stream in value):
        raise RuntimeManifestError("MANIFEST_STREAM_ROSTER_INVALID")
    if any(not _valid_stream_entry(stream) for stream in value):
        raise RuntimeManifestError("MANIFEST_STREAM_ROSTER_INVALID")
    artifacts = [str(stream["artifact"]) for stream in value]
    if len(set(artifacts)) != len(artifacts) or expected_count != sum(
        int(stream["event_count"]) for stream in value
    ):
        raise RuntimeManifestError("MANIFEST_STREAM_ROSTER_INVALID")


def _validate_manifest_header(value: dict[str, Any]) -> int:
    if (
        value.get("canonical_json") != CANONICAL_JSON_FORMAT
        or value.get("schema_set") != "workstack.ssot.v4"
        or value.get("store_format") != "workstack.ssot"
        or value.get("store_schema_version") != 4
        or not isinstance(value.get("workspace_uid"), str)
        or _UUID.fullmatch(value["workspace_uid"]) is None
    ):
        raise RuntimeManifestError("MANIFEST_HEADER_INVALID")
    generation = value.get("generation")
    if type(generation) is not int or generation < 0:
        raise RuntimeManifestError("MANIFEST_GENERATION_INVALID")
    return generation


def _decode_manifest(body: bytes) -> RuntimeManifestState:
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
        raise RuntimeManifestError("MANIFEST_JSON_INVALID") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != body:
        raise RuntimeManifestError("MANIFEST_CANONICAL_BYTES_REQUIRED")
    expected_fields = {
        "canonical_json", "format", "generation", "metadata", "record_count",
        "records", "schema_set", "semantic_task_baselines", "store_format", "store_schema_version",
        "stream_event_count", "streams", "version", "workspace_uid",
    }
    if set(value) != expected_fields:
        raise RuntimeManifestError("MANIFEST_FIELDS_INVALID")
    if value.get("format") != MANIFEST_FORMAT or value.get("version") != MANIFEST_VERSION:
        raise RuntimeManifestError("MANIFEST_VERSION_UNSUPPORTED")
    generation = _validate_manifest_header(value)
    _validate_metadata(value["metadata"])
    _validate_record_roster(value["records"], value["record_count"])
    _validate_stream_roster(value["streams"], value["stream_event_count"])
    _validate_task_baselines(value["semantic_task_baselines"], value["records"])
    manifest = V4Manifest(body, _digest(body))
    return RuntimeManifestState(manifest, generation)


def read_runtime_manifest(path: Path | str) -> RuntimeManifestState | None:
    target = Path(path)
    try:
        before = _signature(target)
        if before[0] > MAX_RUNTIME_MANIFEST_BYTES:
            raise RuntimeManifestError("MANIFEST_BYTE_LIMIT_EXCEEDED")
        with target.open("rb") as source:
            body = source.read(MAX_RUNTIME_MANIFEST_BYTES + 1)
        after = _signature(target)
    except FileNotFoundError:
        return None
    except RuntimeManifestError:
        raise
    except OSError as error:
        raise RuntimeManifestError("MANIFEST_READ_FAILED") from error
    if len(body) > MAX_RUNTIME_MANIFEST_BYTES:
        raise RuntimeManifestError("MANIFEST_BYTE_LIMIT_EXCEEDED")
    if before != after:
        raise RuntimeManifestError("MANIFEST_CHANGED_DURING_READ")
    return _decode_manifest(body)


def _current_digest(path: Path) -> str | None:
    state = read_runtime_manifest(path)
    return None if state is None else state.manifest.digest


def publish_runtime_manifest(
    path: Path | str,
    manifest: V4Manifest,
    *,
    expected_digest: str | None,
    fault_hook: FaultHook | None = None,
) -> RuntimeManifestState:
    """Atomically publish after an exact CAS check under the writer lease.

    The caller owns the authority writer lease.  The repeated local CAS check
    detects accidental runtime edits without pretending to replace that lease.
    """

    target = Path(path)
    state = _decode_manifest(manifest.canonical_bytes)
    if state.manifest.digest != manifest.digest:
        raise RuntimeManifestError("MANIFEST_DIGEST_MISMATCH")
    if _current_digest(target) != expected_digest:
        raise RuntimeManifestError("MANIFEST_CAS_MISMATCH")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    hook = fault_hook or (lambda _transition: None)
    try:
        hook("before_manifest_stage")
        with os.fdopen(descriptor, "wb") as output:
            output.write(manifest.canonical_bytes)
            output.flush()
            os.fsync(output.fileno())
        hook("after_manifest_stage")
        if _current_digest(target) != expected_digest:
            raise RuntimeManifestError("MANIFEST_CAS_MISMATCH")
        hook("before_manifest_replace")
        os.replace(str(temporary), str(target))
        hook("after_manifest_replace")
    except RuntimeManifestError:
        raise
    except OSError as error:
        raise RuntimeManifestError("MANIFEST_PUBLISH_FAILED") from error
    finally:
        temporary.unlink(missing_ok=True)
    verified = read_runtime_manifest(target)
    if verified is None or verified.manifest.digest != manifest.digest:
        raise RuntimeManifestError("MANIFEST_PUBLISH_VERIFICATION_FAILED")
    return verified
