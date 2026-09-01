from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from workstack.snapshot import (
    SNAPSHOT_FORMAT,
    build_snapshot,
    canonical_snapshot_bytes,
    snapshot_digest,
    SnapshotValidationError,
    validate_snapshot_bytes,
    validate_snapshot_object,
)
from workstack.snapshot_conformance import run_conformance_kit
from workstack import snapshot_safety
from workstack.snapshot_safety import evaluate_safety
from workstack.unicode17 import UNICODE_DATA_VERSION, normalize_nfc


KIT = Path(__file__).resolve().parents[1] / "contracts" / "workstack-conduit-v1"


class SnapshotV1ConformanceTest(unittest.TestCase):
    def test_frozen_kit_passes_in_the_shipping_python_runtime(self) -> None:
        report = run_conformance_kit(KIT)
        self.assertEqual(report["contract_sha256"], "cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70")
        self.assertEqual(report["safety_root"], "sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148")
        self.assertEqual(report["kit_root"], "sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9")
        self.assertEqual(report["valid"], 2)
        self.assertEqual(report["invalid"], 44)
        self.assertEqual(report["safety"], 38)
        self.assertEqual(report["text_boundaries"], 17)
        self.assertEqual(report["unicode_data_version"], "17.0.0")

    def test_unicode_17_discriminator_does_not_use_host_unicode_15(self) -> None:
        discriminator = "".join(chr(value) for value in (97, 2199, 803))
        self.assertEqual(UNICODE_DATA_VERSION, "17.0.0")
        self.assertNotEqual(normalize_nfc(discriminator), discriminator)
        self.assertEqual(
            [ord(value) for value in normalize_nfc(discriminator)],
            [0x1EA1, 0x0897],
        )

    def test_builder_emits_only_the_frozen_shape_and_exact_canonical_bytes(self) -> None:
        workspace_uid = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
        task = {
            "id": "T-0031",
            "uid": "2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
            "revision": 3,
            "title": "Prepare the provider-neutral execution adapter",
            "detail": "Explain the desired work without credentials or raw provider traffic.",
            "priority": "P1",
            "due": None,
        }
        snapshot = build_snapshot(workspace_uid, task, "open")
        self.assertEqual(snapshot["format"], SNAPSHOT_FORMAT)
        self.assertEqual(
            set(snapshot),
            {
                "detail",
                "due_date",
                "format",
                "legacy_task_id",
                "origin_ref",
                "planning_priority",
                "planning_status",
                "planning_task_uid",
                "revision",
                "title",
                "workspace_uid",
            },
        )
        expected = KIT.joinpath("fixtures/valid/basic.snapshot.json").read_bytes()
        self.assertEqual(canonical_snapshot_bytes(snapshot), expected)
        self.assertEqual(
            snapshot_digest(expected),
            "sha256:f0ab308fb6a66d36ba6dc4e2dfddc5c9e0fec1cf7fe76e110bf85cd0bc05b3e4",
        )

    def test_malformed_http_authority_does_not_trigger_s005(self) -> None:
        for value in (
            "Document malformed http://user:abcdefgh@[ reference",
            "Document malformed http://user:abcdefgh@host:abc reference",
        ):
            with self.subTest(value=value):
                self.assertEqual(evaluate_safety(value, "detail"), {"decision": "ALLOW"})

    def test_valid_rfc3986_authority_variants_still_trigger_s005(self) -> None:
        for value in (
            "http://user:abcdefgh" + "@" + "[v1.foo]",
            "http://user:abcdefgh" + "@" + "example.com:",
            "http://%FF:abcdefgh" + "@" + "example.com",
            "http://user:abcdefgh" + "@",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    evaluate_safety(value, "detail"),
                    {
                        "decision": "REFUSE",
                        "code": "SNAPSHOT_CREDENTIAL_SUSPECTED",
                        "rule": "S005",
                    },
                )

    def test_s005_authority_boundary_is_characterized(self) -> None:
        at = chr(64)
        valid = (
            "user:abcdefgh" + at + "example.com",
            "user:abcdefgh" + at + "example.com:",
            "user:abcdefgh" + at + "%41.example",
            "user:abcdefgh" + at + "[::1]",
            "user:abcdefgh" + at + "[::1]:443",
            "user:abcdefgh" + at + "[v1.foo]",
            "user:abcdefgh" + at,
        )
        invalid = (
            "user:abcdefgh",
            at + "example.com",
            "%GG:abcdefgh" + at + "example.com",
            "user:abcdefgh" + at + "café.example",
            "user:abcdefgh" + at + "[",
            "user:abcdefgh" + at + "[127.0.0.1]",
            "user:abcdefgh" + at + "[v.foo]",
            "user:abcdefgh" + at + "[v1.]",
            "user:abcdefgh" + at + "[::1]tail",
            "user:abcdefgh" + at + "[::1]:abc",
            "user:abcdefgh" + at + "::1",
            "user:abcdefgh" + at + ":443",
            "user:abcdefgh" + at + "example.com:abc",
            "user:abcdefgh" + at + "example[com",
            "user:abcdefgh" + at + "example%",
            "user:abcdefgh" + at + "example%G0",
        )

        for authority in valid:
            with self.subTest(authority=authority, expected="valid"):
                self.assertTrue(snapshot_safety._authority_valid(authority))
        for authority in invalid:
            with self.subTest(authority=authority, expected="invalid"):
                self.assertFalse(snapshot_safety._authority_valid(authority))

    def test_s005_public_decision_tracks_authority_validity(self) -> None:
        at = chr(64)
        for authority, decision in (
            ("user:abcdefgh" + at + "%41.example", "REFUSE"),
            ("user:abcdefgh" + at + "[::1]:443", "REFUSE"),
            ("user:abcdefgh" + at + "[v1.foo]", "REFUSE"),
            ("user:abcdefgh" + at + "[127.0.0.1]", "ALLOW"),
            ("user:abcdefgh" + at + "[::1]:abc", "ALLOW"),
            ("user:abcdefgh" + at + "example%G0", "ALLOW"),
        ):
            with self.subTest(authority=authority):
                self.assertEqual(
                    evaluate_safety("Document http://" + authority, "detail")["decision"],
                    decision,
                )

    def test_duplicate_key_precedes_revision_numeric_form(self) -> None:
        raw = KIT.joinpath("fixtures/valid/basic.snapshot.json").read_bytes()
        raw = raw.replace(b'"revision":3', b'"revision":3e0', 1)
        raw = raw.replace(b'}\n', b',"title":"Duplicate"}\n', 1)
        with self.assertRaises(SnapshotValidationError) as raised:
            validate_snapshot_bytes(raw)
        self.assertEqual((raised.exception.stage, raised.exception.reason), ("JSON_OBJECT", "DUPLICATE_KEY"))

    def test_duplicate_key_scanning_ignores_key_like_text_inside_a_string(self) -> None:
        candidate = json.loads(
            KIT.joinpath("fixtures/valid/basic.snapshot.json").read_text(encoding="utf-8")
        )
        candidate["detail"] = 'Literal text: \\"title\\":\\"not a member\\"'

        raw = canonical_snapshot_bytes(candidate)

        self.assertEqual(validate_snapshot_bytes(raw), candidate)

    def test_nested_duplicate_key_is_refused_before_field_type_validation(self) -> None:
        raw = KIT.joinpath("fixtures/valid/basic.snapshot.json").read_bytes()
        raw = re.sub(
            rb'"detail":"[^"]*"',
            b'"detail":{"nested":1,"nested":2}',
            raw,
            count=1,
        )

        with self.assertRaises(SnapshotValidationError) as raised:
            validate_snapshot_bytes(raw)

        self.assertEqual(
            (raised.exception.stage, raised.exception.reason),
            ("JSON_OBJECT", "DUPLICATE_KEY"),
        )

    def test_noncanonical_line_ending_precedes_duplicate_detection(self) -> None:
        raw = KIT.joinpath("fixtures/valid/basic.snapshot.json").read_bytes()
        raw = raw.replace(b'}\n', b',"title":"Duplicate"}\r\n', 1)

        with self.assertRaises(SnapshotValidationError) as raised:
            validate_snapshot_bytes(raw)

        self.assertEqual(
            (raised.exception.stage, raised.exception.reason),
            ("BYTE_ENVELOPE", "NONCANONICAL_LINE_ENDING"),
        )

    def test_unhashable_enum_values_refuse_as_wrong_type(self) -> None:
        base = json.loads(
            KIT.joinpath("fixtures/valid/basic.snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        for field, value in (("planning_status", []), ("planning_priority", {})):
            with self.subTest(field=field):
                candidate = dict(base)
                candidate[field] = value
                with self.assertRaises(SnapshotValidationError) as raised:
                    validate_snapshot_object(candidate)
                self.assertEqual(
                    (raised.exception.stage, raised.exception.reason, raised.exception.field),
                    ("FIELD", "WRONG_TYPE", field),
                )

    def test_unmanifested_kit_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "kit"
            shutil.copytree(KIT, copied)
            copied.joinpath("unexpected.txt").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "roster"):
                run_conformance_kit(copied)


if __name__ == "__main__":
    unittest.main()
