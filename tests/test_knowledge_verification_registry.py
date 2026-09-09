"""Focused tests for the optional verifier stanza in the driver registry.

Nothing here starts a process, opens a socket, creates a Store or reads a real
operator directory. The registry files are written into a temporary directory
for the duration of one test, and the executables they pin do not exist: the
loader must admit a path without resolving, stating or launching it, and must
refuse a half-written verifier stanza *before* the caller would ever take a
store lease.

The central claim under test is that ``verification`` is optional in exactly
one direction. An entry written before the field existed keeps its exact former
behaviour, and an entry that names the field is held to the whole closed shape.
"""

from __future__ import annotations

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
    REGISTRY_SCHEMA,
    KnowledgeDriverBinding,
    KnowledgeDriverConfigurationError,
    KnowledgeVerificationBinding,
    load_driver_registry,
)
from workstack.knowledge_execution_runtime import admit_drivers  # noqa: E402

UPSTREAM = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
SECRET = "SUPER-SECRET-TOKEN-VALUE"
SEARCH_EXE = os.path.join(os.path.abspath(os.sep), "opt", "adapters", "od-adapter")
VERIFY_EXE = os.path.join(os.path.abspath(os.sep), "opt", "adapters", "od-verifier")


def _verification(**overrides: object) -> dict[str, object]:
    stanza: dict[str, object] = {
        "command": [VERIFY_EXE, "--verify"],
        "environment": {"WORKSTACK_OD_VERIFIER_CONFIG": SECRET},
    }
    stanza.update(overrides)
    return stanza


