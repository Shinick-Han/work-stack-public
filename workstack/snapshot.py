"""Work Stack planning-task snapshot v1 model, validator, and serializer."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Any

from .snapshot_safety import evaluate_safety
from .unicode17 import normalize_nfc


SNAPSHOT_FORMAT = "workstack.planning-task-snapshot.v1"
MAX_REVISION = 9_007_199_254_740_991
MAX_SNAPSHOT_BYTES = 65_536
SNAPSHOT_FIELDS = {
    "detail",
    "due_date",
    "format",
    "legacy_task_id",
    "origin_ref",
    "planning_priority",
    "planning_status",
    "planning_task_uid",
    "revision",
    "title",
    "workspace_uid",
}
FORBIDDEN_AUTHORITY_FIELDS = {
    "task_type",
    "taskType",
    "data_class",
    "dataClass",
    "risk",
    "provider",
    "harness",
    "model",
    "workspace_id",
    "conduit_task_id",
    "room",
    "seat",
    "run",
}
_LEGACY_ID = re.compile(r"^T-[0-9]{4,}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_TOKEN = re.compile(rb'"revision":([^,}\s]+)')
_SHORTEST_INTEGER = re.compile(rb"(?:0|[1-9][0-9]*)$")


class SnapshotValidationError(ValueError):
    """Privacy-safe refusal carrying only frozen classification metadata."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        field: str | None = None,
        public_code: str | None = None,
        measured: dict[str, int] | None = None,
    ) -> None:
        super().__init__("snapshot refused: {} / {}".format(stage, reason))
        self.stage = stage
        self.reason = reason
        self.field = field
        self.public_code = public_code
        self.measured = measured or {}

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stage": self.stage,
            "reason": self.reason,
        }
        if self.field is not None:
            value["field"] = self.field
        if self.public_code is not None:
            value["code"] = self.public_code
        if self.measured:
            value["measured"] = dict(self.measured)
        return value


def snapshot_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _refuse(
    stage: str,
    reason: str,
    *,
    field: str | None = None,
    public_code: str | None = None,
    measured: dict[str, int] | None = None,
) -> None:
    raise SnapshotValidationError(
        stage,
        reason,
        field=field,
        public_code=public_code,
        measured=measured,
    )


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field=field)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _refuse("FIELD", "UUID_INVALID", field=field)
    if (
        parsed.int == 0
        or str(parsed) != value
        or parsed.variant != uuid.RFC_4122
    ):
        _refuse("FIELD", "UUID_INVALID", field=field)
    return value


def _text_metrics(value: str) -> tuple[int, int]:
    scalars = len(value)
    units = len(value.encode("utf-16-le", errors="surrogatepass")) // 2
    return scalars, units


def _validate_text_characters(value: str, field: str) -> None:
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            _refuse(
                "FIELD",
                "UNPAIRED_SURROGATE",
                field=field,
                public_code="SNAPSHOT_FIELD_INVALID",
            )
        if codepoint == 0x7F:
            _refuse(
                "FIELD", "DEL_FORBIDDEN", field=field,
                public_code="SNAPSHOT_FIELD_INVALID",
            )
        if 0x80 <= codepoint <= 0x9F:
            _refuse(
                "FIELD", "C1_FORBIDDEN", field=field,
                public_code="SNAPSHOT_FIELD_INVALID",
            )
        if codepoint <= 0x1F and not (
            field == "detail" and character in {"\n", "\t"}
        ):
            _refuse(
                "FIELD", "C0_FORBIDDEN", field=field,
                public_code="SNAPSHOT_FIELD_INVALID",
            )


def _validate_text_normalization(value: str, field: str) -> None:
    if normalize_nfc(value) != value:
        _refuse(
            "FIELD", "NOT_NFC", field=field,
            public_code="SNAPSHOT_FIELD_INVALID",
        )


def _text_limit_reason(scalars: int, units: int, minimum: int, maximum: int) -> str | None:
    scalar_bad = not minimum <= scalars <= maximum
    units_bad = not minimum <= units <= maximum
    if not scalar_bad and not units_bad:
        return None
    if scalars < minimum or (scalar_bad and not units_bad):
        return "SCALAR_LIMIT"
    if scalar_bad and units_bad:
        return "SCALAR_AND_UTF16_LIMIT"
    return "UTF16_LIMIT"


def _validate_text_length(value: str, field: str) -> None:
    scalars, units = _text_metrics(value)
    minimum = 1 if field == "title" else 0
    maximum = 256 if field == "title" else 4096
    reason = _text_limit_reason(scalars, units, minimum, maximum)
    if reason is not None:
        _refuse(
            "FIELD",
            reason,
            field=field,
            public_code="SNAPSHOT_FIELD_INVALID",
            measured={"scalars": scalars, "utf16_code_units": units},
        )


