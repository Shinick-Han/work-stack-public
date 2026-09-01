"""Disposable SQLite projection built from one verified semantic snapshot.

The projection is deliberately ignorant of Store, the v4 writer, and service
objects.  A caller must supply the exact authority stamp that was verified
while producing the immutable :class:`WorkspaceSnapshot`.  Publication uses a
versioned database plus one small atomic pointer, so an interrupted rebuild can
never turn a partial SQLite file into an admitted read model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .canonical import canonical_json_bytes, canonical_sha256
from .semantic import WorkspaceSnapshot


PROJECTION_SCHEMA_VERSION = 1
PROJECTION_STATE_NAME = "projection-state.json"
MAX_PROJECTION_STATE_BYTES = 64 * 1024
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATABASE_NAME = re.compile(
    r"^index-p1-g[0-9]+-[0-9a-f]{64}-[0-9a-f]{32}\.sqlite$"
)


class ProjectionError(ValueError):
    """A stable, content-free projection refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProjectionAuthority:
    """Identity of the exact canonical snapshot used for a projection."""

    workspace_uid: str
    format_version: int
    generation: int
    manifest_digest: str
    semantic_digest: str

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.workspace_uid)
        except (AttributeError, ValueError) as error:
            raise ProjectionError("AUTHORITY_WORKSPACE_UID_INVALID") from error
        if str(parsed) != self.workspace_uid or parsed.int == 0:
            raise ProjectionError("AUTHORITY_WORKSPACE_UID_INVALID")
        if self.format_version not in {3, 4}:
            raise ProjectionError("AUTHORITY_FORMAT_UNSUPPORTED")
        if type(self.generation) is not int or not 0 <= self.generation <= 2**53 - 1:
            raise ProjectionError("AUTHORITY_GENERATION_INVALID")
        if not _SHA256.fullmatch(self.manifest_digest):
            raise ProjectionError("AUTHORITY_MANIFEST_DIGEST_INVALID")
        if not _SHA256.fullmatch(self.semantic_digest):
            raise ProjectionError("AUTHORITY_SEMANTIC_DIGEST_INVALID")


@dataclass(frozen=True)
class ProjectionAdmission:
    """Freshness result; non-verified states always require canonical reads."""

    status: str
    reason: str
    canonical_fallback_required: bool
    database_path: Path | None = None

    @property
    def verified(self) -> bool:
        return self.status == "Verified"


@dataclass(frozen=True)
class ProjectionPublication:
    authority: ProjectionAuthority
    database_path: Path
    state_path: Path
    record_count: int
    edge_count: int
    search_count: int


def rebuilding_projection(reason: str = "PROJECTION_BUILD_IN_PROGRESS") -> ProjectionAdmission:
    """Return the explicit state exposed while a background rebuild is running."""

    return ProjectionAdmission("Rebuilding", reason, True)


def _bypassed(reason: str) -> ProjectionAdmission:
    return ProjectionAdmission("Bypassed", reason, True)


def _stable_uid(workspace_uid: str, kind: str, item: Mapping[str, Any]) -> str:
    candidate = item.get("uid")
    if isinstance(candidate, str):
        try:
            if str(uuid.UUID(candidate)) == candidate:
                return candidate
        except ValueError:
            pass
    display_id = str(item.get("id", ""))
    return str(uuid.uuid5(uuid.UUID(workspace_uid), f"projection:{kind}:{display_id}"))


def _record_rows(value: Mapping[str, Any], workspace_uid: str) -> tuple[list[tuple[Any, ...]], dict[tuple[str, str], str]]:
    rows: list[tuple[Any, ...]] = []
    identities: dict[tuple[str, str], str] = {}
    collections = (
        ("task", value["tasks"]),
        ("objective", value["objectives"]),
        ("note", value["notes"]),
        ("capture", value["captures"]),
    )
    for kind, items in collections:
        for item in items:
            display_id = str(item["id"])
            record_uid = _stable_uid(workspace_uid, kind, item)
            identity_key = (kind, display_id)
            if identity_key in identities:
                raise ProjectionError("PROJECTION_DISPLAY_ID_DUPLICATE")
            identities[identity_key] = record_uid
            rows.append((
                record_uid,
                kind,
                display_id,
                item.get("status"),
                item.get("priority"),
                item.get("due"),
                item.get("updated_at") or item.get("created"),
                "workspace-snapshot",
                canonical_sha256(item),
            ))
    rows.sort(key=lambda row: (row[1], row[2], row[0]))
    return rows, identities


