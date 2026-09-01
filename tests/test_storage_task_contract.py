from __future__ import annotations

import copy
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from unittest import mock

from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_repository import V4TaskRepository


class SimulatedTaskCommitInterruption(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskState:
    tasks: tuple[dict[str, Any], ...]
    activity: tuple[dict[str, Any], ...]
    planning: tuple[dict[str, Any], ...]
    idempotency: tuple[dict[str, Any], ...]


class TaskContractBackend(Protocol):
    def create(self, body: dict[str, Any], key: str) -> dict[str, Any]: ...
    def patch(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def state(self) -> TaskState: ...
    def interrupt_next_commit(self) -> None: ...
    def restart(self) -> None: ...


class InterruptingStore(Store):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.interrupt = False

    def _atomic_write_locked(self, path: Path, value: Any) -> None:
        super()._atomic_write_locked(path, value)
        if self.interrupt and path.name in DEFAULTS:
            self.interrupt = False
            raise SimulatedTaskCommitInterruption("SIMULATED_TASK_COMMIT_INTERRUPTION")


class V3TaskBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = InterruptingStore(root)
        self.stack = WorkStack(self.store)

    def create(self, body: dict[str, Any], key: str) -> dict[str, Any]:
        return self.stack.create_task_v1(body, key)

    def patch(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.stack.patch_task(task_id, body)

    def state(self) -> TaskState:
        activity = self.store.load("activity.json")
        return TaskState(
            tuple(self.store.load("backlog.json")["tasks"]),
            tuple(activity["activity"]), tuple(activity["planning_status"]),
            tuple(activity["idempotency"]),
        )

    def interrupt_next_commit(self) -> None:
        self.store.interrupt = True

    def restart(self) -> None:
        self.store = InterruptingStore(self.root)
        self.stack = WorkStack(self.store)


def _write_conversion(root: Path, conversion: Any) -> None:
    def write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write(root / "store.json", canonical_json_bytes(dict(conversion.store)))
    write(root / "workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(root / "records" / kind / uid[:2] / f"{uid}.json", canonical_json_bytes(dict(record)))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(dict(event))
    for (kind, segment), events in grouped.items():
        write(root / "streams" / kind / f"{segment}.ndjson", b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda value: value["sequence"])
        ))


class V4TaskBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        source = root.parent / "task-bootstrap-v3"
        store = Store(source)
        WorkStack(store)
        documents = {name: store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at="2026-09-01T00:00:00Z"
        )
        _write_conversion(root, self.conversion)
        self.runtime = resolve_runtime_authority(
            root, Path(os.environ["WORK_STACK_RUNTIME"]),
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(root), generation=0), expected_digest=None,
        )
        self.tick = 0
        self.repository = self._repository()

    def _clock(self) -> str:
        self.tick += 1
        return f"2026-09-02T00:20:{self.tick:02d}Z"

    def _repository(self) -> V4TaskRepository:
        return V4TaskRepository(
            self.root, self.runtime,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            clock=self._clock,
            enable_v4_task_commands=True,
        )

    def create(self, body: dict[str, Any], key: str) -> dict[str, Any]:
        return self.repository.create_task_v1(body, key)

    def patch(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.repository.patch_task(task_id, body)

    def state(self) -> TaskState:
        documents = self.repository.state_documents()
        activity = documents["activity.json"]
        return TaskState(
            tuple(documents["backlog.json"]["tasks"]),
            tuple(activity["activity"]), tuple(activity["planning_status"]),
            tuple(activity["idempotency"]),
        )

    def interrupt_next_commit(self) -> None:
        fired = False

        def fail(transition: str) -> None:
            nonlocal fired
            if not fired and transition.startswith("target_replaced:authority:"):
                fired = True
                raise SimulatedTaskCommitInterruption("SIMULATED_TASK_COMMIT_INTERRUPTION")

        self.repository.fault_hook = fail

    def restart(self) -> None:
        self.repository = self._repository()


class TaskContractCases:
    backend: TaskContractBackend

    def make_backend(self, root: Path) -> TaskContractBackend:
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(Path(self.temporary.name) / "runtime")}
        )
        self.runtime_environment.start()
        self.backend = self.make_backend(Path(self.temporary.name) / "authority")

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.temporary.cleanup()
        super().tearDown()  # type: ignore[misc]

    @staticmethod
    def body() -> dict[str, Any]:
        return {
            "title": "  Contract Task  ", "detail": "  Frozen detail  ",
            "priority": "P1", "due": "2030-01-02", "scheduled": "2030-01-01",
            "estimate_minutes": 45,
        }

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(Exception) as raised:  # type: ignore[attr-defined]
            action()
        self.assertEqual(getattr(raised.exception, "code", None), code)  # type: ignore[attr-defined]

    def test_create_shape_planning_and_idempotency_are_frozen(self) -> None:
        result = self.backend.create(self.body(), "task.create.contract.0001")
        self.assertEqual(result["status"], 201)
        self.assertFalse(result["body"]["meta"]["replayed"])
        task = result["body"]["data"]
        self.assertEqual(task["title"], "Contract Task")
        self.assertEqual(task["detail"], "Frozen detail")
        self.assertEqual(task["revision"], 0)
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["context_count"], 0)
        state = self.backend.state()
        self.assertEqual(len(state.tasks), 1)
        self.assertEqual(len(state.planning), 1)
        self.assertEqual(state.planning[0]["provenance"], "api.v1")
        self.assertEqual(state.planning[0]["new_revision"], 0)
        self.assertEqual(len(state.idempotency), 1)
        self.assertEqual(state.activity, ())

    def test_create_replay_is_frozen_after_patch_and_conflict_refuses(self) -> None:
        first = self.backend.create(self.body(), "task.create.replay.0001")
        task = first["body"]["data"]
        self.backend.patch(task["id"], {"revision": 0, "title": "Later title"})
        replay = self.backend.create(copy.deepcopy(self.body()), "task.create.replay.0001")
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], task)
        changed = self.body()
        changed["title"] = "Different intent"
        self.assert_code(
            "idempotency_conflict",
            lambda: self.backend.create(changed, "task.create.replay.0001"),
        )
        self.assertEqual(len(self.backend.state().tasks), 1)

    def test_scalar_patch_normalizes_advances_cas_and_emits_field_names(self) -> None:
        task = self.backend.create({"title": "Initial"}, "task.patch.contract.0001")["body"]["data"]
        changed = self.backend.patch(task["id"], {
            "revision": 0, "title": "  Changed  ", "detail": "  Detail  ",
            "priority": "P0", "due": None, "scheduled": "2030-03-04",
            "estimate_minutes": 30,
        })
        self.assertEqual(changed["revision"], 1)
        self.assertEqual(changed["title"], "Changed")
        self.assertEqual(changed["detail"], "Detail")
        self.assertEqual(changed["scheduled"], "2030-03-04")
        update = self.backend.state().activity[-1]
        self.assertEqual(update["type"], "task.updated")
        self.assertEqual(update["details"]["fields"], [
            "detail", "due", "estimate_minutes", "priority", "scheduled", "title",
        ])
        self.assertNotIn("Changed", str(update))
        self.assert_code(
            "revision_conflict",
            lambda: self.backend.patch(task["id"], {"revision": 0, "title": "stale"}),
        )

    def test_revision_only_is_noop_but_same_scalar_is_a_revision(self) -> None:
        task = self.backend.create({"title": "Initial"}, "task.noop.contract.0001")["body"]["data"]
        before = self.backend.state()
        unchanged = self.backend.patch(task["id"], {"revision": 0})
        self.assertEqual(unchanged["revision"], 0)
        self.assertEqual(self.backend.state(), before)
        changed = self.backend.patch(task["id"], {"revision": 0, "title": "Initial"})
        self.assertEqual(changed["revision"], 1)
        self.assertEqual(self.backend.state().activity[-1]["details"], {"fields": ["title"]})

    def test_interrupted_create_recovers_task_planning_and_replay(self) -> None:
        self.backend.interrupt_next_commit()
        with self.assertRaises(SimulatedTaskCommitInterruption):  # type: ignore[attr-defined]
            self.backend.create({"title": "Recovered"}, "task.crash.contract.0001")
        self.backend.restart()
        state = self.backend.state()
        self.assertEqual(len(state.tasks), 1)
        self.assertEqual(len(state.planning), 1)
        self.assertEqual(len(state.idempotency), 1)
        replay = self.backend.create({"title": "Recovered"}, "task.crash.contract.0001")
        self.assertTrue(replay["body"]["meta"]["replayed"])


class V3TaskContractTests(TaskContractCases, unittest.TestCase):
    def make_backend(self, root: Path) -> TaskContractBackend:
        return V3TaskBackend(root)


class V4TaskContractTests(TaskContractCases, unittest.TestCase):
    def make_backend(self, root: Path) -> TaskContractBackend:
        return V4TaskBackend(root)

    def test_v4_commands_are_default_off_without_filesystem_touch(self) -> None:
        before = sorted(path.relative_to(self.backend.root.parent) for path in self.backend.root.parent.rglob("*"))  # type: ignore[attr-defined]
        with self.assertRaisesRegex(Exception, "v4_task_commands_not_enabled"):
            V4TaskRepository(self.backend.root, self.backend.runtime)  # type: ignore[attr-defined]
        after = sorted(path.relative_to(self.backend.root.parent) for path in self.backend.root.parent.rglob("*"))  # type: ignore[attr-defined]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
