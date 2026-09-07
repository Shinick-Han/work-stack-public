"""Contract tests for the pure mutation-notice and Undo builder."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

from workstack import mutation_notice
from workstack.mutation_notice import (
    COMMIT_UNKNOWN,
    COMMITTED,
    COMPENSATION_FORMAT,
    FORMAT,
    PERMANENT_DELETE_OPERATION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    TASK_STATUS_OPERATION,
    MutationNoticeError,
    build_compensation,
    build_notice,
    derive_mutation_uid,
    derive_notice_id,
    serialize_compensation,
    serialize_notice,
    validate_compensation,
    validate_notice,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "workstack-mutation-notice-v1.schema.json"
SOURCE_PATH = ROOT / "workstack" / "mutation_notice.py"
WORKSPACE = "123e4567-e89b-42d3-a456-426614174000"
ENTITY = "123e4567-e89b-42d3-a456-426614174001"
OTHER_WORKSPACE = "123e4567-e89b-42d3-a456-426614174002"
OTHER_ENTITY = "123e4567-e89b-42d3-a456-426614174003"
KEY = "intent:status-1"
SECRET_FIELD = "pass" + "word"
RETRY_FIELD = "retry" + "able"
PATH_VALUE = "C:\\" + "Users\\demo\\secret\\"
COMMAND_VALUE = "cmd.exe /c echo hi"
MODULE_NAME = "workstack.mutation_notice"
FORBIDDEN_HOTSPOTS = frozenset(
    {
        "workstack.store",
        "workstack.service",
        "workstack.server",
        "workstack.cli",
        "workstack.sse_events",
        "workstack.storage.canonical",
    }
)


def _dotted_prefixes(name: str) -> set[str]:
    """Every package prefix an import of ``name`` also binds."""
    parts = name.split(".")
    return {".".join(parts[: index + 1]) for index in range(len(parts))}


def _imported_modules(tree: ast.Module, package: str) -> set[str]:
    """Module names the parsed source's own import statements can bind.

    Resolves ``import a.b``/``import a.b as c``, ``from a.b import c`` where ``c``
    may itself be a submodule, and the relative ``from . import c`` /
    ``from .b import c`` spellings against ``package``.
    """
    package_parts = package.split(".")
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules |= _dotted_prefixes(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
            else:
                base = []
            head = ".".join([*base, *([node.module] if node.module else [])])
            if head:
                modules |= _dotted_prefixes(head)
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = f"{head}.{alias.name}" if head else alias.name
                modules |= _dotted_prefixes(bound)
    return modules


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _notice_validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _compensation_validator() -> Draft202012Validator:
    schema = _schema()
    wrapper = {
        "$schema": schema["$schema"],
        "$id": str(schema["$id"]) + "/compensation",
        "$defs": schema["$defs"],
        "allOf": [{"$ref": "#/$defs/compensationRequest"}],
    }
    Draft202012Validator.check_schema(wrapper)
    return Draft202012Validator(wrapper, format_checker=FormatChecker())


def status_kwargs(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "actor": "gui-user",
        "after_revision": 4,
        "before_revision": 3,
        "commit_state": COMMITTED,
        "entity_kind": "task",
        "entity_uid": ENTITY,
        "idempotency_key": KEY,
        "operation": TASK_STATUS_OPERATION,
        "source": "gui",
        "status_after": "started",
        "status_before": "open",
        "workspace_uid": WORKSPACE,
    }
    value.update(overrides)
    return value


def status_notice(**overrides: object) -> dict[str, object]:
    return build_notice(**status_kwargs(**overrides))


def refuse(code: str, fn: object, *args: object, **kwargs: object) -> MutationNoticeError:
    with unittest.TestCase().assertRaises(MutationNoticeError) as caught:
        fn(*args, **kwargs)
    error = caught.exception
    unittest.TestCase().assertEqual(error.code, code)
    unittest.TestCase().assertEqual(str(error), "invalid mutation notice")
    return error


class SchemaContract(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = _notice_validator()
        self.compensation = _compensation_validator()

    def test_schema_identity_and_version_are_frozen(self) -> None:
        schema = _schema()
        self.assertEqual(schema["$id"], SCHEMA_ID)
        self.assertEqual(schema["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertEqual(schema["properties"]["format"]["const"], FORMAT)

    def test_canonical_notice_matches_strict_schema(self) -> None:
        notice = status_notice()
        self.validator.validate(notice)
        self.assertEqual(validate_notice(notice), notice)

    def test_missing_unknown_oversized_and_secret_fields_are_rejected(self) -> None:
        notice = status_notice()
        missing = dict(notice)
        del missing["summary"]
        self.assertFalse(self.validator.is_valid(missing))
        refuse("missing", validate_notice, missing)

        unknown = dict(notice)
        unknown["extra"] = "x"
        self.assertFalse(self.validator.is_valid(unknown))
        refuse("unknown_field", validate_notice, unknown)

        oversized = dict(notice)
        oversized["summary"] = "A" * 161
        self.assertFalse(self.validator.is_valid(oversized))
        refuse("oversized", validate_notice, oversized)

        secret = dict(notice)
        secret[SECRET_FIELD] = "x"
        self.assertFalse(self.validator.is_valid(secret))
        refuse("secret_bearing", validate_notice, secret)

    def test_secret_paths_commands_and_retry_fields_cannot_enter(self) -> None:
        retry = dict(status_notice())
        retry[RETRY_FIELD] = True
        refuse("secret_bearing", validate_notice, retry)
        refuse(
            "secret_bearing",
            build_notice,
            **status_kwargs(actor=PATH_VALUE),
        )
        refuse(
            "secret_bearing",
            build_notice,
            **status_kwargs(actor=COMMAND_VALUE),
        )

    def test_unsupported_version_is_rejected(self) -> None:
        payload = dict(status_notice())
        payload["schema_version"] = 2
        refuse("unsupported_version", validate_notice, payload)


class IdempotencyIdentity(unittest.TestCase):
    def test_replaying_the_same_key_yields_the_same_notice_identity(self) -> None:
        first = status_notice()
        second = status_notice()
        self.assertEqual(first["notice_id"], second["notice_id"])
        self.assertEqual(first["mutation_uid"], second["mutation_uid"])
        self.assertEqual(
            first["notice_id"],
            derive_notice_id(workspace_uid=WORKSPACE, idempotency_key=KEY),
        )
        self.assertEqual(
            first["mutation_uid"],
            derive_mutation_uid(workspace_uid=WORKSPACE, idempotency_key=KEY),
        )
        self.assertEqual(serialize_notice(first), serialize_notice(second))

    def test_a_distinct_key_yields_a_distinct_notice_identity(self) -> None:
        left = status_notice(idempotency_key="intent:status-1")
        right = status_notice(idempotency_key="intent:status-2")
        self.assertNotEqual(left["notice_id"], right["notice_id"])
        self.assertNotEqual(left["mutation_uid"], right["mutation_uid"])


class CompensationGuards(unittest.TestCase):
    def test_matching_workspace_entity_and_revision_build_task_status_compensation(
        self,
    ) -> None:
        notice = status_notice()
        compensation = build_compensation(
            notice,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=4,
        )
        _compensation_validator().validate(compensation)
        self.assertEqual(compensation["format"], COMPENSATION_FORMAT)
        self.assertEqual(compensation["operation"], TASK_STATUS_OPERATION)
        self.assertEqual(compensation["requested_status"], "open")
        self.assertEqual(compensation["expected_revision"], 4)
        self.assertEqual(validate_compensation(compensation), compensation)

    def test_workspace_entity_or_intervening_revision_is_refused(self) -> None:
        notice = status_notice()
        refuse(
            "workspace_mismatch",
            build_compensation,
            notice,
            current_workspace_uid=OTHER_WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=4,
        )
        refuse(
            "entity_mismatch",
            build_compensation,
            notice,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=OTHER_ENTITY,
            current_revision=4,
        )
        refuse(
            "revision_mismatch",
            build_compensation,
            notice,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=5,
        )


class CommitUnknown(unittest.TestCase):
    def test_commit_unknown_never_exposes_undo_or_a_retry_suggestion(self) -> None:
        notice = status_notice(commit_state=COMMIT_UNKNOWN, after_revision=None)
        self.assertFalse(notice["undoable"])
        self.assertIsNone(notice["after_revision"])
        self.assertNotIn("retry", notice["summary"].lower())
        self.assertNotIn(RETRY_FIELD, notice)
        _notice_validator().validate(notice)
        refuse(
            "commit_unknown",
            build_compensation,
            notice,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=3,
        )


class StatusVersusDeletion(unittest.TestCase):
    def test_task_status_compensation_does_not_claim_permanent_deletion_restore(
        self,
    ) -> None:
        notice = status_notice()
        compensation = build_compensation(
            notice,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=4,
        )
        self.assertEqual(compensation["requested_status"], "open")
        self.assertNotEqual(compensation["operation"], PERMANENT_DELETE_OPERATION)
        deleted = build_notice(
            **status_kwargs(
                operation=PERMANENT_DELETE_OPERATION,
                status_before=None,
                status_after=None,
            )
        )
        self.assertFalse(deleted["undoable"])
        self.assertIn("not undoable", deleted["summary"])
        _notice_validator().validate(deleted)
        refuse(
            "permanent_deletion",
            build_compensation,
            deleted,
            current_workspace_uid=WORKSPACE,
            current_entity_uid=ENTITY,
            current_revision=4,
        )

    def test_append_only_and_destructive_classes_are_not_undoable(self) -> None:
        cases = (
            ("worklog.append", "worklog"),
            ("capture.ingest", "capture"),
            ("authority.change", "workspace"),
            ("profile.change", "profile"),
            ("storage.migration", "storage"),
            ("storage.backup", "storage"),
            ("storage.restore", "storage"),
        )
        for operation, entity_kind in cases:
            with self.subTest(operation=operation):
                notice = build_notice(
                    **status_kwargs(
                        operation=operation,
                        entity_kind=entity_kind,
                        status_before=None,
                        status_after=None,
                    )
                )
                self.assertFalse(notice["undoable"])
                _notice_validator().validate(notice)
                refuse(
                    "not_undoable",
                    build_compensation,
                    notice,
                    current_workspace_uid=WORKSPACE,
                    current_entity_uid=ENTITY,
                    current_revision=4,
                )


class Serialization(unittest.TestCase):
    def test_serialization_is_deterministic_utf8_json_and_performs_no_io(self) -> None:
        notice = status_notice()
        shuffled = {key: notice[key] for key in reversed(list(notice))}
        with mock.patch.object(builtins, "open", side_effect=AssertionError("io")):
            left = serialize_notice(notice)
            right = serialize_notice(shuffled)
            compensation = serialize_compensation(
                build_compensation(
                    notice,
                    current_workspace_uid=WORKSPACE,
                    current_entity_uid=ENTITY,
                    current_revision=4,
                )
            )
        self.assertEqual(left, right)
        self.assertEqual(left, json.dumps(notice, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        self.assertEqual(hashlib.sha256(left).hexdigest(), hashlib.sha256(right).hexdigest())
        self.assertTrue(compensation.startswith(b"{"))
        self.assertNotIn(b"retryable", compensation)

    def test_module_is_pure_and_does_not_import_product_hotspots(self) -> None:
        self.assertEqual(mutation_notice.__name__, MODULE_NAME)
        self.assertEqual(build_notice.__module__, MODULE_NAME)
        self.assertEqual(Path(mutation_notice.__file__).resolve(), SOURCE_PATH)
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        opened = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ]
        self.assertEqual(opened, [])
        imported = _imported_modules(tree, MODULE_NAME.rpartition(".")[0])
        self.assertTrue(
            imported.isdisjoint(FORBIDDEN_HOTSPOTS),
            f"forbidden imports: {sorted(imported & FORBIDDEN_HOTSPOTS)}",
        )


if __name__ == "__main__":
    unittest.main()
