from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "profile_inspection.py"
SPEC = importlib.util.spec_from_file_location("profile_inspection_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

from workstack.store import Store, StoreReadiness  # noqa: E402
from workstack.store_rosters import (  # noqa: E402
    REPORTS_DOCUMENT_NAME,
    V3_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
)


PROFILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
_CORE_SEAM = callable(getattr(Store, "validate_document_values", None))
_SKIP_CORE = "blocked pending Store.validate_document_values composition"
_REPORTS_EMPTY = {"version": 1, "reports": [], "idempotency": []}


def local_candidate(data_dir: Path, expected: str | None = None) -> object:
    return MODULE.profile_test_candidate_from_document(
        {
            "profile_id": PROFILE_ID,
            "label": "Local work",
            "kind": "local",
            "enabled": False,
            "live_updates": True,
            "data_dir": str(data_dir.absolute()),
            "expected_workspace_id": expected,
        }
    )


def ssh_candidate(expected: str | None = None) -> object:
    return MODULE.profile_test_candidate_from_document(
        {
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
            "expected_workspace_id": expected,
        }
    )


def create_store(data_dir: Path, runtime_dir: Path) -> str:
    with mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(runtime_dir)}):
        readiness = Store(data_dir).initialize()
    return readiness.workspace_uid


def clone_v3_fixture(data_dir: Path, name: str = "empty") -> None:
    shutil.copytree(ROOT / "tests" / "fixtures" / "store-v3" / name, data_dir)


def write_store_schema(data_dir: Path, schema: int) -> None:
    path = data_dir / "store-meta.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["store_schema_version"] = schema
    path.write_text(json.dumps(metadata), encoding="utf-8")


def forbid_store_construction():
    return mock.patch.object(
        MODULE.Store,
        "__init__",
        side_effect=AssertionError("Store construction is forbidden"),
    )


def isolated_readiness(schema_version: int, workspace_uid: str = WORKSPACE_ID) -> StoreReadiness:
    """Synthetic StoreReadiness for ordering oracles. Not a composed core PASS."""

    return StoreReadiness(
        schema_version=schema_version,
        workspace_uid=workspace_uid,
        task_count=0,
        migration_origin="isolated-ordering-oracle",
    )


