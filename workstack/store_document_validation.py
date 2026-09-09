"""Semantic validation of decoded store documents, per schema version.

``workstack.store`` used to be the only place that knew what a workspace, a
backlog, an activity log or a metadata record has to look like, and it knew it
only for the version the running build writes. Two callers now need the same
knowledge for an *older* version: the migration that must admit a v1, v2 or v3
input before upgrading it, and the archive verifier that must judge a backup
without unpacking it into a directory and initializing it.

`validate_document_values` is that seam. It takes mappings the caller already
decoded, judges them as exactly one schema version, and returns the readiness
that version implies. It opens no file, initializes nothing, mutates nothing
and never consults the running build's `STORE_SCHEMA_VERSION`; the caller owns
byte acquisition and says which version it believes it has.

The validators are the released ones, moved here rather than copied, so there
is still one source for each rule. The default payload table is here too, and
``workstack.store`` composes its `DEFAULTS` from it, so a shape cannot drift
between the two.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from . import (
    capture_observations,
    knowledge_capture_packets,
    knowledge_ledger_document,
    report_documents,
    store_rosters,
)
from .planning_status import PlanningStatusValidationError, validate_and_project
from .store_errors import StoreCorruptError
from .task_display_id import (
    FIELD as TASK_DISPLAY_ID_HIGH_WATER,
    TaskDisplayIdError,
    admitted_high_water,
    read_optional_high_water,
    task_ids_from_records,
)


MAX_REVISION = 9_007_199_254_740_991
CAPTURES_DOCUMENT_NAME: Final[str] = "captures.json"
IDENTITY_STORES: Final[tuple[str, ...]] = (
    "workspace.json", "backlog.json", "store-meta.json", "activity.json"
)
SUPPORTED_SCHEMA_VERSIONS: Final[tuple[int, ...]] = (1, 2, 3, 5, 6)

_WORKSPACE_REQUIRED_KEYS = frozenset({"version", "id", "name"})
_WORKSPACE_OPTIONAL_KEYS = frozenset({TASK_DISPLAY_ID_HIGH_WATER})
# The exact field set the version 2 captures container carries. Written out
# rather than derived from the version 1 default, because a container this
# build may admit is a roster fact and not a delta of another shape.
_CAPTURES_V2_FIELDS = frozenset({"version", "captures", "observations"})

# The auxiliary payload shapes, owned once. Every schema version from 1 to 5
# carries the same five, and ``workstack.store.DEFAULTS`` is built from this
# table rather than repeating it.
AUXILIARY_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "okr.json": {"version": 1, "objectives": []},
    "worklog.json": {"version": 1, "days": {}},
    "notes.json": {"version": 1, "notes": []},
    "captures.json": {"version": 1, "captures": []},
    "replies.json": {"version": 1, "replies": []},
}
BACKLOG_DEFAULT: Final[dict[str, Any]] = {"version": 3, "tasks": []}
ACTIVITY_DEFAULT: Final[dict[str, Any]] = {
    "version": 2, "activity": [], "idempotency": [], "planning_status": [],
}
REPORTS_DEFAULT: Final[dict[str, Any]] = {
    "version": 1, "reports": [], "idempotency": [],
}
# Owned by ``workstack.knowledge_ledger_document`` so the ledger's shape rule
# and its default payload cannot drift apart; named here because this is where
# every other roster default is read from.
KNOWLEDGE_DEFAULT: Final[dict[str, Any]] = knowledge_ledger_document.KNOWLEDGE_DEFAULT

# What each schema version's documents look like: the roster, the workspace and
# backlog versions, whether the metadata document exists, and which evidence
# records that metadata carries.
_VERSION_ROSTERS: Final[dict[int, frozenset[str]]] = {
    1: store_rosters.V1_DOCUMENT_NAMES,
    2: store_rosters.V2_DOCUMENT_NAMES,
    3: store_rosters.V3_DOCUMENT_NAMES,
    5: store_rosters.V5_DOCUMENT_NAMES,
    6: store_rosters.V6_DOCUMENT_NAMES,
}
_WORKSPACE_VERSIONS: Final[dict[int, int]] = {1: 1, 2: 2, 3: 2, 5: 2, 6: 2}
_BACKLOG_VERSIONS: Final[dict[int, int]] = {1: 1, 2: 2, 3: 3, 5: 3, 6: 3}
_EVIDENCE_NAMES: Final[dict[int, frozenset[str]]] = {
    3: frozenset({"identity", "planning_status"}),
    5: frozenset({"identity", "planning_status", "reports"}),
    6: frozenset({"identity", "planning_status", "reports", "knowledge"}),
}
# The versions that carry reports.json. Named as a set rather than tested with
# ``>= 5`` so a future version has to say for itself which documents it holds.
_REPORTS_VERSIONS: Final[frozenset[int]] = frozenset({5, 6})
_REPORTS_EVIDENCE_IDS: Final[dict[str, str]] = {
    "fresh": "workstack.reports.v5",
    "migrated_v1": "workstack.reports.v3-to-v5",
    "migrated_v2": "workstack.reports.v3-to-v5",
    "migrated_v3": "workstack.reports.v3-to-v5",
}
_KNOWLEDGE_EVIDENCE_IDS: Final[dict[str, str]] = {
    "fresh": "workstack.knowledge.v6",
    "migrated_v1": "workstack.knowledge.v5-to-v6",
    "migrated_v2": "workstack.knowledge.v5-to-v6",
    "migrated_v3": "workstack.knowledge.v5-to-v6",
    "migrated_v5": "workstack.knowledge.v5-to-v6",
}


def supported_roster(schema_version: object, /) -> tuple[str, ...]:
    """The document names one supported collection version holds, sorted."""

    if type(schema_version) is not int or schema_version not in _VERSION_ROSTERS:
        raise StoreCorruptError("store schema version is not supported")
    return tuple(sorted(_VERSION_ROSTERS[schema_version]))


def decode_documents(bodies: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    """Decode already-acquired document bytes, and judge nothing else.

    One decoder serves the live store, the migration and the archive verifier,
    so a body refused as a live document is refused as an archived one for the
    same reason and in the same words. Whether the resulting mappings really
    are a store of some version is `validate_document_values`'s question, not
    this one's.
    """

    values: dict[str, dict[str, Any]] = {}
    for name, body in bodies.items():
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreCorruptError("{} is not valid JSON".format(name)) from error
        if not isinstance(decoded, dict):
            raise StoreCorruptError("{} must contain an object".format(name))
        values[name] = decoded
    return values


def _workspace_keys_are_canonical(value: Mapping[str, Any]) -> bool:
    keys = set(value)
    extra = keys - _WORKSPACE_REQUIRED_KEYS
    return _WORKSPACE_REQUIRED_KEYS <= keys and extra <= _WORKSPACE_OPTIONAL_KEYS


def _require_workspace_high_water_field(value: Mapping[str, Any]) -> None:
    try:
        read_optional_high_water(value)
    except TaskDisplayIdError as error:
        raise StoreCorruptError("workspace identity schema is invalid") from error


def _require_task_display_id_authority(
    workspace: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
) -> None:
    try:
        admitted_high_water(read_optional_high_water(workspace), task_ids_from_records(tasks))
    except TaskDisplayIdError as error:
        raise StoreCorruptError("task display-id high-water is invalid") from error


@dataclass(frozen=True)
class StoreReadiness:
    schema_version: int
    workspace_uid: str
    task_count: int
    migration_origin: str


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StoreCorruptError("{} must be a canonical UUID string".format(label))
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise StoreCorruptError("{} must be a canonical UUID string".format(label)) from error
    if parsed.int == 0 or str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise StoreCorruptError(
            "{} must be a non-nil lowercase canonical RFC 4122 UUID".format(label)
        )
    return value


def _stored_revision(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise StoreCorruptError(
            "{} must be an integer between 0 and {}".format(label, MAX_REVISION)
        )
    return value


def _compact_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _migration_evidence_records(
    migrations: Any, expected_names: frozenset[str]
) -> dict[str, dict[str, Any]]:
    """Every evidence record the given schema version carries, shape-checked."""

    if not isinstance(migrations, dict) or set(migrations) != expected_names:
        raise StoreCorruptError("store migration evidence is invalid")
    expected = {"id", "origin", "source_sha256"}
    for record in migrations.values():
        if not isinstance(record, dict) or set(record) != expected:
            raise StoreCorruptError("store migration evidence is invalid")
    return dict(migrations)


def _validate_identity_migration(identity: dict[str, Any]) -> str:
    origin = identity.get("origin")
    source_sha256 = identity.get("source_sha256")
    if origin == "fresh":
        if identity.get("id") != "workstack.store.v2" or source_sha256 is not None:
            raise StoreCorruptError("fresh store migration evidence is invalid")
        return origin
    if origin == "migrated_v1":
        valid_digest = isinstance(source_sha256, str) and re.fullmatch(
            r"sha256:[0-9a-f]{64}", source_sha256
        )
        if identity.get("id") != "workstack.store.v1-to-v2" or not valid_digest:
            raise StoreCorruptError("v1 migration evidence is invalid")
        return origin
    raise StoreCorruptError("store migration origin is invalid")


def _validate_planning_migration(planning: dict[str, Any]) -> None:
    origin = planning.get("origin")
    digest = planning.get("source_sha256")
    if planning.get("id") != "workstack.planning-status.v1":
        raise StoreCorruptError("planning-status migration evidence is invalid")
    if origin == "fresh":
        if digest is not None:
            raise StoreCorruptError("fresh planning-status evidence is invalid")
        return
    if origin in {"migrated_v1", "migrated_v2"}:
        if not (
            isinstance(digest, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise StoreCorruptError("planning-status migration evidence is invalid")
        return
    raise StoreCorruptError("planning-status migration origin is invalid")


def _backlog_identity_tasks(
    backlog: dict[str, Any], version: int
) -> list[Any]:
    if set(backlog) != {"version", "tasks"} or backlog.get("version") != version:
        raise StoreCorruptError("backlog identity schema is invalid")
    tasks = backlog.get("tasks")
    if not isinstance(tasks, list):
        raise StoreCorruptError("backlog.tasks must be an array")
    return tasks


def _validated_task_id(source: dict[str, Any], label: str, seen: set[str]) -> str:
    task_id = source.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"T-[0-9]{4,}", task_id):
        raise StoreCorruptError("{}.id is invalid".format(label))
    if task_id in seen:
        raise StoreCorruptError("duplicate task id: {}".format(task_id))
    seen.add(task_id)
    return task_id


def _validated_task_uid(
    task: dict[str, Any],
    task_id: str,
    label: str,
    workspace_uid: str,
    seen: set[str],
    migrate_legacy: bool,
) -> str:
    if "uid" in task:
        task_uid = _canonical_uuid(task["uid"], "{}.uid".format(label))
    elif migrate_legacy:
        task_uid = str(uuid.uuid5(uuid.UUID(workspace_uid), task_id))
        task["uid"] = task_uid
    else:
        raise StoreCorruptError("{}.uid is missing".format(label))
    if task_uid in seen:
        raise StoreCorruptError("duplicate persisted UUID: {}".format(task_uid))
    seen.add(task_uid)
    return task_uid


def _validate_task_revision(
    task: dict[str, Any], label: str, migrate_legacy: bool
) -> None:
    if "revision" in task:
        _stored_revision(task["revision"], "{}.revision".format(label))
    elif migrate_legacy:
        task["revision"] = 0
    else:
        raise StoreCorruptError("{}.revision is missing".format(label))


def _validate_task_status_fact(task: dict[str, Any], label: str, version: int) -> None:
    if version != 3:
        return
    status_fact_id = task.get("status_fact_id")
    if not isinstance(status_fact_id, str) or not re.fullmatch(
        r"PS-[0-9]{6,}", status_fact_id
    ):
        raise StoreCorruptError("{}.status_fact_id is invalid".format(label))


def _validated_task_identity(
    source: Any,
    index: int,
    workspace_uid: str,
    version: int,
    migrate_legacy: bool,
    seen_ids: set[str],
    seen_uids: set[str],
) -> dict[str, Any]:
    label = "backlog.tasks[{}]".format(index)
    if not isinstance(source, dict):
        raise StoreCorruptError("{} must be an object".format(label))
    task_id = _validated_task_id(source, label, seen_ids)
    task = copy.deepcopy(source)
    _validated_task_uid(
        task, task_id, label, workspace_uid, seen_uids, migrate_legacy
    )
    _validate_task_revision(task, label, migrate_legacy)
    _validate_task_status_fact(task, label, version)
    return task


def _validated_v2_identity_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    if (
        set(metadata) != {"version", "store_schema_version", "migration"}
        or metadata.get("version") != 1
        or metadata.get("store_schema_version") != 2
        or not isinstance(metadata.get("migration"), dict)
    ):
        raise StoreCorruptError("v2 store migration evidence is invalid")
    identity = copy.deepcopy(metadata["migration"])
    if set(identity) != {"id", "origin", "source_sha256"}:
        raise StoreCorruptError("v2 store migration evidence is invalid")
    origin = identity.get("origin")
    if origin == "fresh":
        valid = (
            identity.get("id") == "workstack.store.v2"
            and identity.get("source_sha256") is None
        )
    elif origin == "migrated_v1":
        valid = (
            identity.get("id") == "workstack.store.v1-to-v2"
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(identity.get("source_sha256", ""))
            )
            is not None
        )
    else:
        valid = False
    if not valid:
        raise StoreCorruptError("v2 identity evidence is invalid")
    return identity


def _validated_v2_activity(value: dict[str, Any]) -> dict[str, Any]:
    activity = copy.deepcopy(value)
    if (
        set(activity) != {"version", "activity", "idempotency"}
        or activity.get("version") != 1
        or not isinstance(activity.get("activity"), list)
        or not isinstance(activity.get("idempotency"), list)
    ):
        raise StoreCorruptError("v2 activity schema is invalid")
    return activity


def _validate_auxiliary_store(name: str, value: dict[str, Any]) -> None:
    expected = AUXILIARY_DEFAULTS.get(name)
    if expected is None:
        raise ValueError("auxiliary store validator received an identity store")
    defect = store_rosters.auxiliary_store_defect(name, value, expected)
    if defect is not None:
        raise StoreCorruptError(defect)


def _validate_captures_container(value: dict[str, Any], workspace_uid: str) -> None:
    """Admit a schema 6 captures document as exactly container 1 or container 2.

    Container 1 keeps the released auxiliary-shape admission, in its own words,
    so a store that never activated the feature is judged exactly as before.
    Container 2 is that same captures list beside the closed owner-internal
    observation list, judged by the pure model against the workspace uid these
    very documents declare rather than one a caller supplied. Anything else --
    an unknown container version, a non-integer version that merely compares
    equal to one, a version 2 document with an extra or a missing field -- is
    refused here instead of being admitted as some other shape.

    A stored observation list that the model refuses is malformed stored data,
    so it raises the store's own corruption error carrying only the model's
    closed code. Admission is stricter than the model alone: the stored list
    must already BE the canonical list the model returns, so a merely
    reorderable container 2 is refused here rather than admitted as authority
    that a later identical replay would silently rewrite. Container 2 has
    never been released, so no existing store can hold a noncanonical one.
    Only collection version 6 reaches this function: versions 1, 2, 3 and 5
    keep the container 1 rule they released with.
    """

    version = value.get("version")
    if type(version) is not int or version != 2:
        _validate_auxiliary_store(CAPTURES_DOCUMENT_NAME, value)
        return
    if set(value) != _CAPTURES_V2_FIELDS:
        raise StoreCorruptError("captures.json schema is invalid")
    if not isinstance(value["captures"], list):
        raise StoreCorruptError("captures.json.captures must be an array")
    try:
        canonical = capture_observations.validate_observations(
            value["observations"], workspace_uid=workspace_uid
        )
    except capture_observations.CaptureObservationError as error:
        raise StoreCorruptError(
            "captures.json observations are invalid: {}".format(error.code)
        ) from error
    if canonical != value["observations"]:
        raise StoreCorruptError("captures.json observations are not canonical")


def _validate_auxiliary_document(
    name: str, value: dict[str, Any], version: int, workspace_uid: str
) -> None:
    """Route one auxiliary document to the rule its collection version implies.

    Only ``captures.json`` under collection version 6 has ever had more than
    one admissible shape. Every other auxiliary document, and every one of
    them under versions 1, 2, 3 and 5, keeps the single default-payload rule
    the roster released with.
    """

    if version == 6 and name == CAPTURES_DOCUMENT_NAME:
        _validate_captures_container(value, workspace_uid)
        return
    _validate_auxiliary_store(name, value)


def _validate_imported_captures(value: dict[str, Any]) -> None:
    """Admit the knowledge-import capture records a v6 store may hold.

    Only records written at the import schema version are judged, and only for
    the retrieval wire they carry: every historical 1.0 record keeps exactly
    the released auxiliary-shape admission above and is not looked at here. A
    malformed stored 1.1 retrieval refuses the load rather than surviving to be
    read back as metadata.
    """

    defect = knowledge_capture_packets.imported_capture_defect(value)
    if defect is not None:
        raise StoreCorruptError(defect)


def _validate_reports_migration(reports: dict[str, Any]) -> None:
    origin = reports.get("origin")
    digest = reports.get("source_sha256")
    if origin not in _REPORTS_EVIDENCE_IDS:
        raise StoreCorruptError("reports migration origin is invalid")
    if reports.get("id") != _REPORTS_EVIDENCE_IDS[origin]:
        raise StoreCorruptError("reports migration evidence is invalid")
    if origin == "fresh":
        if digest is not None:
            raise StoreCorruptError("fresh reports evidence is invalid")
        return
    if not (isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise StoreCorruptError("reports migration evidence is invalid")


def _validate_knowledge_migration(knowledge: dict[str, Any]) -> None:
    origin = knowledge.get("origin")
    digest = knowledge.get("source_sha256")
    if origin not in _KNOWLEDGE_EVIDENCE_IDS:
        raise StoreCorruptError("knowledge migration origin is invalid")
    if knowledge.get("id") != _KNOWLEDGE_EVIDENCE_IDS[origin]:
        raise StoreCorruptError("knowledge migration evidence is invalid")
    if origin == "fresh":
        if digest is not None:
            raise StoreCorruptError("fresh knowledge evidence is invalid")
        return
    if not (isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise StoreCorruptError("knowledge migration evidence is invalid")


def _validate_knowledge(value: Any, workspace_uid: str) -> None:
    """Delegate to the pure ledger model, on the decoded mapping itself.

    The ledger is where a workspace's connection policy and its outstanding
    requests live, so a store whose ledger cannot be admitted is not a store
    this build may answer questions from. The refusal is deliberately closed:
    the code names the rule and, at most, a field of the closed schema.
    """

    try:
        knowledge_ledger_document.validate_knowledge_document(
            value, workspace_uid=workspace_uid
        )
    except knowledge_ledger_document.KnowledgeLedgerError as error:
        raise StoreCorruptError(
            "knowledge.json schema is invalid: {}".format(error.code)
        ) from error


def _validate_metadata(metadata: Any, expected_schema: int) -> str:
    """The v3 and v5 metadata document, judged as exactly the version asked for.

    The released refusal for a store written by a newer build is kept: a stored
    version above the one being validated is reported as newer rather than as
    merely invalid.
    """

    if not isinstance(metadata, dict) or set(metadata) != {
        "version", "store_schema_version", "migrations",
    }:
        raise StoreCorruptError("store metadata has unknown or missing fields")
    if metadata.get("version") != 2:
        raise StoreCorruptError("store metadata version is unsupported")
    schema_version = metadata.get("store_schema_version")
    if schema_version != expected_schema:
        if type(schema_version) is int and schema_version > expected_schema:
            raise StoreCorruptError("store schema is newer than this Work Stack build")
        raise StoreCorruptError("store schema version is invalid")
    records = _migration_evidence_records(
        metadata.get("migrations"), _EVIDENCE_NAMES[expected_schema]
    )
    origin = _validate_identity_migration(records["identity"])
    _validate_planning_migration(records["planning_status"])
    if "reports" in records:
        _validate_reports_migration(records["reports"])
    if "knowledge" in records:
        _validate_knowledge_migration(records["knowledge"])
    return origin


def _validate_activity(value: Any, schema_version: int) -> None:
    if schema_version in (1, 2):
        _validated_v2_activity(value)
        return
    if (
        not isinstance(value, dict)
        or set(value) != set(ACTIVITY_DEFAULT)
        or value.get("version") != 2
        or not isinstance(value.get("activity"), list)
        or not isinstance(value.get("idempotency"), list)
        or not isinstance(value.get("planning_status"), list)
    ):
        raise StoreCorruptError("activity.json schema is invalid")


def _validate_reports(value: Any, workspace_uid: str) -> None:
    """Delegate to the accepted pure model, on the decoded mapping itself.

    The document is handed over as it was read rather than re-serialized, so
    nothing a round trip would quietly normalize slips past the deep validator.
    """

    try:
        report_documents.validate_reports_document(value, workspace_uid=workspace_uid)
    except report_documents.ReportDocumentError as error:
        raise StoreCorruptError(
            "reports.json schema is invalid: {}".format(error.code)
        ) from error


def validate_workspace(value: Any, version: int) -> str:
    if not isinstance(value, dict):
        raise StoreCorruptError("workspace identity schema is invalid")
    if not _workspace_keys_are_canonical(value) or value.get("version") != version:
        raise StoreCorruptError("workspace identity schema is invalid")
    workspace_uid = _canonical_uuid(value.get("id"), "workspace.id")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise StoreCorruptError("workspace.name must be a non-empty string")
    _require_workspace_high_water_field(value)
    return workspace_uid


def admitted_tasks(
    backlog: Any,
    workspace_uid: str,
    *,
    version: int,
    migrate_legacy: bool = False,
) -> list[dict[str, Any]]:
    """The backlog's tasks as detached, identity-complete records.

    Each task is deep-copied before any legacy field is filled in, so admitting
    a v1 backlog never edits the caller's mapping.
    """

    tasks = _backlog_identity_tasks(backlog, version)
    seen_ids: set[str] = set()
    seen_uids: set[str] = {workspace_uid}
    return [
        _validated_task_identity(
            source, index, workspace_uid, version, migrate_legacy, seen_ids, seen_uids
        )
        for index, source in enumerate(tasks)
    ]


def validate_document_values(
    values: object, /, *, schema_version: object
) -> StoreReadiness:
    """Judge already-decoded documents as exactly one collection schema version.

    No path is opened, nothing is initialized and no argument is mutated. The
    caller decides which version it believes it holds and owns acquiring the
    bytes; this only answers whether those documents really are that version.
    """

    roster = supported_roster(schema_version)
    version = int(schema_version)  # supported_roster proved the exact int
    if not isinstance(values, dict) or set(values) != set(roster):
        raise StoreCorruptError("store roster does not match the schema version")
    for name in roster:
        if not isinstance(values[name], dict):
            raise StoreCorruptError("{} must contain an object".format(name))
    workspace_uid = validate_workspace(
        values["workspace.json"], _WORKSPACE_VERSIONS[version]
    )
    tasks = admitted_tasks(
        values["backlog.json"],
        workspace_uid,
        version=_BACKLOG_VERSIONS[version],
        migrate_legacy=version == 1,
    )
    for name in roster:
        if name in AUXILIARY_DEFAULTS:
            _validate_auxiliary_document(name, values[name], version, workspace_uid)
    _validate_activity(values["activity.json"], version)
    origin = "pre_metadata"
    if version == 2:
        evidence = _validated_v2_identity_evidence(values["store-meta.json"])
        origin = str(evidence["origin"])
    elif version in _EVIDENCE_NAMES:
        origin = _validate_metadata(values["store-meta.json"], version)
    if version in _EVIDENCE_NAMES:
        try:
            validate_and_project(values["backlog.json"], values["activity.json"])
        except PlanningStatusValidationError as error:
            raise StoreCorruptError(str(error)) from error
        _require_task_display_id_authority(values["workspace.json"], tasks)
    if version in _REPORTS_VERSIONS:
        _validate_reports(values[store_rosters.REPORTS_DOCUMENT_NAME], workspace_uid)
    if version == 6:
        _validate_knowledge(
            values[store_rosters.KNOWLEDGE_DOCUMENT_NAME], workspace_uid
        )
        _validate_imported_captures(values["captures.json"])
    return StoreReadiness(
        schema_version=version,
        workspace_uid=workspace_uid,
        task_count=len(tasks),
        migration_origin=origin,
    )
