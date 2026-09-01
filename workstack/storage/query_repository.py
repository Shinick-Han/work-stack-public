"""Backend-neutral search and graph queries with verified projection fallback."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .projection import (
    ProjectionAdmission,
    ProjectionAuthority,
    _search_rows,
    admit_projection,
)
from .read_repository import WorkspaceReadResult, WorkspaceRepository
from .semantic import WorkspaceSnapshot


class WorkspaceQueryError(ValueError):
    command_boundary = "query"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, order=True)
class SearchHit:
    rank: int
    kind: str
    item_id: str
    title: str
    subtitle: str
    target_kind: str
    target_id: str | None

    def to_released_item(self) -> dict[str, str | None]:
        """Return the public item shape frozen by ``WorkStack.search_projection``."""

        return {
            "kind": self.kind,
            "id": self.item_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
        }


@dataclass(frozen=True)
class SearchQueryResult:
    hits: tuple[SearchHit, ...]
    read_source: str
    projection_reason: str
    query: str

    def to_released_projection(self) -> dict[str, object]:
        return {
            "query": self.query,
            "items": [hit.to_released_item() for hit in self.hits],
        }


@dataclass(frozen=True)
class GraphQueryResult:
    edges: tuple[tuple[str, str, str], ...]
    read_source: str
    projection_reason: str


@runtime_checkable
class WorkspaceQueryContract(Protocol):
    def search(self, query: str, *, limit: int = 50) -> SearchQueryResult: ...

    def graph(self) -> GraphQueryResult: ...


def _authority(read: WorkspaceReadResult) -> ProjectionAuthority:
    stamp = read.stamp
    return ProjectionAuthority(
        workspace_uid=stamp.workspace_uid,
        format_version=stamp.format_version,
        generation=stamp.generation,
        manifest_digest=stamp.authority_manifest_digest,
        semantic_digest=stamp.snapshot_digest,
    )


def _canonical_search(
    snapshot: WorkspaceSnapshot, folded_query: str, limit: int
) -> tuple[SearchHit, ...]:
    documents, terms = _released_search_rows(snapshot)
    searchable: dict[tuple[str, str], list[str]] = {}
    for kind, item_id, _ordinal, value in terms:
        searchable.setdefault((kind, item_id), []).append(value)
    order = {"task": 0, "objective": 1, "note": 2, "capture": 3, "activity": 4}
    candidates = []
    for kind, item_id, title, subtitle, target_kind, target_id, folded_title, folded_id in documents:
        rank = _search_rank(
            folded_query, folded_id, folded_title,
            searchable.get((kind, item_id), []),
        )
        if rank is not None:
            candidates.append((
                order[kind], rank, folded_title, item_id, kind,
                title, subtitle, target_kind, target_id,
            ))
    candidates.sort()
    return tuple(
        SearchHit(rank, kind, item_id, title, subtitle, target_kind, target_id)
        for (
            _kind_order, rank, _folded_title, item_id, kind,
            title, subtitle, target_kind, target_id,
        ) in candidates[:limit]
    )


def _released_search_rows(
    snapshot: WorkspaceSnapshot,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Use the frozen search corpus with released planning-status overlays."""

    value = snapshot.to_dict()
    documents, terms = _search_rows(value)
    tasks = {str(task["id"]): task for task in value["tasks"]}
    planning = value["planning_status"]
    released = []
    for row in documents:
        kind, item_id, title, subtitle, target_kind, target_id, folded_title, folded_id = row
        if kind == "task":
            task = tasks[str(item_id)]
            due = f" · due {task['due']}" if task.get("due") else ""
            subtitle = f"{planning[str(item_id)]} · {task.get('priority', 'P2')}{due}"
        released.append((
            kind, item_id, title, subtitle, target_kind, target_id,
            folded_title, folded_id,
        ))
    return released, terms


def _search_shapes(snapshot: WorkspaceSnapshot) -> dict[tuple[str, str], SearchHit]:
    documents, _terms = _released_search_rows(snapshot)
    return {
        (str(kind), str(item_id)): SearchHit(
            0, str(kind), str(item_id), str(title), str(subtitle),
            str(target_kind), None if target_id is None else str(target_id),
        )
        for kind, item_id, title, subtitle, target_kind, target_id, _folded_title, _folded_id
        in documents
    }


def _ranked_hit(rank: int, shape: SearchHit) -> SearchHit:
    return SearchHit(
        rank, shape.kind, shape.item_id, shape.title, shape.subtitle,
        shape.target_kind, shape.target_id,
    )


def _search_rank(
    needle: str, folded_id: str, folded_title: str, values: list[str]
) -> int | None:
    if needle == folded_id:
        return 0
    if folded_title.startswith(needle):
        return 1
    if needle in folded_title or needle in folded_id:
        return 2
    if any(needle in value for value in values):
        return 3
    return None


