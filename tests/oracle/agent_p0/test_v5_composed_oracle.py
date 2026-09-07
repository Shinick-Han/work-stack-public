"""v5 Oracle overlay: manifest registration plus real composed product oracles."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import tempfile
import typing
import unittest
from pathlib import Path
from unittest.mock import patch

import fixture_support
from workstack.agent_authority import admit_authority
from workstack.agent_cli_contract import (
    AgentOutcome,
    AuthorityAdmission,
    StatusRequest,
    contract_fixture_bytes,
    render_outcome,
)
from workstack.agent_local_backend import create_local_backend
from workstack.agent_transport import create_running_server_backend
from workstack.store import Store
from workstack.store_errors import StoreCorruptError
from workstack.store_rosters import V5_DOCUMENT_NAMES


CANONICAL_UID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_UID = "4d36e96e-e325-41ce-bfc1-08002be10318"
LIVE_FIXTURE_SHA256 = "f85678576139aa2f67a6a0f2ec1f44f41d732a56e623b7ab32e55305be693421"


def load_manifest() -> dict:
    return json.loads(
        (fixture_support.ORACLE_DIR / "manifest.v1.json").read_text(encoding="utf-8")
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _workspace(uid: object = CANONICAL_UID) -> dict[str, object]:
    return {"version": 2, "id": uid, "name": "Oracle v5 fixture"}


def _metadata(schema: object = 3) -> dict[str, object]:
    return {"version": 2, "store_schema_version": schema, "migrations": {}}


def _make_v3(root: Path) -> None:
    root.mkdir(parents=True)
    _write_json(root / "workspace.json", _workspace())
    _write_json(root / "store-meta.json", _metadata(3))


def _make_v5(root: Path) -> None:
    root.mkdir(parents=True)
    _write_json(root / "workspace.json", _workspace())
    _write_json(root / "store-meta.json", _metadata(5))
    _write_json(root / "reports.json", {"version": 1, "reports": [], "idempotency": []})


def _make_v4(root: Path) -> None:
    root.mkdir(parents=True)
    _write_json(root / "workspace.json", _workspace())
    _write_json(root / "store.json", {"format": "workstack.ssot", "schema_version": 4})


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    if not root.exists():
        return ()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "dir"))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        else:
            entries.append((relative, "other"))
    return tuple(entries)


def _admit(data_dir: Path, expected_uid: str = CANONICAL_UID):
    with patch.object(
        Store,
        "__init__",
        side_effect=AssertionError("Store construction is forbidden"),
    ):
        return admit_authority(data_dir=data_dir, expected_workspace_uid=expected_uid)


class ManifestRegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_authority_admission_registers_required_storage_format_v3_or_v5(self):
        spec = self.manifest["abi"]["modules"]["workstack.agent_cli_contract"]["dataclasses"][
            "AuthorityAdmission"
        ]
        fields = spec["fields"]
        self.assertEqual(
            [item["name"] for item in fields],
            ["data_dir", "workspace_uid", "storage_format"],
        )
        self.assertTrue(spec["frozen"])
        self.assertTrue(spec["keyword_only"])
        self.assertTrue(spec["all_constructor_fields_required"])
        storage = fields[2]
        self.assertTrue(storage["required"])
        self.assertEqual(storage["type"], "Literal['v3','v5']")
        hints = typing.get_type_hints(AuthorityAdmission)
        self.assertEqual(set(typing.get_args(hints["storage_format"])), {"v3", "v5"})

    def test_status_projection_and_fixture_digest_match_composed_product(self):
        self.assertEqual(
            self.manifest["limits"]["storage_format_values"],
            ["unknown", "v3", "v4", "v5"],
        )
        keys = self.manifest["digest_recipes"]["contract_fixture_projection"]
        projection = {key: self.manifest[key] for key in keys}
        canonical = fixture_support.canonical_bytes(projection)
        digest = fixture_support.sha256_hex(canonical)
        live = contract_fixture_bytes()
        self.assertEqual(digest, LIVE_FIXTURE_SHA256)
        self.assertEqual(self.manifest["digest_recipes"]["contract"]["expected_sha256"], digest)
        self.assertEqual(hashlib.sha256(live).hexdigest(), digest)
        self.assertEqual(live, canonical)
        decoded = json.loads(live)
        self.assertEqual(decoded["limits"]["storage_format_values"], ["unknown", "v3", "v4", "v5"])
        self.assertIn("v5", json.dumps(decoded["limits"], separators=(",", ":")))

    def test_v3_only_runtime_wording_and_g21_g30_assertions(self):
        self.assertEqual(
            self.manifest["composition"]["runtime_order"][3],
            "construct Store for admitted v3 or v5",
        )
        self.assertIn(
            "admission of v3 or v5",
            self.manifest["module_rules"]["workstack.agent_local_backend"]["rules"][0],
        )
        g21 = self.manifest["sentinels"]["gates"]["G21-B1"]
        for item in (
            "explicit_path_with_spaces_and_non_ascii_admitted",
            "patched_store_init_refusals_succeed_store_free",
            "refusal_leaves_authority_tree_byte_identical",
            "wrong_workspace_uid_refuses_without_task_content",
            "v5_exact_metadata_admitted_store_free",
            "v4_capability_not_enabled_before_store",
            "mixed_store_json_plus_collection_or_reports_invalid",
            "schema_6_plus_and_malformed_versions_invalid",
        ):
            self.assertIn(item, g21)
        g30 = self.manifest["sentinels"]["gates"]["G30"]
        self.assertIn("v4_never_reaches_legacy_store", g30)
        self.assertIn("absent_owner_uses_local_v3_or_v5_transaction_path", g30)
        self.assertIn("v5_status_emits_actual_v5_label", g30)
        self.assertIn("v3_status_remains_exact_v3", g30)
        self.assertIn("dead_owner_never_falls_back_locally", g30)
        self.assertNotIn("absent_owner_uses_local_v3_transaction_path", g30)
        requirement = self.manifest["sentinels"]["bad_mutants"][0]["requirement"]
        self.assertIn("mixed", requirement)
        self.assertIn("newer", requirement)
        self.assertIn("v4", requirement)


class FrozenAdmissionConstructorTest(unittest.TestCase):
    def test_storage_format_is_required_and_frozen(self):
        with tempfile.TemporaryDirectory(prefix="p0-oracle-ctor-") as temporary:
            data_dir = Path(temporary)
            with self.assertRaises(TypeError):
                AuthorityAdmission(data_dir=data_dir, workspace_uid=CANONICAL_UID)
            v3 = AuthorityAdmission(
                data_dir=data_dir,
                workspace_uid=CANONICAL_UID,
                storage_format="v3",
            )
            v5 = AuthorityAdmission(
                data_dir=data_dir,
                workspace_uid=CANONICAL_UID,
                storage_format="v5",
            )
            self.assertEqual(v3.storage_format, "v3")
            self.assertEqual(v5.storage_format, "v5")
            with self.assertRaises(dataclasses.FrozenInstanceError):
                v5.storage_format = "v3"  # type: ignore[misc]


class RealAuthorityPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p0-oracle-auth-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_refused(self, data_dir: Path, code: str) -> None:
        before = _tree_snapshot(self.root)
        with self.assertRaises(ValueError) as raised:
            _admit(data_dir)
        self.assertEqual(_tree_snapshot(self.root), before)
        self.assertEqual(raised.exception.args, (code,))

    def test_historical_v3_positive_and_v5_positive_are_store_free(self):
        v3 = self.root / "v3"
        v5 = self.root / "v5"
        _make_v3(v3)
        _make_v5(v5)
        before = _tree_snapshot(self.root)
        admitted_v3 = _admit(v3)
        admitted_v5 = _admit(v5)
        self.assertEqual(_tree_snapshot(self.root), before)
        self.assertEqual(admitted_v3.storage_format, "v3")
        self.assertEqual(admitted_v5.storage_format, "v5")
        self.assertEqual(admitted_v3.workspace_uid, CANONICAL_UID)
        self.assertEqual(admitted_v5.workspace_uid, CANONICAL_UID)

    def test_v4_mixed_newer_and_roster_mismatch_refuse_without_writes(self):
        v4 = self.root / "v4"
        _make_v4(v4)
        self._assert_refused(v4, "capability_not_enabled")

        mixed = self.root / "mixed"
        _make_v4(mixed)
        _write_json(mixed / "reports.json", {"version": 1, "reports": [], "idempotency": []})
        self._assert_refused(mixed, "invalid_authority")

        mixed_meta = self.root / "mixed-v4-v5-meta"
        _make_v4(mixed_meta)
        _write_json(mixed_meta / "store-meta.json", _metadata(5))
        self._assert_refused(mixed_meta, "invalid_authority")

        newer = self.root / "schema-6"
        newer.mkdir()
        _write_json(newer / "workspace.json", _workspace())
        _write_json(newer / "store-meta.json", _metadata(6))
        self._assert_refused(newer, "invalid_authority")

        v3_plus_reports = self.root / "v3-reports"
        _make_v3(v3_plus_reports)
        _write_json(
            v3_plus_reports / "reports.json",
            {"version": 1, "reports": [], "idempotency": []},
        )
        self._assert_refused(v3_plus_reports, "invalid_authority")

        v5_missing_reports = self.root / "v5-missing-reports"
        v5_missing_reports.mkdir()
        _write_json(v5_missing_reports / "workspace.json", _workspace())
        _write_json(v5_missing_reports / "store-meta.json", _metadata(5))
        self._assert_refused(v5_missing_reports, "invalid_authority")


class RealComposedCoreAndStatusTest(unittest.TestCase):
    def test_real_store_validator_accepts_initialized_v5_and_refuses_roster_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="p0-oracle-store-") as temporary:
            store = Store(Path(temporary) / "v5-store")
            readiness = store.initialize()
            values = {name: copy.deepcopy(store.load(name)) for name in V5_DOCUMENT_NAMES}
            accepted = Store.validate_document_values(values, schema_version=5)
            self.assertEqual(accepted.schema_version, 5)
            self.assertEqual(accepted.workspace_uid, readiness.workspace_uid)
            with self.assertRaises(StoreCorruptError):
                Store.validate_document_values(values, schema_version=3)
            with self.assertRaises(StoreCorruptError):
                Store.validate_document_values({"workspace.json": values["workspace.json"]}, schema_version=5)

    def test_local_status_emits_exact_admitted_v5_label(self):
        with tempfile.TemporaryDirectory(prefix="p0-oracle-local-") as temporary:
            root = Path(temporary) / "authority"
            store = Store(root)
            readiness = store.initialize()
            admission = _admit(root, readiness.workspace_uid)
            self.assertEqual(admission.storage_format, "v5")

            def factory(*, root: Path) -> Store:
                self.assertEqual(root, admission.data_dir)
                return store

            backend = create_local_backend(admission=admission, store_factory=factory)
            status = backend.status(
                request=StatusRequest(
                    data_dir=admission.data_dir,
                    expected_workspace_uid=readiness.workspace_uid,
                )
            )
            self.assertEqual(status["storage_format"], "v5")
            self.assertTrue(status["capability_supported"])

    def test_running_status_emits_v5_and_never_falls_back_to_store(self):
        with tempfile.TemporaryDirectory(prefix="p0-oracle-transport-") as temporary:
            root = Path(temporary)
            info = root / "server-info.json"
            info.write_text(
                json.dumps({"host": "127.0.0.1", "port": 8765, "version": 1}, separators=(",", ":")),
                encoding="utf-8",
            )
            calls: list[str] = []

            class Requester:
                def request(self, *, host, port, method, path, body, headers):
                    calls.append(path)
                    if path == "/api/v1/session":
                        return 200, {"data": {"csrf_token": "csrf"}}
                    if path == "/api/v1/storage":
                        return 200, {
                            "data": {
                                "store_schema_version": 5,
                                "workspace_id": CANONICAL_UID,
                            }
                        }
                    if path == "/api/v1/sync/status":
                        return 200, {"data": {"state": "in-sync"}}
                    raise AssertionError(path)

            backend = create_running_server_backend(
                server_info_path=info,
                expected_workspace_uid=CANONICAL_UID,
                request_json=Requester(),
            )
            with patch.object(
                Store,
                "__init__",
                side_effect=AssertionError("running status must not construct Store"),
            ):
                status = backend.status(
                    request=StatusRequest(data_dir=root, expected_workspace_uid=CANONICAL_UID)
                )
            self.assertEqual(status["storage_format"], "v5")
            self.assertTrue(status["capability_supported"])
            self.assertEqual(
                calls,
                ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
            )

    def test_status_renderer_accepts_v5_and_rejects_v6(self):
        data = {
            "actual_workspace_uid": CANONICAL_UID,
            "capability_reason": None,
            "capability_supported": True,
            "contract": "workstack.cli.v1",
            "data_dir_available": True,
            "exclusive_local_available": True,
            "expected_workspace_uid": CANONICAL_UID,
            "ready": True,
            "running_server_available": False,
            "storage_format": "v5",
        }
        outcome = AgentOutcome(
            command="agent.status",
            commit_state=None,
            data=data,
            error_code=None,
            error_details={},
            error_message=None,
            intent_id=None,
            replayed=None,
            retryable=None,
            task_id=None,
            transport="exclusive-local",
            workspace_uid=CANONICAL_UID,
        )
        rendered = render_outcome(outcome=outcome)
        self.assertEqual(json.loads(rendered)["data"]["storage_format"], "v5")
        rejected = dict(data, storage_format="v6")
        with self.assertRaises(ValueError):
            render_outcome(outcome=AgentOutcome(**{**outcome.__dict__, "data": rejected}))


class HistoricalSentinelStrengthTest(unittest.TestCase):
    def test_original_mutant_ids_are_unchanged(self):
        records = fixture_support.read_jsonl(
            fixture_support.ORACLE_DIR / "golden" / "sentinel-verdicts.v1.jsonl"
        )
        by_file = {item["file"]: item for item in records if item["probe"] == "authority-preflight"}
        self.assertEqual(by_file["fixture_good_authority.py"]["expected_verdict"], "pass")
        self.assertEqual(by_file["store_before_preflight.py"]["expected_violation_ids"], ["P0-STORE-BEFORE-PREFLIGHT"])
        self.assertEqual(by_file["refusal_writes.py"]["expected_violation_ids"], ["P0-PREFLIGHT-TREE-MUTATION"])


if __name__ == "__main__":
    unittest.main()
