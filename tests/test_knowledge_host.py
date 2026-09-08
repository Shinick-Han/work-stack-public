from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for import_root in (SHELL, ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import knowledge_host as KH
import knowledge_host_search as KHS


REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE = "66666666-6666-4666-8666-666666666666"
OTHER_WORKSPACE = "55555555-5555-4555-8555-555555555555"
TASK_UID = "77777777-7777-4777-8777-777777777777"
REFERENCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SHA = "ab" * 32
BINDING = {
    "workspace_uid": WORKSPACE,
    "task_uid": TASK_UID,
    "task_id": "T-0033",
    "task_revision": 2,
}


def request(operation: str, **extra: object) -> str:
    return json.dumps(
        {
            "type": "workstack-knowledge-request",
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "operation": operation,
            **extra,
        },
        separators=(",", ":"),
    )


def parse(payload: str) -> dict[str, object]:
    return json.loads(payload)


class FakeRegistryError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code)


class FakeRegistry:
    def __init__(self) -> None:
        self.vaults: list[dict[str, str]] = []
        self.references: list[dict[str, object]] = []
        self.calls: list[tuple[object, ...]] = []
        self.read_result: dict[str, object] | None = None
        self.search_result: dict[str, object] | None = None
        self.search_error: str | None = None

    def status(self) -> dict[str, object]:
        self.calls.append(("status",))
        return {"vaults": list(self.vaults)}

    def add_vault(self, root: object) -> dict[str, str]:
        self.calls.append(("add_vault", root))
        vault = {"vault_id": "personal-wiki", "label": Path(str(root)).name or "vault"}
        self.vaults.append(vault)
        return vault

    def list_references(self, binding: object) -> list[dict[str, object]]:
        self.calls.append(("list_references", binding))
        return list(self.references)

    def read_reference(
        self,
        binding: object,
        vault_id: str,
        document_path: str,
        start_line: int,
        end_line: int,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "read_reference",
                binding,
                vault_id,
                document_path,
                start_line,
                end_line,
                expected_sha256,
            )
        )
        if self.read_result is not None:
            return self.read_result
        return sample_read(vault_id, document_path, start_line, end_line, expected_sha256)

    def pin_reference(
        self,
        binding: object,
        vault_id: str,
        document_path: str,
        start_line: int,
        end_line: int,
        expected_sha256: str,
        reason: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "pin_reference",
                binding,
                vault_id,
                document_path,
                start_line,
                end_line,
                expected_sha256,
                reason,
            )
        )
        metadata = sample_metadata(vault_id, document_path, start_line, end_line, reason)
        self.references = [item for item in self.references if item["reference_id"] != metadata["reference_id"]]
        self.references.append(metadata)
        return metadata

    def search_references(
        self, binding: object, vault_id: str, query: str
    ) -> dict[str, object]:
        self.calls.append(("search_references", binding, vault_id, query))
        if self.search_error is not None:
            raise FakeRegistryError(self.search_error)
        if self.search_result is not None:
            return self.search_result
        return sample_search()

    def unpin_reference(self, binding: object, reference_id: str) -> bool:
        self.calls.append(("unpin_reference", binding, reference_id))
        before = len(self.references)
        self.references = [
            item for item in self.references if item["reference_id"] != reference_id
        ]
        return len(self.references) != before


def sample_metadata(
    vault_id: str = "personal-wiki",
    document_path: str = "notes/review.md",
    start_line: int = 1,
    end_line: int = 2,
    reason: str = "evidence",
) -> dict[str, object]:
    return {
        "reference_id": REFERENCE_ID,
        "vault_id": vault_id,
        "document_path": document_path,
        "start_line": start_line,
        "end_line": end_line,
        "source_sha256": SHA,
        "reason": reason,
    }