def isolated_validate_seam(workspace_uid: str = WORKSPACE_ID):
    """Patch only the desktop call order. Report as mock coverage, never integrated."""

    def _validate(values, *, schema_version):
        return isolated_readiness(schema_version, workspace_uid)

    return mock.patch.object(
        MODULE.Store,
        "validate_document_values",
        staticmethod(_validate),
        create=True,
    )


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ProfileInspectionTest(unittest.TestCase):
    def test_nonexistent_and_empty_local_directories_are_candidates_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "missing", root / "empty")
            paths[1].mkdir()
            before = tree_hashes(root)

            results = [MODULE.inspect_profile(local_candidate(path)) for path in paths]

            self.assertEqual([result.status for result in results], ["candidate", "candidate"])
            self.assertTrue(all(result.actual_workspace_id is None for result in results))
            self.assertFalse(paths[0].exists())
            self.assertEqual(tree_hashes(root), before)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_complete_store_is_validated_and_identity_is_detected_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            workspace_id = create_store(data, root / "runtime")
            before = tree_hashes(root)

            result = MODULE.inspect_profile(local_candidate(data))

            self.assertEqual(result.status, "ready")
            self.assertEqual(result.actual_workspace_id, workspace_id)
            self.assertIsInstance(result.product_version, str)
            self.assertIsInstance(result.protocol_version, int)
            self.assertEqual(tree_hashes(root), before)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_complete_store_accepts_durable_task_display_id_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            workspace_id = create_store(data, root / "runtime")
            workspace_path = data / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["task_display_id_high_water"] = 53
            workspace_path.write_text(json.dumps(workspace), encoding="utf-8")
            before = tree_hashes(root)

            result = MODULE.inspect_profile(local_candidate(data))

            self.assertEqual(result.status, "ready")
            self.assertEqual(result.actual_workspace_id, workspace_id)
            self.assertEqual(tree_hashes(root), before)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_task_display_id_high_water_must_be_a_safe_non_negative_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            create_store(data, root / "runtime")
            workspace_path = data / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["task_display_id_high_water"] = True
            workspace_path.write_text(json.dumps(workspace), encoding="utf-8")

            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(data))

            self.assertEqual(raised.exception.code, "invalid_store")

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_task_display_id_high_water_cannot_be_below_live_task_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            shutil.copytree(ROOT / "tests" / "fixtures" / "store-v3" / "populated", data)
            workspace_path = data / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["task_display_id_high_water"] = 1
            workspace_path.write_text(json.dumps(workspace), encoding="utf-8")

            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(data))

            self.assertEqual(raised.exception.code, "invalid_store")

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_expected_identity_mismatch_is_explicit_and_does_not_rebind_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            actual = create_store(data, root / "runtime")
            before = tree_hashes(root)

            result = MODULE.inspect_profile(local_candidate(data, OTHER_WORKSPACE_ID))

            self.assertEqual(result.status, "identity_mismatch")
            self.assertEqual(result.actual_workspace_id, actual)
            self.assertEqual(tree_hashes(root), before)

    def test_partial_and_nonstore_directories_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "partial"
            partial.mkdir()
            (partial / "workspace.json").write_text("{}", encoding="utf-8")
            unrelated = root / "unrelated"
            unrelated.mkdir()
            (unrelated / "notes.txt").write_text("not a Store", encoding="utf-8")
            with forbid_store_construction():
                for path, code in (
                    (partial, "partial_store"),
                    (unrelated, "local_directory_not_empty"),
                ):
                    with self.subTest(path=path), self.assertRaises(MODULE.ProfileInspectionError) as raised:
                        MODULE.inspect_profile(local_candidate(path))
                    self.assertEqual(raised.exception.code, code)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_corrupt_complete_store_is_invalid_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "corrupt"
            create_store(corrupt, root / "runtime")
            (corrupt / "workspace.json").write_text("{}", encoding="utf-8")
            before = tree_hashes(root)
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(corrupt))
            self.assertEqual(raised.exception.code, "invalid_store")
            self.assertEqual(tree_hashes(root), before)

    def test_root_unc_device_traversal_and_reparse_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = (
                str(Path(root.anchor)),
                r"\\server\share\workstack",
                r"\\?\C:\workstack",
                str(root / ".." / "escape"),
            )
            for path in unsafe:
                with self.subTest(path=path), self.assertRaises(MODULE.ProfileInspectionError):
                    MODULE.validate_local_directory_path(path)

            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                return
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.validate_local_directory_path(link)
            self.assertEqual(raised.exception.code, "unsafe_local_path")

    def test_authoritative_store_file_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            create_store(data, root / "runtime")
            workspace = data / "workspace.json"
            target = root / "outside-workspace.json"
            target.write_bytes(workspace.read_bytes())
            workspace.unlink()
            try:
                workspace.symlink_to(target)
            except OSError:
                return

            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(data))

            self.assertEqual(raised.exception.code, "unsafe_local_path")

    def test_candidate_parser_allows_null_identity_but_remains_exact_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = {
                "profile_id": PROFILE_ID,
                "label": "Local work",
                "kind": "local",
                "enabled": False,
                "live_updates": True,
                "data_dir": str(Path(directory).absolute()),
                "expected_workspace_id": None,
            }
            candidate = MODULE.profile_test_candidate_from_document(raw)
            self.assertIsNone(candidate.expected_workspace_id)
            self.assertFalse(candidate.profile.enabled)

            for change in (
                {"unknown": True},
                {"enabled": 1},
                {"expected_workspace_id": "bad"},
                {"data_dir": str(Path(directory) / ".." / "escape")},
            ):
                with self.subTest(change=change), self.assertRaises(RuntimeError):
                    MODULE.profile_test_candidate_from_document({**raw, **change})

    def test_ssh_test_retains_only_bounded_metadata_and_derives_identity_state(self) -> None:
        tester = mock.Mock(
            return_value=MODULE.SshProfileMetadata(WORKSPACE_ID, "1.0.6", 1)
        )
        result = MODULE.inspect_profile(ssh_candidate(), ssh_profile_tester=tester)
        document = MODULE.profile_test_result_to_document(result)

        self.assertEqual(
            document,
            {
                "profile_id": PROFILE_ID,
                "kind": "ssh",
                "status": "ready",
                "actual_workspace_id": WORKSPACE_ID,
                "product_version": "1.0.6",
                "protocol_version": 1,
            },
        )
        tested = tester.call_args.args[0]
        self.assertEqual(tested.ssh_host_alias, "work-linux")
        self.assertNotIn("remote_app_dir", document)
        self.assertNotIn("remote_data_dir", document)

        mismatch = MODULE.inspect_profile(
            ssh_candidate(OTHER_WORKSPACE_ID), ssh_profile_tester=tester
        )
        self.assertEqual(mismatch.status, "identity_mismatch")

    def test_ssh_runner_failure_is_sanitized_and_missing_runner_fails_closed(self) -> None:
        with self.assertRaises(MODULE.ProfileInspectionError) as missing:
            MODULE.inspect_profile(ssh_candidate())
        self.assertEqual(missing.exception.code, "ssh_test_unavailable")

        def leaks_secret(_profile: object) -> object:
            raise RuntimeError("C:/secret/id_rsa password")

        with self.assertRaises(MODULE.ProfileInspectionError) as failed:
            MODULE.inspect_profile(ssh_candidate(), ssh_profile_tester=leaks_secret)
        self.assertEqual(failed.exception.code, "ssh_test_failed")
        self.assertNotIn("secret", str(failed.exception).casefold())

    def test_ssh_stable_tokens_map_to_public_codes_without_leaking_stderr(self) -> None:
        cases = (
            ("SSH_AUTH_FAILED", "ssh_auth_failed"),
            ("REMOTE_PYTHON_REQUIRED", "remote_python_required"),
            ("REMOTE_PYTHON_NOT_FOUND", "remote_python_not_found"),
            ("REMOTE_PYTHON_TOO_OLD", "remote_python_too_old"),
            ("REMOTE_APP_MISMATCH", "remote_app_mismatch"),
            ("REMOTE_WORKSPACE_MISMATCH", "remote_workspace_mismatch"),
            ("REMOTE_LOCK_OWNED", "remote_lock_owned"),
            ("REMOTE_PROTOCOL_INVALID", "remote_protocol_invalid"),
        )
        for token, code in cases:
            def leak(_profile: object, token: str = token) -> object:
                raise RuntimeError(f"{token}: C:/secret/id_rsa pid=9 password")

            with self.subTest(token=token), self.assertRaises(MODULE.ProfileInspectionError) as failed:
                MODULE.inspect_profile(ssh_candidate(), ssh_profile_tester=leak)
            self.assertEqual(failed.exception.code, code)
            text = str(failed.exception).casefold()
            self.assertNotIn("secret", text)
            self.assertNotIn("id_rsa", text)
            self.assertNotIn("password", text)
            self.assertNotIn("pid=9", text)

    def test_result_metadata_is_bounded_and_candidate_cannot_claim_identity(self) -> None:
        invalid = (
            MODULE.SshProfileMetadata(WORKSPACE_ID, "x" * 65, 1),
            MODULE.SshProfileMetadata(WORKSPACE_ID, "1.0.6", -1),
            MODULE.SshProfileMetadata("not-a-uuid", "1.0.6", 1),
        )
        for metadata in invalid:
            with self.subTest(metadata=metadata), self.assertRaises(RuntimeError):
                MODULE.inspect_profile(
                    ssh_candidate(), ssh_profile_tester=lambda _profile: metadata
                )
        forged = MODULE.ProfileTestResult(
            PROFILE_ID, "local", "candidate", WORKSPACE_ID, "1.0.6", 1
        )
        with self.assertRaises(RuntimeError):
            MODULE.profile_test_result_to_document(forged)


