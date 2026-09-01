from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.contracts import validate_instance


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "contracts" / "workstack-ssot-v4"
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
VALID_CASES = CONTRACT_ROOT / "examples" / "valid" / "cases.json"
INVALID_CASES = CONTRACT_ROOT / "examples" / "invalid" / "cases.json"
MIGRATION_MAPPING = CONTRACT_ROOT / "MIGRATION-MAPPING-V3-TO-V4.md"

CONCRETE_SCHEMAS = {
    "activity-event.schema.json",
    "capture.schema.json",
    "note.schema.json",
    "objective.schema.json",
    "planning-status-event.schema.json",
    "reply.schema.json",
    "store.schema.json",
    "task.schema.json",
    "worklog-event.schema.json",
    "workspace.schema.json",
    "migration-receipt.schema.json",
    "idempotency-ledger.schema.json",
}
ALL_SCHEMAS = CONCRETE_SCHEMAS | {"common.schema.json"}


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _walk_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            yield reference
        for nested in value.values():
            yield from _walk_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_refs(nested)


def _resolve_pointer(document: Any, fragment: str) -> Any:
    if not fragment:
        return document
    if not fragment.startswith("/"):
        raise AssertionError(f"unsupported JSON Pointer fragment: #{fragment}")
    current = document
    for token in fragment[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        current = current[int(key)] if isinstance(current, list) else current[key]
    return current


def _replace_pointer(instance: Any, pointer: str, value: Any) -> None:
    if not pointer.startswith("/"):
        raise AssertionError(f"invalid replacement pointer: {pointer}")
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]
    target = instance
    for token in tokens[:-1]:
        target = target[int(token)] if isinstance(target, list) else target[token]
    leaf = tokens[-1]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value


def _error_keywords(errors: Iterator[Any]) -> set[str]:
    keywords: set[str] = set()
    pending = list(errors)
    while pending:
        error = pending.pop()
        keywords.add(str(error.validator))
        pending.extend(error.context)
    return keywords