def sample_read(
    vault_id: str = "personal-wiki",
    document_path: str = "notes/review.md",
    start_line: int = 1,
    end_line: int = 1,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "markdown-vault",
        "vault_id": vault_id,
        "document_path": document_path,
        "title": "Review",
        "source_sha256": SHA,
        "expected_sha256": expected_sha256,
        "freshness": "uncompared" if expected_sha256 is None else "unchanged",
        "start_line": start_line,
        "end_line": end_line,
        "excerpt": "plain evidence",
        "excerpt_truncated": False,
        "trust": "external_reference",
        "read_only": True,
    }


def sample_corpus(
    label: str = "Personal wiki",
    document_count: int = 30,
    indexed_at: str = "2026-09-08T09:15:00Z",
) -> dict[str, object]:
    return {
        "label": label,
        "document_count": document_count,
        "indexed_at": indexed_at,
    }


def sample_search(
    matches: list[dict[str, object]] | None = None,
    omitted_count: int = 0,
    corpus: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "corpus": sample_corpus() if corpus is None else corpus,
        "matches": [sample_read(expected_sha256=SHA)] if matches is None else matches,
        "omitted_count": omitted_count,
    }


def search_request(query: str = "release notes", **extra: object) -> str:
    fields: dict[str, object] = {
        "binding": BINDING,
        "vault_id": "personal-wiki",
        "query": query,
    }
    fields.update(extra)
    return request("search-references", **fields)


def service(
    registry: FakeRegistry | None,
    *,
    picker: object | None = None,
    workspace: object = WORKSPACE,
) -> KH.KnowledgeHostService:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
    return KH.KnowledgeHostService(
        root,
        picker if picker is not None else (lambda: None),
        workspace,
        registry=registry,
    )


