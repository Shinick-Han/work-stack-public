from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Protocol

from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_relationship_repository import (
    TaskMutationReceipt,
    TaskRelationshipError,
    V3TaskRelationshipAdapter,
    admit_experimental_v4_task_relationship_repository,
)


CREATED_AT = "2026-09-01T08:00:00Z"


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _write_conversion(root: Path, conversion: Any) -> None:
    _write(root / "store.json", canonical_json_bytes(dict(conversion.store)))
    _write(root / "workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            _write(
                root / "records" / kind / uid[:2] / f"{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(
                dict(event)
            )
    for (kind, segment), events in grouped.items():
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda value: int(value["sequence"]))
        )
        _write(root / "streams" / kind / f"{segment}.ndjson", body)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".lock" not in path.name
    }


class RelationshipBackend(Protocol):
    ids: tuple[str, str, str]

    def patch(self, task_id: str, request: dict[str, Any]) -> TaskMutationReceipt: ...
    def delete(self, task_id: str, revision: int) -> TaskMutationReceipt: ...
    def hard_delete(self, task_id: str, revision: int) -> None: ...
    def snapshot(self) -> dict[str, bytes]: ...
    def idempotency_bytes(self) -> bytes: ...
    def task_exists(self, task_id: str) -> bool: ...


class V3RelationshipBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = Store(root)
        self.stack = WorkStack(self.store)
        self.ids = tuple(
            self.stack.add_task(title)["id"]
            for title in ("Parent", "Target", "Dependency")
        )
        self.repository = V3TaskRelationshipAdapter(self.stack)

    def patch(self, task_id: str, request: dict[str, Any]) -> TaskMutationReceipt:
        return self.repository.patch_relationships(task_id, request)

    def delete(self, task_id: str, revision: int) -> TaskMutationReceipt:
        return self.repository.delete_task(task_id, revision)

    def hard_delete(self, task_id: str, revision: int) -> None:
        self.repository.hard_delete_task(task_id, revision)

    def snapshot(self) -> dict[str, bytes]:
        return _tree(self.root)

    def idempotency_bytes(self) -> bytes:
        return canonical_json_bytes(self.store.load("activity.json")["idempotency"])

    def task_exists(self, task_id: str) -> bool:
        return any(
            task["id"] == task_id
            for task in self.store.load("backlog.json")["tasks"]
        )


class V4RelationshipBackend:
    def __init__(self, base: Path) -> None:
        source = base / "source"
        store = Store(source)
        stack = WorkStack(store)
        self.ids = tuple(
            stack.add_task(title)["id"]
            for title in ("Parent", "Target", "Dependency")
        )
        documents = {name: store.load(name) for name in DEFAULTS}
        conversion = convert_v3_documents(
            documents, candidate_created_at=CREATED_AT
        )
        self.root = base / "authority"
        _write_conversion(self.root, conversion)
        self.runtime = resolve_runtime_authority(
            self.root, base / "runtime", str(conversion.store["workspace_uid"])
        )
        self.runtime.runtime_root.mkdir(parents=True, exist_ok=True)
        _write(
            self.runtime.idempotency_path,
            canonical_json_bytes(dict(conversion.idempotency_ledger)),
        )
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.root), generation=0),
            expected_digest=None,
        )
        self.tick = 0
        self.repository = admit_experimental_v4_task_relationship_repository(
            self.root,
            self.runtime,
            allow_v4_task_relationships=True,
            clock=self._clock,
        )

    def _clock(self) -> str:
        self.tick += 1
        return f"2030-09-01T08:01:{self.tick:02d}Z"

    def patch(self, task_id: str, request: dict[str, Any]) -> TaskMutationReceipt:
        return self.repository.patch_relationships(task_id, request)

    def delete(self, task_id: str, revision: int) -> TaskMutationReceipt:
        return self.repository.delete_task(task_id, revision)

    def hard_delete(self, task_id: str, revision: int) -> None:
        self.repository.hard_delete_task(task_id, revision)

    def snapshot(self) -> dict[str, bytes]:
        return {
            **{f"authority/{key}": value for key, value in _tree(self.root).items()},
            **{
                f"runtime/{key}": value
                for key, value in _tree(self.runtime.runtime_root).items()
            },
        }

    def idempotency_bytes(self) -> bytes:
        return self.runtime.idempotency_path.read_bytes()

    def task_exists(self, task_id: str) -> bool:
        return any(
            task["display_id"] == task_id
            for task in read_v4(self.root).records["tasks"]
        )

    def task_record(self, task_id: str) -> dict[str, Any]:
        return next(
            dict(task)
            for task in read_v4(self.root).records["tasks"]
            if task["display_id"] == task_id
        )


