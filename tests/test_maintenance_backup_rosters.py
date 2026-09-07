from __future__ import annotations

import copy
import datetime as dt
import hashlib
import inspect
import json
import tempfile
import unittest
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from workstack import __version__
from workstack.maintenance import (
    BACKUP_MANIFEST,
    BACKUP_SCHEMA_VERSION,
    BackupDownload,
    BackupValidationError,
    _backup_roster,
    _build_backup_download_from_validated_bodies,
    _sha256,
    create_backup_download,
)
from workstack.store import DEFAULTS, STORE_SCHEMA_VERSION, Store
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V1_DOCUMENT_NAMES,
    V2_DOCUMENT_NAMES,
    V3_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
)


FIXED_UTC = dt.datetime(2026, 9, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
WORKSPACE_ID = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
SCHEMA_UNSUPPORTED = "backup store schema version is unsupported"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _synthetic_bodies(roster: tuple[str, ...], tag: bytes) -> dict[str, bytes]:
    return {name: tag + name.encode("ascii") for name in roster}


def _open_archive(body: bytes) -> tuple[list[str], dict[str, bytes], dict[str, Any]]:
    with zipfile.ZipFile(io_bytes(body), "r") as archive:
        names = [info.filename for info in archive.infolist()]
        members = {name: archive.read(name) for name in names}
    manifest = json.loads(members[BACKUP_MANIFEST].decode("utf-8"))
    return names, members, manifest


def io_bytes(body: bytes):
    import io

    return io.BytesIO(body)


def _authority_digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(DEFAULTS)
    }


def _tree_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    records: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            records.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(records)


