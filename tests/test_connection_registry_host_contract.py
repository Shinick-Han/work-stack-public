from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "connection_registry_host_contract.py"
SPEC = importlib.util.spec_from_file_location(
    "connection_registry_host_contract_test", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
ABSENT_REGISTRY_DIGEST = MODULE.registry_digest(None)


def local_registry(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": PROFILE_ID,
        "profiles": [
            {
                "profile_id": PROFILE_ID,
                "label": "Local work",
                "kind": "local",
                "enabled": True,
                "live_updates": True,
                "data_dir": str((root / "ssot").resolve()),
                "expected_workspace_id": WORKSPACE_ID,
            }
        ],
    }


def request(operation: str, **extra: object) -> str:
    return json.dumps(
        {
            "type": "workstack-connection-registry-request",
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "operation": operation,
            **extra,
        }
    )


def local_test_profile(root: Path, expected_workspace_id: str | None = None) -> dict[str, object]:
    return {
        "profile_id": PROFILE_ID,
        "label": "Local work",
        "kind": "local",
        "enabled": False,
        "live_updates": True,
        "data_dir": str((root / "ssot").absolute()),
        "expected_workspace_id": expected_workspace_id,
    }


def ssh_test_profile(expected_workspace_id: str | None = None) -> dict[str, object]:
    return {
        "profile_id": PROFILE_ID,
        "label": "Remote work",
        "kind": "ssh",
        "enabled": False,
        "live_updates": True,
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/work-stack",
        "remote_data_dir": "/srv/work-stack-data",
        "preferred_forward_port": 18765,
        "remote_port": 8765,
        "expected_workspace_id": expected_workspace_id,
    }


class ConnectionRegistryHostContractTest(unittest.TestCase):
    def test_get_registry_reports_missing_registry_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = MODULE.ConnectionRegistryHostService(root)

            response = json.loads(service.handle_json(request("get-registry")))

            self.assertEqual(
                response,
                {
                    "type": "workstack-connection-registry-response",
                    "schema_version": 1,
                    "request_id": REQUEST_ID,
                    "operation": "get-registry",
                    "ok": True,
                    "result": {
                        "registry": None,
                        "registry_digest": ABSENT_REGISTRY_DIGEST,
                    },
                },
            )
            self.assertEqual(list(root.iterdir()), [])

    def test_save_and_get_use_canonical_registry_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = local_registry(root)
            current = MODULE.save_connection_registry(root, registry)
            current_digest = MODULE.registry_digest(current)
            service = MODULE.ConnectionRegistryHostService(
                root,
                mutation_service=MODULE.ConnectionRegistryMutationService(root),
            )

            saved = json.loads(
                service.handle_json(request(
                    "save-registry",
                    registry=registry,
                    expected_registry_digest=current_digest,
                ))
            )
            loaded = json.loads(service.handle_json(request("get-registry")))

            self.assertTrue(saved["ok"])
            self.assertEqual(saved["result"], {
                "registry": registry,
                "registry_digest": current_digest,
            })
            self.assertEqual(loaded["result"], {
                "registry": registry,
                "registry_digest": current_digest,
            })
            self.assertTrue((root / "connection-registry.json").is_file())

    def test_alias_discovery_uses_only_injected_config_and_returns_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "ssh-config"
            discoverer = mock.Mock(return_value=("build", "work-linux"))
            service = MODULE.ConnectionRegistryHostService(
                root, ssh_config_path=config, alias_discoverer=discoverer
            )

            response = json.loads(
                service.handle_json(request("discover-ssh-aliases"))
            )

            self.assertEqual(response["result"], {"aliases": ["build", "work-linux"]})
            discoverer.assert_called_once_with(config)
            self.assertNotIn("config", json.dumps(response).casefold())

    def test_choose_local_directory_uses_injected_picker_and_cancel_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            picker = mock.Mock(return_value=selected)
            service = MODULE.ConnectionRegistryHostService(
                root, local_directory_picker=picker
            )

            response = json.loads(
                service.handle_json(request("choose-local-directory"))
            )

            self.assertEqual(response["result"], {"selection": str(selected.absolute())})
            picker.assert_called_once_with()
            self.assertFalse(selected.exists())

            cancelled = MODULE.ConnectionRegistryHostService(
                root, local_directory_picker=lambda: None
            )
            response = json.loads(
                cancelled.handle_json(request("choose-local-directory"))
            )
            self.assertEqual(response["result"], {"selection": None})

    def test_local_test_profile_accepts_null_identity_and_does_not_create_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "ssot"
            service = MODULE.ConnectionRegistryHostService(root)

            response = json.loads(
                service.handle_json(
                    request(
                        "test-profile",
                        profile=local_test_profile(root),
                        base_registry_digest=ABSENT_REGISTRY_DIGEST,
                    )
                )
            )

            self.assertTrue(response["ok"])
            self.assertEqual(
                response["result"],
                {
                    "profile_id": PROFILE_ID,
                    "kind": "local",
                    "status": "candidate",
                    "actual_workspace_id": None,
                    "product_version": None,
                    "protocol_version": None,
                    "proof_id": None,
                },
            )
            self.assertFalse(candidate.exists())

    def test_ssh_test_profile_returns_only_injected_bounded_metadata(self) -> None:
        metadata = MODULE.ProfileTestResult(
            PROFILE_ID, "ssh", "ready", WORKSPACE_ID, "1.0.6", 1
        )
        tester = mock.Mock(
            return_value=MODULE.SshProfileMetadata(WORKSPACE_ID, "1.0.6", 1)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = MODULE.save_connection_registry(root, local_registry(root))
            digest = MODULE.registry_digest(current)
            service = MODULE.ConnectionRegistryHostService(
                root,
                ssh_profile_tester=tester,
                mutation_service=MODULE.ConnectionRegistryMutationService(root),
            )
            response = json.loads(
                service.handle_json(
                    request(
                        "test-profile",
                        profile=ssh_test_profile(),
                        base_registry_digest=digest,
                    )
                )
            )

        self.assertEqual(
            {key: value for key, value in response["result"].items() if key != "proof_id"},
            MODULE.profile_test_result_to_document(metadata),
        )
        self.assertRegex(
            response["result"]["proof_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        serialized = json.dumps(response)
        self.assertNotIn("remote_app_dir", serialized)
        self.assertNotIn("remote_data_dir", serialized)
        tester.assert_called_once()

    def test_profile_inspection_failure_preserves_safe_code_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = MODULE.ConnectionRegistryHostService(root)
            response = json.loads(
                service.handle_json(
                    request(
                        "test-profile",
                        profile=ssh_test_profile(),
                        base_registry_digest=ABSENT_REGISTRY_DIGEST,
                    )
                )
            )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "ssh_test_unavailable")
        self.assertNotIn("/srv/", json.dumps(response))

    def test_activation_requires_and_consumes_exact_correlated_test_proof(self) -> None:
        tester = mock.Mock(
            return_value=MODULE.SshProfileMetadata(WORKSPACE_ID, "1.0.6", 1)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = local_registry(root)
            document["profiles"] = [ssh_test_profile(WORKSPACE_ID)]
            document["profiles"][0]["enabled"] = True  # type: ignore[index]
            registry = MODULE.save_connection_registry(root, document)
            digest = MODULE.registry_digest(registry)
            service = MODULE.ConnectionRegistryHostService(
                root,
                ssh_profile_tester=tester,
                mutation_service=MODULE.ConnectionRegistryMutationService(root),
            )
            tested_profile = ssh_test_profile(WORKSPACE_ID)
            tested_profile["enabled"] = True

            tested = json.loads(service.handle_json(request(
                "test-profile",
                profile=tested_profile,
                base_registry_digest=digest,
            )))
            proof_id = tested["result"]["proof_id"]
            activated = json.loads(service.handle_json(request(
                "activate-profile",
                registry=document,
                profile_id=PROFILE_ID,
                proof_id=proof_id,
                expected_registry_digest=digest,
            )))

            self.assertTrue(activated["ok"])
            self.assertTrue(activated["result"]["restart_required"])
            self.assertEqual(activated["result"]["registry_digest"], digest)
            replay = json.loads(service.handle_json(request(
                "activate-profile",
                registry=document,
                profile_id=PROFILE_ID,
                proof_id=proof_id,
                expected_registry_digest=digest,
            )))
            self.assertEqual(replay["error"]["code"], "test_required")

    def test_request_schema_rejects_unknown_fields_and_per_operation_shape(self) -> None:
        invalid = (
            request("get-registry", registry={}),
            request("discover-ssh-aliases", ssh_args=["-o", "ProxyCommand=calc"]),
            request("choose-local-directory", initial_path="C:/secret"),
            request("test-profile"),
            request("test-profile", profile=local_test_profile(Path.cwd()), ssh_args=[]),
            request("save-registry"),
            request("future-operation"),
            request("get-registry").replace(
                "workstack-connection-registry-request", "wrong-type"
            ),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                MODULE.decode_registry_host_request(payload)

    def test_rejects_wrong_version_and_noncanonical_or_nil_request_ids(self) -> None:
        values = (
            {"schema_version": True, "request_id": REQUEST_ID},
            {"schema_version": 2, "request_id": REQUEST_ID},
            {"schema_version": 1, "request_id": REQUEST_ID.upper()},
            {
                "schema_version": 1,
                "request_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        for changes in values:
            payload = json.loads(request("get-registry"))
            payload.update(changes)
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                MODULE.decode_registry_host_request(json.dumps(payload))

    def test_save_request_applies_nested_canonical_registry_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = local_registry(Path(directory))
            registry["unsupported"] = True
            with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
                MODULE.decode_registry_host_request(
                    request(
                        "save-registry",
                        registry=registry,
                        expected_registry_digest=ABSENT_REGISTRY_DIGEST,
                    )
                )

    def test_request_byte_limit_is_enforced_before_json_decode(self) -> None:
        oversized = b"{" + b" " * MODULE.MAX_HOST_REQUEST_BYTES + b"}"
        with mock.patch.object(MODULE.json, "loads") as loads:
            with self.assertRaisesRegex(RuntimeError, "too large"):
                MODULE.decode_registry_host_request(oversized)
        loads.assert_not_called()

    def test_invalid_utf8_and_non_object_json_fail_closed(self) -> None:
        for payload in (b"\xff", "[]", "null"):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                MODULE.decode_registry_host_request(payload)

    def test_service_errors_are_sanitized_and_retain_safe_correlation(self) -> None:
        mutations = mock.Mock()
        mutations.save_metadata.side_effect = RuntimeError(
            "secret path and credential"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = MODULE.ConnectionRegistryHostService(
                root, mutation_service=mutations
            )
            response = json.loads(
                service.handle_json(
                    request(
                        "save-registry",
                        registry=local_registry(root),
                        expected_registry_digest=ABSENT_REGISTRY_DIGEST,
                    )
                )
            )

        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["operation"], "save-registry")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "operation_failed")
        self.assertNotIn("secret", json.dumps(response))

    def test_invalid_requests_return_an_uncorrelated_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = MODULE.ConnectionRegistryHostService(Path(directory))
            response = json.loads(service.handle_json("not json"))

        self.assertEqual(
            response,
            {
                "type": "workstack-connection-registry-response",
                "schema_version": 1,
                "request_id": None,
                "operation": None,
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "Connection registry request is invalid.",
                },
            },
        )

    def test_response_encoder_rejects_invalid_alias_output(self) -> None:
        invalid = MODULE.DiscoverSshAliasesResponse(
            request_id=REQUEST_ID, aliases=("work;calc",)
        )
        with self.assertRaisesRegex(RuntimeError, "invalid entry"):
            MODULE.encode_registry_host_response(invalid)

    def test_response_encoder_has_a_separate_bounded_envelope_ceiling(self) -> None:
        self.assertGreater(
            MODULE.MAX_HOST_RESPONSE_BYTES,
            MODULE.MAX_HOST_REQUEST_BYTES,
        )
        oversized = MODULE.RegistryHostErrorResponse(
            request_id=None,
            operation=None,
            code="x",
            message="x" * 257,
        )
        with self.assertRaises(RuntimeError):
            MODULE.encode_registry_host_response(oversized)

    def test_invalid_or_oversized_discovery_output_is_a_sanitized_error(self) -> None:
        cases = (
            ("work", "WORK"),
            tuple(f"host-{index}" for index in range(MODULE.MAX_DISCOVERED_SSH_ALIASES + 1)),
        )
        for aliases in cases:
            with self.subTest(count=len(aliases)), tempfile.TemporaryDirectory() as directory:
                service = MODULE.ConnectionRegistryHostService(
                    Path(directory), alias_discoverer=lambda _path: aliases
                )
                response = json.loads(
                    service.handle_json(request("discover-ssh-aliases"))
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "operation_failed")


if __name__ == "__main__":
    unittest.main()
