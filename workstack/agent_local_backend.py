"""Exclusive-local v3 backend for the agent CLI."""

from __future__ import annotations

from workstack.agent_cli_contract import (
    AgentBackend,
    AuthorityAdmission,
    CheckpointRequest,
    ContextRequest,
    StatusRequest,
    StoreFactory,
)
from workstack.agent_context_pack import needs_planning_material
from workstack.service import WorkStack
from workstack.store import Store


__all__ = ["create_local_backend"]


_SYNC_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})
_SYNC_REQUIRED_REASON = "store_sync_required"
_COLLECTION_FORMATS = {3: "v3", 5: "v5", 6: "v6"}


class _LocalBackend:
    def __init__(
        self,
        *,
        admission: AuthorityAdmission,
        store: Store,
    ) -> None:
        self._admission = admission
        self._store = store
        self._stack = WorkStack(store)

    def _workspace_uid(self) -> str:
        workspace = self._store.load("workspace.json")
        actual = workspace.get("id")
        if actual != self._admission.workspace_uid:
            raise ValueError("workspace identity does not match")
        return actual

    def _sync_state(self) -> str:
        status = self._store.sync_status()
        if type(status) is not dict:
            raise ValueError("sync status is invalid")
        state = status.get("state")
        if type(state) is not str or state not in _SYNC_STATES:
            raise ValueError("sync status is invalid")
        return state

    def _require_in_sync(self) -> None:
        if self._sync_state() != "in-sync":
            raise ValueError(_SYNC_REQUIRED_REASON)

    def status(self, *, request: StatusRequest) -> dict[str, object]:
        if request.expected_workspace_uid != self._admission.workspace_uid:
            raise ValueError("workspace identity does not match")
        with self._store.transaction():
            in_sync = self._sync_state() == "in-sync"
            if in_sync:
                actual_uid = self._workspace_uid()
                metadata = self._store.load("store-meta.json")
                storage_format = _COLLECTION_FORMATS.get(metadata.get("store_schema_version"))
                if storage_format is None:
                    raise ValueError("local storage format is not supported")
            else:
                # Authority admission already read the current canonical UID and
                # label before Store construction. A stale manifest makes
                # Store.load fail closed, so status must not use it merely to
                # report that synchronization is required. After initialize or
                # migration the readable metadata label takes precedence.
                actual_uid = self._admission.workspace_uid
                storage_format = self._admission.storage_format
            return {
                "actual_workspace_uid": actual_uid,
                "capability_reason": None if in_sync else _SYNC_REQUIRED_REASON,
                "capability_supported": True,
                "contract": "workstack.cli.v1",
                "data_dir_available": True,
                "exclusive_local_available": True,
                "expected_workspace_uid": self._admission.workspace_uid,
                "ready": in_sync,
                "running_server_available": False,
                "storage_format": storage_format,
            }

    def _planning_material(self, task_id: str) -> dict[str, object]:
        """Extra planning read material, inside the transaction already held.

        Both reads are the existing product projections. Store.transaction is
        depth-counted, so they join the caller's transaction instead of taking a
        second lease, and the Task, its Objectives and its relationships are read
        from one consistent view.
        """

        detail = self._stack.task_detail(task_id)
        workspace = self._stack.workspace_projection()
        if type(detail) is not dict or type(workspace) is not dict:
            raise ValueError("planning projection is invalid")
        context = detail.get("context")
        objectives = workspace.get("objectives")
        tasks = workspace.get("tasks")
        if (
            type(context) is not list
            or type(objectives) is not list
            or type(tasks) is not list
        ):
            raise ValueError("planning projection is invalid")
        return {"context": context, "objectives": objectives, "tasks": tasks}

    def context(self, *, request: ContextRequest, today: object) -> dict[str, object]:
        with self._store.transaction():
            self._require_in_sync()
            actual_uid = self._workspace_uid()
            task = self._stack.get_task(request.task_id)
            # The SHARED active projection, not the physical document: a
            # superseded checkpoint must not reappear in Agent context. The
            # transaction is already held by the caller above.
            worklog = self._stack.active_worklog_view()
            days = worklog.get("days")
            if type(days) is not dict:
                raise ValueError("worklog authority is invalid")
            date_type = type(today)
            try:
                newest = today.isoformat()
                oldest = date_type.fromordinal(today.toordinal() - 30).isoformat()
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("context date is invalid") from error
            entries: list[dict[str, object]] = []
            for date, day in days.items():
                if type(date) is not str or type(day) is not dict:
                    raise ValueError("worklog authority is invalid")
                try:
                    canonical_date = date_type.fromisoformat(date).isoformat()
                except (AttributeError, TypeError, ValueError):
                    continue
                if canonical_date != date or not oldest <= date <= newest:
                    continue
                raw_entries = day.get("entries", [])
                if type(raw_entries) is not list:
                    raise ValueError("worklog authority is invalid")
                for entry in raw_entries:
                    if type(entry) is not dict:
                        raise ValueError("worklog authority is invalid")
                    if entry.get("task_id") != request.task_id:
                        continue
                    entries.append(
                        {
                            "blockers": entry.get("blockers"),
                            "date": date,
                            "done": entry.get("done"),
                            "next": entry.get("next"),
                            "task_id": request.task_id,
                        }
                    )
            result: dict[str, object] = {
                "entries": entries,
                "task": task,
                "transport": "exclusive-local",
                "workspace_uid": actual_uid,
            }
            if needs_planning_material(request.view):
                result["planning"] = self._planning_material(request.task_id)
            return result

    def checkpoint(self, *, request: CheckpointRequest) -> dict[str, object]:
        with self._store.transaction():
            self._require_in_sync()
            actual_uid = self._workspace_uid()
            result = self._stack.add_worklog_v1(
                {
                    "blockers": request.blockers,
                    "date": request.date,
                    "done": request.done,
                    "next": request.next,
                    "task_id": request.task_id,
                },
                request.intent_id,
                path="/api/v1/review/entries",
            )
            if type(result) is not dict or result.get("status") not in {200, 201}:
                raise ValueError("checkpoint result is invalid")
            body = result.get("body")
            if type(body) is not dict:
                raise ValueError("checkpoint result is invalid")
            entry = body.get("data")
            meta = body.get("meta")
            if (
                type(entry) is not dict
                or type(meta) is not dict
                or type(meta.get("replayed")) is not bool
            ):
                raise ValueError("checkpoint result is invalid")
            return {
                "commit_state": "committed",
                "entry": entry,
                "replayed": meta["replayed"],
                "transport": "exclusive-local",
                "workspace_uid": actual_uid,
            }


def create_local_backend(
    *,
    admission: AuthorityAdmission,
    store_factory: StoreFactory,
) -> AgentBackend:
    if not admission.data_dir.is_dir():
        raise ValueError("admitted authority is no longer available")
    store = store_factory(root=admission.data_dir)
    return _LocalBackend(admission=admission, store=store)
