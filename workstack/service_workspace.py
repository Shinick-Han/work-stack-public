"""Workspace-wide reads: graph notes, the projection, the snapshot, status.

The graph snapshot is an ACTIVE reader - its day counts and Worklog edges use
the same validated membership as review and the Agent readers - while the
physical day itself is retained, so a day whose entries are all superseded
still appears with a zero count.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from . import REMOTE_PROTOCOL_VERSION, __version__
from .context_projection import group_context_by_task
from .maintenance import BackupDownload, create_backup_download
from .service_composition import (
    _optional_command_backend,
    _query_graph_backend,
    _transactional,
)
from .service_domain import _next_id, _required_text
from .service_graph import (
    _append_snapshot_notes,
    _append_snapshot_task,
    _append_snapshot_worklog,
    _snapshot_objective_node,
)
from .storage.document_repository import WorkspaceDocument


class WorkspaceProjectionMixin:
    """Graph notes, storage status, the workspace projection and the snapshot."""

    @_transactional
    def add_note(self, text: str, links: Iterable[str] = ()) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.NOTES)
        note = {
            "id": _next_id(data["notes"], "N", 4),
            "text": _required_text(text, "text"),
            "links": sorted(set(str(link).strip().upper() for link in links if str(link).strip())),
            "created": self._today(),
        }
        data["notes"].append(note)
        self.documents.save(WorkspaceDocument.NOTES, data)
        return note

    @_optional_command_backend("intent_commands", "create_note", "intent")
    @_transactional
    def create_note_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/notes",
    ) -> dict[str, Any]:
        """Create one graph note for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.NOTES)
        note = {
            "id": _next_id(data["notes"], "N", 4),
            "text": _required_text(body["text"], "text"),
            "links": sorted(
                set(str(link).strip().upper() for link in body["links"] if str(link).strip())
            ),
            "created": self._today(),
        }
        data["notes"].append(note)
        response_body = {"data": copy.deepcopy(note), "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.NOTES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="graph-note-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    def storage_status(self) -> dict[str, Any]:
        """Return content-free readiness metadata for the local planning store."""

        with self.store.consistent_read() as readiness:
            total_bytes = self.documents.total_bytes()
            return {
                "workspace_id": readiness.workspace_uid,
                "store_schema_version": readiness.schema_version,
                "product_version": __version__,
                "remote_protocol_version": REMOTE_PROTOCOL_VERSION,
                "file_count": len(WorkspaceDocument),
                "total_bytes": total_bytes,
                "backup_format": "workstack-backup-v1",
                "restore_requires_shutdown": True,
            }

    def create_backup_download(self) -> BackupDownload:
        """Create a read-only archive while this server owns the store lease."""

        return create_backup_download(self.store)

    @_query_graph_backend
    def workspace_projection(self) -> dict[str, Any]:
        with self.store.transaction():
            workspace = self.documents.load(WorkspaceDocument.WORKSPACE)
            captures = self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])
            notes = self.documents.load(WorkspaceDocument.NOTES).get("notes", [])
            objectives = self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])
            source_tasks = self.list_tasks(status="all")
            contexts = group_context_by_task(
                notes, (self._project_capture(capture) for capture in captures),
                (task["id"] for task in source_tasks),
                (objective["id"] for objective in objectives),
            )
            tasks = [
                self._project_task(task, len(contexts[task["id"]]))
                for task in source_tasks
            ]
            snapshot = self.snapshot()
            return {
                "schema_version": "1.0",
                "workspace": {"id": workspace["id"], "name": workspace.get("name", "Work Stack")},
                "tasks": tasks,
                "objectives": [
                    self._project_objective(objective)
                    for objective in objectives
                ],
                "notes": copy.deepcopy(notes),
                "edges": snapshot["edges"],
                "inbox_count": sum(1 for capture in captures if capture.get("status") == "inbox"),
            }

    @_transactional
    def snapshot(self) -> dict[str, Any]:
        objectives = self.list_objectives(status="all")
        tasks = self.list_tasks(status="all")
        # The Graph is an ACTIVE reader: its day counts and Worklog edges use
        # the same validated membership as Review, weekly and the Agent
        # readers. The physical day itself is retained, so a day whose entries
        # are all superseded still appears with a zero count.
        worklog = self._active_worklog().get("days", {})
        notes = self.documents.load(WorkspaceDocument.NOTES).get("notes", [])
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        known: set[str] = set()

        for objective in objectives:
            node = _snapshot_objective_node(objective)
            nodes.append(node)
            known.add(node["id"])
        for task in tasks:
            _append_snapshot_task(task, nodes, edges, known)
        _append_snapshot_worklog(worklog, nodes, edges, known)
        _append_snapshot_notes(notes, nodes, edges, known)
        edges = [edge for edge in edges if edge["source"] in known and edge["target"] in known]
        return {
            "generated_at": self._generated_at(),
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "objectives": len(objectives),
                "tasks": len(tasks),
                "days": len(worklog),
                "notes": len(notes),
            },
        }
