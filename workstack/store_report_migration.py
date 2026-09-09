"""Packing and verifying an archive, and planning the upgrade to schema 5.

The jobs share this module because they share one rule: each works from bytes
or mappings a caller already acquired, and none of them initializes a store,
reads a clock or writes an authoritative document.

`pack_backup_archive` is the archive writer the held-byte backup seam
introduced. It lives here rather than in `workstack.maintenance` because the
migration has to take its own rollback backup while it holds the writer lease,
and the released store may not import the maintenance layer. `maintenance`
wraps it and keeps `BackupValidationError` as the public refusal, so nothing a
caller catches today changes.

`verify_archive_file` is the matching reader, and it lives here for the same
reason: the migration must prove the rollback archive it just persisted is
readable *from the path it would be rolled back from* before it writes the
authoritative journal, and that proof cannot come from a layer above the
store. It is read-only and version-aware: the container's own directory, then
the manifest header, then the roster that manifest's version implies, and only
then a payload member. An archive claiming schema 4, an unknown version or a
version newer than this build is refused before one payload is decompressed.

`plan_upgrade` turns validated v1, v2 or v3 documents into the complete set of
v5 documents to write. It returns every document of the new roster in one
mapping, because the store commits them through a single journal entry: a
generation carrying nine v3 documents and a v5 metadata record would fail its
own readiness check. The evidence it writes names the version actually
detected on disk and digests the documents actually found there, never an
intermediate snapshot the upgrade invented on the way.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from . import store_document_validation as validation
from . import store_rosters
from .planning_status import append_bootstrap


BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST = "manifest.json"
BACKUP_WORKSPACE_ID_LIMIT = 128
# The version *this* planner produces. It is deliberately not the version the
# build writes: ``plan_upgrade`` below is the historical record of the v5 step,
# and ``workstack.store_knowledge_migration`` owns the one that follows it.
CURRENT_SCHEMA_VERSION = 5
# One archive, and everything it expands to, stays inside this bound. It is the
# released `maintenance.MAX_BACKUP_BYTES`, moved down so the reader that
# enforces it and the writer that produces it sit at the same layer.
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024

_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset({
    "schema_version",
    "product_version",
    "created_at",
    "workspace_id",
    "store_schema_version",
    "files",
})

_IDENTITY_IDS: Final[dict[str, str]] = {
    "fresh": "workstack.store.v2",
    "migrated_v1": "workstack.store.v1-to-v2",
}
_PROVENANCE: Final[dict[int, str]] = {1: "store.v1", 2: "store.v2"}


class BackupPackError(ValueError):
    """Refusal raised while packing or verifying an archive.

    `workstack.maintenance` re-raises it as `BackupValidationError`, so the
    messages a caller already matches on are unchanged whichever side of the
    seam produced them.
    """


@dataclass(frozen=True)
class VerifiedArchive:
    """What one archive turned out to hold, judged where it lies.

    `bodies` are the payload members exactly as stored, so a caller that packed
    them can compare its held source bytes against what the file really
    contains; `values` are those bodies decoded and admitted as
    `store_schema_version`. `digest` is of the single image every other field
    here was read from, so comparing it against the digest of the bytes a
    caller wrote binds this verdict to that exact archive.
    """

    path: Path
    workspace_id: str
    created_at: str
    digest: str
    file_count: int
    store_schema_version: int
    bodies: dict[str, bytes]
    values: dict[str, dict[str, Any]]


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def backup_roster(store_schema_version: object, /) -> tuple[str, ...]:
    """The archive member roster for one supported collection schema version."""

    rosters = {
        1: store_rosters.V1_DOCUMENT_NAMES,
        2: store_rosters.V2_DOCUMENT_NAMES,
        3: store_rosters.V3_DOCUMENT_NAMES,
        5: store_rosters.V5_DOCUMENT_NAMES,
        6: store_rosters.V6_DOCUMENT_NAMES,
    }
    if type(store_schema_version) is not int or store_schema_version not in rosters:
        raise BackupPackError("backup store schema version is unsupported")
    return tuple(sorted(rosters[store_schema_version]))


def archive_filename(created: dt.datetime, workspace_id: str) -> str:
    return "workstack-backup-{}-{}.zip".format(
        created.strftime("%Y%m%dT%H%M%S%fZ"), workspace_id[:8]
    )


def pack_backup_archive(
    bodies: object,
    /,
    *,
    workspace_id: object,
    store_schema_version: object,
    created: object,
) -> dict[str, Any]:
    """Wrap validated bytes as one archive; return its body and its facts.

    The caller has already read these bytes under a lease and judged them as
    the stated version. Nothing here re-reads, re-orders or re-serializes a
    payload: each member is written exactly as supplied, in the roster's sorted
    order, and every size and digest is computed from the supplied bytes.
    """

    roster = backup_roster(store_schema_version)
    names_ok = (
        type(bodies) is dict
        and all(type(name) is str for name in bodies)
        and set(bodies) == set(roster)
        and all(type(body) is bytes for body in bodies.values())
    )
    workspace_ok = (
        type(workspace_id) is str and 0 < len(workspace_id) <= BACKUP_WORKSPACE_ID_LIMIT
    )
    time_ok = (
        type(created) is dt.datetime
        and created.tzinfo is not None
        and created.utcoffset() == dt.timedelta(0)
    )
    for ok, message in (
        (names_ok, "backup bodies are invalid"),
        (workspace_ok, "backup workspace identity is invalid"),
        (time_ok, "backup creation time is invalid"),
    ):
        if not ok:
            raise BackupPackError(message)
    payloads: Any = bodies
    created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "product_version": _product_version(),
        "created_at": created_at,
        "workspace_id": workspace_id,
        "store_schema_version": store_schema_version,
        "files": [
            {"name": name, "sha256": _sha256(payloads[name]), "size": len(payloads[name])}
            for name in roster
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BACKUP_MANIFEST, _manifest_bytes(manifest))
        for name in roster:
            archive.writestr(name, payloads[name])
    body = buffer.getvalue()
    return {
        "body": body,
        "filename": archive_filename(created, workspace_id),
        "workspace_id": workspace_id,
        "created_at": created_at,
        "digest": _sha256(body),
        "file_count": len(roster),
    }


def _product_version() -> str:
    from . import __version__

    return __version__


def _archive_candidate(path: Path | str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise BackupPackError("backup archive does not exist")
    if candidate.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupPackError("backup archive exceeds the size limit")
    return candidate


def _held_archive_image(path: Path | str) -> tuple[Path, bytes]:
    """Take the one image of the archive everything after this judges.

    The file is read exactly once. Every later question — the container
    directory, the manifest, the members, and the digest this verification
    reports — is answered from the bytes held here, so no two answers can come
    from two different files that happened to share a path. A second read is
    what lets a swapped or truncated archive be validated in one image and
    digested in another.
    """

    candidate = _archive_candidate(path)
    try:
        image = candidate.read_bytes()
    except OSError as error:
        raise BackupPackError("backup archive is unreadable") from error
    if len(image) > MAX_ARCHIVE_BYTES:
        raise BackupPackError("backup archive exceeds the size limit")
    return candidate, image


def _admitted_member_names(archive: zipfile.ZipFile) -> list[str]:
    """Judge the container's own directory, before one payload is decompressed.

    Duplicate names, directory entries, an oversized member and an oversized
    expansion are all readable from the central directory, so they are settled
    here. Which member set is *correct* is not knowable yet: the roster comes
    from a manifest that is itself a member.
    """

    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)) or BACKUP_MANIFEST not in names:
        raise BackupPackError("backup archive member set is invalid")
    if any(item.is_dir() or item.file_size > MAX_ARCHIVE_BYTES for item in infos):
        raise BackupPackError("backup archive contains an invalid member")
    if sum(item.file_size for item in infos) > MAX_ARCHIVE_BYTES:
        raise BackupPackError("expanded backup exceeds the size limit")
    return names


def _decode_manifest(body: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupPackError("backup manifest is invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != set(_MANIFEST_FIELDS):
        raise BackupPackError("backup manifest fields are invalid")
    return manifest


def _validate_manifest_header(manifest: dict[str, Any]) -> None:
    if manifest["schema_version"] != BACKUP_SCHEMA_VERSION:
        raise BackupPackError("backup schema version is unsupported")
    if (
        not isinstance(manifest["product_version"], str)
        or not manifest["product_version"]
    ):
        raise BackupPackError("backup product version is invalid")
    if not isinstance(manifest["created_at"], str):
        raise BackupPackError("backup creation time is invalid")
    try:
        parsed_time = dt.datetime.fromisoformat(
            manifest["created_at"].replace("Z", "+00:00")
        )
    except ValueError as error:
        raise BackupPackError("backup creation time is invalid") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise BackupPackError("backup creation time must include a timezone")
    if not isinstance(manifest["workspace_id"], str) or not manifest["workspace_id"]:
        raise BackupPackError("backup workspace identity is invalid")


def _validate_file_record(
    record: Any,
    bodies: dict[str, bytes],
    indexed: dict[str, dict[str, Any]],
    roster: tuple[str, ...],
) -> None:
    if not isinstance(record, dict) or set(record) != {"name", "sha256", "size"}:
        raise BackupPackError("backup file record is invalid")
    name = record["name"]
    if name not in roster or name in indexed:
        raise BackupPackError("backup file record is unknown or repeated")
    body = bodies[name]
    if type(record["size"]) is not int or record["size"] != len(body):
        raise BackupPackError("backup member size mismatch")
    if not isinstance(record["sha256"], str) or not secrets.compare_digest(
        record["sha256"], _sha256(body)
    ):
        raise BackupPackError("backup member digest mismatch")
    indexed[name] = record


def _verify_file_manifest(
    manifest: dict[str, Any], bodies: dict[str, bytes], roster: tuple[str, ...]
) -> None:
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(roster):
        raise BackupPackError("backup file manifest is invalid")
    indexed: dict[str, dict[str, Any]] = {}
    for record in files:
        _validate_file_record(record, bodies, indexed, roster)


def _validated_documents(
    manifest: dict[str, Any], bodies: dict[str, bytes]
) -> dict[str, dict[str, Any]]:
    """Judge the archived documents through the store's own document seam.

    The archive is never unpacked into a directory and nothing is initialized,
    so verifying a v3 backup on a v5 build cannot migrate the very copy it was
    asked to judge. A rule cannot hold for a live authority and not for a
    backup of one, because both reach the same validator.
    """

    try:
        values = validation.decode_documents(bodies)
        readiness = validation.validate_document_values(
            values, schema_version=manifest["store_schema_version"]
        )
    except ValueError as error:
        raise BackupPackError("backup store failed semantic validation") from error
    if readiness.workspace_uid != manifest["workspace_id"]:
        raise BackupPackError("backup workspace identity mismatch")
    return values


def verify_archive_file(path: Path | str) -> VerifiedArchive:
    """Verify one archive from its own path, admitting nothing out of order.

    The path is read once into a held image, and the container directory, the
    manifest, every member and the reported digest all come from that one
    image. `path` therefore names where the image was taken, while `digest`
    names the bytes actually judged, and the two can never describe different
    files.

    The manifest is decoded and its header and claimed collection version are
    admitted *before* the member set is judged and before any payload member is
    read, so an archive claiming schema 4, an unknown version or a version
    newer than this build is refused while its payload is still compressed.
    Nothing is written, extracted or initialized.
    """

    candidate, image = _held_archive_image(path)
    try:
        with zipfile.ZipFile(io.BytesIO(image), "r") as archive:
            names = _admitted_member_names(archive)
            manifest = _decode_manifest(archive.read(BACKUP_MANIFEST))
            _validate_manifest_header(manifest)
            roster = backup_roster(manifest["store_schema_version"])
            if set(names) != {BACKUP_MANIFEST, *roster}:
                raise BackupPackError("backup archive member set is invalid")
            bodies = {name: archive.read(name) for name in roster}
    except (zipfile.BadZipFile, OSError) as error:
        raise BackupPackError("backup archive is unreadable") from error
    _verify_file_manifest(manifest, bodies, roster)
    values = _validated_documents(manifest, bodies)
    return VerifiedArchive(
        path=candidate,
        workspace_id=manifest["workspace_id"],
        created_at=manifest["created_at"],
        digest=_sha256(image),
        file_count=len(bodies),
        store_schema_version=int(manifest["store_schema_version"]),
        bodies=bodies,
        values=values,
    )


def source_digest(values: Mapping[str, dict[str, Any]]) -> str:
    """The digest of the documents actually detected on disk."""

    return _sha256(validation._compact_json(dict(values)))


def _evidence(record_id: str, origin: str, digest: str | None) -> dict[str, Any]:
    return {"id": record_id, "origin": origin, "source_sha256": digest}


def _v5_metadata(
    identity: dict[str, Any], planning_origin: str, digest: str
) -> dict[str, Any]:
    return {
        "version": 2,
        "store_schema_version": 5,
        "migrations": {
            "identity": copy.deepcopy(identity),
            "planning_status": _evidence(
                "workstack.planning-status.v1", planning_origin, digest
            ),
            "reports": _evidence("workstack.reports.v3-to-v5", planning_origin, digest),
        },
    }


def _bootstrapped_activity(
    activity: dict[str, Any], tasks: list[dict[str, Any]], provenance: str, now: str
) -> dict[str, Any]:
    migrated = copy.deepcopy(activity)
    migrated["version"] = 2
    migrated["planning_status"] = []
    for task in tasks:
        append_bootstrap(
            migrated,
            task,
            created_at=now,
            actor="workstack.migration",
            provenance=provenance,
        )
    return migrated


def _carried(values: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The auxiliary documents every version carries forward untouched."""

    return {
        name: copy.deepcopy(values[name]) for name in validation.AUXILIARY_DEFAULTS
    }


