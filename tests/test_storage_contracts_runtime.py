from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack.storage.contracts import (
    DEFAULT_SCHEMA_ROOT,
    StorageContractError,
    load_schema_catalog,
    require_valid_by_format,
    validate_by_format,
    validate_instance,
)


class StorageContractRuntimeTests(unittest.TestCase):
    def test_bundled_catalog_is_complete_and_meta_valid(self) -> None:
        catalog = load_schema_catalog()

        self.assertEqual(len(catalog), 13)
        self.assertTrue(all(identifier.startswith("https://workstack.local/") for identifier in catalog))

    def test_runtime_artifact_formats_dispatch_offline(self) -> None:
        valid_cases = json.loads(
            (DEFAULT_SCHEMA_ROOT.parent / "examples" / "valid" / "cases.json").read_text(
                encoding="utf-8"
            )
        )["cases"]
        examples = {case["name"]: case["instance"] for case in valid_cases}

        self.assertEqual(validate_by_format(examples["migration-receipt"]), ())
        self.assertEqual(validate_by_format(examples["idempotency-ledger"]), ())

    def test_valid_store_metadata_passes_offline_refs_and_format_checks(self) -> None:
        value = {
            "format": "workstack.ssot",
            "schema_version": 4,
            "schema_set": "workstack.ssot.v4",
            "workspace_uid": "11111111-1111-4111-8111-111111111111",
            "created_at": "2026-09-01T00:00:00Z",
        }

        self.assertEqual(validate_by_format(value), ())
        require_valid_by_format(value)

    def test_violations_report_paths_and_keywords_without_values(self) -> None:
        sensitive_value = "VALUE_MUST_NOT_APPEAR"
        value = {
            "format": "workstack.ssot",
            "schema_version": 4,
            "schema_set": "workstack.ssot.v4",
            "workspace_uid": sensitive_value,
            "created_at": "not-a-time",
        }

        violations = validate_instance("store.schema.json", value)
        rendered = repr(violations)
        self.assertTrue(any(item.instance_path == "/workspace_uid" for item in violations))
        self.assertTrue(
            any(item.instance_path == "/created_at" and item.code == "pattern" for item in violations)
        )
        self.assertNotIn(sensitive_value, rendered)
        with self.assertRaises(StorageContractError) as caught:
            require_valid_by_format(value)
        self.assertNotIn(sensitive_value, str(caught.exception))

    def test_unknown_format_and_non_object_fail_without_schema_dispatch(self) -> None:
        unknown = validate_by_format({"format": "workstack.future"})
        non_object = validate_by_format([])

        self.assertEqual(unknown[0].code, "unsupported_format")
        self.assertEqual(non_object[0].code, "type")

    def test_duplicate_schema_identifier_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = json.loads((DEFAULT_SCHEMA_ROOT / "store.schema.json").read_text(encoding="utf-8"))
            (root / "one.schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (root / "two.schema.json").write_text(json.dumps(schema), encoding="utf-8")

            with self.assertRaisesRegex(StorageContractError, "duplicate identifier"):
                load_schema_catalog(root)

    def test_untrusted_schema_catalog_failures_are_content_free(self) -> None:
        cases = {
            "malformed": "{",
            "non-object": "[]",
            "invalid-schema": json.dumps({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://workstack.local/invalid.schema.json",
                "type": 123,
            }),
            "missing-id": json.dumps({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }),
        }
        for name, body in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "case.schema.json").write_text(body, encoding="utf-8")
                load_schema_catalog.cache_clear()
                with self.assertRaises(StorageContractError) as raised:
                    load_schema_catalog(root)
                self.assertNotIn(body, str(raised.exception))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            load_schema_catalog.cache_clear()
            with self.assertRaisesRegex(StorageContractError, "catalog is empty"):
                load_schema_catalog(root)

        with self.assertRaisesRegex(StorageContractError, "unknown schema artifact"):
            validate_instance("missing.schema.json", {})


if __name__ == "__main__":
    unittest.main()
