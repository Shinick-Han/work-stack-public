from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from workstack.service import (
    DomainError,
    NotFoundError,
    RevisionConflictError,
    WorkStack,
)
from unittest import mock

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.document_repository import (
    StoreDocumentRepository,
    WorkspaceDocument,
)
from workstack.storage.experimental_application import (
    create_experimental_v4_application,
)
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.store import DEFAULTS, Store


class TaskKeyResultRefsTest(unittest.TestCase):
    """Frozen T5 v3 contract for the optional scoped Task key_result_refs field."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.first = self.stack.add_objective("First objective")
        self.second = self.stack.add_objective("Second objective")
        self.first_kr = self.stack.add_key_result(self.first["id"], "First outcome")
        self.second_kr = self.stack.add_key_result(self.second["id"], "Second outcome")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store_bytes(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.glob("*.json")}

    def _linked_task(self, title: str = "Linked") -> dict[str, object]:
        task = self.stack.add_task(title)
        return self.stack.patch_task(
            task["id"],
            {
                "objective_ids": [self.first["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    }
                ],
                "revision": task["revision"],
            },
        )

    def _persisted(self, task_id: str) -> dict[str, object]:
        import json

        backlog = json.loads((self.root / "backlog.json").read_text(encoding="utf-8"))
        return next(item for item in backlog["tasks"] if item["id"] == task_id)

    def test_omitted_field_stays_omitted_through_an_unrelated_patch(self) -> None:
        task = self.stack.add_task("Legacy")
        self.assertNotIn("key_result_refs", self._persisted(task["id"]))

        updated = self.stack.patch_task(
            task["id"], {"title": "Legacy renamed", "revision": task["revision"]}
        )

        self.assertNotIn("key_result_refs", self._persisted(task["id"]))
        self.assertNotIn("key_result_refs", updated)
        self.assertEqual(updated["title"], "Legacy renamed")

    def test_explicit_empty_list_is_persisted_as_an_explicit_clear(self) -> None:
        linked = self._linked_task()

        cleared = self.stack.patch_task(
            linked["id"], {"key_result_refs": [], "revision": linked["revision"]}
        )

        self.assertEqual(cleared["key_result_refs"], [])
        self.assertEqual(self._persisted(linked["id"])["key_result_refs"], [])
        self.assertEqual(cleared["revision"], linked["revision"] + 1)
        self.assertEqual(cleared["objective_ids"], [self.first["id"]])

    def test_nonempty_refs_are_normalized_sorted_and_projected(self) -> None:
        task = self.stack.add_task("Aligned")
        updated = self.stack.patch_task(
            task["id"],
            {
                "objective_ids": [self.second["id"], self.first["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.second["id"].lower(),
                        "key_result_id": "  " + self.second_kr["id"].lower() + " ",
                    },
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    },
                ],
                "revision": task["revision"],
            },
        )

        self.assertEqual(
            updated["key_result_refs"],
            [
                {
                    "objective_id": self.first["id"],
                    "key_result_id": self.first_kr["id"],
                },
                {
                    "objective_id": self.second["id"],
                    "key_result_id": self.second_kr["id"],
                },
            ],
        )

    def test_same_key_result_display_id_under_two_objectives_stays_distinct(self) -> None:
        self.assertEqual(self.first_kr["id"], self.second_kr["id"])
        task = self.stack.add_task("Both")

        updated = self.stack.patch_task(
            task["id"],
            {
                "objective_ids": [self.first["id"], self.second["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    },
                    {
                        "objective_id": self.second["id"],
                        "key_result_id": self.second_kr["id"],
                    },
                ],
                "revision": task["revision"],
            },
        )

        self.assertEqual(len(updated["key_result_refs"]), 2)
        self.assertEqual(
            {ref["objective_id"] for ref in updated["key_result_refs"]},
            {self.first["id"], self.second["id"]},
        )

    def test_normalized_duplicate_pair_is_refused_without_any_write(self) -> None:
        task = self.stack.add_task("Duplicate")
        before = self._store_bytes()

        with self.assertRaises(DomainError):
            self.stack.patch_task(
                task["id"],
                {
                    "objective_ids": [self.first["id"]],
                    "key_result_refs": [
                        {
                            "objective_id": self.first["id"],
                            "key_result_id": self.first_kr["id"],
                        },
                        {
                            "objective_id": self.first["id"].lower(),
                            "key_result_id": self.first_kr["id"].lower(),
                        },
                    ],
                    "revision": task["revision"],
                },
            )

        self.assertEqual(self._store_bytes(), before)

    def test_malformed_blank_and_unknown_key_shapes_are_refused(self) -> None:
        task = self.stack.add_task("Malformed")
        before = self._store_bytes()
        payloads = [
            "not-a-list",
            ["not-an-object"],
            [{"objective_id": self.first["id"]}],
            [
                {
                    "objective_id": self.first["id"],
                    "key_result_id": self.first_kr["id"],
                    "extra": "x",
                }
            ],
            [{"objective_id": "   ", "key_result_id": self.first_kr["id"]}],
            [{"objective_id": self.first["id"], "key_result_id": 7}],
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(DomainError):
                    self.stack.patch_task(
                        task["id"],
                        {"key_result_refs": payload, "revision": task["revision"]},
                    )

        self.assertEqual(self._store_bytes(), before)

    def test_reference_parent_must_be_in_the_final_objective_ids(self) -> None:
        task = self.stack.add_task("Unaligned")
        before = self._store_bytes()

        with self.assertRaises(DomainError):
            self.stack.patch_task(
                task["id"],
                {
                    "key_result_refs": [
                        {
                            "objective_id": self.first["id"],
                            "key_result_id": self.first_kr["id"],
                        }
                    ],
                    "revision": task["revision"],
                },
            )

        self.assertEqual(self._store_bytes(), before)

    def test_unknown_objective_or_key_result_target_is_refused(self) -> None:
        task = self.stack.add_task("Unknown target")
        before = self._store_bytes()

        with self.assertRaises(DomainError):
            self.stack.patch_task(
                task["id"],
                {
                    "objective_ids": [self.first["id"]],
                    "key_result_refs": [
                        {"objective_id": self.first["id"], "key_result_id": "KR-999"}
                    ],
                    "revision": task["revision"],
                },
            )

        self.assertEqual(self._store_bytes(), before)

    def test_removing_the_parent_alone_refuses_and_writes_nothing(self) -> None:
        linked = self._linked_task()
        before = self._store_bytes()

        with self.assertRaises(DomainError):
            self.stack.patch_task(
                linked["id"], {"objective_ids": [], "revision": linked["revision"]}
            )

        self.assertEqual(self._store_bytes(), before)
        self.assertEqual(
            self.stack.get_task(linked["id"])["objective_ids"], [self.first["id"]]
        )

    def test_parent_and_refs_replace_atomically_in_one_revision(self) -> None:
        linked = self._linked_task()

        updated = self.stack.patch_task(
            linked["id"],
            {
                "objective_ids": [self.second["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.second["id"],
                        "key_result_id": self.second_kr["id"],
                    }
                ],
                "revision": linked["revision"],
            },
        )

        self.assertEqual(updated["objective_ids"], [self.second["id"]])
        self.assertEqual(
            updated["key_result_refs"],
            [
                {
                    "objective_id": self.second["id"],
                    "key_result_id": self.second_kr["id"],
                }
            ],
        )
        self.assertEqual(updated["revision"], linked["revision"] + 1)

    def test_stale_revision_changes_nothing(self) -> None:
        linked = self._linked_task()
        before = self._store_bytes()

        with self.assertRaises(RevisionConflictError):
            self.stack.patch_task(
                linked["id"],
                {"key_result_refs": [], "revision": linked["revision"] - 1},
            )

        self.assertEqual(self._store_bytes(), before)

    def test_dangling_reference_reads_but_blocks_mutation_until_repaired(self) -> None:
        import json

        linked = self._linked_task()
        objectives_path = self.root / "okr.json"
        objectives = json.loads(objectives_path.read_text(encoding="utf-8"))
        objectives["objectives"][0]["key_results"] = []
        objectives_path.write_text(
            json.dumps(objectives, indent=2), encoding="utf-8"
        )
        reloaded = WorkStack(Store(self.root))

        read_back = reloaded.get_task(linked["id"])
        self.assertEqual(
            read_back["key_result_refs"],
            [
                {
                    "objective_id": self.first["id"],
                    "key_result_id": self.first_kr["id"],
                }
            ],
        )

        with self.assertRaises(DomainError):
            reloaded.patch_task(
                linked["id"], {"title": "Renamed", "revision": read_back["revision"]}
            )

        # Repair through an explicit clear is exercised on a healthy store in
        # test_explicit_empty_list_is_persisted_as_an_explicit_clear; writing here
        # would trip the unrelated external-change guard raised by editing
        # okr.json outside Work Stack, which is not this contract's subject.

    def test_linking_never_touches_objective_records_or_manual_progress(self) -> None:
        import json

        before = (self.root / "okr.json").read_bytes()

        linked = self._linked_task()
        self.stack.patch_task(
            linked["id"], {"status": "done", "revision": linked["revision"]}
        )

        self.assertEqual((self.root / "okr.json").read_bytes(), before)
        objectives = json.loads((self.root / "okr.json").read_text(encoding="utf-8"))
        progress = [
            result["progress"]
            for objective in objectives["objectives"]
            for result in objective["key_results"]
        ]
        self.assertEqual(progress, [0, 0])

    def test_healthy_control_patch_without_refs_still_succeeds(self) -> None:
        task = self.stack.add_task("Control")

        updated = self.stack.patch_task(
            task["id"],
            {"objective_ids": [self.first["id"]], "revision": task["revision"]},
        )

        self.assertEqual(updated["objective_ids"], [self.first["id"]])
        self.assertNotIn("key_result_refs", self._persisted(task["id"]))


if __name__ == "__main__":  # pragma: no cover - module is run through unittest
    unittest.main()


class AmbiguousTargetRefusalTest(unittest.TestCase):
    """Duplicate records keep their multiplicity until a reference resolves to one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "w"
        self.stack = WorkStack(Store(self.root))
        self.first = self.stack.add_objective("First objective")
        self.second = self.stack.add_objective("Second objective")
        self.first_kr = self.stack.add_key_result(self.first["id"], "First outcome")
        task = self.stack.add_task("Task")
        self.linked = self.stack.patch_task(
            task["id"],
            {
                "objective_ids": [self.first["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    }
                ],
                "revision": task["revision"],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _documents(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.glob("*.json")}

    def _duplicate_objective(self, objective_id: str) -> WorkStack:
        """Persist a second record with the same ID through the released repository."""

        objectives = self.stack.documents.load(WorkspaceDocument.OBJECTIVES)
        original = next(
            item for item in objectives["objectives"] if item["id"] == objective_id
        )
        duplicate = copy.deepcopy(original)
        duplicate["objective"] = "Duplicate record"
        objectives["objectives"].append(duplicate)
        with self.stack.store.transaction():
            self.stack.documents.save(WorkspaceDocument.OBJECTIVES, objectives)
        return WorkStack(Store(self.root))

    def test_retained_ref_to_an_ambiguous_objective_refuses_without_commit(self) -> None:
        reloaded = self._duplicate_objective(self.first["id"])
        before = self._documents()

        with self.assertRaises(DomainError):
            reloaded.patch_task(
                self.linked["id"],
                {"title": "Renamed", "revision": self.linked["revision"]},
            )

        self.assertEqual(self._documents(), before)
        self.assertEqual(
            reloaded.get_task(self.linked["id"])["revision"], self.linked["revision"]
        )

    def test_ambiguous_objective_still_allows_an_explicit_clear(self) -> None:
        reloaded = self._duplicate_objective(self.first["id"])

        cleared = reloaded.patch_task(
            self.linked["id"],
            {"key_result_refs": [], "revision": self.linked["revision"]},
        )

        self.assertEqual(cleared["key_result_refs"], [])

    def test_ambiguous_objective_allows_replacement_onto_a_unique_parent(self) -> None:
        second_kr = self.stack.add_key_result(self.second["id"], "Second outcome")
        reloaded = self._duplicate_objective(self.first["id"])

        replaced = reloaded.patch_task(
            self.linked["id"],
            {
                "objective_ids": [self.second["id"]],
                "key_result_refs": [
                    {
                        "objective_id": self.second["id"],
                        "key_result_id": second_kr["id"],
                    }
                ],
                "revision": self.linked["revision"],
            },
        )

        self.assertEqual(
            replaced["key_result_refs"],
            [{"objective_id": self.second["id"], "key_result_id": second_kr["id"]}],
        )

    def test_unrelated_duplicate_objective_does_not_block_a_valid_patch(self) -> None:
        reloaded = self._duplicate_objective(self.second["id"])

        updated = reloaded.patch_task(
            self.linked["id"],
            {"title": "Renamed", "revision": self.linked["revision"]},
        )

        self.assertEqual(updated["title"], "Renamed")
        self.assertEqual(updated["revision"], self.linked["revision"] + 1)

    def test_ambiguous_key_result_inside_one_objective_refuses(self) -> None:
        objectives = self.stack.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = next(
            item for item in objectives["objectives"] if item["id"] == self.first["id"]
        )
        objective["key_results"].append(copy.deepcopy(objective["key_results"][0]))
        with self.stack.store.transaction():
            self.stack.documents.save(WorkspaceDocument.OBJECTIVES, objectives)
        reloaded = WorkStack(Store(self.root))
        before = self._documents()

        with self.assertRaises(DomainError):
            reloaded.patch_task(
                self.linked["id"],
                {"title": "Renamed", "revision": self.linked["revision"]},
            )

        self.assertEqual(self._documents(), before)


class _RecordingRepository:
    """Injected repository delegating to the released one and recording writes."""

    def __init__(self, inner: StoreDocumentRepository) -> None:
        self._inner = inner
        self.save_many_calls: list[list[str]] = []

    def load(self, document: WorkspaceDocument) -> dict:
        return self._inner.load(document)

    def save(self, document: WorkspaceDocument, value: object) -> None:
        self._inner.save(document, value)

    def save_many(self, writes, operation_id=None) -> None:
        self.save_many_calls.append(sorted(item.value for item in writes))
        self._inner.save_many(writes, operation_id)

    def total_bytes(self) -> int:
        return self._inner.total_bytes()


class _SpyTaskCommands:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_task_v1(self, body, idempotency_key, *, path):
        self.calls.append("create_task_v1")
        raise AssertionError("backend must not be called")

    def patch_task(self, task_id, patch):
        self.calls.append("patch_task")
        raise AssertionError("backend must not be called")


class UnadmittedCompositionRefusalTest(unittest.TestCase):
    """Only the released v3 document composition may mutate the scoped field."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "w"
        self.store = Store(self.root)
        seeded = WorkStack(self.store)
        self.objective = seeded.add_objective("Objective")
        self.key_result = seeded.add_key_result(self.objective["id"], "Outcome")
        task = seeded.add_task("Task")
        self.task = seeded.patch_task(
            task["id"],
            {"objective_ids": [self.objective["id"]], "revision": task["revision"]},
        )
        self.recording = _RecordingRepository(StoreDocumentRepository(self.store))
        self.injected = WorkStack(self.store, document_repository=self.recording)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _pair(self) -> dict[str, str]:
        return {
            "objective_id": self.objective["id"],
            "key_result_id": self.key_result["id"],
        }

    def test_unrelated_scalar_patch_still_succeeds_on_an_injected_repository(self) -> None:
        updated = self.injected.patch_task(
            self.task["id"], {"title": "Scalar", "revision": self.task["revision"]}
        )

        self.assertEqual(updated["title"], "Scalar")
        self.assertEqual(len(self.recording.save_many_calls), 1)

    def test_injected_repository_refuses_both_ref_payloads_without_any_write(self) -> None:
        before = (self.root / "backlog.json").read_bytes()

        for refs in ([], [self._pair()]):
            with self.subTest(refs=refs):
                with self.assertRaises(DomainError):
                    self.injected.patch_task(
                        self.task["id"],
                        {"key_result_refs": refs, "revision": self.task["revision"]},
                    )

        self.assertEqual(self.recording.save_many_calls, [])
        self.assertEqual((self.root / "backlog.json").read_bytes(), before)

    def test_missing_task_and_stale_revision_keep_their_existing_precedence(self) -> None:
        with self.assertRaises(NotFoundError):
            self.injected.patch_task("T-9999", {"key_result_refs": [], "revision": 0})

        with self.assertRaises(RevisionConflictError):
            self.injected.patch_task(
                self.task["id"],
                {"key_result_refs": [], "revision": self.task["revision"] + 5},
            )

        self.assertEqual(self.recording.save_many_calls, [])

    def test_released_default_composition_still_accepts_the_field(self) -> None:
        released = WorkStack(self.store)

        updated = released.patch_task(
            self.task["id"],
            {"key_result_refs": [self._pair()], "revision": self.task["revision"]},
        )

        self.assertEqual(updated["key_result_refs"], [self._pair()])

    def test_active_command_backend_refuses_the_field_before_any_backend_call(self) -> None:
        spy = _SpyTaskCommands()
        stack = WorkStack(self.store, task_commands=spy)
        before = (self.root / "backlog.json").read_bytes()

        for refs in ([], [self._pair()]):
            with self.subTest(refs=refs):
                with self.assertRaises(DomainError):
                    stack.patch_task(
                        self.task["id"],
                        {"key_result_refs": refs, "revision": self.task["revision"]},
                    )

        self.assertEqual(spy.calls, [])
        self.assertEqual((self.root / "backlog.json").read_bytes(), before)


V4_NOW = "2026-09-01T12:00:00Z"
V4_UID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _write_v4_conversion(root: Path, conversion) -> None:
    """Materialize a converted v4 authority tree, as the composition gate does."""

    def write(relative: str, body: bytes) -> None:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write("store.json", canonical_json_bytes(dict(conversion.store)))
    write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(f"records/{kind}/{uid[:2]}/{uid}.json", canonical_json_bytes(dict(record)))
    segments: dict[tuple[str, str], list[dict]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            segments.setdefault((kind, str(event["created_at"])[:7]), []).append(dict(event))
    for (kind, month), events in sorted(segments.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        )
        write(f"streams/{kind}/{month}.ndjson", body)


class ActualV4CompositionRefusalTest(unittest.TestCase):
    """The real experimental v4 composition must refuse the scoped field outright."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        legacy = WorkStack(Store(self.base / "a"))
        with mock.patch("workstack.service.utc_now", return_value=V4_NOW), mock.patch(
            "workstack.service.today", return_value=V4_NOW[:10]
        ):
            objective = legacy.add_objective("Objective")
            self.key_result = legacy.add_key_result(objective["id"], "Outcome")
            task = legacy.add_task("Task")
            legacy.patch_task(
                task["id"],
                {"objective_ids": [objective["id"]], "revision": task["revision"]},
            )
        self.objective_id = objective["id"]
        self.task_id = task["id"]

        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(documents, candidate_created_at=V4_NOW)
        self.authority = self.base / "b"
        self.authority.mkdir()
        _write_v4_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority, self.base / "c", str(self.conversion.store["workspace_uid"])
        )
        self.runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )

        self.application = create_experimental_v4_application(
            self.authority,
            self.runtime,
            enable_v4_application=True,
            clock=lambda: V4_NOW,
            uid_factory=lambda: V4_UID,
            today=lambda: V4_NOW[:10],
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )
        domain = self.application.domain
        self.stack = WorkStack(
            self.application.store,
            task_commands=domain.tasks,
            relationship_commands=domain.relationships,
            planning_commands=domain.planning,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _tree(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.base)): path.read_bytes()
            for path in sorted(self.base.rglob("*"))
            if path.is_file()
        }

    def _pair(self) -> dict[str, str]:
        return {
            "objective_id": self.objective_id,
            "key_result_id": self.key_result["id"],
        }

    def test_actual_v4_composition_accepts_a_supported_scalar_patch(self) -> None:
        current = self.stack.get_task(self.task_id)["revision"]

        updated = self.stack.patch_task(
            self.task_id, {"title": "Scalar on v4", "revision": current}
        )

        self.assertEqual(updated["title"], "Scalar on v4")
        self.assertEqual(updated["revision"], current + 1)

    def test_actual_v4_composition_refuses_both_ref_payloads(self) -> None:
        domain = self.application.domain
        before = self._tree()
        revision_before = self.stack.get_task(self.task_id)["revision"]

        with mock.patch.object(
            domain.tasks, "patch_task", wraps=domain.tasks.patch_task
        ) as task_backend, mock.patch.object(
            domain.relationships,
            "patch_relationships",
            wraps=domain.relationships.patch_relationships,
        ) as relationship_backend:
            for refs in ([], [self._pair()]):
                with self.subTest(refs=refs):
                    with self.assertRaises(DomainError):
                        self.stack.patch_task(
                            self.task_id,
                            {"key_result_refs": refs, "revision": revision_before},
                        )

            self.assertEqual(task_backend.call_count, 0)
            self.assertEqual(relationship_backend.call_count, 0)

        self.assertEqual(self._tree(), before)
        self.assertEqual(
            self.stack.get_task(self.task_id)["revision"], revision_before
        )
