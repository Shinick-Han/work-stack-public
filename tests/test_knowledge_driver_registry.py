"""Focused tests for the operator's knowledge-driver registry file.

Nothing here starts a process, opens a socket, creates a Store or reads a real
operator directory. The registry files are written into a temporary directory
for the duration of one test, and the executables they pin do not exist: the
loader must admit a path without resolving, stating or launching it.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack.knowledge_driver_registry import (  # noqa: E402
    MAX_CONFIG_BYTES,
    REGISTRY_SCHEMA,
    KnowledgeDriverConfigurationError,
    load_driver_registry,
)

UPSTREAM = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
SECRET = "SUPER-SECRET-TOKEN-VALUE"
EXECUTABLE = os.path.join(os.path.abspath(os.sep), "opt", "adapters", "od-adapter")


def _driver(**overrides: object) -> dict[str, object]:
    driver: dict[str, object] = {
        "alias": "od-primary",
        "upstream_workspace_uid": UPSTREAM,
        "command": [EXECUTABLE, "--serve"],
        "environment": {"OD_KEY_FILE": SECRET},
    }
    driver.update(overrides)
    return driver


def _document(*drivers: dict[str, object], schema: str = REGISTRY_SCHEMA) -> str:
    return json.dumps({"schema": schema, "drivers": list(drivers) or [_driver()]})


class _RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def _write(self, text: str | bytes, name: str = "drivers.json") -> str:
        path = self.directory / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return str(path)

    def _refusal(self, text: str | bytes) -> KnowledgeDriverConfigurationError:
        path = self._write(text)
        before = Path(path).read_bytes()
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            load_driver_registry(path)
        # A refused registry is never rewritten, repaired or truncated.
        self.assertEqual(before, Path(path).read_bytes())
        return caught.exception


class DriverRegistryFileTests(_RegistryTestCase):
    def test_no_flag_is_the_unchanged_default(self) -> None:
        self.assertIsNone(load_driver_registry(None))

    def test_one_admitted_driver_carries_its_argv_and_environment(self) -> None:
        path = self._write(_document())

        drivers = load_driver_registry(path)

        self.assertEqual(["od-primary"], list(drivers))
        binding = drivers["od-primary"]
        self.assertEqual(UPSTREAM, binding.upstream_workspace_uid)
        self.assertEqual((EXECUTABLE, "--serve"), binding.command)
        self.assertEqual({"OD_KEY_FILE": SECRET}, dict(binding.environment))
        # The pinned executable is admitted without being resolved or started.
        self.assertFalse(os.path.exists(EXECUTABLE))

    def test_the_admitted_registry_is_an_immutable_copy(self) -> None:
        drivers = load_driver_registry(self._write(_document()))

        self.assertIsInstance(drivers, MappingProxyType)
        with self.assertRaises(TypeError):
            drivers["other"] = drivers["od-primary"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            drivers["od-primary"].environment["OD_KEY_FILE"] = "changed"

    def test_eight_drivers_are_admitted_and_nine_are_refused(self) -> None:
        eight = [_driver(alias="od-{}".format(index)) for index in range(8)]
        self.assertEqual(8, len(load_driver_registry(self._write(_document(*eight)))))

        nine = [_driver(alias="od-{}".format(index)) for index in range(9)]
        self.assertEqual("driver_registry_full", self._refusal(_document(*nine)).code)

    def test_the_named_file_is_read_exactly_once_and_nothing_else_is(self) -> None:
        path = self._write(_document())
        opened: list[object] = []
        real_open = builtins.open

        def counting_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(file)
            return real_open(file, *args, **kwargs)

        original = builtins.open
        builtins.open = counting_open
        try:
            load_driver_registry(path)
        finally:
            builtins.open = original

        self.assertEqual([path], [str(entry) for entry in opened])


class DriverRegistryRefusalTests(_RegistryTestCase):
    def test_a_relative_path_is_refused_before_any_read(self) -> None:
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            load_driver_registry(os.path.join("relative", "drivers.json"))
        self.assertEqual("driver_config_path_not_absolute", caught.exception.code)

    def test_an_unreadable_file_refuses_without_naming_the_path(self) -> None:
        missing = str(self.directory / "absent.json")
        with self.assertRaises(OSError) as caught:
            load_driver_registry(missing)
        self.assertEqual("driver_config_unreadable", str(caught.exception))

        with self.assertRaises(OSError) as directory_case:
            load_driver_registry(str(self.directory))
        self.assertEqual("driver_config_unreadable", str(directory_case.exception))

    def test_an_oversized_file_is_refused_and_left_untouched(self) -> None:
        padded = json.dumps({
            "schema": REGISTRY_SCHEMA,
            "drivers": [_driver()],
            "padding": "x" * MAX_CONFIG_BYTES,
        })
        self.assertGreater(len(padded.encode("utf-8")), MAX_CONFIG_BYTES)
        self.assertEqual("driver_config_too_large", self._refusal(padded).code)

    def test_the_decoder_grammar_travels_unchanged(self) -> None:
        cases = {
            "duplicate_json_key": '{"schema": "a", "schema": "b", "drivers": []}',
            "non_finite_number": '{"schema": "a", "drivers": 1e9999}',
            "invalid_json": '{"schema": "a", "drivers": [},',
        }
        for code, text in cases.items():
            with self.subTest(code=code):
                self.assertEqual(code, self._refusal(text).code)
        self.assertEqual(
            "invalid_encoding", self._refusal(b'{"schema": "\xff"}').code
        )

    def test_the_top_level_document_is_exactly_the_v1_shape(self) -> None:
        cases = {
            "invalid_driver_registry": "[]",
            "unknown_field": json.dumps({
                "schema": REGISTRY_SCHEMA, "drivers": [_driver()], "extra": 1
            }),
            "missing_field": json.dumps({"schema": REGISTRY_SCHEMA}),
            "unknown_driver_registry_schema": _document(schema="workstack.other.v1"),
            "driver_registry_empty": json.dumps(
                {"schema": REGISTRY_SCHEMA, "drivers": []}
            ),
        }
        for code, text in cases.items():
            with self.subTest(code=code):
                self.assertEqual(code, self._refusal(text).code)
        self.assertEqual(
            "invalid_driver_registry",
            self._refusal(json.dumps({"schema": REGISTRY_SCHEMA, "drivers": {}})).code,
        )

    def test_each_entry_is_exactly_the_four_pinned_fields(self) -> None:
        entry = _driver()
        entry["extra"] = 1
        self.assertEqual("unknown_field", self._refusal(_document(entry)).code)

        short = _driver()
        del short["environment"]
        self.assertEqual("missing_field", self._refusal(_document(short)).code)
        self.assertEqual(
            "invalid_driver_binding", self._refusal(_document("driver")).code
        )

    def test_a_repeated_alias_is_refused_rather_than_resolved(self) -> None:
        self.assertEqual(
            "duplicate_driver_alias",
            self._refusal(_document(_driver(), _driver(command=[EXECUTABLE]))).code,
        )

    def test_environment_names_differing_only_in_case_are_refused(self) -> None:
        collision = _driver(environment={"OD_KEY_FILE": SECRET, "od_key_file": "b"})
        self.assertEqual(
            "duplicate_driver_environment_name",
            self._refusal(_document(collision)).code,
        )
        self.assertEqual(
            "invalid_driver_environment",
            self._refusal(_document(_driver(environment=[]))).code,
        )

    def test_the_transport_shape_rules_are_the_admitting_ones(self) -> None:
        cases = {
            "invalid_driver_alias": _driver(alias="Not An Alias"),
            "invalid_driver_upstream_workspace_uid": _driver(
                upstream_workspace_uid="00000000-0000-0000-0000-000000000000"
            ),
            "invalid_driver_command": _driver(command=["od-adapter", "--serve"]),
            "invalid_driver_environment": _driver(environment={"OD_KEY_FILE": 7}),
        }
        for code, entry in cases.items():
            with self.subTest(code=code):
                self.assertEqual(code, self._refusal(_document(entry)).code)

    def test_no_refusal_repeats_the_document_the_operator_wrote(self) -> None:
        documents = (
            _document(_driver(alias="Not An Alias")),
            _document(_driver(command=["od-adapter"])),
            _document(_driver(environment={"OD_KEY_FILE": SECRET, "od_key_file": "b"})),
            _document(_driver(), _driver()),
        )
        for document in documents:
            with self.subTest(document=document[:40]):
                message = str(self._refusal(document))
                for leaked in (SECRET, EXECUTABLE, "od-primary", "OD_KEY_FILE"):
                    self.assertNotIn(leaked, message)


if __name__ == "__main__":
    unittest.main()
