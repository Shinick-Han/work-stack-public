from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from workstack.agent_cli_contract import (
    AuthorityAdmission,
    CheckpointRequest,
    ContextRequest,
    StatusRequest,
)
from workstack.agent_local_backend import create_local_backend
from workstack.service import IdempotencyConflictError, WorkStack
from workstack.store import LOCK_NAME, Store


WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"


class ObservedStore(Store):
    """The real v3 Store with observable public transaction/load boundaries."""

    def __init__(self, root: Path) -> None:
        self.events: list[tuple[object, ...]] = []
        self.observed_depth = 0
        self.workspace_uid_override: str | None = None
        super().__init__(root)

    @contextmanager
    def transaction(self):
        self.events.append(("transaction-enter", self.observed_depth, self.root / LOCK_NAME))
        with super().transaction():
            self.observed_depth += 1
            try:
                yield
            finally:
                self.observed_depth -= 1
                self.events.append(("transaction-exit", self.observed_depth))

    def load(self, name: str):
        self.events.append(("load", name, self.observed_depth))
        if name == "workspace.json" and self.workspace_uid_override is not None:
            return {"version": 2, "id": self.workspace_uid_override, "name": "Changed"}
        return super().load(name)


class AgentLocalBackendContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "admitted authority"
        self.root.mkdir()
        self.store = ObservedStore(self.root)
        self.stack = WorkStack(self.store)
        workspace = self.store.load("workspace.json")
        workspace["id"] = WORKSPACE_UID
        self.store.save("workspace.json", workspace)
        self.task = self.stack.add_task("Primary", detail="raw Task detail")
        self.other_task = self.stack.add_task("Unrelated")
        self.admission = AuthorityAdmission(
            data_dir=self.root,
            workspace_uid=WORKSPACE_UID,
        )
        self.factory_calls: list[Path] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def factory(self, *, root: Path) -> Store:
        self.factory_calls.append(root)
        return self.store

    def backend(self):
        return create_local_backend(admission=self.admission, store_factory=self.factory)

    def checkpoint_request(
        self,
        *,
        intent_id: str = "checkpoint.intent.0001",
        done: list[str] | None = None,
    ) -> CheckpointRequest:
        return CheckpointRequest(
            task_id=self.task["id"],
            date="2026-09-02",
            done=["implemented"] if done is None else done,
            next=["verify"],
            blockers=[],
            intent_id=intent_id,
        )

    def add_entry(
        self,
        *,
        task_id: str,
        date: str,
        marker: str,
        intent_id: str,
    ) -> None:
        self.stack.add_worklog_v1(
            {
                "date": date,
                "task_id": task_id,
                "done": [marker],
                "next": [],
                "blockers": [],
            },
            intent_id,
        )

    def test_factory_uses_the_admitted_root_once_and_not_after_disappearance(self) -> None:
        backend = self.backend()
        self.assertIsNotNone(backend)
        self.assertEqual(self.factory_calls, [self.root])

        missing = self.root.parent / "authority that disappeared"
        calls: list[Path] = []

        def forbidden_factory(*, root: Path) -> Store:
            calls.append(root)
            raise AssertionError("StoreFactory must not run for a disappeared authority")

        with self.assertRaises(ValueError):
            create_local_backend(
                admission=AuthorityAdmission(
                    data_dir=missing,
                    workspace_uid=WORKSPACE_UID,
                ),
                store_factory=forbidden_factory,
            )
        self.assertEqual(calls, [])

    def test_status_is_the_exact_exclusive_local_v3_raw_mapping(self) -> None:
        result = self.backend().status(
            request=StatusRequest(
                data_dir=self.root,
                expected_workspace_uid=WORKSPACE_UID,
            )
        )
        self.assertEqual(
            result,
            {
                "actual_workspace_uid": WORKSPACE_UID,
                "capability_reason": None,
                "capability_supported": True,
                "contract": "workstack.cli.v1",
                "data_dir_available": True,
                "exclusive_local_available": True,
                "expected_workspace_uid": WORKSPACE_UID,
                "ready": True,
                "running_server_available": False,
                "storage_format": "v3",
            },
        )

    def test_manifest_workspace_mismatch_is_not_ready_and_blocks_content_and_write(self) -> None:
        workspace = self.store.load("workspace.json")
        workspace["id"] = OTHER_UID
        self.store.path("workspace.json").write_text(
            json.dumps(workspace, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        admission = AuthorityAdmission(data_dir=self.root, workspace_uid=OTHER_UID)
        backend = create_local_backend(
            admission=admission,
            store_factory=lambda *, root: self.store,
        )

        status = backend.status(
            request=StatusRequest(
                data_dir=self.root,
                expected_workspace_uid=OTHER_UID,
            )
        )
        self.assertEqual(status["actual_workspace_uid"], OTHER_UID)
        self.assertTrue(status["capability_supported"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["capability_reason"], "store_sync_required")

        for invoke in (
            lambda: backend.context(
                request=ContextRequest(task_id=self.task["id"]),
                today=dt.date(2026, 9, 2),
            ),
            lambda: backend.checkpoint(request=self.checkpoint_request()),
        ):
            with self.assertRaisesRegex(ValueError, "^store_sync_required$"):
                invoke()

    def test_malformed_local_sync_status_fails_closed_without_raw_details(self) -> None:
        backend = self.backend()
        with mock.patch.object(
            self.store,
            "sync_status",
            return_value={"state": "future-state", "reason": "raw-path-canary"},
        ):
            with self.assertRaises(ValueError) as raised:
                backend.status(
                    request=StatusRequest(
                        data_dir=self.root,
                        expected_workspace_uid=WORKSPACE_UID,
                    )
                )
        self.assertEqual(str(raised.exception), "sync status is invalid")
        self.assertNotIn("raw-path-canary", str(raised.exception))

    def test_context_returns_only_raw_task_linked_entries_in_the_31_day_window(self) -> None:
        self.add_entry(
            task_id=self.task["id"],
            date="2026-09-02",
            marker="today",
            intent_id="context.today.0001",
        )
        self.add_entry(
            task_id=self.task["id"],
            date="2026-08-03",
            marker="cutoff",
            intent_id="context.cutoff.0001",
        )
        self.add_entry(
            task_id=self.task["id"],
            date="2026-08-02",
            marker="too old",
            intent_id="context.old.0001",
        )
        self.add_entry(
            task_id=self.other_task["id"],
            date="2026-09-01",
            marker="other task",
            intent_id="context.other.0001",
        )
        expected_task = self.stack.get_task(self.task["id"])
        self.store.events.clear()

        result = self.backend().context(
            request=ContextRequest(task_id=self.task["id"]),
            today=dt.date(2026, 9, 2),
        )

        self.assertEqual(result["workspace_uid"], WORKSPACE_UID)
        self.assertEqual(result["transport"], "exclusive-local")
        self.assertEqual(result["task"], expected_task)
        entries = result["entries"]
        self.assertEqual({entry["date"] for entry in entries}, {"2026-09-02", "2026-08-03"})
        self.assertEqual(
            {entry["done"][0] for entry in entries},
            {"today", "cutoff"},
        )
        for entry in entries:
            self.assertEqual(
                set(entry),
                {"blockers", "date", "done", "next", "task_id"},
            )
            self.assertEqual(entry["task_id"], self.task["id"])

        explicit_loads = [event[1] for event in self.store.events if event[0] == "load"]
        self.assertTrue(set(explicit_loads).issubset(
            {"workspace.json", "backlog.json", "activity.json", "worklog.json"}
        ))
        self.assertNotIn("captures.json", explicit_loads)
        self.assertNotIn("okr.json", explicit_loads)
        self.assertTrue(
            all(event[2] > 0 for event in self.store.events if event[0] == "load"),
            self.store.events,
        )

    def test_uid_is_reread_inside_transaction_before_content_or_mutation(self) -> None:
        backend = self.backend()
        self.store.workspace_uid_override = OTHER_UID

        for invoke in (
            lambda: backend.context(
                request=ContextRequest(task_id=self.task["id"]),
                today=dt.date(2026, 9, 2),
            ),
            lambda: backend.checkpoint(request=self.checkpoint_request()),
        ):
            self.store.events.clear()
            with self.assertRaises(ValueError):
                invoke()
            loads = [event for event in self.store.events if event[0] == "load"]
            self.assertEqual(loads, [("load", "workspace.json", 1)])

        persisted = self.store.load("worklog.json")
        self.assertEqual(persisted.get("days", {}), {})

    def test_checkpoint_passes_exact_body_key_and_path_inside_legacy_transaction(self) -> None:
        calls: list[tuple[dict[str, object], str, str, int]] = []

        class RecordingStack:
            def __init__(inner_self, store: ObservedStore) -> None:
                inner_self.store = store

            def add_worklog_v1(
                inner_self,
                body: dict[str, object],
                intent_id: str,
                *,
                path: str,
            ) -> dict[str, object]:
                calls.append((body, intent_id, path, inner_self.store.observed_depth))
                return {
                    "status": 201,
                    "body": {
                        "data": {"task_id": body["task_id"], "task": "Primary", **body},
                        "meta": {"replayed": False},
                    },
                }

        request = self.checkpoint_request()
        with mock.patch("workstack.agent_local_backend.WorkStack", RecordingStack):
            result = self.backend().checkpoint(request=request)

        self.assertEqual(len(calls), 1)
        body, intent_id, path, depth = calls[0]
        self.assertEqual(
            body,
            {
                "date": request.date,
                "task_id": request.task_id,
                "done": request.done,
                "next": request.next,
                "blockers": request.blockers,
            },
        )
        self.assertNotIn("workspace_uid", body)
        self.assertIs(body["done"], request.done)
        self.assertEqual(intent_id, request.intent_id)
        self.assertEqual(path, "/api/v1/review/entries")
        self.assertGreater(depth, 0)
        self.assertEqual(result["commit_state"], "committed")
        self.assertFalse(result["replayed"])

    def test_checkpoint_commit_replay_conflict_and_restart_use_current_service(self) -> None:
        backend = self.backend()
        request = self.checkpoint_request()
        first = backend.checkpoint(request=request)
        replay = backend.checkpoint(request=request)
        self.assertEqual(first["entry"], replay["entry"])
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])

        changed = self.checkpoint_request(done=["different body"])
        with self.assertRaises(IdempotencyConflictError):
            backend.checkpoint(request=changed)

        reopened_store = ObservedStore(self.root)
        reopened = create_local_backend(
            admission=self.admission,
            store_factory=lambda *, root: reopened_store,
        )
        after_restart = reopened.checkpoint(request=request)
        self.assertTrue(after_restart["replayed"])
        days = reopened_store.load("worklog.json")["days"]
        matching = [
            entry
            for entry in days[request.date]["entries"]
            if entry["task_id"] == request.task_id
        ]
        self.assertEqual(len(matching), 1)

    def test_source_uses_only_the_frozen_contract_and_legacy_service_store_layers(self) -> None:
        import workstack.agent_local_backend as module

        tree = ast.parse(inspect.getsource(module))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
        workstack_imports = {name for name in imported_modules if name.startswith("workstack")}
        self.assertEqual(
            workstack_imports,
            {"workstack.agent_cli_contract", "workstack.service", "workstack.store"},
        )
        source = inspect.getsource(module)
        self.assertNotIn("workstack.storage", source)
        self.assertNotIn("server_lease", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("urllib", source)
        signature = inspect.signature(module.create_local_backend)
        self.assertEqual(list(signature.parameters), ["admission", "store_factory"])
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
