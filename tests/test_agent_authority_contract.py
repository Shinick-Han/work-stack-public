"""Independent conformance tests for store-free authority admission."""

from __future__ import annotations

import builtins
import dataclasses
import importlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_REAL_IMPORT = builtins.__import__
_FORBIDDEN_IMPORT_PREFIXES = (
    "workstack.store",
    "workstack.storage",
    "workstack.server",
    "workstack.service",
)


def _absolute_import_name(
    name: str,
    globals_: dict[str, object] | None,
    level: int,
) -> str:
    if not level:
        return name
    package = "" if globals_ is None else str(globals_.get("__package__", ""))
    try:
        return importlib.util.resolve_name("." * level + name, package)
    except (ImportError, ValueError):
        return name


def _reject_forbidden_imports(
    name: str,
    globals_: dict[str, object] | None = None,
    locals_: dict[str, object] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
):
    absolute = _absolute_import_name(name, globals_, level)
    candidates = {absolute}
    candidates.update(f"{absolute}.{item}" for item in (fromlist or ()))
    for candidate in candidates:
        if candidate.startswith(_FORBIDDEN_IMPORT_PREFIXES) or candidate.endswith(
            "connection_registry"
        ):
            raise AssertionError(
                f"authority admission imported forbidden layer: {candidate}"
            )
    return _REAL_IMPORT(name, globals_, locals_, fromlist, level)


with patch.object(builtins, "__import__", side_effect=_reject_forbidden_imports):
    from workstack.agent_authority import admit_authority
    from workstack.agent_cli_contract import AuthorityAdmission

from workstack.store import Store


CANONICAL_UID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_UID = "4d36e96e-e325-41ce-bfc1-08002be10318"
NIL_UID = "00000000-0000-0000-0000-000000000000"
SECRET_TASK_TEXT = "TOP-SECRET-WRONG-WORKSPACE-TASK"


class _ReadGuard:
    def __init__(self, source, observed_sizes: list[int]) -> None:
        self._source = source
        self._observed_sizes = observed_sizes

    def __enter__(self):
        self._source.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._source.__exit__(exc_type, exc_value, traceback)

    def read(self, size: int = -1) -> bytes:
        self._observed_sizes.append(size)
        return self._source.read(size)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _workspace(uid: object = CANONICAL_UID, **updates: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 2,
        "id": uid,
        "name": "Authority fixture",
    }
    document.update(updates)
    return document


def _metadata(schema: object = 3, **updates: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 2,
        "store_schema_version": schema,
        "migrations": {},
    }
    document.update(updates)
    return document


def _make_v3(root: Path, *, uid: object = CANONICAL_UID) -> None:
    root.mkdir(parents=True)
    _write_json(root / "workspace.json", _workspace(uid))
    _write_json(root / "store-meta.json", _metadata(3))