def _plan_legacy(
    detected: int, values: Mapping[str, dict[str, Any]], digest: str, now: str
) -> dict[str, dict[str, Any]]:
    workspace_uid = validation.validate_workspace(
        values["workspace.json"], 1 if detected == 1 else 2
    )
    tasks = validation.admitted_tasks(
        values["backlog.json"],
        workspace_uid,
        version=detected,
        migrate_legacy=detected == 1,
    )
    workspace = copy.deepcopy(values["workspace.json"])
    workspace["version"] = 2
    if detected == 1:
        identity = _evidence(_IDENTITY_IDS["migrated_v1"], "migrated_v1", digest)
    else:
        identity = validation._validated_v2_identity_evidence(values["store-meta.json"])
    origin = "migrated_v1" if detected == 1 else "migrated_v2"
    writes = _carried(values)
    writes.update({
        "workspace.json": workspace,
        "backlog.json": {"version": 3, "tasks": tasks},
        "activity.json": _bootstrapped_activity(
            values["activity.json"], tasks, _PROVENANCE[detected], now
        ),
        "store-meta.json": _v5_metadata(identity, origin, digest),
        store_rosters.REPORTS_DOCUMENT_NAME: copy.deepcopy(validation.REPORTS_DEFAULT),
    })
    return writes