def _edge_rows(value: Mapping[str, Any], identities: Mapping[tuple[str, str], str]) -> list[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for task in value["tasks"]:
        source = identities[("task", str(task["id"]))]
        for objective_id in task["objective_ids"]:
            target = identities.get(("objective", str(objective_id)))
            if target is not None:
                edges.add(("objective", source, target))
        parent_id = task.get("parent_id")
        if parent_id is not None:
            target = identities.get(("task", str(parent_id)))
            if target is not None:
                edges.add(("parent", source, target))
        for dependency_id in task["dependencies"]:
            target = identities.get(("task", str(dependency_id)))
            if target is not None:
                edges.add(("dependency", source, target))
    for note in value["notes"]:
        source = identities[("note", str(note["id"]))]
        for target_id in note["links"]:
            target = next(
                (uid for (kind, display_id), uid in identities.items() if display_id == str(target_id)),
                None,
            )
            if target is not None:
                edges.add(("reference", source, target))
    return sorted(edges)


def _capture_task_rows(value: Mapping[str, Any], identities: Mapping[tuple[str, str], str]) -> list[tuple[str, str, str]]:
    rows: set[tuple[str, str, str]] = set()
    for capture in value["captures"]:
        capture_uid = identities[("capture", str(capture["id"]))]
        for field, relation in (("linked_task_ids", "linked"), ("converted_task_ids", "converted")):
            for task_id in capture.get(field, []):
                task_uid = identities.get(("task", str(task_id)))
                if task_uid is not None:
                    rows.add((capture_uid, task_uid, relation))
    return sorted(rows)


SearchValue = tuple[str, str, str, str, str, str | None, list[str]]


def _task_search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    for task in value["tasks"]:
        due = f" · due {task['due']}" if task.get("due") else ""
        searchable = [str(task.get("detail", "")), *map(str, task.get("tags", [])), *map(str, task.get("objective_ids", []))]
        searchable.extend(str(note.get("text", "")) for note in task.get("notes", []))
        searchable.extend(str(item.get("title", "")) for item in task.get("subtasks", []))
        yield "task", str(task["id"]), str(task["title"]), f"{task.get('status', 'open')} · {task.get('priority', 'P2')}{due}", "task", str(task["id"]), searchable


def _objective_search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    for objective in value["objectives"]:
        key_results = objective.get("key_results", [])
        searchable = [str(item.get("text", "")) for item in key_results]
        searchable.extend(str(item.get("target", "")) for item in key_results)
        yield "objective", str(objective["id"]), str(objective["objective"]), f"{objective.get('quarter', 'No quarter')} · {objective.get('status', 'active')}", "objective", str(objective["id"]), searchable


def _note_search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    for note in value["notes"]:
        text = str(note.get("text", ""))
        item_id = str(note["id"])
        yield "note", item_id, text[:100] or item_id, f"Graph note · {len(note.get('links', []))} links", "workspace", None, [text, *map(str, note.get("links", []))]


def _capture_search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    for capture in value["captures"]:
        source = capture.get("source", {})
        normalized = capture.get("normalized", {})
        searchable = [str(normalized.get("summary", "")), str(normalized.get("context", ""))]
        searchable.extend(str(item.get("title", "")) for item in normalized.get("action_items", []))
        item_id = str(capture["id"])
        yield "capture", item_id, str(source.get("display_title", item_id)), f"{source.get('provider', 'manual')} · {capture.get('status', 'inbox')}", "capture", item_id, searchable


def _activity_search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    for event in value["activity"]:
        details = event.get("details", {})
        details = details if isinstance(details, dict) else {}
        item_id = str(event.get("id", ""))
        event_type = str(event.get("type", ""))
        target_kind = "task" if isinstance(event.get("task_id"), str) else "capture" if isinstance(event.get("capture_id"), str) else "workspace"
        target_id = event.get("task_id") or event.get("capture_id")
        yield "activity", item_id, event_type.replace(".", " ").strip().title() or "Activity", str(event.get("created_at", "")), target_kind, target_id, [event_type, str(details.get("provider", "")), str(details.get("state", ""))]


def _search_values(value: Mapping[str, Any]) -> Iterator[SearchValue]:
    yield from _task_search_values(value)
    yield from _objective_search_values(value)
    yield from _note_search_values(value)
    yield from _capture_search_values(value)
    yield from _activity_search_values(value)


def _search_rows(value: Mapping[str, Any]) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    documents: list[tuple[Any, ...]] = []
    terms: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    for kind, item_id, title, subtitle, target_kind, target_id, searchable in _search_values(value):
        key = (kind, item_id)
        if key in seen:
            raise ProjectionError("PROJECTION_SEARCH_ID_DUPLICATE")
        seen.add(key)
        documents.append((kind, item_id, title, subtitle, target_kind, target_id, title.casefold(), item_id.casefold()))
        for ordinal, text in enumerate(searchable):
            terms.append((kind, item_id, ordinal, text.casefold()))
    documents.sort(key=lambda row: (row[0], row[1]))
    terms.sort(key=lambda row: (row[0], row[1], row[2]))
    return documents, terms


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro" if read_only else str(path),
        uri=read_only,
        timeout=5,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        connection.close()
        raise ProjectionError("PROJECTION_FOREIGN_KEYS_DISABLED")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE projection_meta (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          schema_version INTEGER NOT NULL,
          workspace_uid TEXT NOT NULL,
          format_version INTEGER NOT NULL,
          authority_generation INTEGER NOT NULL,
          authority_manifest_digest TEXT NOT NULL,
          semantic_digest TEXT NOT NULL,
          record_count INTEGER NOT NULL,
          edge_count INTEGER NOT NULL,
          search_count INTEGER NOT NULL
        );
        CREATE TABLE record_index (
          record_uid TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          display_id TEXT NOT NULL,
          status TEXT,
          priority TEXT,
          due TEXT,
          updated_at TEXT,
          provenance TEXT NOT NULL,
          value_digest TEXT NOT NULL,
          UNIQUE(kind, display_id)
        );
        CREATE TABLE graph_edge (
          kind TEXT NOT NULL,
          source_uid TEXT NOT NULL REFERENCES record_index(record_uid),
          target_uid TEXT NOT NULL REFERENCES record_index(record_uid),
          PRIMARY KEY(kind, source_uid, target_uid)
        );
        CREATE TABLE capture_task (
          capture_uid TEXT NOT NULL REFERENCES record_index(record_uid),
          task_uid TEXT NOT NULL REFERENCES record_index(record_uid),
          relation TEXT NOT NULL CHECK(relation IN ('linked', 'converted')),
          PRIMARY KEY(capture_uid, task_uid, relation)
        );
        CREATE TABLE search_document (
          kind TEXT NOT NULL,
          item_id TEXT NOT NULL,
          title TEXT NOT NULL,
          subtitle TEXT NOT NULL,
          target_kind TEXT NOT NULL,
          target_id TEXT,
          folded_title TEXT NOT NULL,
          folded_id TEXT NOT NULL,
          PRIMARY KEY(kind, item_id)
        );
        CREATE TABLE search_term (
          kind TEXT NOT NULL,
          item_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          folded_value TEXT NOT NULL,
          PRIMARY KEY(kind, item_id, ordinal),
          FOREIGN KEY(kind, item_id) REFERENCES search_document(kind, item_id)
        );
        CREATE INDEX task_filter_idx ON record_index(kind, status, priority, due, updated_at);
        CREATE INDEX edge_source_idx ON graph_edge(source_uid, kind);
        CREATE INDEX edge_target_idx ON graph_edge(target_uid, kind);
        CREATE INDEX search_title_idx ON search_document(folded_title);
        CREATE INDEX search_id_idx ON search_document(folded_id);
        CREATE INDEX search_term_idx ON search_term(folded_value);
    """)


def _counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("record_index", "graph_edge", "search_document")
    )  # type: ignore[return-value]


def _verify_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ProjectionError("PROJECTION_INTEGRITY_CHECK_FAILED")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ProjectionError("PROJECTION_FOREIGN_KEY_CHECK_FAILED")


def _write_database(path: Path, snapshot: WorkspaceSnapshot, authority: ProjectionAuthority) -> tuple[int, int, int]:
    value = snapshot.to_dict()
    if snapshot.digest != authority.semantic_digest:
        raise ProjectionError("PROJECTION_SEMANTIC_DIGEST_MISMATCH")
    if value.get("workspace", {}).get("id") != authority.workspace_uid:
        raise ProjectionError("PROJECTION_WORKSPACE_UID_MISMATCH")
    records, identities = _record_rows(value, authority.workspace_uid)
    edges = _edge_rows(value, identities)
    capture_tasks = _capture_task_rows(value, identities)
    search_documents, search_terms = _search_rows(value)
    connection = _connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        with connection:
            _create_schema(connection)
            connection.executemany("INSERT INTO record_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
            connection.executemany("INSERT INTO graph_edge VALUES (?, ?, ?)", edges)
            connection.executemany("INSERT INTO capture_task VALUES (?, ?, ?)", capture_tasks)
            connection.executemany("INSERT INTO search_document VALUES (?, ?, ?, ?, ?, ?, ?, ?)", search_documents)
            connection.executemany("INSERT INTO search_term VALUES (?, ?, ?, ?)", search_terms)
            counts = len(records), len(edges), len(search_documents)
            connection.execute(
                "INSERT INTO projection_meta VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (PROJECTION_SCHEMA_VERSION, authority.workspace_uid, authority.format_version,
                 authority.generation, authority.manifest_digest, authority.semantic_digest, *counts),
            )
        _verify_database(connection)
        if _counts(connection) != counts:
            raise ProjectionError("PROJECTION_COUNT_MISMATCH")
        return counts
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows rejects fsync on a read-only CRT descriptor even for a regular
    # file.  The staging database is owned by this rebuild, so opening it for
    # update is safe and does not alter the already verified bytes.
    with path.open("rb+") as source:
        os.fsync(source.fileno())


def _atomic_state(path: Path, value: Mapping[str, Any]) -> None:
    body = canonical_json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".projection-state-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_and_publish_projection(
    runtime_root: Path | str,
    snapshot: WorkspaceSnapshot,
    authority: ProjectionAuthority,
) -> ProjectionPublication:
    """Build, verify, and atomically publish one disposable projection."""

    root = Path(runtime_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    build_uid = uuid.uuid4().hex
    database_name = f"index-p1-g{authority.generation}-{authority.manifest_digest[7:]}-{build_uid}.sqlite"
    database_path = root / database_name
    staging = root / f".{database_name}.tmp"
    if database_path.exists() or staging.exists():
        raise ProjectionError("PROJECTION_PUBLICATION_COLLISION")
    try:
        record_count, edge_count, search_count = _write_database(staging, snapshot, authority)
        _fsync_file(staging)
        os.replace(staging, database_path)
        database_digest = _sha256_file(database_path)
        state = {
            "authority_format_version": authority.format_version,
            "authority_generation": authority.generation,
            "authority_manifest_digest": authority.manifest_digest,
            "database_file": database_name,
            "database_sha256": database_digest,
            "edge_count": edge_count,
            "format": "workstack.projection-state",
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "record_count": record_count,
            "search_count": search_count,
            "semantic_digest": authority.semantic_digest,
            "status": "Verified",
            "workspace_uid": authority.workspace_uid,
        }
        state_path = root / PROJECTION_STATE_NAME
        _atomic_state(state_path, state)
        return ProjectionPublication(
            authority, database_path, state_path, record_count, edge_count, search_count
        )
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _load_state(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise ProjectionError("PROJECTION_STATE_UNAVAILABLE") from error
    if len(body) > MAX_PROJECTION_STATE_BYTES:
        raise ProjectionError("PROJECTION_STATE_TOO_LARGE")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectionError("PROJECTION_STATE_INVALID") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != body:
        raise ProjectionError("PROJECTION_STATE_INVALID")
    return value


def _state_matches(value: Mapping[str, Any], authority: ProjectionAuthority) -> bool:
    return (
        value.get("format") == "workstack.projection-state"
        and value.get("projection_schema_version") == PROJECTION_SCHEMA_VERSION
        and value.get("status") == "Verified"
        and value.get("workspace_uid") == authority.workspace_uid
        and value.get("authority_format_version") == authority.format_version
        and value.get("authority_generation") == authority.generation
        and value.get("authority_manifest_digest") == authority.manifest_digest
        and value.get("semantic_digest") == authority.semantic_digest
    )


def _metadata_matches(connection: sqlite3.Connection, value: Mapping[str, Any], authority: ProjectionAuthority) -> bool:
    try:
        row = connection.execute(
            "SELECT schema_version, workspace_uid, format_version, authority_generation, "
            "authority_manifest_digest, semantic_digest, record_count, edge_count, search_count "
            "FROM projection_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    expected = (
        PROJECTION_SCHEMA_VERSION,
        authority.workspace_uid,
        authority.format_version,
        authority.generation,
        authority.manifest_digest,
        authority.semantic_digest,
        value.get("record_count"),
        value.get("edge_count"),
        value.get("search_count"),
    )
    return row == expected


def admit_projection(runtime_root: Path | str, authority: ProjectionAuthority) -> ProjectionAdmission:
    """Admit SQLite only when pointer, bytes, metadata, FK, and integrity are fresh."""

    root = Path(runtime_root).expanduser().resolve()
    try:
        state = _load_state(root / PROJECTION_STATE_NAME)
    except ProjectionError as error:
        return _bypassed(error.code)
    if state.get("format") != "workstack.projection-state":
        return _bypassed("PROJECTION_STATE_FORMAT_UNSUPPORTED")
    if state.get("projection_schema_version") != PROJECTION_SCHEMA_VERSION:
        return _bypassed("PROJECTION_SCHEMA_UNSUPPORTED")
    if not _state_matches(state, authority):
        return _bypassed("PROJECTION_AUTHORITY_STALE")
    database_name = state.get("database_file")
    if not isinstance(database_name, str) or not _DATABASE_NAME.fullmatch(database_name):
        return _bypassed("PROJECTION_DATABASE_NAME_INVALID")
    database_path = root / database_name
    if not database_path.is_file() or database_path.parent != root:
        return _bypassed("PROJECTION_DATABASE_UNAVAILABLE")
    try:
        database_digest = _sha256_file(database_path)
    except OSError:
        return _bypassed("PROJECTION_DATABASE_UNAVAILABLE")
    if state.get("database_sha256") != database_digest:
        return _bypassed("PROJECTION_DATABASE_DIGEST_MISMATCH")
    try:
        connection = _connect(database_path, read_only=True)
        try:
            _verify_database(connection)
            if not _metadata_matches(connection, state, authority):
                return _bypassed("PROJECTION_METADATA_STALE")
            expected_counts = (state.get("record_count"), state.get("edge_count"), state.get("search_count"))
            if _counts(connection) != expected_counts:
                return _bypassed("PROJECTION_COUNT_MISMATCH")
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, ProjectionError):
        return _bypassed("PROJECTION_DATABASE_INVALID")
    return ProjectionAdmission("Verified", "PROJECTION_FRESH", False, database_path)
