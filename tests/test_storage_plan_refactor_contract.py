"""Compatibility contract for the storage plan/validation module split.

The deletion planner, the v3 deletion transaction and the storage path
validator were each split into a facade plus explicit collaborators. These
tests guard only what the split itself could break: the facade export
surface, single exception-class identity across the new modules, the
planner-to-transaction refusal mapping, the absence of new import cycles,
and the monkeypatch seams the validator's callers still rely on.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.storage import (
    StoragePathValidationReport,
    StorageValidationIssue,
    validate_storage_path,
)
from workstack.storage import deletion_plan_primitives, deletion_v3_documents
from workstack.storage import validation
from workstack.storage.task_deletion_plan import (
    KNOWN_NON_REFERENCE_BODY_KEYS,
    PLAN_SCHEMA,
    PREVIEW_SCHEMA,
    TASK_DISPLAY_RE,
    TaskDeletionPlan,
    TaskDeletionPlanError,
    plan_v3_task_deletion,
    plan_v4_task_deletion,
)
from workstack.storage.task_deletion_transaction import (
    PREVIEW_TTL_SECONDS,
    V3_DOCUMENT_NAMES,
    TaskDeletionTransactionError,
    apply_v3_task_deletion_plan,
    commit_v3_hard_delete,
    commit_v3_task_deletion,
    preview_v3_task_deletion,
    store_digest_from_documents,
)


ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "workstack" / "storage"
FACADES = (
    "task_deletion_plan",
    "task_deletion_transaction",
    "validation",
)
COLLABORATORS = (
    "deletion_plan_primitives",
    "deletion_plan_v3",
    "deletion_plan_v4",
    "deletion_v3_apply",
    "deletion_v3_backup",
    "deletion_v3_documents",
    "validation_primitives",
    "validation_records",
    "validation_streams",
    "validation_v3_probe",
)


def _local_imports(module: str) -> set[str]:
    """Sibling ``workstack.storage`` modules one module imports."""

    tree = ast.parse((STORAGE / f"{module}.py").read_text(encoding="utf-8"))
    siblings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                siblings.add(node.module.split(".")[0])
            else:
                siblings.update(alias.name for alias in node.names)
    return siblings


class FacadeExportSurfaceTest(unittest.TestCase):
    """Every name consumers already import still resolves from its facade."""

    def test_deletion_plan_facade_keeps_its_public_names(self) -> None:
        self.assertEqual(PLAN_SCHEMA, "workstack.task-deletion-plan.v1")
        self.assertEqual(PREVIEW_SCHEMA, "workstack.task-deletion-preview.v1")
        self.assertTrue(TASK_DISPLAY_RE.fullmatch("T-0001"))
        self.assertIn("title", KNOWN_NON_REFERENCE_BODY_KEYS)
        self.assertTrue(issubclass(TaskDeletionPlanError, ValueError))
        for entrypoint in (plan_v3_task_deletion, plan_v4_task_deletion):
            self.assertTrue(callable(entrypoint))
        self.assertTrue(hasattr(TaskDeletionPlan, "canonical_bytes"))

    def test_deletion_transaction_facade_keeps_its_public_names(self) -> None:
        self.assertEqual(PREVIEW_TTL_SECONDS, 900)
        self.assertEqual(len(V3_DOCUMENT_NAMES), 9)
        self.assertIn("backlog.json", V3_DOCUMENT_NAMES)
        for entrypoint in (
            apply_v3_task_deletion_plan,
            commit_v3_hard_delete,
            commit_v3_task_deletion,
            preview_v3_task_deletion,
            store_digest_from_documents,
        ):
            self.assertTrue(callable(entrypoint))

    def test_validation_facade_keeps_its_public_and_probed_names(self) -> None:
        self.assertEqual(validation.MAX_V4_JSON_BYTES, 4 * 1024 * 1024)
        self.assertEqual(validation.MAX_V4_STREAM_SEGMENT_BYTES, 16 * 1024 * 1024)
        self.assertTrue(callable(validation._detect_format))
        self.assertTrue(callable(validation._v3_source_digests))
        self.assertTrue(callable(validation._is_link))
        self.assertIs(validation.StorageValidationIssue, StorageValidationIssue)
        self.assertIs(
            validation.StoragePathValidationReport, StoragePathValidationReport
        )
        self.assertIs(validation.validate_storage_path, validate_storage_path)


class ExceptionIdentityTest(unittest.TestCase):
    """One class per refusal, however many modules now raise it."""

    def test_plan_error_raised_by_both_planners_is_one_class(self) -> None:
        self.assertIs(TaskDeletionPlanError, deletion_plan_primitives.TaskDeletionPlanError)
        for planner in (plan_v3_task_deletion, plan_v4_task_deletion):
            with self.subTest(planner=planner.__name__):
                with self.assertRaises(TaskDeletionPlanError) as caught:
                    planner({}, task_id="not-a-task", expected_revision=0)
                self.assertIs(type(caught.exception), TaskDeletionPlanError)
                self.assertEqual(caught.exception.code, "malformed_reference_container")

    def test_transaction_error_is_one_class_across_the_split(self) -> None:
        self.assertIs(
            TaskDeletionTransactionError,
            deletion_v3_documents.TaskDeletionTransactionError,
        )
        with self.assertRaises(TaskDeletionTransactionError) as caught:
            apply_v3_task_deletion_plan(
                {},
                TaskDeletionPlan(
                    layout="v4",
                    target_display_id="T-0001",
                    target_uid="uid-1",
                    expected_revision=1,
                    display_id_high_water=1,
                    operations=(),
                ),
            )
        self.assertEqual(caught.exception.code, "invalid_layout")
        self.assertEqual(caught.exception.status, 400)


class PlanRefusalMappingTest(unittest.TestCase):
    """Each planner refusal keeps its transaction code, message and status."""

    def test_every_planner_code_maps_to_its_frozen_transaction_refusal(self) -> None:
        expected = {
            "not_found": ("not_found", 404),
            "revision_mismatch": ("revision_conflict", 409),
            "unknown_unsafe_reference": ("unknown_unsafe_reference", 409),
            "malformed_reference_container": ("malformed_reference_container", 400),
            "duplicate_identity": ("duplicate_identity", 409),
            "some_future_code": ("some_future_code", 400),
        }
        for code, (mapped, status) in expected.items():
            with self.subTest(code=code):
                with self.assertRaises(TaskDeletionTransactionError) as caught:
                    deletion_v3_documents.raise_plan_refusal(TaskDeletionPlanError(code))
                self.assertEqual(caught.exception.code, mapped)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(str(caught.exception), mapped)
                self.assertIsInstance(caught.exception.__cause__, TaskDeletionPlanError)


class ModuleGraphTest(unittest.TestCase):
    """The split adds collaborators, not cycles."""

    def test_no_collaborator_imports_its_facade_back(self) -> None:
        for module in COLLABORATORS:
            with self.subTest(module=module):
                siblings = _local_imports(module)
                offenders = sorted(siblings & set(FACADES) - {"task_deletion_plan"})
                self.assertEqual(offenders, [])

    def test_storage_split_modules_have_no_import_cycle(self) -> None:
        members = set(FACADES) | set(COLLABORATORS)
        graph = {name: _local_imports(name) & members for name in members}
        visiting: set[str] = set()
        done: set[str] = set()
        cycles: list[str] = []

        def visit(name: str, trail: tuple[str, ...]) -> None:
            if name in visiting:
                cycles.append(" -> ".join(trail + (name,)))
                return
            if name in done:
                return
            visiting.add(name)
            for target in sorted(graph[name]):
                visit(target, trail + (name,))
            visiting.discard(name)
            done.add(name)

        for name in sorted(members):
            visit(name, ())
        self.assertEqual(cycles, [])


class ValidationSeamTest(unittest.TestCase):
    """Bounds and probes patched on the facade still govern the collaborators."""

    def test_patched_json_bound_still_bounds_record_tree_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text(
                json.dumps({"workspace_uid": "w"}), encoding="utf-8"
            )
            with mock.patch.object(validation, "MAX_V4_JSON_BYTES", 4):
                report = validate_storage_path(root)
        self.assertFalse(report.valid)
        self.assertIn("JSON_TOO_LARGE", {issue.code for issue in report.issues})

    def test_patched_link_probe_still_rejects_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(validation, "_is_link", return_value=True):
                report = validate_storage_path(root)
        self.assertFalse(report.valid)
        self.assertEqual([issue.code for issue in report.issues], ["SYMLINK_REJECTED"])

    def test_patched_v3_digest_reader_is_used_by_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workspace.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(
                validation, "_v3_source_digests", return_value={}
            ) as digests:
                report = validate_storage_path(root)
        self.assertGreaterEqual(digests.call_count, 2)
        self.assertEqual(report.format_version, 3)


if __name__ == "__main__":
    unittest.main()