def _plan_v3(
    values: Mapping[str, dict[str, Any]], digest: str
) -> dict[str, dict[str, Any]]:
    """v3 documents keep their content and their history; only the roster grows."""

    metadata = copy.deepcopy(values["store-meta.json"])
    migrations = metadata["migrations"]
    metadata["store_schema_version"] = 5
    migrations["reports"] = _evidence(
        "workstack.reports.v3-to-v5", "migrated_v3", digest
    )
    writes = {
        name: copy.deepcopy(values[name]) for name in store_rosters.V3_DOCUMENT_ORDER
    }
    writes["store-meta.json"] = metadata
    writes[store_rosters.REPORTS_DOCUMENT_NAME] = copy.deepcopy(
        validation.REPORTS_DEFAULT
    )
    return writes


def plan_upgrade(
    detected: int,
    values: Mapping[str, dict[str, Any]],
    digest: str,
    *,
    now: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Every v5 document to write, and the operation id that names the upgrade.

    `digest` is taken over the documents that were detected, so a v1 store's
    reports evidence points at the v1 bytes a rollback would restore, not at
    the v3-shaped values this function derives on the way to v5.
    """

    if detected not in (1, 2, 3):
        raise validation.StoreCorruptError("store schema version is not supported")
    writes = _plan_v3(values, digest) if detected == 3 else _plan_legacy(
        detected, values, digest, now
    )
    if set(writes) != set(store_rosters.V5_DOCUMENT_NAMES):
        raise validation.StoreCorruptError("upgrade did not produce the v5 roster")
    operation_id = "store-migrate-v{}-v5-{}".format(detected, digest[7:23])
    return writes, operation_id