class BackupRosterMappingTest(unittest.TestCase):
    def test_exact_frozen_sorted_rosters_for_supported_versions(self) -> None:
        expected = (
            (1, 8, V1_DOCUMENT_NAMES),
            (2, 9, V2_DOCUMENT_NAMES),
            (3, 9, V3_DOCUMENT_NAMES),
            (5, 10, V5_DOCUMENT_NAMES),
        )
        for version, count, frozen in expected:
            with self.subTest(version=version):
                roster = _backup_roster(version)
                self.assertEqual(roster, tuple(sorted(frozen)))
                self.assertEqual(len(roster), count)
                self.assertEqual(set(roster), set(frozen))
                self.assertEqual(roster, tuple(sorted(roster)))

    def test_bool_four_and_unknown_versions_are_refused(self) -> None:
        for value in (True, False, 0, 4, 6, 99, -1, "3", 3.0, None, object()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(BackupValidationError, SCHEMA_UNSUPPORTED) as caught:
                    _backup_roster(value)
                self.assertNotIn(str(value), str(caught.exception))
                self.assertEqual(str(caught.exception), SCHEMA_UNSUPPORTED)


class BackupArchivePackagingTest(unittest.TestCase):
    def test_builder_packs_each_synthetic_roster_in_sorted_order(self) -> None:
        cases = (
            (1, V1_DOCUMENT_NAMES, b"v1:"),
            (2, V2_DOCUMENT_NAMES, b"v2:"),
            (3, V3_DOCUMENT_NAMES, b"v3:"),
            (5, V5_DOCUMENT_NAMES, b"v5:"),
        )
        for version, frozen, tag in cases:
            roster = tuple(sorted(frozen))
            bodies = _synthetic_bodies(roster, tag)
            download = _build_backup_download_from_validated_bodies(
                bodies,
                workspace_id=WORKSPACE_ID,
                store_schema_version=version,
                created=FIXED_UTC,
            )
            names, members, manifest = _open_archive(download.body)
            with self.subTest(version=version):
                self.assertEqual(names, [BACKUP_MANIFEST, *roster])
                self.assertEqual(download.file_count, len(roster))
                self.assertEqual(manifest["store_schema_version"], version)
                self.assertEqual(manifest["schema_version"], BACKUP_SCHEMA_VERSION)
                self.assertEqual(manifest["product_version"], __version__)
                self.assertEqual(manifest["workspace_id"], WORKSPACE_ID)
                self.assertEqual(manifest["created_at"], "2026-09-06T12:00:00.000000Z")
                self.assertEqual(len(manifest["files"]), len(roster))
                self.assertEqual(
                    [record["name"] for record in manifest["files"]],
                    list(roster),
                )
                for record, name in zip(manifest["files"], roster, strict=True):
                    body = members[name]
                    self.assertEqual(body, bodies[name])
                    self.assertEqual(record["size"], len(body))
                    self.assertEqual(record["sha256"], _digest(body))
                    self.assertEqual(record["sha256"], _sha256(body))
                self.assertEqual(download.digest, _digest(download.body))
                self.assertEqual(
                    download.filename,
                    "workstack-backup-20260906T120000000000Z-{}.zip".format(
                        WORKSPACE_ID[:8]
                    ),
                )
                if version == 5:
                    self.assertIn(REPORTS_DOCUMENT_NAME, names)
                else:
                    self.assertNotIn(REPORTS_DOCUMENT_NAME, names)


class BackupBuilderRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = _backup_roster(3)
        self.bodies = _synthetic_bodies(self.roster, b"ok:")
        self.kwargs = {
            "workspace_id": WORKSPACE_ID,
            "store_schema_version": 3,
            "created": FIXED_UTC,
        }

    def _refuse(
        self,
        message: str,
        bodies: object | None = None,
        excerpt: str | None = None,
        **overrides: object,
    ) -> None:
        payload = self.bodies if bodies is None else bodies
        before = copy.deepcopy(payload) if type(payload) is dict else copy.deepcopy(payload)
        identities = (
            {name: id(value) for name, value in payload.items()}
            if type(payload) is dict
            else None
        )
        kwargs = dict(self.kwargs)
        kwargs.update(overrides)
        # The archive container belongs to the module that actually packs it,
        # so the "refused before any output" oracle watches that module rather
        # than reaching the same global through the maintenance wrapper.
        with mock.patch(
            "workstack.store_report_migration.zipfile.ZipFile"
        ) as archive_cls:
            with self.assertRaisesRegex(BackupValidationError, message) as caught:
                _build_backup_download_from_validated_bodies(payload, **kwargs)
            archive_cls.assert_not_called()
        self.assertNotRegex(str(caught.exception), r"[\\/]")
        if excerpt is not None:
            self.assertNotIn(excerpt, str(caught.exception))
        if type(payload) is dict:
            self.assertEqual(payload, before)
            self.assertEqual(
                {name: id(value) for name, value in payload.items()},
                identities,
            )
        else:
            self.assertEqual(payload, before)

    def test_missing_extra_and_non_string_keys_refuse_before_output(self) -> None:
        missing = dict(self.bodies)
        secret = missing.pop("notes.json")
        extra = dict(self.bodies)
        extra["extra.json"] = b"EXTRA-BODY"
        non_string = dict(self.bodies)
        non_string[1] = non_string.pop("notes.json")
        self._refuse("backup bodies are invalid", bodies=missing, excerpt=secret.decode("ascii"))
        self._refuse("backup bodies are invalid", bodies=extra, excerpt="EXTRA-BODY")
        self._refuse("backup bodies are invalid", bodies=non_string)

    def test_non_bytes_body_refuses_before_output(self) -> None:
        payload = dict(self.bodies)
        payload["notes.json"] = bytearray(b"NOT-BYTES-EXCERPT")
        self._refuse("backup bodies are invalid", bodies=payload, excerpt="NOT-BYTES-EXCERPT")
        payload = dict(self.bodies)
        payload["notes.json"] = "text-body"
        self._refuse("backup bodies are invalid", bodies=payload, excerpt="text-body")

    def test_invalid_workspace_time_and_schema_refuse_before_output(self) -> None:
        self._refuse(
            "backup workspace identity is invalid",
            workspace_id="",
            excerpt=WORKSPACE_ID,
        )
        self._refuse(
            "backup workspace identity is invalid",
            workspace_id="x" * 129,
            excerpt="x" * 129,
        )
        self._refuse(
            "backup workspace identity is invalid",
            workspace_id=WORKSPACE_ID.encode("ascii"),
        )
        self._refuse(
            "backup creation time is invalid",
            created=dt.datetime(2026, 9, 6, 12, 0, 0),
        )
        self._refuse(
            "backup creation time is invalid",
            created=dt.datetime(
                2026, 9, 6, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))
            ),
        )
        self._refuse(
            SCHEMA_UNSUPPORTED,
            store_schema_version=4,
        )
        self._refuse(
            SCHEMA_UNSUPPORTED,
            store_schema_version=True,
        )

    def test_successful_packaging_does_not_retain_or_mutate_inputs(self) -> None:
        bodies = _synthetic_bodies(self.roster, b"keep:")
        before = copy.deepcopy(bodies)
        identities = {name: id(value) for name, value in bodies.items()}
        download = _build_backup_download_from_validated_bodies(
            bodies,
            workspace_id=WORKSPACE_ID,
            store_schema_version=3,
            created=FIXED_UTC,
        )
        self.assertIsInstance(download, BackupDownload)
        self.assertEqual(bodies, before)
        self.assertEqual({name: id(value) for name, value in bodies.items()}, identities)
        self.assertNotIn(id(bodies), [id(download), id(download.body)])