def _make_v4_store(root: Path, *, uid: object = CANONICAL_UID) -> None:
    root.mkdir(parents=True)
    _write_json(root / "workspace.json", _workspace(uid))
    _write_json(
        root / "store.json",
        {"format": "workstack.ssot", "schema_version": 4},
    )


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture path/type/content and write metadata for the full test tree."""

    entries: list[tuple[object, ...]] = []
    if not root.exists():
        return ()
    for path in [root, *sorted(root.rglob("*"), key=lambda p: p.as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        stat = path.stat()
        if path.is_dir():
            entries.append((relative, "dir", stat.st_mtime_ns))
        elif path.is_file():
            entries.append((relative, "file", stat.st_mtime_ns, path.read_bytes()))
        else:
            entries.append((relative, "other", stat.st_mtime_ns))
    return tuple(entries)


class AuthorityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _call(self, data_dir: Path, expected_uid: str = CANONICAL_UID):
        with (
            patch.object(
                Store,
                "__init__",
                side_effect=AssertionError("Store construction is forbidden"),
            ),
            patch.object(
                builtins,
                "__import__",
                side_effect=_reject_forbidden_imports,
            ),
        ):
            return admit_authority(
                data_dir=data_dir,
                expected_workspace_uid=expected_uid,
            )

    def _assert_refused(
        self,
        data_dir: Path,
        code: str,
        *,
        expected_uid: str = CANONICAL_UID,
        forbidden_text: tuple[str, ...] = (),
    ) -> ValueError:
        before = _tree_snapshot(self.root)
        with self.assertRaises(ValueError) as raised:
            self._call(data_dir, expected_uid)
        after = _tree_snapshot(self.root)
        self.assertEqual(before, after, "authority refusal mutated its surrounding tree")
        error = raised.exception
        self.assertEqual(error.args, (code,))
        self.assertEqual(str(error), code)
        message = str(error)
        for text in (str(data_dir.resolve(strict=False)), *forbidden_text):
            self.assertNotIn(text, message)
        return error

    def test_public_seam_is_exactly_keyword_only(self) -> None:
        self.assertEqual(
            sys.modules[admit_authority.__module__].__all__, ("admit_authority",)
        )
        signature = inspect.signature(admit_authority)
        self.assertEqual(tuple(signature.parameters), ("data_dir", "expected_workspace_uid"))
        self.assertEqual(
            signature.parameters["data_dir"].annotation, "pathlib.Path"
        )
        self.assertEqual(
            signature.parameters["expected_workspace_uid"].annotation, "str"
        )
        self.assertEqual(
            signature.return_annotation,
            "workstack.agent_cli_contract.AuthorityAdmission",
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )
        authority = self.root / "valid"
        _make_v3(authority)
        with self.assertRaises(TypeError):
            admit_authority(authority, CANONICAL_UID)

    def test_importing_seam_does_not_import_store_storage_server_or_service(self) -> None:
        original = sys.modules.pop("workstack.agent_authority")
        package = sys.modules["workstack"]
        original_attribute = getattr(package, "agent_authority")
        try:
            with patch.object(
                builtins,
                "__import__",
                side_effect=_reject_forbidden_imports,
            ):
                reloaded = importlib.import_module("workstack.agent_authority")
            self.assertTrue(callable(reloaded.admit_authority))
        finally:
            sys.modules["workstack.agent_authority"] = original
            setattr(package, "agent_authority", original_attribute)

    def test_valid_v3_metadata_returns_frozen_resolved_admission(self) -> None:
        authority = self.root / "nested" / "valid authority"
        _make_v3(authority)
        admission = self._call(authority / ".." / "valid authority")
        self.assertIs(type(admission), AuthorityAdmission)
        self.assertEqual(admission.data_dir, authority.resolve())
        self.assertEqual(admission.workspace_uid, CANONICAL_UID)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            admission.workspace_uid = OTHER_UID  # type: ignore[misc]

    def test_valid_v3_path_supports_spaces_and_unicode(self) -> None:
        authority = self.root / "team source 한글 café"
        _make_v3(authority)
        before = _tree_snapshot(self.root)
        admission = self._call(authority)
        self.assertEqual(_tree_snapshot(self.root), before)
        self.assertEqual(admission.data_dir, authority.resolve())
        self.assertEqual(admission.workspace_uid, CANONICAL_UID)

    def test_missing_non_directory_empty_and_unrecognizable_are_invalid(self) -> None:
        missing = self.root / "missing"
        self._assert_refused(missing, "invalid_authority")

        regular_file = self.root / "regular-file"
        regular_file.write_text("not a directory", encoding="utf-8")
        self._assert_refused(regular_file, "invalid_authority")

        empty = self.root / "empty"
        empty.mkdir()
        self._assert_refused(empty, "invalid_authority")

        noise = self.root / "noise"
        noise.mkdir()
        (noise / "readme.txt").write_text("not an authority", encoding="utf-8")
        self._assert_refused(noise, "invalid_authority")

    def test_malformed_or_non_object_documents_are_invalid(self) -> None:
        for index, body in enumerate((b"{", b"[]", b"null", b'"text"')):
            authority = self.root / f"bad-workspace-{index}"
            authority.mkdir()
            (authority / "workspace.json").write_bytes(body)
            _write_json(authority / "store-meta.json", _metadata())
            self._assert_refused(authority, "invalid_authority")

        authority = self.root / "bad-metadata"
        authority.mkdir()
        _write_json(authority / "workspace.json", _workspace())
        (authority / "store-meta.json").write_bytes(b"{")
        self._assert_refused(authority, "invalid_authority")

    def test_workspace_v2_shape_is_exact(self) -> None:
        variants = (
            {"version": 1, "id": CANONICAL_UID, "name": "old"},
            {"version": True, "id": CANONICAL_UID, "name": "boolean"},
            {"version": 2, "id": CANONICAL_UID, "name": ""},
            {"version": 2, "id": CANONICAL_UID, "name": "   "},
            {"version": 2, "id": CANONICAL_UID, "name": 7},
            {"version": 2, "id": CANONICAL_UID, "name": "x", "extra": 1},
        )
        for index, document in enumerate(variants):
            authority = self.root / f"workspace-shape-{index}"
            authority.mkdir()
            _write_json(authority / "workspace.json", document)
            _write_json(authority / "store-meta.json", _metadata())
            self._assert_refused(authority, "invalid_authority")

    def test_v3_metadata_shape_is_exact(self) -> None:
        variants = (
            _metadata(2),
            _metadata(5),
            _metadata(True),
            _metadata(3, version=1),
            _metadata(3, migrations=[]),
            _metadata(3, extra="field"),
        )
        for index, document in enumerate(variants):
            authority = self.root / f"metadata-shape-{index}"
            authority.mkdir()
            _write_json(authority / "workspace.json", _workspace())
            _write_json(authority / "store-meta.json", document)
            self._assert_refused(authority, "invalid_authority")

    def test_authority_documents_are_bounded(self) -> None:
        huge_workspace = self.root / "huge-workspace"
        huge_workspace.mkdir()
        _write_json(
            huge_workspace / "workspace.json",
            _workspace(name="x" * (70 * 1024)),
        )
        _write_json(huge_workspace / "store-meta.json", _metadata())
        self._assert_refused(huge_workspace, "invalid_authority")

        huge_metadata = self.root / "huge-metadata"
        huge_metadata.mkdir()
        _write_json(huge_metadata / "workspace.json", _workspace())
        _write_json(
            huge_metadata / "store-meta.json",
            _metadata(3, migrations={"padding": "x" * (70 * 1024)}),
        )
        self._assert_refused(huge_metadata, "invalid_authority")

    def test_every_authority_document_read_has_the_frozen_byte_bound(self) -> None:
        authority = self.root / "bounded-read"
        _make_v3(authority)
        observed_sizes: list[int] = []
        original_open = Path.open

        def monitored_open(path: Path, *args, **kwargs):
            source = original_open(path, *args, **kwargs)
            return _ReadGuard(source, observed_sizes)

        with patch.object(Path, "open", new=monitored_open):
            self._call(authority)
        self.assertEqual(observed_sizes, [64 * 1024 + 1, 64 * 1024 + 1])

    def test_v4_store_marker_and_v4_metadata_are_capability_refusals(self) -> None:
        store_authority = self.root / "v4-store"
        _make_v4_store(store_authority)
        self._assert_refused(store_authority, "capability_not_enabled")

        metadata_authority = self.root / "v4-metadata"
        metadata_authority.mkdir()
        _write_json(metadata_authority / "workspace.json", _workspace())
        _write_json(metadata_authority / "store-meta.json", _metadata(4))
        self._assert_refused(metadata_authority, "capability_not_enabled")

    def test_invalid_v4_marker_and_mixed_format_conflicts_are_invalid(self) -> None:
        invalid_markers = (
            {"format": "other", "schema_version": 4},
            {"format": "workstack.ssot", "schema_version": 3},
            {"format": "workstack.ssot", "schema_version": True},
        )
        for index, marker in enumerate(invalid_markers):
            authority = self.root / f"invalid-v4-marker-{index}"
            authority.mkdir()
            _write_json(authority / "workspace.json", _workspace())
            _write_json(authority / "store.json", marker)
            self._assert_refused(authority, "invalid_authority")

        conflict = self.root / "v4-and-v3"
        _make_v4_store(conflict)
        _write_json(conflict / "backlog.json", {})
        self._assert_refused(conflict, "invalid_authority")

        double_marker = self.root / "v4-and-metadata"
        _make_v4_store(double_marker)
        _write_json(double_marker / "store-meta.json", _metadata(4))
        self._assert_refused(double_marker, "invalid_authority")

    def test_marker_directories_are_not_authority_documents(self) -> None:
        for marker in ("store.json", "store-meta.json"):
            authority = self.root / f"directory-{marker}"
            authority.mkdir()
            _write_json(authority / "workspace.json", _workspace())
            (authority / marker).mkdir()
            self._assert_refused(authority, "invalid_authority")

    def test_actual_uid_must_be_canonical_non_nil_rfc4122(self) -> None:
        invalid = (
            NIL_UID,
            CANONICAL_UID.upper(),
            "not-a-uuid",
            "550e8400-e29b-41d4-7716-446655440000",
            123,
            None,
        )
        for index, uid in enumerate(invalid):
            authority = self.root / f"invalid-actual-uid-{index}"
            _make_v3(authority, uid=uid)
            self._assert_refused(authority, "invalid_authority")

    def test_expected_uid_must_be_canonical_non_nil_and_match_exactly(self) -> None:
        for index, expected in enumerate(
            (NIL_UID, CANONICAL_UID.upper(), "not-a-uuid", OTHER_UID)
        ):
            authority = self.root / f"invalid-expected-uid-{index}"
            _make_v3(authority)
            self._assert_refused(
                authority,
                "workspace_mismatch",
                expected_uid=expected,
            )

    def test_wrong_workspace_never_exposes_task_or_path(self) -> None:
        authority = self.root / "wrong workspace private path"
        _make_v3(authority)
        _write_json(
            authority / "backlog.json",
            {
                "tasks": [
                    {
                        "id": "T-0001",
                        "title": SECRET_TASK_TEXT,
                        "detail": SECRET_TASK_TEXT,
                    }
                ]
            },
        )
        self._assert_refused(
            authority,
            "workspace_mismatch",
            expected_uid=OTHER_UID,
            forbidden_text=(SECRET_TASK_TEXT,),
        )

    def test_explicit_authority_ignores_home_and_desktop_profile(self) -> None:
        explicit = self.root / "explicit"
        fallback = self.root / "ambient-profile-authority"
        _make_v3(explicit)
        _make_v3(fallback, uid=OTHER_UID)

        fake_local = self.root / "fake-local-app-data"
        fake_state = fake_local / "WorkStack"
        fake_state.mkdir(parents=True)
        _write_json(
            fake_state / "connection-registry.json",
            {
                "schema_version": 1,
                "active_profile_id": OTHER_UID,
                "profiles": [
                    {
                        "profile_id": OTHER_UID,
                        "kind": "local",
                        "data_dir": str(fallback),
                        "expected_workspace_id": OTHER_UID,
                        "enabled": True,
                    }
                ],
            },
        )
        environment = {
            "WORK_STACK_HOME": str(fallback),
            "LOCALAPPDATA": str(fake_local),
            "APPDATA": str(fake_local),
            "USERPROFILE": str(fake_local),
        }
        before = _tree_snapshot(self.root)
        with patch.dict(os.environ, environment, clear=False):
            admission = self._call(explicit)
        self.assertEqual(_tree_snapshot(self.root), before)
        self.assertEqual(admission.workspace_uid, CANONICAL_UID)
        self.assertEqual(admission.data_dir, explicit.resolve())

    def test_missing_explicit_authority_never_falls_back_to_ambient_profile(self) -> None:
        fallback = self.root / "ambient-valid-authority"
        _make_v3(fallback)
        missing = self.root / "missing-explicit"
        environment = {
            "WORK_STACK_HOME": str(fallback),
            "LOCALAPPDATA": str(self.root / "ambient-state"),
            "APPDATA": str(self.root / "ambient-state"),
            "USERPROFILE": str(self.root / "ambient-state"),
        }
        with patch.dict(os.environ, environment, clear=False):
            self._assert_refused(missing, "invalid_authority")


if __name__ == "__main__":
    unittest.main()