class ProfileInspectionReadBoundTest(unittest.TestCase):
    """Exercise the public reader over committed fixtures, without constructing Store."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"
        self.assertTrue(self.data.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))

    def copy_fixture(self, name: str = "populated") -> None:
        shutil.copytree(ROOT / "tests" / "fixtures" / "store-v3" / name, self.data)

    def observe_reads(self, target: Path, before_open=None, after_read=None):
        real_open = Path.open
        reads: list[tuple[int, int]] = []

        class ObservedFile:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                payload = self.stream.read(size)
                reads.append((size, len(payload)))
                if after_read is not None:
                    after_read(real_open)
                return payload

        def open_file(path, mode="r", *args, **kwargs):
            if path == target and mode == "rb":
                if before_open is not None:
                    before_open(real_open, len(reads))
                return ObservedFile(real_open(path, mode, *args, **kwargs))
            return real_open(path, mode, *args, **kwargs)

        return mock.patch.object(Path, "open", open_file), reads

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_healthy_empty_and_populated_profiles_use_configured_bounded_reads(self) -> None:
        self.assertEqual(MODULE.MAX_STORE_FILE_BYTES, 64 * 1024 * 1024)
        self.assertEqual(MODULE.MAX_STORE_TOTAL_BYTES, 128 * 1024 * 1024)
        for fixture in ("empty", "populated"):
            with self.subTest(fixture=fixture):
                self.data = self.root / fixture
                self.copy_fixture(fixture)
                before = tree_hashes(self.root)
                observer, reads = self.observe_reads(self.data / "workspace.json")
                with observer:
                    result = MODULE.inspect_profile(local_candidate(self.data))
                self.assertEqual(result.status, "ready")
                self.assertEqual(result.actual_workspace_id, WORKSPACE_ID)
                self.assertEqual(len(reads), 2)
                self.assertTrue(all(size == MODULE.MAX_STORE_FILE_BYTES + 1 for size, _ in reads))
                self.assertEqual(tree_hashes(self.root), before)

    def test_file_growing_before_initial_open_is_bounded_and_refused_read_only(self) -> None:
        self.copy_fixture()
        target = self.data / "workspace.json"
        original = target.read_bytes()
        limit = 4096
        self.assertLess(len(original), limit)
        grown = original + b" " * (limit * 4)
        expected = tree_hashes(self.root)
        expected[str(target.relative_to(self.root))] = hashlib.sha256(grown).hexdigest()

        def grow(real_open, count):
            self.assertEqual(count, 0)
            with real_open(target, "wb") as stream:
                stream.write(grown)

        observer, reads = self.observe_reads(target, before_open=grow)
        with mock.patch.object(MODULE, "MAX_STORE_FILE_BYTES", limit), observer:
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(self.data))
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertEqual(reads, [(limit + 1, limit + 1)])
        self.assertEqual(tree_hashes(self.root), expected)

    def test_payload_length_must_match_observed_size_even_if_metadata_is_restored(self) -> None:
        self.copy_fixture()
        target = self.data / "workspace.json"
        original = target.read_bytes()
        metadata = target.stat()
        before = tree_hashes(self.root)

        def grow(real_open, count):
            self.assertEqual(count, 0)
            with real_open(target, "wb") as stream:
                stream.write(original + b"not-json")

        def restore(real_open):
            with real_open(target, "wb") as stream:
                stream.write(original)
            os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

        observer, reads = self.observe_reads(target, before_open=grow, after_read=restore)
        with observer, self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(local_candidate(self.data))
        # Refuse inconsistent bytes before parsing, even when both stats agree.
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertEqual(reads, [(MODULE.MAX_STORE_FILE_BYTES + 1, len(original) + 8)])
        self.assertEqual(tree_hashes(self.root), before)

    def test_total_byte_budget_remains_enforced_without_writes(self) -> None:
        self.copy_fixture()
        before = tree_hashes(self.root)
        total = sum((self.data / name).stat().st_size for name in MODULE.STORE_FILES)
        with mock.patch.object(MODULE, "MAX_STORE_TOTAL_BYTES", total - 1):
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(self.data))
        self.assertEqual(raised.exception.code, "store_too_large")
        self.assertEqual(tree_hashes(self.root), before)

    def test_missing_and_malformed_files_keep_existing_refusal_codes(self) -> None:
        self.copy_fixture()
        target = self.data / "workspace.json"
        for payload, code in ((b"{", "invalid_store"), (None, "partial_store")):
            with self.subTest(code=code):
                if payload is None:
                    target.unlink()
                else:
                    target.write_bytes(payload)
                before = tree_hashes(self.root)
                with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                    MODULE.inspect_profile(local_candidate(self.data))
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(tree_hashes(self.root), before)

    def test_final_stability_pass_still_refuses_changed_authoritative_bytes(self) -> None:
        """Final hash/stat pass after validation. Isolated mock seam, not composed PASS."""
        self.copy_fixture()
        target = self.data / "workspace.json"
        original = target.read_bytes()
        changed = original.replace(WORKSPACE_ID.encode(), OTHER_WORKSPACE_ID.encode())
        self.assertNotEqual(original, changed)
        expected = tree_hashes(self.root)
        expected[str(target.relative_to(self.root))] = hashlib.sha256(changed).hexdigest()

        def replace_on_second_open(real_open, count):
            if count == 1:
                with real_open(target, "wb") as stream:
                    stream.write(changed)

        observer, reads = self.observe_reads(target, before_open=replace_on_second_open)
        with isolated_validate_seam(), observer, self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(local_candidate(self.data))
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertEqual(len(reads), 2)
        self.assertEqual(tree_hashes(self.root), expected)


class ProfileInspectionRosterOccupancyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"

    def _inspect_without_store(self, path: Path):
        with forbid_store_construction():
            return MODULE.inspect_profile(local_candidate(path))

    def test_v3_plus_reports_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        (self.data / REPORTS_DOCUMENT_NAME).write_text(
            json.dumps(_REPORTS_EMPTY), encoding="utf-8"
        )
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_v5_metadata_without_reports_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        write_store_schema(self.data, 5)
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_newer_schema_on_v3_roster_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        write_store_schema(self.data, 6)
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_store_json_mixed_with_reports_is_mixed_store(self) -> None:
        self.data.mkdir()
        (self.data / "store.json").write_text(
            json.dumps({"format": "workstack.ssot", "schema_version": 4}),
            encoding="utf-8",
        )
        (self.data / REPORTS_DOCUMENT_NAME).write_text(
            json.dumps(_REPORTS_EMPTY), encoding="utf-8"
        )
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "mixed_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_reports_json_directory_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        (self.data / REPORTS_DOCUMENT_NAME).mkdir()
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_store_json_directory_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        (self.data / "store.json").mkdir()
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_store_json_symlink_is_invalid_without_store_construction(self) -> None:
        clone_v3_fixture(self.data)
        target = self.root / "store-target.json"
        target.write_text("{}", encoding="utf-8")
        try:
            (self.data / "store.json").symlink_to(target)
        except OSError:
            self.skipTest("symlinks are unavailable on this host")
        before = tree_hashes(self.root)
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            self._inspect_without_store(self.data)
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertEqual(tree_hashes(self.root), before)

    def test_isolated_mutation_during_validation_refuses_stale_digest(self) -> None:
        """Ordering oracle with a mock seam. Not a real composed v3/v5 inspector PASS."""

        clone_v3_fixture(self.data)
        target = self.data / "workspace.json"
        original = target.read_bytes()
        changed = original.replace(WORKSPACE_ID.encode(), OTHER_WORKSPACE_ID.encode())
        self.assertNotEqual(original, changed)
        expected = tree_hashes(self.root)
        expected[str(target.relative_to(self.root))] = hashlib.sha256(changed).hexdigest()

        def mutate_during_validation(values, *, schema_version):
            target.write_bytes(changed)
            return isolated_readiness(schema_version)

        with mock.patch.object(
            MODULE.Store,
            "validate_document_values",
            staticmethod(mutate_during_validation),
            create=True,
        ):
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(
                    local_candidate(self.data), enable_format_neutral=True
                )
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertEqual(tree_hashes(self.root), expected)

    def test_isolated_reports_added_during_validation_refuses_stale_v3(self) -> None:
        """Final exact-roster admission after core validation. Isolated mock seam, not composed PASS."""

        clone_v3_fixture(self.data)
        reports = self.data / REPORTS_DOCUMENT_NAME
        before = tree_hashes(self.root)

        def add_reports_during_validation(values, *, schema_version):
            reports.write_text(json.dumps(_REPORTS_EMPTY), encoding="utf-8")
            return isolated_readiness(schema_version)

        with mock.patch.object(
            MODULE.Store,
            "validate_document_values",
            staticmethod(add_reports_during_validation),
            create=True,
        ):
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(
                    local_candidate(self.data), enable_format_neutral=True
                )
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertTrue(reports.is_file())
        after = tree_hashes(self.root)
        self.assertIn(str(reports.relative_to(self.root)), after)
        self.assertNotEqual(before, after)

    def test_isolated_store_json_added_during_validation_refuses(self) -> None:
        """Final forbidden-marker admission after core validation. Isolated mock seam, not composed PASS."""

        clone_v3_fixture(self.data)
        marker = self.data / "store.json"

        def add_store_marker_during_validation(values, *, schema_version):
            marker.write_text(
                json.dumps({"format": "workstack.ssot", "schema_version": 4}),
                encoding="utf-8",
            )
            return isolated_readiness(schema_version)

        with mock.patch.object(
            MODULE.Store,
            "validate_document_values",
            staticmethod(add_store_marker_during_validation),
            create=True,
        ):
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(
                    local_candidate(self.data), enable_format_neutral=True
                )
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertTrue(marker.is_file())

    def test_store_files_budget_covers_the_v3_roster(self) -> None:
        self.assertEqual(set(MODULE.STORE_FILES), set(V3_DOCUMENT_NAMES))
        self.assertEqual(len(MODULE.STORE_FILES), 9)
        self.assertEqual(len(V5_DOCUMENT_NAMES), 10)
        self.assertIn(REPORTS_DOCUMENT_NAME, V5_DOCUMENT_NAMES)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_complete_v3_and_v5_directories_are_read_once_then_stability_hashed(self) -> None:
        clone_v3_fixture(self.data, "empty")
        v5 = self.root / "v5"
        create_store(v5, self.root / "runtime-v5")
        cases = (
            (self.data, V3_DOCUMENT_NAMES, "v3", 3),
            (v5, V5_DOCUMENT_NAMES, "v5", 5),
        )
        for path, roster, label, schema in cases:
            with self.subTest(label=label):
                before = tree_hashes(self.root)
                observed: dict[str, int] = {name: 0 for name in roster}
                real_open = Path.open

                def open_file(file_path: Path, mode="r", *args, **kwargs):
                    name = file_path.name
                    if name in observed and "b" in mode:
                        observed[name] += 1
                    return real_open(file_path, mode, *args, **kwargs)

                with mock.patch.object(Path, "open", open_file):
                    result = MODULE.inspect_profile(
                        local_candidate(path), enable_format_neutral=True
                    )
                self.assertEqual(result.status, "ready")
                self.assertEqual(result.authority.storage_format, label)
                self.assertEqual(result.authority.schema_version, schema)
                self.assertEqual(set(observed), set(roster))
                self.assertTrue(all(count == 2 for count in observed.values()))
                extra = path / "readme.txt"
                extra.write_text("noise", encoding="utf-8")
                second = MODULE.inspect_profile(
                    local_candidate(path), enable_format_neutral=True
                )
                extra.unlink()
                self.assertEqual(
                    second.authority.authority_manifest_digest,
                    result.authority.authority_manifest_digest,
                )
                self.assertEqual(tree_hashes(self.root), before)

    @unittest.skipUnless(_CORE_SEAM, _SKIP_CORE)
    def test_malformed_reports_json_delegates_to_core_validator(self) -> None:
        create_store(self.data, self.root / "runtime")
        (self.data / REPORTS_DOCUMENT_NAME).write_text(
            json.dumps({"version": 1, "reports": "not-a-list"}),
            encoding="utf-8",
        )
        before = tree_hashes(self.root)
        with mock.patch.object(
            MODULE.Store,
            "__init__",
            side_effect=AssertionError("Store construction is forbidden"),
        ):
            with self.assertRaises(MODULE.ProfileInspectionError) as raised:
                MODULE.inspect_profile(local_candidate(self.data))
        self.assertEqual(raised.exception.code, "invalid_store")
        self.assertNotIn("not-a-list", raised.exception.safe_message)
        self.assertEqual(tree_hashes(self.root), before)


if __name__ == "__main__":
    unittest.main()