class StorageV4SchemaArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {path.name: _load(path) for path in SCHEMA_ROOT.glob("*.schema.json")}
        cls.schema_by_id = {schema["$id"]: schema for schema in cls.schemas.values()}
        cls.registry = Registry().with_resources(
            (schema_id, Resource.from_contents(schema))
            for schema_id, schema in cls.schema_by_id.items()
        )
        cls.valid_cases = _load(VALID_CASES)["cases"]
        cls.invalid_cases = _load(INVALID_CASES)["cases"]

    def validator(self, schema_name: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[schema_name],
            registry=self.registry,
            format_checker=FormatChecker(),
        )

    def test_expected_artifact_roster_is_complete(self) -> None:
        self.assertEqual(ALL_SCHEMAS, set(self.schemas))
        self.assertTrue((CONTRACT_ROOT / "README.md").is_file())
        self.assertTrue(MIGRATION_MAPPING.is_file())
        self.assertTrue(VALID_CASES.is_file())
        self.assertTrue(INVALID_CASES.is_file())

    def test_schemas_are_valid_draft_2020_12_documents(self) -> None:
        self.assertEqual(len(self.schemas), len(self.schema_by_id), "schema $id values must be unique")
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertTrue(schema["$id"].startswith("https://workstack.local/"))
                self.assertIsInstance(schema.get("title"), str)
                self.assertTrue(schema["title"])
                Draft202012Validator.check_schema(schema)

    def test_every_reference_resolves_inside_the_offline_schema_set(self) -> None:
        for name, schema in self.schemas.items():
            for reference in _walk_refs(schema):
                with self.subTest(schema=name, reference=reference):
                    reference_uri, fragment = urldefrag(urljoin(schema["$id"], reference))
                    self.assertIn(reference_uri, self.schema_by_id)
                    _resolve_pointer(self.schema_by_id[reference_uri], fragment)

    def test_concrete_schemas_are_closed_and_have_valid_examples(self) -> None:
        self.assertEqual(CONCRETE_SCHEMAS, {case["schema"] for case in self.valid_cases})
        self.assertEqual(len(self.valid_cases), len({case["name"] for case in self.valid_cases}))
        for name in CONCRETE_SCHEMAS:
            with self.subTest(schema=name):
                schema = self.schemas[name]
                self.assertTrue(
                    schema.get("additionalProperties") is False
                    or schema.get("unevaluatedProperties") is False,
                    f"{name} must reject unknown top-level fields",
                )
        for case in self.valid_cases:
            with self.subTest(example=case["name"]):
                self.validator(case["schema"]).validate(case["instance"])

    def test_invalid_examples_fail_the_declared_keyword(self) -> None:
        valid_by_name = {case["name"]: case for case in self.valid_cases}
        self.assertEqual(CONCRETE_SCHEMAS, {case["schema"] for case in self.invalid_cases})
        self.assertEqual(len(self.invalid_cases), len({case["name"] for case in self.invalid_cases}))
        for case in self.invalid_cases:
            with self.subTest(case=case["name"]):
                base = valid_by_name[case["base"]]
                self.assertEqual(case["schema"], base["schema"])
                instance = copy.deepcopy(base["instance"])
                replacement = case["replace"]
                _replace_pointer(instance, replacement["path"], replacement["value"])
                errors = list(self.validator(case["schema"]).iter_errors(instance))
                self.assertTrue(errors, f"{case['name']} unexpectedly passed validation")
                validators = _error_keywords(iter(errors))
                self.assertIn(case["expected_keyword"], validators)

    def test_task_record_does_not_duplicate_authoritative_planning_status(self) -> None:
        task = next(case["instance"] for case in self.valid_cases if case["name"] == "task")
        self.assertNotIn("status", task)
        self.assertIn("planning status is intentionally absent", self.schemas["task.schema.json"]["$comment"].lower())

    def test_v3_semantic_hotspots_have_executable_v4_representations(self) -> None:
        examples = {case["name"]: case["instance"] for case in self.valid_cases}
        task = examples["task"]
        capture = examples["capture"]
        reply = examples["reply"]
        note = examples["note"]
        task_note = examples["task-annotation-note"]
        planning = examples["planning-status-event"]
        legacy_objective = examples["legacy-objective-without-source-revision"]
        check_in = examples["worklog-check-in-event"]
        entry = examples["worklog-entry-event"]
        session = examples["worklog-session-event"]

        self.assertIn("status", task["subtasks"][0])
        self.assertTrue(capture["normalized"]["action_items"])
        self.assertTrue(capture["task_hints"])
        self.assertTrue(capture["recent_revisions"])
        self.assertEqual(reply["capture_revision"], 1)
        self.assertEqual(reply["receipt"]["reply_display_id"], reply["display_id"])
        self.assertEqual(note["links"], ["O-6", "T-0031"])
        self.assertEqual(task_note["note_kind"], "task_annotation")
        self.assertEqual(task_note["created_at"], "2026-09-01")
        self.assertEqual(planning["legacy_fact_id"], "PS-000001")
        self.assertEqual(legacy_objective["revision_origin"], "legacy_missing")
        self.assertNotIn("description", legacy_objective)
        self.assertEqual(
            {examples[name]["kind"] for name in (
                "worklog-check-in-event", "worklog-entry-event", "worklog-session-event"
            )},
            {"check-in", "entry", "session"},
        )
        self.assertEqual(check_in["start_time"], "09:00")
        self.assertEqual(entry["duration_seconds"], 1800)
        self.assertEqual(entry["done"], ["Captured the field inventory"])
        self.assertEqual(session["state"], "stopped")
        self.assertEqual(session["worklog_state"], "recorded")
        self.assertEqual(len(session["segments"]), 2)

    def test_wave_three_runtime_contracts_are_activation_prerequisites(self) -> None:
        mapping = MIGRATION_MAPPING.read_text(encoding="utf-8")
        self.assertIn("idempotency-ledger.schema.json", mapping)
        self.assertIn("migration-receipt.schema.json", mapping)
        self.assertIn("must validate before candidate activation", mapping)

    def test_migration_receipt_is_content_free_and_records_verification_evidence(self) -> None:
        receipt = next(
            case["instance"] for case in self.valid_cases if case["name"] == "migration-receipt"
        )
        serialized = json.dumps(receipt, sort_keys=True)

        for forbidden in ("path", "title", "detail", "body", "raw", "password", "secret"):
            self.assertNotIn(f'"{forbidden}"', serialized)
        self.assertEqual(receipt["source"]["semantic_digest"], receipt["candidate"]["semantic_digest"])
        self.assertTrue(receipt["generated_id_roster"])
        self.assertEqual(receipt["task_note_source_roster"][0]["source_index"], 0)
        self.assertEqual(set(receipt["artifacts"]), {"backup", "candidate"})
        self.assertEqual(
            set(receipt["runtime_evidence"]),
            {"idempotency_ledger_digest", "idempotency_record_count"},
        )
        self.assertEqual(
            receipt["candidate"]["authority_digest"],
            receipt["artifacts"]["candidate"]["digest"],
        )
        self.assertTrue(all(receipt["checks"].values()))

    def test_idempotency_ledger_preserves_exact_bounded_response_forms(self) -> None:
        ledger = next(
            case["instance"] for case in self.valid_cases if case["name"] == "idempotency-ledger"
        )
        body_record, reference_record = ledger["records"]

        self.assertEqual(set(body_record) & {"response_body", "response_ref"}, {"response_body"})
        self.assertEqual(set(reference_record) & {"response_body", "response_ref"}, {"response_ref"})
        self.assertNotIn("request_body", json.dumps(ledger, sort_keys=True))
        self.assertIn("expires_at", body_record)
        self.assertLessEqual(ledger["compaction_policy"]["max_records"], 10000)

        invalid = copy.deepcopy(ledger)
        invalid["records"][0]["response_ref"] = copy.deepcopy(reference_record["response_ref"])
        errors = list(self.validator("idempotency-ledger.schema.json").iter_errors(invalid))
        self.assertTrue(errors)
        self.assertIn("oneOf", _error_keywords(iter(errors)))

    def test_runtime_artifacts_are_canonical_compatible_and_reject_secret_carriers(self) -> None:
        examples = {
            case["name"]: case["instance"]
            for case in self.valid_cases
            if case["name"] in {"migration-receipt", "idempotency-ledger"}
        }
        for name, instance in examples.items():
            with self.subTest(canonical=name):
                encoded = canonical_json_bytes(instance)
                self.assertEqual(json.loads(encoded), instance)

        canary = "VALUE_MUST_NOT_APPEAR"
        receipt = copy.deepcopy(examples["migration-receipt"])
        receipt["source"]["source_path"] = canary
        receipt_violations = validate_instance("migration-receipt.schema.json", receipt)
        self.assertTrue(receipt_violations)
        self.assertNotIn(canary, repr(receipt_violations))

        ledger = copy.deepcopy(examples["idempotency-ledger"])
        ledger["records"][0]["request_body"] = {"opaque": canary}
        ledger_violations = validate_instance("idempotency-ledger.schema.json", ledger)
        self.assertTrue(ledger_violations)
        self.assertNotIn(canary, repr(ledger_violations))

        for value, keyword in ((0.5, "type"), (9_007_199_254_740_992, "maximum")):
            with self.subTest(noncanonical=value):
                invalid = copy.deepcopy(examples["idempotency-ledger"])
                invalid["records"][0]["response_body"]["data"] = {"value": value}
                errors = list(
                    self.validator("idempotency-ledger.schema.json").iter_errors(invalid)
                )
                self.assertTrue(errors)
                self.assertIn(keyword, _error_keywords(iter(errors)))

    def test_runtime_ledger_schema_has_explicit_collection_and_text_bounds(self) -> None:
        schema = self.schemas["idempotency-ledger.schema.json"]
        self.assertEqual(schema["properties"]["records"]["maxItems"], 10000)
        self.assertEqual(schema["$defs"]["runtimeValue"]["oneOf"][1]["maxItems"], 512)
        runtime_object = schema["$defs"]["runtimeValue"]["oneOf"][2]
        self.assertEqual(runtime_object["maxProperties"], 256)
        self.assertEqual(runtime_object["propertyNames"]["maxLength"], 128)

    def test_activity_details_match_the_recursive_canonical_json_value_space(self) -> None:
        activity = next(
            case["instance"] for case in self.valid_cases if case["name"] == "activity-event"
        )
        validator = self.validator("activity-event.schema.json")
        validator.validate(activity)
        self.assertEqual(activity["details"]["nested"]["labels"], ["storage", "contract"])

        for value, keyword in ((0.5, "type"), (9_007_199_254_740_992, "maximum")):
            with self.subTest(value=value):
                invalid = copy.deepcopy(activity)
                invalid["details"] = {"outer": [{"value": value}]}
                errors = list(validator.iter_errors(invalid))
                self.assertTrue(errors)
                self.assertIn(keyword, _error_keywords(iter(errors)))


if __name__ == "__main__":
    unittest.main()
