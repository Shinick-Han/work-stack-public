from __future__ import annotations

import hashlib
import importlib.util
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

from workstack.store import Store  # noqa: E402


PROFILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


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

    def test_partial_nonstore_and_corrupt_store_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "partial"
            partial.mkdir()
            (partial / "workspace.json").write_text("{}", encoding="utf-8")
            unrelated = root / "unrelated"
            unrelated.mkdir()
            (unrelated / "notes.txt").write_text("not a Store", encoding="utf-8")
            corrupt = root / "corrupt"
            create_store(corrupt, root / "runtime")
            (corrupt / "workspace.json").write_text("{}", encoding="utf-8")

            cases = (
                (partial, "partial_store"),
                (unrelated, "local_directory_not_empty"),
                (corrupt, "invalid_store"),
            )
            for path, code in cases:
                with self.subTest(path=path), self.assertRaises(MODULE.ProfileInspectionError) as raised:
                    MODULE.inspect_profile(local_candidate(path))
                self.assertEqual(raised.exception.code, code)

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
        with observer, self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(local_candidate(self.data))
        self.assertEqual(raised.exception.code, "store_changed")
        self.assertEqual(len(reads), 2)
        self.assertEqual(tree_hashes(self.root), expected)


if __name__ == "__main__":
    unittest.main()