class KnowledgeHostServiceTest(unittest.TestCase):
    def test_status_returns_local_only_vaults_from_registry(self) -> None:
        registry = FakeRegistry()
        registry.vaults = [{"vault_id": "personal-wiki", "label": "notes"}]
        host = service(registry)

        response = parse(host.handle_json(request("status")))

        self.assertEqual(response["type"], "workstack-knowledge-response")
        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["operation"], "status")
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["data"],
            {
                "vaults": [{"vault_id": "personal-wiki", "label": "notes"}],
                "local_only": True,
            },
        )
        self.assertEqual(registry.calls, [("status",)])

    def test_unknown_keys_and_operations_are_rejected(self) -> None:
        host = service(FakeRegistry())
        cases = (
            request("status", unexpected=True),
            request("nope"),
            request("choose-vault", root="C:/secret"),
            request("choose-vault", vault_id="personal-wiki"),
            '{"type":"workstack-knowledge-request"}',
            "not json",
            json.dumps({"type": "workstack-knowledge-request", "schema_version": True, "request_id": REQUEST_ID, "operation": "status"}),
        )
        for payload in cases:
            with self.subTest(payload=payload[:80]):
                response = parse(host.handle_json(payload))
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertNotIn("secret", json.dumps(response))

    def test_oversized_request_is_rejected_without_registry_work(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        payload = "x" * (KH.MAX_HOST_REQUEST_BYTES + 1)

        response = parse(host.handle_json(payload))

        self.assertFalse(response["ok"])
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(registry.calls, [])

    def test_choose_vault_cancel_does_not_write_registry(self) -> None:
        registry = FakeRegistry()
        host = service(registry, picker=lambda: None)

        response = parse(host.handle_json(request("choose-vault")))

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"], {"cancelled": True})
        self.assertEqual(registry.calls, [])

    def test_choose_vault_uses_picker_root_never_message_root(self) -> None:
        registry = FakeRegistry()
        selected = Path(tempfile.gettempdir()) / "chosen-vault"
        host = service(registry, picker=lambda: selected)

        response = parse(host.handle_json(request("choose-vault")))

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["data"],
            {
                "cancelled": False,
                "vault": {"vault_id": "personal-wiki", "label": "chosen-vault"},
            },
        )
        self.assertEqual(registry.calls, [("add_vault", selected)])

    def test_binding_required_operations_validate_task_snapshot(self) -> None:
        host = service(FakeRegistry(), workspace=WORKSPACE)
        response = parse(host.handle_json(request("list-references")))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

        invalid = dict(BINDING)
        invalid["task_revision"] = -1
        response = parse(host.handle_json(request("list-references", binding=invalid)))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_task_revision")

    def test_workspace_is_checked_before_registry_work(self) -> None:
        registry = FakeRegistry()
        host = service(registry, workspace=OTHER_WORKSPACE)

        response = parse(host.handle_json(request("list-references", binding=BINDING)))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "workspace_mismatch")
        self.assertEqual(registry.calls, [])

    def test_workspace_is_rechecked_after_registry_work(self) -> None:
        registry = FakeRegistry()
        current = {"uid": WORKSPACE}

        def workspace() -> str:
            return current["uid"]

        def list_references(binding: object) -> list[object]:
            current["uid"] = OTHER_WORKSPACE
            return []

        registry.list_references = list_references  # type: ignore[method-assign]
        host = KH.KnowledgeHostService(
            Path(tempfile.gettempdir()),
            lambda: None,
            workspace,
            registry=registry,
        )

        response = parse(host.handle_json(request("list-references", binding=BINDING)))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "workspace_mismatch")

    def test_choose_vault_does_not_write_if_workspace_changes_during_picker(self) -> None:
        registry = FakeRegistry()
        current = {"uid": WORKSPACE}

        def picker() -> str:
            current["uid"] = OTHER_WORKSPACE
            return str(Path(tempfile.gettempdir()) / "late-vault")

        host = KH.KnowledgeHostService(
            Path(tempfile.gettempdir()),
            picker,
            lambda: current["uid"],
            registry=registry,
        )

        response = parse(
            host.handle_json(request("choose-vault", binding=BINDING))
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "workspace_mismatch")
        self.assertEqual(registry.calls, [])

    def test_list_read_pin_unpin_round_trip_with_unit_double(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        listed = parse(host.handle_json(request("list-references", binding=BINDING)))
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["data"]["references"], [])
        self.assertTrue(listed["data"]["local_only"])

        read = parse(
            host.handle_json(
                request(
                    "read-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=2,
                    expected_sha256=None,
                )
            )
        )
        self.assertTrue(read["ok"])
        self.assertEqual(read["data"]["binding"], BINDING)
        self.assertEqual(read["data"]["reference"]["excerpt"], "plain evidence")
        self.assertEqual(read["data"]["reference"]["trust"], "external_reference")

        pinned = parse(
            host.handle_json(
                request(
                    "pin-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=2,
                    expected_sha256=SHA,
                    reason="link this span",
                )
            )
        )
        self.assertTrue(pinned["ok"])
        self.assertEqual(pinned["data"]["reference"]["reference_id"], REFERENCE_ID)
        self.assertTrue(pinned["data"]["local_only"])
        self.assertNotIn("excerpt", pinned["data"]["reference"])

        unpinned = parse(
            host.handle_json(
                request(
                    "unpin-reference",
                    binding=BINDING,
                    reference_id=REFERENCE_ID,
                )
            )
        )
        self.assertTrue(unpinned["ok"])
        self.assertEqual(unpinned["data"]["removed"], True)

        absent = parse(
            host.handle_json(
                request(
                    "unpin-reference",
                    binding=BINDING,
                    reference_id=REFERENCE_ID,
                )
            )
        )
        self.assertTrue(absent["ok"])
        self.assertEqual(absent["data"]["removed"], True)

    def test_expected_source_errors_do_not_leak_paths_or_content(self) -> None:
        registry = FakeRegistry()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise FakeRegistryError(
                "document_missing",
                r"C:\secret\vault\note.md contents: classified",
            )

        registry.read_reference = boom  # type: ignore[method-assign]
        host = service(registry)
        response = parse(
            host.handle_json(
                request(
                    "read-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=1,
                )
            )
        )
        serialized = json.dumps(response)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "document_missing")
        self.assertNotIn("secret", serialized)
        self.assertNotIn("classified", serialized)
        self.assertNotIn("note.md", serialized)

    def test_pin_source_revision_conflict_is_closed(self) -> None:
        registry = FakeRegistry()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise FakeRegistryError("source_revision_conflict", "hash mismatch at D:/vault")

        registry.pin_reference = boom  # type: ignore[method-assign]
        host = service(registry)
        response = parse(
            host.handle_json(
                request(
                    "pin-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=1,
                    expected_sha256=SHA,
                    reason="keep",
                )
            )
        )
        self.assertEqual(response["error"]["code"], "source_revision_conflict")
        self.assertNotIn("D:/vault", json.dumps(response))

    def test_pin_reason_and_hash_limits(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        too_long = parse(
            host.handle_json(
                request(
                    "pin-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=1,
                    expected_sha256=SHA,
                    reason="x" * 501,
                )
            )
        )
        self.assertEqual(too_long["error"]["code"], "invalid_request")
        bad_hash = parse(
            host.handle_json(
                request(
                    "pin-reference",
                    binding=BINDING,
                    vault_id="personal-wiki",
                    document_path="notes/review.md",
                    start_line=1,
                    end_line=1,
                    expected_sha256="deadbeef",
                    reason="keep",
                )
            )
        )
        self.assertEqual(bad_hash["error"]["code"], "invalid_source_revision")
        self.assertEqual(registry.calls, [])

    def test_missing_registry_is_reported_without_a_replacement(self) -> None:
        host = service(None)
        response = parse(host.handle_json(request("status")))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "registry_unavailable")

    def test_correlated_overflow_keeps_request_identity(self) -> None:
        host = service(FakeRegistry())
        payload = request("status")
        response = parse(host.overflow_response(payload))
        self.assertFalse(response["ok"])
        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["operation"], "status")
        self.assertEqual(response["error"]["code"], "busy")

    def test_overflow_does_not_guess_invalid_correlation(self) -> None:
        host = service(FakeRegistry())
        response = parse(host.overflow_response('{"request_id":"secret"}'))
        self.assertIsNone(response["request_id"])
        self.assertIsNone(response["operation"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertNotIn("secret", json.dumps(response))

    def test_oversized_response_becomes_operation_failed(self) -> None:
        registry = FakeRegistry()
        registry.references = [
            {
                **sample_metadata(),
                "reference_id": f"{index:08x}-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "reason": "r" * 500,
            }
            for index in range(400)
        ]
        host = service(registry)
        response = parse(host.handle_json(request("list-references", binding=BINDING)))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "operation_failed")


def pin_request(**overrides: object) -> str:
    fields: dict[str, object] = {
        "binding": BINDING,
        "vault_id": "personal-wiki",
        "document_path": "notes/review.md",
        "start_line": 1,
        "end_line": 2,
        "expected_sha256": SHA,
        "reason": "evidence",
    }
    fields.update(overrides)
    return request("pin-reference", **fields)


class KnowledgeHostImportTest(unittest.TestCase):
    def test_application_root_is_the_project_root_not_the_desktop_folder(self) -> None:
        self.assertEqual(KH._APPLICATION_ROOT, ROOT)
        self.assertEqual(KH._SHELL_DIRECTORY, SHELL)
        self.assertIn(str(ROOT), sys.path)

    def test_module_loads_isolated_from_an_unrelated_working_directory(self) -> None:
        script = textwrap.dedent(
            """
            import importlib.util
            import sys

            spec = importlib.util.spec_from_file_location(
                "knowledge_host_isolated", sys.argv[1]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            print(module.HOST_CONTRACT_VERSION)
            """
        )
        with tempfile.TemporaryDirectory() as unrelated:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script, str(SHELL / "knowledge_host.py")],
                cwd=unrelated,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "1")