def _driver(**overrides: object) -> dict[str, object]:
    driver: dict[str, object] = {
        "alias": "od-primary",
        "upstream_workspace_uid": UPSTREAM,
        "command": [SEARCH_EXE, "--serve"],
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

    def _load(self, *drivers: dict[str, object]):
        return load_driver_registry(self._write(_document(*drivers)))

    def _refuse(self, *drivers: dict[str, object], code: str) -> None:
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            load_driver_registry(self._write(_document(*drivers)))
        self.assertEqual(caught.exception.code, code)


class LegacyEntryTests(_RegistryTestCase):
    """An entry written before R20 must behave exactly as it did before R20."""

    def test_no_flag_is_still_the_unchanged_absent_registry(self) -> None:
        self.assertIsNone(load_driver_registry(None))

    def test_a_legacy_entry_is_admitted_with_no_verifier(self) -> None:
        registry = self._load(_driver())
        self.assertEqual(set(registry), {"od-primary"})
        binding = registry["od-primary"]
        self.assertEqual(binding.upstream_workspace_uid, UPSTREAM)
        self.assertEqual(binding.command, (SEARCH_EXE, "--serve"))
        self.assertEqual(dict(binding.environment), {"OD_KEY_FILE": SECRET})
        self.assertIsNone(binding.verification)

    def test_absence_is_never_read_as_an_enabled_verifier(self) -> None:
        registry = self._load(
            _driver(),
            _driver(alias="od-secondary", command=[SEARCH_EXE]),
        )
        self.assertEqual(
            [binding.verification for binding in registry.values()], [None, None]
        )

    def test_legacy_refusals_keep_their_exact_codes(self) -> None:
        self._refuse(_driver(alias="OD-Primary"), code="invalid_driver_alias")
        self._refuse(_driver(), _driver(), code="duplicate_driver_alias")
        self._refuse(_driver(command=["od-adapter"]), code="invalid_driver_command")
        self._refuse(
            _driver(environment={"OD_KEY": "a", "od_key": "b"}),
            code="duplicate_driver_environment_name",
        )
        self._refuse(
            _driver(upstream_workspace_uid="nope"),
            code="invalid_driver_upstream_workspace_uid",
        )
        self._refuse(_driver(extra=1), code="unknown_field")
        missing = _driver()
        del missing["environment"]
        self._refuse(missing, code="missing_field")

    def test_a_positional_legacy_binding_still_constructs(self) -> None:
        binding = KnowledgeDriverBinding(UPSTREAM, (SEARCH_EXE,), {"A": "B"})
        self.assertIsNone(binding.verification)
        admitted = admit_drivers({"od-primary": binding})
        self.assertIsNone(admitted["od-primary"].verification)

    def test_the_search_command_is_untouched_by_a_verifier_stanza(self) -> None:
        registry = self._load(_driver(verification=_verification()))
        binding = registry["od-primary"]
        self.assertEqual(binding.command, (SEARCH_EXE, "--serve"))
        self.assertEqual(dict(binding.environment), {"OD_KEY_FILE": SECRET})


class VerifierStanzaTests(_RegistryTestCase):
    def test_an_explicit_stanza_pins_its_own_argv_and_environment(self) -> None:
        registry = self._load(_driver(verification=_verification()))
        verification = registry["od-primary"].verification
        self.assertIsInstance(verification, KnowledgeVerificationBinding)
        self.assertEqual(verification.command, (VERIFY_EXE, "--verify"))
        self.assertEqual(
            dict(verification.environment),
            {"WORKSTACK_OD_VERIFIER_CONFIG": SECRET},
        )

    def test_an_empty_verifier_environment_is_admitted(self) -> None:
        registry = self._load(_driver(verification=_verification(environment={})))
        self.assertEqual(dict(registry["od-primary"].verification.environment), {})

    def test_the_alias_and_upstream_identity_cover_both_operations(self) -> None:
        registry = self._load(
            _driver(verification=_verification()),
            _driver(alias="od-secondary", upstream_workspace_uid=UPSTREAM),
        )
        self.assertEqual(
            registry["od-primary"].upstream_workspace_uid,
            registry["od-secondary"].upstream_workspace_uid,
        )
        self.assertIsNotNone(registry["od-primary"].verification)
        self.assertIsNone(registry["od-secondary"].verification)

    def test_neither_pin_reaches_a_repr(self) -> None:
        registry = self._load(_driver(verification=_verification()))
        rendered = repr(dict(registry))
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn(VERIFY_EXE, rendered)
        self.assertNotIn(SEARCH_EXE, rendered)
        self.assertIn("verification=KnowledgeVerificationBinding()", rendered)


class MalformedVerifierRefusalTests(_RegistryTestCase):
    """Every malformed stanza refuses at load, before a server could exist."""

    def test_an_explicit_null_is_not_a_way_to_spell_the_default(self) -> None:
        self._refuse(
            _driver(verification=None), code="invalid_driver_verification"
        )

    def test_a_non_object_stanza_is_refused(self) -> None:
        for value in ([VERIFY_EXE], VERIFY_EXE, 7, True):
            self._refuse(
                _driver(verification=value), code="invalid_driver_verification"
            )

    def test_an_empty_or_half_written_stanza_is_refused(self) -> None:
        self._refuse(_driver(verification={}), code="missing_field")
        self._refuse(
            _driver(verification={"command": [VERIFY_EXE]}), code="missing_field"
        )
        self._refuse(
            _driver(verification={"environment": {}}), code="missing_field"
        )

    def test_an_unknown_key_inside_the_stanza_is_refused(self) -> None:
        self._refuse(
            _driver(verification=_verification(timeout_seconds=30)),
            code="unknown_field",
        )
        self._refuse(
            _driver(verification=_verification(upstream_workspace_uid=UPSTREAM)),
            code="unknown_field",
        )

    def test_a_relative_or_malformed_verifier_command_is_refused(self) -> None:
        for value in (
            ["od-verifier"],
            [],
            [VERIFY_EXE, ""],
            [VERIFY_EXE, 7],
            VERIFY_EXE,
            [VERIFY_EXE] * 17,
        ):
            self._refuse(
                _driver(verification=_verification(command=value)),
                code="invalid_verification_command",
            )

    def test_a_malformed_verifier_environment_is_refused(self) -> None:
        # A non-object environment and a non-string *name* are the loader's own
        # refusals; a non-string *value* is caught by the shared transport
        # predicate the runtime applies, under the verifier's own code. Both
        # arms refuse the start; neither leaves a half-admitted registry.
        for value in ([], "OD=1", 7):
            self._refuse(
                _driver(verification=_verification(environment=value)),
                code="invalid_driver_environment",
            )
        self._refuse(
            _driver(verification=_verification(environment={"A": 1})),
            code="invalid_verification_environment",
        )

    def test_case_variant_verifier_environment_names_are_refused(self) -> None:
        self._refuse(
            _driver(
                verification=_verification(
                    environment={"OD_VERIFIER_CONFIG": "a", "od_verifier_config": "b"}
                )
            ),
            code="duplicate_driver_environment_name",
        )

    def test_a_malformed_stanza_refuses_the_whole_registry(self) -> None:
        self._refuse(
            _driver(),
            _driver(alias="od-secondary", verification={"command": [VERIFY_EXE]}),
            code="missing_field",
        )

    def test_the_strict_decoder_still_governs_a_document_naming_a_verifier(
        self,
    ) -> None:
        document = _document(_driver(verification=_verification()))
        duplicated = document.replace('"command"', '"command": [], "command"', 1)
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            load_driver_registry(self._write(duplicated))
        self.assertEqual(caught.exception.code, "duplicate_json_key")

    def test_no_refusal_echoes_the_operator_command_or_environment(self) -> None:
        for entry, code in (
            (_driver(verification=None), "invalid_driver_verification"),
            (_driver(verification={"command": [VERIFY_EXE]}), "missing_field"),
            (
                _driver(verification=_verification(command=["od-verifier"])),
                "invalid_verification_command",
            ),
        ):
            with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
                load_driver_registry(self._write(_document(entry)))
            rendered = "{}|{}".format(caught.exception.code, caught.exception)
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(VERIFY_EXE, rendered)
            self.assertNotIn(str(self.directory), rendered)


class DirectAdmissionTests(unittest.TestCase):
    """``admit_drivers`` is the same gate for an embedder-supplied registry."""

    def _binding(self, **overrides: object) -> KnowledgeDriverBinding:
        fields: dict[str, object] = {
            "upstream_workspace_uid": UPSTREAM,
            "command": (SEARCH_EXE,),
            "environment": {"OD_KEY_FILE": SECRET},
        }
        fields.update(overrides)
        return KnowledgeDriverBinding(**fields)  # type: ignore[arg-type]

    def test_a_verifier_binding_is_copied_and_frozen(self) -> None:
        environment = {"WORKSTACK_OD_VERIFIER_CONFIG": SECRET}
        command = [VERIFY_EXE, "--verify"]
        admitted = admit_drivers(
            {
                "od-primary": self._binding(
                    verification=KnowledgeVerificationBinding(
                        command=command, environment=environment
                    )
                )
            }
        )
        verification = admitted["od-primary"].verification
        command.append("--injected")
        environment["WORKSTACK_OD_VERIFIER_CONFIG"] = "replaced"
        self.assertEqual(verification.command, (VERIFY_EXE, "--verify"))
        self.assertEqual(
            dict(verification.environment),
            {"WORKSTACK_OD_VERIFIER_CONFIG": SECRET},
        )
        self.assertIsInstance(verification.environment, MappingProxyType)
        with self.assertRaises(TypeError):
            verification.environment["X"] = "1"  # type: ignore[index]

    def test_a_wrong_verifier_type_is_refused(self) -> None:
        for value in ({"command": [VERIFY_EXE]}, (VERIFY_EXE,), "verifier", 0):
            with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
                admit_drivers({"od-primary": self._binding(verification=value)})
            self.assertEqual(caught.exception.code, "invalid_driver_verification")

    def test_verifier_argv_and_environment_are_admitted_separately(self) -> None:
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            admit_drivers(
                {
                    "od-primary": self._binding(
                        verification=KnowledgeVerificationBinding(
                            command=("od-verifier",), environment={}
                        )
                    )
                }
            )
        self.assertEqual(caught.exception.code, "invalid_verification_command")
        with self.assertRaises(KnowledgeDriverConfigurationError) as caught:
            admit_drivers(
                {
                    "od-primary": self._binding(
                        verification=KnowledgeVerificationBinding(
                            command=(VERIFY_EXE,), environment={"A": 1}
                        )
                    )
                }
            )
        self.assertEqual(caught.exception.code, "invalid_verification_environment")

    def test_an_admitted_registry_without_verifiers_is_unchanged(self) -> None:
        admitted = admit_drivers({"od-primary": self._binding()})
        binding = admitted["od-primary"]
        self.assertIsNone(binding.verification)
        self.assertEqual(binding.command, (SEARCH_EXE,))
        self.assertIsInstance(binding.environment, MappingProxyType)


if __name__ == "__main__":
    unittest.main()