def validate_text(value: Any, field: str) -> str:
    if field not in {"title", "detail"}:
        raise ValueError("unknown snapshot text field")
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field=field)
    _validate_text_characters(value, field)
    _validate_text_normalization(value, field)
    _validate_text_length(value, field)
    return value


def _validate_snapshot_keys(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse("JSON_OBJECT", "TOP_LEVEL_NOT_OBJECT")
    keys = set(value)
    missing = SNAPSHOT_FIELDS - keys
    unknown = keys - SNAPSHOT_FIELDS
    if unknown:
        if unknown & FORBIDDEN_AUTHORITY_FIELDS:
            _refuse("KEY_SET", "FORBIDDEN_AUTHORITY_FIELD")
        _refuse("KEY_SET", "UNKNOWN_FIELD")
    if missing:
        _refuse("KEY_SET", "MISSING_FIELD")
    return value


def _validate_snapshot_format(value: Any) -> None:
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field="format")
    if value != SNAPSHOT_FORMAT:
        _refuse("FIELD", "FORMAT_INVALID", field="format")


def _validate_legacy_task_id(value: Any) -> str:
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field="legacy_task_id")
    if _LEGACY_ID.fullmatch(value) is None:
        _refuse("FIELD", "LEGACY_TASK_ID_INVALID", field="legacy_task_id")
    return value


def _validate_revision(value: Any) -> int:
    if type(value) is not int:
        _refuse("FIELD", "WRONG_TYPE", field="revision")
    if not 0 <= value <= MAX_REVISION:
        _refuse("FIELD", "REVISION_RANGE", field="revision")
    return value


def _validate_enum(value: Any, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field=field)
    if value not in allowed:
        _refuse("FIELD", "ENUM_INVALID", field=field)
    return value


def _validate_due_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field="due_date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        _refuse("FIELD", "DATE_INVALID", field="due_date")
    if parsed.isoformat() != value:
        _refuse("FIELD", "DATE_INVALID", field="due_date")
    return value


def _validate_origin(value: Any, workspace_uid: str, task_uid: str) -> str:
    expected = "workstack://{}/planning-tasks/{}".format(workspace_uid, task_uid)
    if not isinstance(value, str):
        _refuse("FIELD", "WRONG_TYPE", field="origin_ref")
    if value != expected:
        _refuse("ORIGIN", "ORIGIN_DERIVATION_MISMATCH", field="origin_ref")
    return value


def _validate_snapshot_safety(title: str, detail: str) -> None:
    for field, text in (("title", title), ("detail", detail)):
        decision = evaluate_safety(text, field)
        if decision["decision"] == "REFUSE":
            _refuse(
                "SAFETY",
                decision["rule"],
                field=field,
                public_code=decision["code"],
            )


def validate_snapshot_object(
    value: Any, *, safety: bool = True
) -> dict[str, Any]:
    value = _validate_snapshot_keys(value)
    _validate_snapshot_format(value["format"])
    workspace_uid = _uuid(value["workspace_uid"], "workspace_uid")
    task_uid = _uuid(value["planning_task_uid"], "planning_task_uid")
    _validate_legacy_task_id(value["legacy_task_id"])
    _validate_revision(value["revision"])
    title = validate_text(value["title"], "title")
    detail = validate_text(value["detail"], "detail")
    _validate_enum(value["planning_status"], "planning_status", {"open", "started", "done", "dropped"})
    _validate_enum(value["planning_priority"], "planning_priority", {"P0", "P1", "P2", "P3"})
    _validate_due_date(value["due_date"])
    _validate_origin(value["origin_ref"], workspace_uid, task_uid)
    if safety:
        _validate_snapshot_safety(title, detail)
    return dict(value)


def canonical_snapshot_bytes(value: dict[str, Any]) -> bytes:
    validated = validate_snapshot_object(value)
    try:
        encoded = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", errors="strict") + b"\n"
    except (UnicodeEncodeError, ValueError) as error:
        raise SnapshotValidationError("CANONICAL_BYTES", "SERIALIZATION_FAILED") from error
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        _refuse("BYTE_LENGTH", "SNAPSHOT_SIZE_LIMIT")
    return encoded


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = child
    return value


def _skip_json_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _json_string_end(text: str, index: int) -> int:
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return len(text)


def _read_top_level_key(body: str, index: int) -> tuple[str, int] | None:
    index = _skip_json_whitespace(body, index)
    if index >= len(body) or body[index] in "}":
        return None
    if body[index] != '"':
        return None
    end = _json_string_end(body, index)
    try:
        key = json.loads(body[index:end])
    except json.JSONDecodeError:
        return None
    return key, end