class KnowledgeHostDuplicateMemberTest(unittest.TestCase):
    def test_duplicate_envelope_members_are_refused_without_registry_work(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        cases = (
            '{"type":"workstack-knowledge-request","schema_version":1,'
            f'"request_id":"{REQUEST_ID}","operation":"choose-vault",'
            '"operation":"status"}',
            '{"type":"trusted-shell-request",'
            '"type":"workstack-knowledge-request","schema_version":1,'
            f'"request_id":"{REQUEST_ID}","operation":"status"}}',
            '{"type":"workstack-knowledge-request","schema_version":2,'
            f'"schema_version":1,"request_id":"{REQUEST_ID}","operation":"status"}}',
        )
        for payload in cases:
            with self.subTest(payload=payload[:60]):
                response = parse(host.handle_json(payload))
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(registry.calls, [])

    def test_duplicate_nested_members_are_refused_at_every_depth(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        binding = (
            f'{{"workspace_uid":"{OTHER_WORKSPACE}","workspace_uid":"{WORKSPACE}",'
            f'"task_uid":"{TASK_UID}","task_id":"T-0033","task_revision":2}}'
        )
        payload = (
            '{"type":"workstack-knowledge-request","schema_version":1,'
            f'"request_id":"{REQUEST_ID}","operation":"list-references",'
            f'"binding":{binding}}}'
        )

        response = parse(host.handle_json(payload))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(registry.calls, [])

    def test_distinct_members_are_still_accepted(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        response = parse(host.handle_json(request("list-references", binding=BINDING)))

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["binding"], BINDING)


class KnowledgeHostChooseVaultCancelTest(unittest.TestCase):
    def cancelling_host(
        self, registry: FakeRegistry, selection: object, current: dict[str, str]
    ) -> KH.KnowledgeHostService:
        def picker() -> object:
            current["uid"] = OTHER_WORKSPACE
            return selection

        return KH.KnowledgeHostService(
            Path(tempfile.gettempdir()),
            picker,
            lambda: current["uid"],
            registry=registry,
        )

    def test_cancel_is_refused_when_the_picker_switched_the_workspace(self) -> None:
        for selection in (None, "", "   ", Path(" ")):
            with self.subTest(selection=repr(selection)):
                registry = FakeRegistry()
                current = {"uid": WORKSPACE}
                host = self.cancelling_host(registry, selection, current)

                response = parse(
                    host.handle_json(request("choose-vault", binding=BINDING))
                )

                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "workspace_mismatch")
                self.assertEqual(registry.calls, [])

    def test_cancel_still_succeeds_while_the_workspace_holds(self) -> None:
        registry = FakeRegistry()
        host = KH.KnowledgeHostService(
            Path(tempfile.gettempdir()), lambda: None, WORKSPACE, registry=registry
        )

        response = parse(host.handle_json(request("choose-vault", binding=BINDING)))

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"], {"cancelled": True})
        self.assertEqual(registry.calls, [])


class KnowledgeHostReasonTest(unittest.TestCase):
    def test_multiline_reason_reaches_the_registry_verbatim(self) -> None:
        registry = FakeRegistry()
        host = service(registry)
        reason = "why this span matters:\n\t- audited on review\n\t- keep"

        response = parse(host.handle_json(pin_request(reason=reason)))

        self.assertTrue(response["ok"], response)
        self.assertEqual(registry.calls[0][-1], reason)
        self.assertEqual(response["data"]["reference"]["reason"], reason)

    def test_stored_multiline_reason_survives_list_references(self) -> None:
        registry = FakeRegistry()
        registry.references = [sample_metadata(reason="first line\nsecond line")]
        host = service(registry)

        response = parse(host.handle_json(request("list-references", binding=BINDING)))

        self.assertTrue(response["ok"], response)
        self.assertEqual(
            response["data"]["references"][0]["reason"], "first line\nsecond line"
        )

    def test_other_control_characters_in_reason_stay_refused(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        for control in ("\r", "\x00", "\x0b", "\x0c", "\x1b", "\x1f"):
            with self.subTest(control=repr(control)):
                response = parse(
                    host.handle_json(pin_request(reason=f"keep{control}this"))
                )
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(registry.calls, [])

    def test_empty_and_oversized_reasons_stay_refused(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        for reason in ("", "\n" * (KH.MAX_REASON_CHARS + 1)):
            with self.subTest(reason=len(reason)):
                response = parse(host.handle_json(pin_request(reason=reason)))
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(registry.calls, [])

class KnowledgeHostSearchTest(unittest.TestCase):
    def test_search_returns_exactly_the_documented_payload(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        response = parse(host.handle_json(search_request()))

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["operation"], "search-references")
        data = response["data"]
        self.assertEqual(
            set(data), {"binding", "query", "corpus", "matches", "omitted_count"}
        )
        self.assertEqual(data["binding"], BINDING)
        self.assertEqual(data["query"], "release notes")
        self.assertEqual(data["corpus"], sample_corpus())
        self.assertEqual(data["omitted_count"], 0)
        self.assertEqual(data["matches"][0]["freshness"], "unchanged")
        self.assertEqual(data["matches"][0]["trust"], "external_reference")
        self.assertEqual(
            registry.calls,
            [("search_references", BINDING, "personal-wiki", "release notes")],
        )

    def test_the_query_reaches_the_registry_trimmed(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        response = parse(host.handle_json(search_request("  release notes  ")))

        self.assertTrue(response["ok"], response)
        self.assertEqual(registry.calls[0][-1], "release notes")
        self.assertEqual(response["data"]["query"], "release notes")

    def test_omitted_candidates_are_reported_without_matches(self) -> None:
        registry = FakeRegistry()
        registry.search_result = sample_search(matches=[], omitted_count=3)
        host = service(registry)

        response = parse(host.handle_json(search_request()))

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["matches"], [])
        self.assertEqual(response["data"]["omitted_count"], 3)

    def test_empty_control_and_oversized_queries_are_refused(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        for query in ("", "   ", "a" * (KH.MAX_QUERY_CHARS + 1), "drop\x00here",
                      "bell\x07", "del\x7f", "c1\x9b"):
            with self.subTest(query=repr(query)[:40]):
                response = parse(host.handle_json(search_request(query)))
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_query")
        response = parse(host.handle_json(search_request(query=None)))
        self.assertEqual(response["error"]["code"], "invalid_query")
        self.assertEqual(registry.calls, [])

    def test_the_ui_cannot_name_a_command_root_or_url(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        for payload in (
            search_request(command=["C:/tools/evil.exe"]),
            search_request(vault_root="C:/secret"),
            search_request(url="https://example.invalid/search"),
            request("search-references", binding=BINDING, query="notes"),
            request("search-references", binding=BINDING, vault_id="personal-wiki"),
            request("search-references", vault_id="personal-wiki", query="notes"),
        ):
            with self.subTest(payload=payload[:90]):
                response = parse(host.handle_json(payload))
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertNotIn("secret", json.dumps(response))
                self.assertNotIn("evil", json.dumps(response))
        self.assertEqual(registry.calls, [])

    def test_an_invalid_vault_identity_is_refused(self) -> None:
        registry = FakeRegistry()
        host = service(registry)

        response = parse(host.handle_json(search_request(vault_id="../escape")))

        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "invalid_vault_id")
        self.assertEqual(registry.calls, [])

    def test_registry_search_codes_reach_the_ui_without_sources(self) -> None:
        for code in (
            "search_unconfigured",
            "search_unavailable",
            "search_timeout",
            "search_invalid_response",
        ):
            with self.subTest(code=code):
                registry = FakeRegistry()
                registry.search_error = code
                host = service(registry)

                response = parse(host.handle_json(search_request()))

                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], code)
                self.assertEqual(
                    response["error"]["message"], KH._SAFE_MESSAGES[code]
                )
                self.assertNotIn("C:", response["error"]["message"])

    def test_a_stale_workspace_is_refused_before_and_after_the_search(self) -> None:
        registry = FakeRegistry()
        host = service(registry, workspace=OTHER_WORKSPACE)

        response = parse(host.handle_json(search_request()))

        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "workspace_mismatch")
        self.assertEqual(registry.calls, [])

    def test_a_workspace_switch_during_the_search_discards_the_answer(self) -> None:
        current = {"uid": WORKSPACE}

        class SwitchingRegistry(FakeRegistry):
            def search_references(
                self, binding: object, vault_id: str, query: str
            ) -> dict[str, object]:
                current["uid"] = OTHER_WORKSPACE
                return super().search_references(binding, vault_id, query)

        switching = SwitchingRegistry()
        host = KH.KnowledgeHostService(
            Path(tempfile.gettempdir()),
            lambda: None,
            lambda: current["uid"],
            registry=switching,
        )

        response = parse(host.handle_json(search_request()))

        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "workspace_mismatch")
        self.assertEqual(len(switching.calls), 1)

    def test_an_unusable_registry_answer_is_refused(self) -> None:
        cases = (
            sample_search(matches=[sample_read(expected_sha256=SHA)] * 6),
            sample_search(omitted_count=-1),
            sample_search(omitted_count=True),
            sample_search(corpus=sample_corpus(label="C:\\vault\\personal")),
            sample_search(corpus=sample_corpus(label="two\nlines")),
            sample_search(corpus=sample_corpus(label="")),
            sample_search(corpus=sample_corpus(document_count=-1)),
            sample_search(corpus=sample_corpus(document_count=True)),
            sample_search(corpus=sample_corpus(indexed_at="yesterday")),
            sample_search(corpus={"label": "wiki", "document_count": 1}),
            {"corpus": sample_corpus(), "matches": []},
        )
        for result in cases:
            with self.subTest(result=str(result)[:70]):
                registry = FakeRegistry()
                registry.search_result = result
                host = service(registry)

                response = parse(host.handle_json(search_request()))

                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "operation_failed")

    def test_a_missing_registry_refuses_the_search(self) -> None:
        host = service(None)

        response = parse(host.handle_json(search_request()))

        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "registry_unavailable")

    def test_search_requests_are_correlated_for_overflow_refusals(self) -> None:
        self.assertEqual(
            KH.correlate_knowledge_request(search_request()),
            (REQUEST_ID, "search-references"),
        )
        host = service(FakeRegistry())
        response = parse(host.overflow_response(search_request()))
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["operation"], "search-references")
        self.assertEqual(response["error"]["code"], "busy")


class SearchBoundsModuleTest(unittest.TestCase):
    """The extracted search bounds stay one module the host still publishes."""

    def test_the_host_publishes_the_extracted_search_bounds(self) -> None:
        for name in (
            "MAX_QUERY_CHARS",
            "MAX_SEARCH_MATCHES",
            "MAX_CORPUS_LABEL_CHARS",
            "MAX_DOCUMENT_COUNT",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(KH, name), getattr(KHS, name))

    def test_a_search_refusal_carries_only_a_closed_code(self) -> None:
        error = KHS.SearchContractError("invalid_query")

        self.assertEqual(error.code, "invalid_query")
        self.assertEqual(str(error), "invalid_query")

    def test_the_search_bounds_do_not_import_the_host_back(self) -> None:
        """One direction only: the dispatcher imports the bounds, never the reverse."""

        tree = ast.parse((SHELL / "knowledge_host_search.py").read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        self.assertNotIn("knowledge_host", imported)
        self.assertNotIn("knowledge_registry", imported)


if __name__ == "__main__":
    unittest.main()