def _projection_search(
    path: Path,
    folded_query: str,
    limit: int,
    shapes: Mapping[tuple[str, str], SearchHit],
) -> tuple[SearchHit, ...]:
    escaped = folded_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    prefix = f"{escaped}%"
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT d.kind, d.item_id, "
            "CASE WHEN d.folded_id = ? THEN 0 "
            "WHEN d.folded_title LIKE ? ESCAPE '\\' THEN 1 "
            "WHEN d.folded_title LIKE ? ESCAPE '\\' OR d.folded_id LIKE ? ESCAPE '\\' THEN 2 "
            "ELSE 3 END AS rank "
            "FROM search_document AS d WHERE d.folded_id = ? "
            "OR d.folded_title LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM search_term AS t WHERE t.kind = d.kind "
            "AND t.item_id = d.item_id AND t.folded_value LIKE ? ESCAPE '\\') "
            "ORDER BY CASE d.kind WHEN 'task' THEN 0 WHEN 'objective' THEN 1 "
            "WHEN 'note' THEN 2 WHEN 'capture' THEN 3 ELSE 4 END, "
            "rank, d.folded_title, d.item_id LIMIT ?",
            (
                folded_query, prefix, pattern, pattern, folded_query,
                pattern, pattern, limit,
            ),
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        _ranked_hit(int(rank), shapes[(str(kind), str(item_id))])
        for kind, item_id, rank in rows
    )


def _projection_graph(path: Path) -> tuple[tuple[str, str, str], ...]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT e.kind, source.display_id, target.display_id "
            "FROM graph_edge AS e "
            "JOIN record_index AS source ON source.record_uid = e.source_uid "
            "JOIN record_index AS target ON target.record_uid = e.target_uid "
            "ORDER BY e.kind, source.display_id, target.display_id"
        ).fetchall()
    finally:
        connection.close()
    return tuple(sorted(
        ("note" if kind == "reference" else str(kind), str(source), str(target))
        for kind, source, target in rows
    ))


def _released_graph_edges(snapshot: WorkspaceSnapshot) -> tuple[tuple[str, str, str], ...]:
    """Reproduce the relationship slice returned by released ``snapshot`` views."""

    value = snapshot.to_dict()
    known = {
        str(item["id"])
        for collection in (value["objectives"], value["tasks"], value["notes"])
        for item in collection
    }
    known.update(
        f"{task['id']}-{subtask['id']}"
        for task in value["tasks"]
        for subtask in task.get("subtasks", [])
    )
    known.update(f"D-{date}" for date in value["worklog_days"])
    edges = (*_planning_edges(value), *_supplemental_edges(value))
    return tuple(sorted(edge for edge in edges if edge[1] in known and edge[2] in known))


def _planning_edges(value: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    edges: list[tuple[str, str, str]] = []
    for task in value["tasks"]:
        task_id = str(task["id"])
        if task.get("parent_id"):
            edges.append(("parent", task_id, str(task["parent_id"])))
        edges.extend(("dependency", task_id, str(item)) for item in task.get("dependencies", []))
        edges.extend(("objective", task_id, str(item)) for item in task.get("objective_ids", []))
    for note in value["notes"]:
        edges.extend(("note", str(note["id"]), str(item)) for item in note.get("links", []))
    return edges


def _supplemental_edges(value: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    task_ids = {str(task["id"]) for task in value["tasks"]}
    edges = [
        ("parent", f"{task['id']}-{subtask['id']}", str(task["id"]))
        for task in value["tasks"]
        for subtask in task.get("subtasks", [])
    ]
    for date, day in value["worklog_days"].items():
        edges.extend(
            ("worklog", f"D-{date}", str(entry["task_id"]))
            for entry in day.get("entries", [])
            if str(entry.get("task_id", "")) in task_ids
        )
    return edges


class WorkspaceQueryRepository:
    """Read canonical authority once; admit SQLite only for that exact stamp."""

    def __init__(self, repository: WorkspaceRepository, projection_root: Path | str) -> None:
        self.repository = repository
        self.projection_root = Path(projection_root).expanduser().resolve(strict=False)

    @staticmethod
    def _fallback_search(
        read: WorkspaceReadResult, admission: ProjectionAdmission,
        query: str, folded_query: str, limit: int,
    ) -> SearchQueryResult:
        return SearchQueryResult(
            _canonical_search(read.snapshot, folded_query, limit),
            "canonical", admission.reason, query,
        )

    def search(self, query: str, *, limit: int = 50) -> SearchQueryResult:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
            raise WorkspaceQueryError("SEARCH_QUERY_REQUIRED")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise WorkspaceQueryError("SEARCH_LIMIT_INVALID")
        read = self.repository.read()
        admission = admit_projection(self.projection_root, _authority(read))
        normalized = query.strip()
        folded = normalized.casefold()
        if not admission.verified or admission.database_path is None:
            return self._fallback_search(read, admission, normalized, folded, limit)
        try:
            hits = _projection_search(
                admission.database_path, folded, limit, _search_shapes(read.snapshot)
            )
        except (KeyError, OSError, sqlite3.DatabaseError):
            return self._fallback_search(read, admission, normalized, folded, limit)
        return SearchQueryResult(hits, "projection", admission.reason, normalized)

    @staticmethod
    def _fallback_graph(
        read: WorkspaceReadResult, admission: ProjectionAdmission
    ) -> GraphQueryResult:
        return GraphQueryResult(
            _released_graph_edges(read.snapshot), "canonical", admission.reason
        )

    def graph(self) -> GraphQueryResult:
        read = self.repository.read()
        admission = admit_projection(self.projection_root, _authority(read))
        if not admission.verified or admission.database_path is None:
            return self._fallback_graph(read, admission)
        try:
            projected = _projection_graph(admission.database_path)
            supplemental = _supplemental_edges(read.snapshot.to_dict())
            edges = tuple(sorted((*projected, *supplemental)))
        except (OSError, sqlite3.DatabaseError):
            return self._fallback_graph(read, admission)
        return GraphQueryResult(edges, "projection", admission.reason)
