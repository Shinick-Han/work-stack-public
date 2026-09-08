"""Local search over allowlisted projections.

Entries are built once per Store generation and cached: search must not pay a
full document read per keystroke, and it must not observe a half-written store,
so the cache key is the generation the transaction sees. Only projected fields
are indexed - no capture source payload and no reply body ever enters an entry.
"""

from __future__ import annotations

from typing import Any, Iterable

from .service_composition import _query_search_backend
from .service_errors import DomainError
from .service_text import _reject_controls
from .storage.document_repository import WorkspaceDocument


class SearchProjectionMixin:
    """The cached search index and its ranked query projection."""

    @_query_search_backend
    def search_projection(self, query: str, limit: int = 30) -> dict[str, Any]:
        """Search allowlisted local projections without exposing source or reply payloads."""

        query, limit = self._validate_search_request(query, limit)
        needle = query.casefold()
        entries = self._cached_search_entries()
        candidates = [
            candidate
            for entry in entries
            if (candidate := self._search_candidate(entry, needle)) is not None
        ]
        candidates.sort(key=lambda candidate: candidate[:4])
        return {"query": query, "items": [candidate[4] for candidate in candidates[:limit]]}

    @staticmethod
    def _validate_search_request(query: str, limit: int) -> tuple[str, int]:
        if not isinstance(query, str):
            raise DomainError("search query must be a string")
        query = _reject_controls(query.strip(), "query", multiline=False)
        if not 2 <= len(query) <= 100:
            raise DomainError("search query must be between 2 and 100 characters")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainError("search limit must be between 1 and 50")
        return query, limit

    @staticmethod
    def _search_entry(
        kind: str,
        item_id: str,
        title: str,
        subtitle: str,
        searchable: Iterable[str],
        target_kind: str,
        target_id: str | None,
    ) -> dict[str, Any]:
        values = [str(value) for value in searchable if value is not None]
        return {
            "kind": kind,
            "id": item_id,
            "title": title,
            "subtitle": subtitle,
            "target_kind": target_kind,
            "target_id": target_id,
            "folded_title": title.casefold(),
            "folded_id": item_id.casefold(),
            "folded_values": tuple(value.casefold() for value in values),
        }

    def _task_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for task in self.list_tasks(status="all"):
            due = " · due {}".format(task["due"]) if task.get("due") else ""
            searchable = [task.get("detail", ""), *task.get("tags", []), *task.get("objective_ids", [])]
            searchable.extend(note.get("text", "") for note in task.get("notes", []))
            searchable.extend(subtask.get("title", "") for subtask in task.get("subtasks", []))
            entries.append(self._search_entry(
                "task",
                task["id"],
                task["title"],
                "{} · {}{}".format(task.get("status", "open"), task.get("priority", "P2"), due),
                searchable,
                "task",
                task["id"],
            ))
        return entries

    def _objective_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for objective in self.list_objectives(status="all"):
            key_results = objective.get("key_results", [])
            searchable = [result.get("text", "") for result in key_results]
            searchable.extend(result.get("target", "") for result in key_results)
            entries.append(self._search_entry(
                "objective",
                objective["id"],
                objective["objective"],
                "{} · {}".format(
                    objective.get("quarter", "No quarter"),
                    objective.get("status", "active"),
                ),
                searchable,
                "objective",
                objective["id"],
            ))
        return entries

    def _note_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for note in self.documents.load(WorkspaceDocument.NOTES).get("notes", []):
            text = str(note.get("text", ""))
            item_id = str(note.get("id", ""))
            entries.append(self._search_entry(
                "note",
                item_id,
                text[:100] or str(note.get("id", "Note")),
                "Graph note · {} links".format(len(note.get("links", []))),
                [text, *note.get("links", [])],
                "workspace",
                None,
            ))
        return entries

    def _capture_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for capture in self.documents.load(WorkspaceDocument.CAPTURES).get("captures", []):
            projected = self._project_capture(capture)
            source = projected.get("source", {})
            normalized = projected.get("normalized", {})
            searchable = [normalized.get("summary", ""), normalized.get("context", "")]
            searchable.extend(
                action.get("title", "") for action in normalized.get("action_items", [])
            )
            entries.append(self._search_entry(
                "capture",
                projected["id"],
                source.get("display_title", projected["id"]),
                "{} · {}".format(
                    source.get("provider", "manual"), projected.get("status", "inbox")
                ),
                searchable,
                "capture",
                projected["id"],
            ))
        return entries

    @staticmethod
    def _activity_target(event: dict[str, Any]) -> tuple[str, str | None]:
        if event.get("task_id"):
            return "task", event["task_id"]
        if event.get("capture_id"):
            return "capture", event["capture_id"]
        return "workspace", None

    def _activity_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for event in self.documents.load(WorkspaceDocument.ACTIVITY).get("activity", []):
            event_type = str(event.get("type", ""))
            details = event.get("details", {})
            details = details if isinstance(details, dict) else {}
            target_kind, target_id = self._activity_target(event)
            entries.append(self._search_entry(
                "activity",
                str(event.get("id", "")),
                event_type.replace(".", " ").strip().title() or "Activity",
                str(event.get("created_at", "")),
                [event_type, details.get("provider", ""), details.get("state", "")],
                target_kind,
                target_id if isinstance(target_id, str) else None,
            ))
        return entries

    def _build_search_entries(self) -> list[dict[str, Any]]:
        entries = self._task_search_entries()
        entries.extend(self._objective_search_entries())
        entries.extend(self._note_search_entries())
        entries.extend(self._capture_search_entries())
        entries.extend(self._activity_search_entries())
        return entries

    def _cached_search_entries(self) -> list[dict[str, Any]]:
        with self.store.transaction():
            generation = self.store.generation
            if self._search_index_generation != generation:
                self._search_entries = self._build_search_entries()
                self._search_index_generation = generation
            return self._search_entries

    @staticmethod
    def _search_score(entry: dict[str, Any], needle: str) -> int | None:
        if needle == entry["folded_id"]:
            return 0
        if entry["folded_title"].startswith(needle):
            return 1
        if needle in entry["folded_title"] or needle in entry["folded_id"]:
            return 2
        if any(needle in value for value in entry["folded_values"]):
            return 3
        return None

    @classmethod
    def _search_candidate(
        cls, entry: dict[str, Any], needle: str
    ) -> tuple[int, int, str, str, dict[str, Any]] | None:
        score = cls._search_score(entry, needle)
        if score is None:
            return None
        kind_order = {"task": 0, "objective": 1, "note": 2, "capture": 3, "activity": 4}
        item = {
            field: entry[field]
            for field in ("kind", "id", "title", "subtitle", "target_kind", "target_id")
        }
        return kind_order[entry["kind"]], score, entry["folded_title"], entry["id"], item
