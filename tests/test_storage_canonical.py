from __future__ import annotations

import math
import unittest

from workstack.storage import (
    CANONICAL_JSON_FORMAT,
    MAX_CANONICAL_INTEGER,
    CanonicalJsonError,
    canonical_json_bytes,
    canonical_sha256,
)


class CanonicalJsonTest(unittest.TestCase):
    def test_format_identifier_and_exact_compact_bytes_are_frozen(self) -> None:
        self.assertEqual(CANONICAL_JSON_FORMAT, "workstack.canonical-json.v1")
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            b'{"a":1,"b":2}',
        )
        self.assertEqual(
            canonical_sha256({"b": 2, "a": 1}),
            "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
        )

    def test_nested_key_order_and_insertion_order_do_not_change_bytes(self) -> None:
        first = {
            "z": [{"z": 0, "a": 1}],
            "outer": {"\u03b2": 2, "a": 1},
        }
        second = {
            "outer": {"a": 1, "\u03b2": 2},
            "z": [{"a": 1, "z": 0}],
        }
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(
            canonical_json_bytes({"\u00e4": 3, "z": 1, "a": 2}).decode("utf-8"),
            '{"a":2,"z":1,"\u00e4":3}',
        )

    def test_unicode_is_utf8_strict_and_not_normalized(self) -> None:
        decomposed = "e\u0301"
        composed = "\u00e9"
        encoded = canonical_json_bytes(
            {"combining": decomposed, "emoji": "\U0001f642", "\ud55c\uae00": "\uac12"}
        )
        self.assertEqual(
            encoded.decode("utf-8"),
            '{"combining":"e\u0301","emoji":"\U0001f642","\ud55c\uae00":"\uac12"}',
        )
        self.assertNotEqual(
            canonical_json_bytes({"text": decomposed}),
            canonical_json_bytes({"text": composed}),
        )
        with self.assertRaisesRegex(CanonicalJsonError, "INVALID_UNICODE"):
            canonical_json_bytes({"text": "\ud800"})

    def test_json_scalars_and_safe_integer_boundaries_are_supported(self) -> None:
        value = [None, True, False, -MAX_CANONICAL_INTEGER, 0, MAX_CANONICAL_INTEGER, "text"]
        self.assertEqual(
            canonical_json_bytes(value),
            (
                '[null,true,false,-9007199254740991,0,9007199254740991,"text"]'
            ).encode("utf-8"),
        )

    def test_every_float_is_rejected_including_non_finite_values(self) -> None:
        for value in (0.0, -1.25, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CanonicalJsonError, "UNSUPPORTED_FLOAT"):
                    canonical_json_bytes({"value": value})

    def test_non_string_keys_are_rejected_at_any_depth(self) -> None:
        with self.assertRaises(CanonicalJsonError) as raised:
            canonical_json_bytes({"outer": {1: "must not be disclosed"}})
        self.assertEqual(raised.exception.code, "NON_STRING_KEY")
        self.assertNotIn("must not be disclosed", str(raised.exception))

    def test_unsafe_integers_are_rejected(self) -> None:
        for value in (-MAX_CANONICAL_INTEGER - 1, MAX_CANONICAL_INTEGER + 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CanonicalJsonError, "UNSAFE_INTEGER"):
                    canonical_json_bytes(value)

    def test_non_json_containers_and_values_are_rejected(self) -> None:
        for value in ((1, 2), {1, 2}, b"bytes", object()):
            with self.subTest(type=type(value).__name__):
                with self.assertRaisesRegex(CanonicalJsonError, "UNSUPPORTED_TYPE"):
                    canonical_json_bytes(value)

    def test_direct_and_indirect_cycles_are_rejected(self) -> None:
        direct: list[object] = []
        direct.append(direct)
        indirect: dict[str, object] = {}
        indirect["loop"] = [indirect]
        for value in (direct, indirect):
            with self.subTest(type=type(value).__name__):
                with self.assertRaisesRegex(CanonicalJsonError, "CYCLIC_VALUE"):
                    canonical_json_bytes(value)

    def test_repeated_references_without_cycles_are_supported(self) -> None:
        shared = {"b": 2, "a": 1}
        self.assertEqual(
            canonical_json_bytes([shared, shared]),
            b'[{"a":1,"b":2},{"a":1,"b":2}]',
        )


if __name__ == "__main__":
    unittest.main()