class TaskRelationshipContractTests(unittest.TestCase):
    def backends(self, base: Path) -> tuple[RelationshipBackend, ...]:
        return (
            V3RelationshipBackend(base / "v3"),
            V4RelationshipBackend(base / "v4"),
        )

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(TaskRelationshipError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_parent_and_dependencies_share_revision_activity_and_no_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for backend in self.backends(Path(directory)):
                with self.subTest(backend=type(backend).__name__):
                    parent, target, dependency = backend.ids
                    ledger = backend.idempotency_bytes()
                    changed = backend.patch(
                        target,
                        {
                            "revision": 0,
                            "parent_id": parent.lower(),
                            "dependencies": [dependency.lower(), dependency],
                        },
                    )
                    self.assertEqual(changed.revision, 1)
                    self.assertEqual(changed.parent_id, parent)
                    self.assertEqual(changed.dependencies, (dependency,))
                    self.assertEqual(
                        changed.changed_fields, ("dependencies", "parent_id")
                    )
                    self.assertTrue(changed.activity_appended)
                    self.assertFalse(changed.planning_appended)
                    self.assertFalse(changed.idempotency_recorded)
                    self.assertEqual(backend.idempotency_bytes(), ledger)

                    repeated = backend.patch(
                        target,
                        {
                            "revision": 1,
                            "parent_id": parent,
                            "dependencies": [dependency],
                        },
                    )
                    self.assertEqual(repeated.revision, 2)
                    self.assertTrue(repeated.activity_appended)

    def test_revision_only_is_exact_noop_and_stale_revision_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for backend in self.backends(Path(directory)):
                with self.subTest(backend=type(backend).__name__):
                    target = backend.ids[1]
                    before = backend.snapshot()
                    unchanged = backend.patch(target, {"revision": 0})
                    self.assertEqual(unchanged.revision, 0)
                    self.assertFalse(unchanged.activity_appended)
                    self.assertEqual(backend.snapshot(), before)
                    self.assert_code(
                        "revision_conflict",
                        lambda: backend.patch(
                            target, {"revision": 1, "dependencies": []}
                        ),
                    )
                    self.assertEqual(backend.snapshot(), before)

    def test_parent_and_dependency_cycles_fail_without_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for backend in self.backends(Path(directory)):
                with self.subTest(backend=type(backend).__name__):
                    parent, target, _dependency = backend.ids
                    backend.patch(parent, {"revision": 0, "parent_id": target})
                    before = backend.snapshot()
                    self.assert_code(
                        "invalid_request",
                        lambda: backend.patch(
                            target, {"revision": 0, "parent_id": parent}
                        ),
                    )
                    self.assertEqual(backend.snapshot(), before)

            second = Path(directory) / "dependency-cycles"
            for backend in self.backends(second):
                with self.subTest(backend=type(backend).__name__, relation="dependency"):
                    first, target, _third = backend.ids
                    backend.patch(first, {"revision": 0, "dependencies": [target]})
                    before = backend.snapshot()
                    self.assert_code(
                        "invalid_request",
                        lambda: backend.patch(
                            target, {"revision": 0, "dependencies": [first]}
                        ),
                    )
                    self.assertEqual(backend.snapshot(), before)

    def test_delete_is_dropped_transition_and_never_physical_erase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for backend in self.backends(Path(directory)):
                with self.subTest(backend=type(backend).__name__):
                    target = backend.ids[1]
                    ledger = backend.idempotency_bytes()
                    deleted = backend.delete(target, 0)
                    self.assertTrue(deleted.logically_deleted)
                    self.assertEqual(deleted.revision, 1)
                    self.assertEqual(deleted.changed_fields, ("status",))
                    self.assertTrue(deleted.activity_appended)
                    self.assertTrue(deleted.planning_appended)
                    self.assertTrue(backend.task_exists(target))
                    self.assertEqual(backend.idempotency_bytes(), ledger)

                    before = backend.snapshot()
                    repeated = backend.delete(target, 1)
                    self.assertEqual(repeated.revision, 1)
                    self.assertFalse(repeated.activity_appended)
                    self.assertFalse(repeated.planning_appended)
                    self.assertEqual(backend.snapshot(), before)
                    self.assert_code(
                        "revision_conflict", lambda: backend.delete(target, 0)
                    )
                    self.assertEqual(backend.snapshot(), before)

                    self.assert_code(
                        "task_hard_delete_unsupported",
                        lambda: backend.hard_delete(target, 1),
                    )
                    self.assertEqual(backend.snapshot(), before)

    def test_references_are_explicit_v4_capability_and_v3_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            v3 = V3RelationshipBackend(base / "v3")
            before = v3.snapshot()
            self.assert_code(
                "references_unsupported",
                lambda: v3.patch(
                    v3.ids[1], {"revision": 0, "references": []}
                ),
            )
            self.assertEqual(v3.snapshot(), before)

            v4 = V4RelationshipBackend(base / "v4")
            reference, target, _dependency = v4.ids
            changed = v4.patch(
                target, {"revision": 0, "references": [reference.lower()]}
            )
            self.assertEqual(changed.references, (reference,))
            self.assertEqual(changed.changed_fields, ("references",))
            self.assertTrue(changed.activity_appended)
            self.assertFalse(changed.planning_appended)
            record = v4.task_record(target)
            by_id = {
                item["display_id"]: item["uid"]
                for item in read_v4(v4.root).records["tasks"]
            }
            self.assertEqual(record["reference_uids"], [by_id[reference]])

            before = v4.snapshot()
            self.assert_code(
                "invalid_request",
                lambda: v4.patch(
                    target, {"revision": 1, "references": ["T-9999"]}
                ),
            )
            self.assertEqual(v4.snapshot(), before)

    def test_v4_admission_is_default_off_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = V4RelationshipBackend(Path(directory))
            before = backend.snapshot()
            with self.assertRaisesRegex(Exception, "V4_MUTATION_OPT_IN_REQUIRED"):
                admit_experimental_v4_task_relationship_repository(
                    backend.root, backend.runtime
                )
            self.assertEqual(backend.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
