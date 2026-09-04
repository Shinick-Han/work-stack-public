from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
import typing
import unittest
from unittest import mock

from workstack import agent_cli_contract as contract


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "quality" / "agent-p0-oracle" / "manifest.v1.json"
WORKSPACE_UID = "123e4567-e89b-42d3-a456-426614174000"
TASK_ID = "T-12345"
INTENT_ID = "intent:_1"
CONTRACT_STRING = "workstack.cli.v1"
STATUS_WIRE = "agent.status"
CONTEXT_WIRE = "agent.context"
CHECKPOINT_WIRE = "agent.checkpoint"
EXPECTED_FIXTURE_SHA256 = (
    "4a93a811c76afe0208aa9d9e11ed026e6735d5f5f7c62f6bd014a5b26ab6e8d3"
)
EXPECTED_EXPORTS = (
    "AuthorityAdmission",
    "AgentBackend",
    "AgentOutcome",
    "CHECKPOINT_COMMAND",
    "CheckpointRequest",
    "CONTEXT_COMMAND",
    "ContextRequest",
    "JsonRequester",
    "RuntimeDependencies",
    "ServerCoordinates",
    "STATUS_COMMAND",
    "StatusRequest",
    "StoreFactory",
    "contract_fixture_bytes",
    "parse_checkpoint_packet",
    "render_outcome",
)
ERROR_CODES = (
    "capability_not_enabled",
    "commit_unknown",
    "context_too_large",
    "internal_error",
    "invalid_authority",
    "invalid_body",
    "owner_unavailable",
    "workspace_mismatch",
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _packet(**changes: object) -> bytes:
    value: dict[str, object] = {
        "task_id": TASK_ID,
        "date": "2026-09-02",
        "done": ["Implemented the seam"],
        "next": [],
        "blockers": [],
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _status_data() -> dict[str, object]:
    return {
        "actual_workspace_uid": WORKSPACE_UID,
        "capability_reason": "v3 authority admitted",
        "capability_supported": True,
        "contract": CONTRACT_STRING,
        "data_dir_available": True,
        "exclusive_local_available": True,
        "expected_workspace_uid": WORKSPACE_UID,
        "ready": True,
        "running_server_available": False,
        "storage_format": "v3",
    }


def _context_data(*, detail: str = "Execute the next bounded step") -> dict[str, object]:
    return {
        "omitted": [
            "attachments",
            "captures",
            "objectives",
            "relationships",
            "work_sessions",
        ],
        "recent_worklog": [
            {
                "blockers": [],
                "date": "2026-09-02",
                "done": ["Reviewed context"],
                "next": ["Execute"],
            }
        ],
        "task": {
            "detail": detail,
            "due": "2026-09-03",
            "id": TASK_ID,
            "priority": "P1",
            "revision": 7,
            "status": "in_progress",
            "title": "Ship agent contract",
            "uid": "223e4567-e89b-42d3-a456-426614174000",
        },
        "workspace_uid": WORKSPACE_UID,
    }


def _checkpoint_data() -> dict[str, object]:
    return {
        "blockers": [],
        "date": "2026-09-02",
        "done": ["Implemented the seam"],
        "next": [],
        "task": "Ship agent contract",
        "task_id": TASK_ID,
    }


def _outcome(**changes: object) -> contract.AgentOutcome:
    values: dict[str, object] = {
        "command": STATUS_WIRE,
        "commit_state": None,
        "data": _status_data(),
        "error_code": None,
        "error_details": {},
        "error_message": None,
        "intent_id": None,
        "replayed": None,
        "retryable": None,
        "task_id": None,
        "transport": "exclusive-local",
        "workspace_uid": WORKSPACE_UID,
    }
    values.update(changes)
    return contract.AgentOutcome(**values)


def _rendered(outcome: contract.AgentOutcome) -> tuple[bytes, dict[str, object]]:
    rendered = contract.render_outcome(outcome=outcome)
    return rendered, json.loads(rendered)


def _annotation_text(annotation: object) -> str:
    if isinstance(annotation, type):
        if annotation.__module__ == "builtins":
            return annotation.__qualname__
        return f"{annotation.__module__}.{annotation.__qualname__}"
    return (
        str(annotation)
        .replace("typing.", "")
        .replace(" ", "")
        .strip("'\"")
    )


class PublicAbiTests(unittest.TestCase):
    def test_public_exports_and_command_constants_are_exact(self) -> None:
        self.assertEqual(set(contract.__all__), set(EXPECTED_EXPORTS))
        self.assertEqual(len(contract.__all__), len(EXPECTED_EXPORTS))
        self.assertEqual(contract.STATUS_COMMAND, "status")
        self.assertEqual(contract.CONTEXT_COMMAND, "context")
        self.assertEqual(contract.CHECKPOINT_COMMAND, "checkpoint")

    def test_contract_functions_have_exact_keyword_only_boundaries(self) -> None:
        expected = {
            contract.parse_checkpoint_packet: ("raw", "intent_id"),
            contract.render_outcome: ("outcome",),
            contract.contract_fixture_bytes: (),
        }
        for function, names in expected.items():
            with self.subTest(function=function.__name__):
                signature = inspect.signature(function)
                self.assertEqual(tuple(signature.parameters), names)
                for parameter in signature.parameters.values():
                    self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

        parse_signature = inspect.signature(contract.parse_checkpoint_packet)
        self.assertIn("bytes", _annotation_text(parse_signature.parameters["raw"].annotation))
        self.assertIn("str", _annotation_text(parse_signature.parameters["intent_id"].annotation))
        self.assertIn("CheckpointRequest", _annotation_text(parse_signature.return_annotation))
        self.assertIn(
            "AgentOutcome", _annotation_text(inspect.signature(contract.render_outcome).parameters["outcome"].annotation)
        )
        self.assertIn("bytes", _annotation_text(inspect.signature(contract.render_outcome).return_annotation))
        self.assertIn("bytes", _annotation_text(inspect.signature(contract.contract_fixture_bytes).return_annotation))

    def test_dataclasses_are_frozen_keyword_only_required_and_exact(self) -> None:
        expected_fields = {
            contract.AuthorityAdmission: ("data_dir", "workspace_uid"),
            contract.ServerCoordinates: ("host", "port"),
            contract.StatusRequest: ("data_dir", "expected_workspace_uid"),
            contract.ContextRequest: ("task_id",),
            contract.CheckpointRequest: (
                "task_id",
                "date",
                "done",
                "next",
                "blockers",
                "intent_id",
            ),
            contract.AgentOutcome: (
                "command",
                "commit_state",
                "data",
                "error_code",
                "error_details",
                "error_message",
                "intent_id",
                "replayed",
                "retryable",
                "task_id",
                "transport",
                "workspace_uid",
            ),
            contract.RuntimeDependencies: (
                "admit_authority",
                "create_local_backend",
                "create_running_server_backend",
                "request_json",
                "store_factory",
                "today",
            ),
        }
        for cls, names in expected_fields.items():
            with self.subTest(dataclass=cls.__name__):
                self.assertTrue(dataclasses.is_dataclass(cls))
                self.assertTrue(cls.__dataclass_params__.frozen)
                fields = dataclasses.fields(cls)
                self.assertEqual(tuple(field.name for field in fields), names)
                self.assertTrue(all(field.kw_only for field in fields))
                self.assertTrue(all(field.default is dataclasses.MISSING for field in fields))
                self.assertTrue(
                    all(field.default_factory is dataclasses.MISSING for field in fields)
                )
                with self.assertRaises(TypeError):
                    cls(*([None] * len(fields)))
                with self.assertRaises(TypeError):
                    cls()

    def test_dataclass_annotations_are_semantically_exact(self) -> None:
        self.assertEqual(
            typing.get_type_hints(contract.AuthorityAdmission),
            {"data_dir": Path, "workspace_uid": str},
        )
        self.assertEqual(
            typing.get_type_hints(contract.ServerCoordinates),
            {"host": str, "port": int},
        )
        self.assertEqual(
            typing.get_type_hints(contract.StatusRequest),
            {"data_dir": Path, "expected_workspace_uid": str},
        )
        self.assertEqual(typing.get_type_hints(contract.ContextRequest), {"task_id": str})
        self.assertEqual(
            typing.get_type_hints(contract.CheckpointRequest),
            {
                "task_id": str,
                "date": str,
                "done": list[str],
                "next": list[str],
                "blockers": list[str],
                "intent_id": str,
            },
        )
        self.assertEqual(
            typing.get_type_hints(contract.AgentOutcome),
            {
                "command": str,
                "commit_state": str | None,
                "data": dict[str, object] | None,
                "error_code": str | None,
                "error_details": dict[str, object],
                "error_message": str | None,
                "intent_id": str | None,
                "replayed": bool | None,
                "retryable": bool | None,
                "task_id": str | None,
                "transport": str | None,
                "workspace_uid": str | None,
            },
        )

    def test_runtime_dependency_annotations_name_every_seam(self) -> None:
        annotations = inspect.get_annotations(contract.RuntimeDependencies, eval_str=False)
        expected_tokens = {
            "admit_authority": ("data_dir", "Path", "expected_workspace_uid", "str", "AuthorityAdmission"),
            "create_local_backend": ("admission", "AuthorityAdmission", "store_factory", "StoreFactory", "AgentBackend"),
            "create_running_server_backend": ("server_info_path", "Path", "expected_workspace_uid", "request_json", "JsonRequester", "AgentBackend"),
            "request_json": ("JsonRequester",),
            "store_factory": ("StoreFactory",),
            "today": ("date",),
        }
        self.assertEqual(set(annotations), set(expected_tokens))
        for name, tokens in expected_tokens.items():
            text = _annotation_text(annotations[name])
            with self.subTest(dependency=name):
                for token in tokens:
                    self.assertIn(token, text)

    def test_protocols_and_protocol_method_signatures_are_exact(self) -> None:
        self.assertTrue(contract.AgentBackend._is_protocol)
        self.assertTrue(contract.JsonRequester._is_protocol)
        self.assertTrue(contract.StoreFactory._is_protocol)

        methods = {
            contract.AgentBackend.status: (("request",), "dict[str,object]"),
            contract.AgentBackend.context: (("request", "today"), "dict[str,object]"),
            contract.AgentBackend.checkpoint: (("request",), "dict[str,object]"),
            contract.JsonRequester.request: (
                ("host", "port", "method", "path", "body", "headers"),
                "tuple[int,dict[str,object]]",
            ),
            contract.StoreFactory.__call__: (("root",), "workstack.store.Store"),
        }
        for method, (names, return_text) in methods.items():
            with self.subTest(method=method.__qualname__):
                signature = inspect.signature(method)
                parameters = tuple(signature.parameters.values())
                self.assertEqual(parameters[0].name, "self")
                self.assertEqual(tuple(p.name for p in parameters[1:]), names)
                self.assertTrue(
                    all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters[1:])
                )
                self.assertEqual(
                    _annotation_text(signature.return_annotation), return_text
                )

    def test_protocol_parameter_annotations_name_frozen_types(self) -> None:
        expected = {
            contract.AgentBackend.status: {"request": "StatusRequest"},
            contract.AgentBackend.context: {
                "request": "ContextRequest",
                "today": "datetime.date",
            },
            contract.AgentBackend.checkpoint: {"request": "CheckpointRequest"},
            contract.JsonRequester.request: {
                "host": "str",
                "port": "int",
                "method": "str",
                "path": "str",
                "body": "bytes|None",
                "headers": "dict[str,str]|None",
            },
            contract.StoreFactory.__call__: {"root": "Path"},
        }
        for method, annotations in expected.items():
            signature = inspect.signature(method)
            for name, expected_text in annotations.items():
                with self.subTest(method=method.__qualname__, parameter=name):
                    actual = _annotation_text(signature.parameters[name].annotation)
                    if expected_text == "Path":
                        self.assertIn(actual, {"Path", "pathlib.Path"})
                    else:
                        self.assertTrue(
                            actual == expected_text or actual.endswith("." + expected_text),
                            (actual, expected_text),
                        )


class FixtureAndIsolationTests(unittest.TestCase):
    def test_fixture_is_exact_frozen_projection(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        keys = manifest["digest_recipes"]["contract_fixture_projection"]
        expected = _canonical({key: manifest[key] for key in keys})
        actual = contract.contract_fixture_bytes()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 7242)
        self.assertEqual(hashlib.sha256(actual).hexdigest(), EXPECTED_FIXTURE_SHA256)

    def test_fixture_builder_is_deterministic_and_uses_no_filesystem(self) -> None:
        expected = contract.contract_fixture_bytes()
        with (
            mock.patch("builtins.open", side_effect=AssertionError("filesystem read")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("filesystem read")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("filesystem read")),
        ):
            self.assertEqual(contract.contract_fixture_bytes(), expected)
            self.assertEqual(contract.contract_fixture_bytes(), expected)

    def test_module_imports_only_manifest_allowed_modules(self) -> None:
        source_path = ROOT / "workstack" / "agent_cli_contract.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        allowed = {"__future__", "dataclasses", "datetime", "json", "pathlib", "re", "typing"}
        self.assertEqual(imported - allowed, set())
        forbidden_calls = {"open", "exec", "eval", "compile", "__import__"}
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertFalse(called_names & forbidden_calls)


class CheckpointParserTests(unittest.TestCase):
    def test_parses_valid_packet_and_preserves_json_like_text(self) -> None:
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(done=['{"still":"text"}', "한글 항목"]),
            intent_id=INTENT_ID,
        )
        self.assertEqual(parsed.task_id, TASK_ID)
        self.assertEqual(parsed.date, "2026-09-02")
        self.assertEqual(parsed.done, ['{"still":"text"}', "한글 항목"])
        self.assertEqual(parsed.next, [])
        self.assertEqual(parsed.blockers, [])
        self.assertEqual(parsed.intent_id, INTENT_ID)

    def test_normalizes_surrounding_whitespace_without_reordering(self) -> None:
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(done=["  first  ", "\tsecond\n"], next=[" third "]),
            intent_id=INTENT_ID,
        )
        self.assertEqual(parsed.done, ["first", "second"])
        self.assertEqual(parsed.next, ["third"])

    def test_accepts_five_digit_task_and_intent_punctuation(self) -> None:
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(task_id="T-123456"), intent_id="run:_id.42-abc"
        )
        self.assertEqual(parsed.task_id, "T-123456")
        self.assertEqual(parsed.intent_id, "run:_id.42-abc")

    def test_accepts_strict_leap_day(self) -> None:
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(date="2028-02-29"), intent_id=INTENT_ID
        )
        self.assertEqual(parsed.date, "2028-02-29")

    def test_accepts_maximum_valid_item_count_and_character_count(self) -> None:
        items = [(str(index % 10) * 1000) for index in range(20)]
        raw = _packet(done=items)
        self.assertLess(len(raw), 32768)
        parsed = contract.parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)
        self.assertEqual(parsed.done, items)

    def test_accepts_exact_32kib_packet_and_rejects_one_byte_more(self) -> None:
        done = ["a" * 1000 for _ in range(20)]
        next_items = ["a" * 1000 for _ in range(12)] + ["a" * 489] + ["a"] * 7
        blockers = ["a"] * 20
        exact = _packet(done=done, next=next_items, blockers=blockers)
        self.assertEqual(len(exact), 32768)
        parsed = contract.parse_checkpoint_packet(raw=exact, intent_id=INTENT_ID)
        self.assertEqual(parsed.next[12], "a" * 489)

        next_items[12] += "a"
        too_large = _packet(done=done, next=next_items, blockers=blockers)
        self.assertEqual(len(too_large), 32769)
        with self.assertRaises(ValueError):
            contract.parse_checkpoint_packet(raw=too_large, intent_id=INTENT_ID)

    def test_multibyte_limit_counts_characters_not_utf8_bytes(self) -> None:
        accepted = "한" * 1000
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(done=[accepted]), intent_id=INTENT_ID
        )
        self.assertEqual(parsed.done, [accepted])
        with self.assertRaises(ValueError):
            contract.parse_checkpoint_packet(
                raw=_packet(done=["한" * 1001]), intent_id=INTENT_ID
            )

    def test_input_over_32kib_is_rejected_before_json_semantics(self) -> None:
        with self.assertRaises(ValueError):
            contract.parse_checkpoint_packet(raw=b"{" + (b"x" * 32768), intent_id=INTENT_ID)

    def test_invalid_utf8_and_invalid_json_are_rejected(self) -> None:
        for raw in (b"\xff", b"{", b"null", b"[]", b'"text"'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = (
            b'{"task_id":"T-12345","task_id":"T-99999","date":"2026-09-02",'
            b'"done":["x"],"next":[],"blockers":[]}'
        )
        with self.assertRaises(ValueError):
            contract.parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)

    def test_fields_are_exact(self) -> None:
        valid = json.loads(_packet())
        for missing in tuple(valid):
            value = dict(valid)
            value.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(raw=_canonical(value), intent_id=INTENT_ID)
        for extra in ("workspace_uid", "intent_id", "unexpected"):
            value = dict(valid)
            value[extra] = "not admitted"
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(raw=_canonical(value), intent_id=INTENT_ID)

    def test_scalar_field_types_are_exact(self) -> None:
        invalid_values = {
            "task_id": [None, 1, True, "T-123", "t-12345", "T-1234x"],
            "date": [None, 20260902, True, "2026-9-2", "2026-02-30", "2026-09-02Z"],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    contract.parse_checkpoint_packet(
                        raw=_packet(**{field: value}), intent_id=INTENT_ID
                    )

    def test_list_types_and_item_types_are_exact(self) -> None:
        for field in ("done", "next", "blockers"):
            for value in (None, "text", {}, [1], [True], [None], [["nested"]]):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    contract.parse_checkpoint_packet(
                        raw=_packet(**{field: value}), intent_id=INTENT_ID
                    )

    def test_empty_or_blank_journal_is_rejected(self) -> None:
        for changes in (
            {"done": [], "next": [], "blockers": []},
            {"done": ["   "], "next": [], "blockers": []},
            {"done": [], "next": ["\t"], "blockers": ["\n"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(
                    raw=_packet(**changes), intent_id=INTENT_ID
                )

    def test_blank_items_are_dropped_when_a_nonempty_item_remains(self) -> None:
        parsed = contract.parse_checkpoint_packet(
            raw=_packet(done=[" ", "kept", "\t"], next=["\n"], blockers=[]),
            intent_id=INTENT_ID,
        )
        self.assertEqual(parsed.done, ["kept"])
        self.assertEqual(parsed.next, [])

    def test_each_list_has_twenty_item_limit(self) -> None:
        for field in ("done", "next", "blockers"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(
                    raw=_packet(**{field: ["x"] * 21}), intent_id=INTENT_ID
                )

    def test_intent_identifier_boundaries_and_type(self) -> None:
        for valid in ("a" * 8, "z" * 128, "a_b:c.d-e"):
            with self.subTest(valid=valid):
                parsed = contract.parse_checkpoint_packet(raw=_packet(), intent_id=valid)
                self.assertEqual(parsed.intent_id, valid)
        for invalid in (
            None,
            123,
            "a" * 7,
            "a" * 129,
            "has space",
            "nonascii-한글",
            "slash/id",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(raw=_packet(), intent_id=invalid)  # type: ignore[arg-type]

    def test_raw_must_be_bytes(self) -> None:
        for raw in ("{}", bytearray(b"{}"), memoryview(b"{}"), None):
            with self.subTest(raw=type(raw).__name__), self.assertRaises(ValueError):
                contract.parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)  # type: ignore[arg-type]


class OutcomeRendererTests(unittest.TestCase):
    def assertCanonicalEnvelope(self, rendered: bytes) -> dict[str, object]:
        self.assertIs(type(rendered), bytes)
        self.assertTrue(rendered.endswith(b"\n"))
        self.assertFalse(rendered.endswith(b"\n\n"))
        self.assertLessEqual(len(rendered), 32768)
        parsed = json.loads(rendered)
        self.assertIs(type(parsed), dict)
        self.assertEqual(rendered, _canonical(parsed))
        return parsed

    def test_status_success_has_exact_data_and_meta(self) -> None:
        rendered, parsed = _rendered(_outcome())
        self.assertCanonicalEnvelope(rendered)
        self.assertEqual(
            parsed,
            {
                "contract": CONTRACT_STRING,
                "data": _status_data(),
                "meta": {
                    "command": "agent.status",
                    "transport": "exclusive-local",
                    "workspace_uid": WORKSPACE_UID,
                },
            },
        )

    def test_context_success_has_exact_data_and_meta(self) -> None:
        outcome = _outcome(
            command=CONTEXT_WIRE,
            data=_context_data(),
            task_id=TASK_ID,
            transport="running-server",
        )
        rendered, parsed = _rendered(outcome)
        self.assertCanonicalEnvelope(rendered)
        self.assertEqual(
            parsed,
            {
                "contract": CONTRACT_STRING,
                "data": _context_data(),
                "meta": {
                    "command": "agent.context",
                    "task_id": TASK_ID,
                    "transport": "running-server",
                    "workspace_uid": WORKSPACE_UID,
                },
            },
        )

    def test_checkpoint_success_has_exact_data_and_meta(self) -> None:
        outcome = _outcome(
            command=CHECKPOINT_WIRE,
            commit_state="committed",
            data=_checkpoint_data(),
            intent_id=INTENT_ID,
            replayed=False,
            task_id=TASK_ID,
            transport="exclusive-local",
        )
        rendered, parsed = _rendered(outcome)
        self.assertCanonicalEnvelope(rendered)
        self.assertEqual(
            parsed,
            {
                "contract": CONTRACT_STRING,
                "data": _checkpoint_data(),
                "meta": {
                    "command": "agent.checkpoint",
                    "commit_state": "committed",
                    "intent_id": INTENT_ID,
                    "replayed": False,
                    "task_id": TASK_ID,
                    "transport": "exclusive-local",
                    "workspace_uid": WORKSPACE_UID,
                },
            },
        )

    def test_ordinary_failure_has_exact_shape_and_omits_placeholders(self) -> None:
        outcome = _outcome(
            data=None,
            error_code="invalid_authority",
            error_details={},
            error_message="Authority admission failed",
            retryable=False,
            transport=None,
            workspace_uid=None,
        )
        rendered, parsed = _rendered(outcome)
        self.assertCanonicalEnvelope(rendered)
        self.assertEqual(set(parsed), {"contract", "error", "meta"})
        self.assertEqual(parsed["contract"], CONTRACT_STRING)
        self.assertEqual(parsed["meta"], {"command": "agent.status"})
        self.assertEqual(set(parsed["error"]), {"code", "details", "message", "retryable"})  # type: ignore[arg-type]
        self.assertEqual(parsed["error"]["code"], "invalid_authority")  # type: ignore[index]
        self.assertEqual(parsed["error"]["details"], {})  # type: ignore[index]
        self.assertIs(parsed["error"]["retryable"], False)  # type: ignore[index]
        self.assertIs(type(parsed["error"]["message"]), str)  # type: ignore[index]
        self.assertTrue(parsed["error"]["message"])  # type: ignore[index]

    def test_commit_unknown_has_exact_metadata_and_no_replayed_placeholder(self) -> None:
        outcome = _outcome(
            command=CHECKPOINT_WIRE,
            commit_state="unknown",
            data=None,
            error_code="commit_unknown",
            error_details={},
            error_message="Mutation result could not be established",
            intent_id=INTENT_ID,
            replayed=None,
            retryable=None,
            task_id=TASK_ID,
            transport="running-server",
        )
        rendered, parsed = _rendered(outcome)
        self.assertCanonicalEnvelope(rendered)
        self.assertEqual(set(parsed), {"contract", "error", "meta"})
        self.assertEqual(
            parsed["meta"],
            {
                "command": "agent.checkpoint",
                "commit_state": "unknown",
                "intent_id": INTENT_ID,
                "task_id": TASK_ID,
                "transport": "running-server",
                "workspace_uid": WORKSPACE_UID,
            },
        )
        self.assertEqual(parsed["error"]["code"], "commit_unknown")  # type: ignore[index]
        self.assertEqual(parsed["error"]["details"], {})  # type: ignore[index]
        self.assertNotIn("retryable", parsed["error"])  # type: ignore[operator]
        self.assertIs(type(parsed["error"]["message"]), str)  # type: ignore[index]
        self.assertTrue(parsed["error"]["message"])  # type: ignore[index]

    def test_all_error_codes_are_accepted_with_command_appropriate_metadata(self) -> None:
        for code in ERROR_CODES:
            changes: dict[str, object] = {
                "data": None,
                "error_code": code,
                "error_details": {},
                "error_message": "Operation failed",
                "transport": None,
                "workspace_uid": None,
            }
            if code == "commit_unknown":
                changes.update(
                    command=CHECKPOINT_WIRE,
                    commit_state="unknown",
                    intent_id=INTENT_ID,
                    task_id=TASK_ID,
                    transport="running-server",
                    workspace_uid=WORKSPACE_UID,
                )
            elif code == "context_too_large":
                changes["command"] = CONTEXT_WIRE
            elif code == "invalid_body":
                changes["command"] = CHECKPOINT_WIRE
            with self.subTest(code=code):
                rendered, parsed = _rendered(_outcome(**changes))
                self.assertCanonicalEnvelope(rendered)
                self.assertEqual(parsed["error"]["code"], code)  # type: ignore[index]
                self.assertIs(type(parsed["error"]["message"]), str)  # type: ignore[index]
                self.assertTrue(parsed["error"]["message"])  # type: ignore[index]

    def test_untrusted_error_message_is_rejected_or_redacted(self) -> None:
        canary = "TOKEN_CANARY_5bfa"
        outcome = _outcome(
            data=None,
            error_code="invalid_authority",
            error_details={},
            error_message=canary,
            transport=None,
            workspace_uid=None,
        )
        try:
            rendered = contract.render_outcome(outcome=outcome)
        except ValueError:
            return
        self.assertNotIn(canary.encode("utf-8"), rendered)

    def test_success_failure_exclusivity_is_rejected(self) -> None:
        invalid = (
            _outcome(error_code="internal_error", error_message="x"),
            _outcome(data=None),
            _outcome(data=None, error_message="message without code"),
            _outcome(data=None, error_code="internal_error", error_message=None),
        )
        for outcome in invalid:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                contract.render_outcome(outcome=outcome)

    def test_invalid_command_transport_and_identifier_values_are_rejected(self) -> None:
        invalid = (
            _outcome(command="apply"),
            _outcome(command=contract.STATUS_COMMAND),
            _outcome(transport="remote"),
            _outcome(workspace_uid="not-a-uuid"),
            _outcome(workspace_uid=WORKSPACE_UID.upper()),
            _outcome(command=CONTEXT_WIRE, data=_context_data(), task_id="T-123"),
            _outcome(
                command=CHECKPOINT_WIRE,
                commit_state="committed",
                data=_checkpoint_data(),
                intent_id="short",
                replayed=False,
                task_id=TASK_ID,
            ),
        )
        for outcome in invalid:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                contract.render_outcome(outcome=outcome)

    def test_wrong_primitive_types_are_rejected_not_coerced(self) -> None:
        invalid = (
            _outcome(retryable=1),
            _outcome(replayed=0),
            _outcome(error_details=[]),
            _outcome(data=[]),
            _outcome(workspace_uid=123),
            _outcome(task_id=123),
            _outcome(intent_id=True),
            _outcome(commit_state=False),
        )
        for outcome in invalid:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                contract.render_outcome(outcome=outcome)

    def test_inapplicable_metadata_is_rejected(self) -> None:
        invalid = (
            _outcome(task_id=TASK_ID),
            _outcome(intent_id=INTENT_ID),
            _outcome(replayed=False),
            _outcome(commit_state="committed"),
            _outcome(command=CONTEXT_WIRE, data=_context_data(), task_id=TASK_ID, replayed=False),
            _outcome(
                command=CHECKPOINT_WIRE,
                commit_state="committed",
                data=_checkpoint_data(),
                intent_id=INTENT_ID,
                replayed=None,
                task_id=TASK_ID,
            ),
        )
        for outcome in invalid:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                contract.render_outcome(outcome=outcome)

    def test_failure_metadata_rules_are_exact(self) -> None:
        ordinary_with_commit_state = _outcome(
            commit_state="unknown",
            data=None,
            error_code="owner_unavailable",
            error_message="x",
        )
        commit_unknown_variants = (
            _outcome(
                command=CHECKPOINT_WIRE,
                commit_state="unknown",
                data=None,
                error_code="commit_unknown",
                error_message="x",
                intent_id=INTENT_ID,
                replayed=False,
                task_id=TASK_ID,
                transport="running-server",
            ),
            _outcome(
                command=CHECKPOINT_WIRE,
                commit_state="unknown",
                data=None,
                error_code="commit_unknown",
                error_message="x",
                intent_id=INTENT_ID,
                replayed=None,
                task_id=TASK_ID,
                transport="exclusive-local",
            ),
        )
        ordinary_with_extra_meta = (
            _outcome(
                data=None,
                error_code="owner_unavailable",
                error_message="x",
                task_id=TASK_ID,
                transport=None,
                workspace_uid=None,
            ),
            _outcome(
                data=None,
                error_code="owner_unavailable",
                error_message="x",
                transport="running-server",
                workspace_uid=WORKSPACE_UID,
            ),
            _outcome(
                data=None,
                error_code="owner_unavailable",
                error_message="x",
                intent_id=INTENT_ID,
                transport=None,
                workspace_uid=None,
            ),
        )
        for outcome in (
            ordinary_with_commit_state,
            *ordinary_with_extra_meta,
            *commit_unknown_variants,
        ):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                contract.render_outcome(outcome=outcome)

    def test_status_data_shape_and_types_are_exact(self) -> None:
        baseline = _status_data()
        for missing in baseline:
            changed = dict(baseline)
            changed.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                contract.render_outcome(outcome=_outcome(data=changed))
        changed = dict(baseline, unexpected=True)
        with self.assertRaises(ValueError):
            contract.render_outcome(outcome=_outcome(data=changed))
        for name in (
            "capability_supported",
            "data_dir_available",
            "exclusive_local_available",
            "ready",
            "running_server_available",
        ):
            changed = dict(baseline)
            changed[name] = 1
            with self.subTest(type_field=name), self.assertRaises(ValueError):
                contract.render_outcome(outcome=_outcome(data=changed))
        changed = dict(baseline, storage_format="v5")
        with self.assertRaises(ValueError):
            contract.render_outcome(outcome=_outcome(data=changed))
        for name in ("actual_workspace_uid", "expected_workspace_uid"):
            changed = dict(baseline)
            changed[name] = "NOT-A-CANONICAL-UUID"
            with self.subTest(uuid_field=name), self.assertRaises(ValueError):
                contract.render_outcome(outcome=_outcome(data=changed))

    def test_context_data_shape_and_nested_allowlists_are_exact(self) -> None:
        baseline = _context_data()
        for missing in baseline:
            changed = dict(baseline)
            changed.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                contract.render_outcome(
                    outcome=_outcome(
                        command=CONTEXT_WIRE,
                        data=changed,
                        task_id=TASK_ID,
                    )
                )
        changed = _context_data()
        changed["task"] = dict(
            changed["task"], unexpected_field="must not pass"
        )  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(command=CONTEXT_WIRE, data=changed, task_id=TASK_ID)
            )
        changed = _context_data()
        changed["recent_worklog"] = [{"date": "2026-09-02", "done": [], "next": []}]
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(command=CONTEXT_WIRE, data=changed, task_id=TASK_ID)
            )
        changed = _context_data()
        changed["omitted"] = ["unknown_category"]
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(command=CONTEXT_WIRE, data=changed, task_id=TASK_ID)
            )
        changed = _context_data()
        changed["recent_worklog"] = "not-a-list"
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(command=CONTEXT_WIRE, data=changed, task_id=TASK_ID)
            )
        changed = _context_data()
        changed["workspace_uid"] = "NOT-A-CANONICAL-UUID"
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(command=CONTEXT_WIRE, data=changed, task_id=TASK_ID)
            )

    def test_checkpoint_data_shape_and_nested_types_are_exact(self) -> None:
        baseline = _checkpoint_data()
        for missing in baseline:
            changed = dict(baseline)
            changed.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                contract.render_outcome(
                    outcome=_outcome(
                        command=CHECKPOINT_WIRE,
                        commit_state="committed",
                        data=changed,
                        intent_id=INTENT_ID,
                        replayed=False,
                        task_id=TASK_ID,
                    )
                )
        changed = dict(baseline, workspace_uid=WORKSPACE_UID)
        with self.assertRaises(ValueError):
            contract.render_outcome(
                outcome=_outcome(
                    command=CHECKPOINT_WIRE,
                    commit_state="committed",
                    data=changed,
                    intent_id=INTENT_ID,
                    replayed=False,
                    task_id=TASK_ID,
                )
            )
        for field in ("done", "next", "blockers"):
            changed = dict(baseline)
            changed[field] = "not-a-list"
            with self.subTest(type_field=field), self.assertRaises(ValueError):
                contract.render_outcome(
                    outcome=_outcome(
                        command=CHECKPOINT_WIRE,
                        commit_state="committed",
                        data=changed,
                        intent_id=INTENT_ID,
                        replayed=False,
                        task_id=TASK_ID,
                    )
                )
        for invalid_task in ("", 1, None, {}, []):
            changed = dict(baseline, task=invalid_task)
            with self.subTest(task=invalid_task), self.assertRaises(ValueError):
                contract.render_outcome(
                    outcome=_outcome(
                        command=CHECKPOINT_WIRE,
                        commit_state="committed",
                        data=changed,
                        intent_id=INTENT_ID,
                        replayed=False,
                        task_id=TASK_ID,
                    )
                )

    def test_envelope_accepts_exact_32kib_and_rejects_one_byte_more(self) -> None:
        def make(detail_length: int) -> contract.AgentOutcome:
            return _outcome(
                command=CONTEXT_WIRE,
                data=_context_data(detail="a" * detail_length),
                task_id=TASK_ID,
            )

        empty_parsed = {
            "contract": CONTRACT_STRING,
            "data": _context_data(detail=""),
            "meta": {
                "command": "agent.context",
                "task_id": TASK_ID,
                "transport": "exclusive-local",
                "workspace_uid": WORKSPACE_UID,
            },
        }
        exact_length = 32768 - len(_canonical(empty_parsed))
        exact = contract.render_outcome(outcome=make(exact_length))
        self.assertEqual(len(exact), 32768)
        with self.assertRaises(ValueError):
            contract.render_outcome(outcome=make(exact_length + 1))

    def test_secret_path_csrf_token_and_raw_body_never_render(self) -> None:
        canaries = (
            "TOKEN_CANARY_93bf",
            "CSRF_CANARY_77ea",
            r"C:\\Users\\secret\\workspace.json",
            '{"raw_server_body":"CANARY_RAW_BODY"}',
        )
        details = {
            "token": canaries[0],
            "csrf": canaries[1],
            "path": canaries[2],
            "raw_body": canaries[3],
        }
        outcome = _outcome(
            data=None,
            error_code="internal_error",
            error_details=details,
            error_message=" ".join(canaries),
            transport=None,
            workspace_uid=None,
        )
        try:
            rendered = contract.render_outcome(outcome=outcome)
        except ValueError:
            return
        for canary in canaries:
            self.assertNotIn(canary.encode("utf-8"), rendered)
        parsed = self.assertCanonicalEnvelope(rendered)
        self.assertEqual(parsed["error"]["details"], {})  # type: ignore[index]
        self.assertIs(type(parsed["error"]["message"]), str)  # type: ignore[index]

    def test_renderer_requires_an_agent_outcome(self) -> None:
        for value in (None, {}, object()):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                contract.render_outcome(outcome=value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