class PublicV3BackupDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.store = Store(self.source)
        self.readiness = self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_public_download_is_ten_member_schema_five_with_one_held_read(self) -> None:
        before_authority = _authority_digests(self.source)
        before_tree = _tree_fingerprint(self.source)
        document_reads: list[str] = []
        original_read_bytes = Path.read_bytes
        source_root = self.source.resolve()

        def spy_read_bytes(path: Path) -> bytes:
            resolved = path if path.is_absolute() else source_root / path
            try:
                resolved = resolved.resolve()
            except OSError:
                pass
            if resolved.parent == source_root and path.name in DEFAULTS:
                document_reads.append(path.name)
            return original_read_bytes(path)

        original_consistent_read = self.store.consistent_read
        read_scopes = {"count": 0}

        @contextmanager
        def counting_consistent_read():
            read_scopes["count"] += 1
            with original_consistent_read() as readiness:
                yield readiness

        writes = {"save": 0, "save_many": 0, "initialize": 0, "bytes": 0, "text": 0}
        original_save = self.store.save
        original_save_many = self.store.save_many
        original_initialize = self.store.initialize
        original_write_bytes = Path.write_bytes
        original_write_text = Path.write_text

        def counted_save(*args: object, **kwargs: object) -> object:
            writes["save"] += 1
            return original_save(*args, **kwargs)

        def counted_save_many(*args: object, **kwargs: object) -> object:
            writes["save_many"] += 1
            return original_save_many(*args, **kwargs)

        def counted_initialize(*args: object, **kwargs: object) -> object:
            writes["initialize"] += 1
            return original_initialize(*args, **kwargs)

        def counted_write_bytes(path: Path, data: bytes) -> int:
            writes["bytes"] += 1
            return original_write_bytes(path, data)

        def counted_write_text(path: Path, data: str, encoding: str = "utf-8", errors: str = "strict") -> int:
            writes["text"] += 1
            return original_write_text(path, data, encoding=encoding, errors=errors)

        self.store.consistent_read = counting_consistent_read
        self.store.save = counted_save
        self.store.save_many = counted_save_many
        self.store.initialize = counted_initialize
        with mock.patch.object(Path, "read_bytes", spy_read_bytes), mock.patch(
            "workstack.maintenance._utc_now", return_value=FIXED_UTC
        ), mock.patch.object(Path, "write_bytes", counted_write_bytes), mock.patch.object(
            Path, "write_text", counted_write_text
        ):
            download = create_backup_download(self.store)

        self.assertEqual(read_scopes["count"], 1)
        self.assertEqual(Counter(document_reads), Counter(sorted(DEFAULTS)))
        self.assertEqual(sum(writes.values()), 0)
        names, members, manifest = _open_archive(download.body)
        # The public download follows this build's readiness, which is now v5.
        # The frozen v3 roster is still what a v3 archive is packed against,
        # and the builder cases above still prove that.
        roster = tuple(sorted(V5_DOCUMENT_NAMES))
        self.assertEqual(names, [BACKUP_MANIFEST, *roster])
        self.assertEqual(download.file_count, 10)
        self.assertEqual(manifest["store_schema_version"], 5)
        self.assertEqual(STORE_SCHEMA_VERSION, 5)
        self.assertEqual(frozenset(DEFAULTS), V5_DOCUMENT_NAMES)
        self.assertIn(REPORTS_DOCUMENT_NAME, DEFAULTS)
        self.assertIn(REPORTS_DOCUMENT_NAME, names)
        self.assertTrue((self.source / REPORTS_DOCUMENT_NAME).exists())
        self.assertEqual(download.workspace_id, self.readiness.workspace_uid)
        self.assertEqual(download.created_at, "2026-09-06T12:00:00.000000Z")
        self.assertEqual(
            download.filename,
            "workstack-backup-20260906T120000000000Z-{}.zip".format(
                self.readiness.workspace_uid[:8]
            ),
        )
        self.assertEqual(download.digest, _digest(download.body))
        self.assertEqual(download.file_count, len(manifest["files"]))
        for record in manifest["files"]:
            body = members[record["name"]]
            on_disk = (self.source / record["name"]).read_bytes()
            self.assertEqual(body, on_disk)
            self.assertEqual(record["size"], len(body))
            self.assertEqual(record["sha256"], _digest(body))
        self.assertEqual(_authority_digests(self.source), before_authority)
        self.assertEqual(_tree_fingerprint(self.source), before_tree)


class BackupBuilderIsolationTest(unittest.TestCase):
    def test_builder_source_and_imports_do_not_own_store_path_or_clock(self) -> None:
        builder_source = inspect.getsource(_build_backup_download_from_validated_bodies)
        wrapper_source = inspect.getsource(create_backup_download)
        self.assertNotIn("Store", builder_source)
        self.assertNotIn("Path", builder_source)
        self.assertNotIn("_utc_now", builder_source)
        self.assertIn("_utc_now", wrapper_source)
        self.assertIn("store.path", wrapper_source)
        self.assertIn("consistent_read", wrapper_source)
        self.assertIn("_build_backup_download_from_validated_bodies", wrapper_source)

        bodies = _synthetic_bodies(_backup_roster(3), b"iso:")
        with mock.patch("workstack.maintenance.Store") as store_cls, mock.patch(
            "workstack.maintenance.Path"
        ) as path_cls, mock.patch(
            "workstack.maintenance._utc_now"
        ) as clock:
            download = _build_backup_download_from_validated_bodies(
                bodies,
                workspace_id=WORKSPACE_ID,
                store_schema_version=3,
                created=FIXED_UTC,
            )
            store_cls.assert_not_called()
            path_cls.assert_not_called()
            clock.assert_not_called()
        self.assertEqual(download.file_count, 9)


if __name__ == "__main__":
    unittest.main()