def _next_top_level_member(body: str, index: int) -> int | None:
    depth = 0
    while index < len(body):
        character = body[index]
        if character == '"':
            index = _json_string_end(body, index)
            continue
        if character in "[{":
            depth += 1
        elif character in "]}":
            if character == "}" and depth == 0:
                return None
            depth -= 1
        elif character == "," and depth == 0:
            return index + 1
        index += 1
    return None


def _has_duplicate_top_level_key(text: str) -> bool:
    body = text[:-1]
    index = _skip_json_whitespace(body, 0)
    if index >= len(body) or body[index] != "{":
        return False
    index += 1
    seen: set[str] = set()
    while index < len(body):
        member = _read_top_level_key(body, index)
        if member is None:
            return False
        key, index = member
        if key in seen:
            return True
        seen.add(key)
        index = _skip_json_whitespace(body, index)
        if index >= len(body) or body[index] != ":":
            return False
        index = _next_top_level_member(body, index + 1)
        if index is None:
            return False
    return False


def _validate_snapshot_digest(raw: bytes, supplied_digest: str | None) -> None:
    if supplied_digest is None:
        return
    if not isinstance(supplied_digest, str) or _DIGEST.fullmatch(supplied_digest) is None:
        _refuse("DIGEST", "DIGEST_SYNTAX")
    if snapshot_digest(raw) != supplied_digest:
        _refuse("DIGEST", "DIGEST_MISMATCH")


def _decode_snapshot_envelope(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        _refuse("BYTE_ENVELOPE", "BOM_FORBIDDEN")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _refuse("BYTE_ENVELOPE", "INVALID_UTF8")
    if raw.endswith(b"\r\n"):
        _refuse("BYTE_ENVELOPE", "NONCANONICAL_LINE_ENDING")
    if not raw.endswith(b"\n"):
        _refuse("BYTE_ENVELOPE", "FINAL_LF_MISSING")
    if raw.endswith(b"\n\n"):
        _refuse("BYTE_ENVELOPE", "TRAILING_BYTES")
    return text


def _parse_snapshot_json(raw: bytes, text: str) -> dict[str, Any]:
    if _has_duplicate_top_level_key(text):
        _refuse("JSON_OBJECT", "DUPLICATE_KEY")
    revision_match = _REVISION_TOKEN.search(raw)
    if revision_match is not None and _SHORTEST_INTEGER.fullmatch(revision_match.group(1)) is None:
        _refuse("JSON_OBJECT", "REVISION_NUMERIC_FORM")
    try:
        return json.loads(text[:-1], object_pairs_hook=_object_without_duplicates)
    except _DuplicateKeyError:
        _refuse("JSON_OBJECT", "DUPLICATE_KEY")
    except json.JSONDecodeError:
        _refuse("JSON_OBJECT", "JSON_INVALID")


def _noncanonical_reason(raw: bytes, canonical: bytes) -> str:
    if b"\\/" in raw:
        return "NONCANONICAL_ESCAPE"
    if re.search(rb"\\u[0-9A-Fa-f]{4}", raw):
        return "NONCANONICAL_UNICODE_ESCAPE"
    raw_keys = re.findall(rb'"([^"\\]+)":', raw)
    canonical_keys = re.findall(rb'"([^"\\]+)":', canonical)
    if sorted(raw_keys) == sorted(canonical_keys) and raw_keys != canonical_keys:
        return "NONCANONICAL_KEY_ORDER"
    return "NONCANONICAL_JSON"


def validate_snapshot_bytes(
    raw: bytes, supplied_digest: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, bytes):
        raise TypeError("snapshot bytes must be bytes")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        _refuse("BYTE_LENGTH", "SNAPSHOT_SIZE_LIMIT")
    _validate_snapshot_digest(raw, supplied_digest)
    text = _decode_snapshot_envelope(raw)
    value = _parse_snapshot_json(raw, text)
    validated = validate_snapshot_object(value)
    canonical = canonical_snapshot_bytes(validated)
    if canonical != raw:
        _refuse("CANONICAL_BYTES", _noncanonical_reason(raw, canonical))
    return validated


def build_snapshot(
    workspace_uid: str, task: dict[str, Any], planning_status: str
) -> dict[str, Any]:
    snapshot = {
        "detail": task.get("detail", ""),
        "due_date": task.get("due"),
        "format": SNAPSHOT_FORMAT,
        "legacy_task_id": task.get("id"),
        "origin_ref": "workstack://{}/planning-tasks/{}".format(
            workspace_uid, task.get("uid")
        ),
        "planning_priority": task.get("priority"),
        "planning_status": planning_status,
        "planning_task_uid": task.get("uid"),
        "revision": task.get("revision"),
        "title": task.get("title"),
        "workspace_uid": workspace_uid,
    }
    return validate_snapshot_object(snapshot)
