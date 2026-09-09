"""Detecting the collection version on disk and upgrading it to v6.

An upgrade rewrites every authoritative document, so the whole of this module is
about earning the right to start one: which version this directory actually is,
whether the generation it holds still matches the baseline it was committed
under, and whether a rollback archive of the *detected* bytes exists and
verifies from the path a rollback would read it from.

Every refusal here happens before the journal exists, which is what leaves a
drifted or foreign store exactly as it was found and makes a retry refuse the
same way.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from pathlib import Path
from typing import Any, Mapping

from . import store_knowledge_migration
from . import store_report_migration
from . import store_rosters
from .store_document_validation import StoreReadiness, validate_document_values
from .store_errors import StoreCorruptError
from .store_layout import (
    DEFAULTS,
    MIGRATION_BACKUP_DIR,
    STORE_SCHEMA_VERSION,
    _utc_stamp,
)
from .store_manifest_validation import _task_semantics

__all__ = ["StoreSchemaUpgradeMixin"]


class StoreSchemaUpgradeMixin:
    """Schema detection and the v6 upgrade for the composed ``Store``.

    Collaborators the composed Store owns: ``root``, ``path``,
    ``_read_documents_locked``, ``_decoded_documents``,
    ``_validate_ready_state_locked``, ``_atomic_write_bytes_locked``,
    ``save_many`` and the sync collaborator ``_read_manifest_locked``.
    """

    @staticmethod
    def _detected_schema(
        existing: set[str], values: Mapping[str, dict[str, Any]]
    ) -> int:
        """Which collection version this directory already is.

        v2 and v3 hold the same nine names, so only the metadata record they
        actually carry separates them; that is the signature the released store
        already used.
        """

        if existing == set(store_rosters.V6_DOCUMENT_NAMES):
            return 6
        if existing == set(store_rosters.V5_DOCUMENT_NAMES):
            # A v5 directory is exactly the ten v5 names. A v6 one carries
            # knowledge.json as well, so it can never be admitted as v5 and
            # then be judged against v5's evidence and v5's roster.
            return 5
        if existing == set(store_rosters.V1_DOCUMENT_NAMES):
            if (
                values["workspace.json"].get("version") != 1
                or values["backlog.json"].get("version") != 1
            ):
                raise StoreCorruptError("store migration is partial or missing evidence")
            return 1
        if existing != set(store_rosters.V3_DOCUMENT_NAMES):
            missing = sorted(set(DEFAULTS) - existing)
            raise StoreCorruptError(
                "required store roster is incomplete: {}".format(", ".join(missing))
            )
        metadata = values["store-meta.json"]
        is_v2 = (
            metadata.get("version") == 1
            and metadata.get("store_schema_version") == 2
            and values["backlog.json"].get("version") == 2
            and values["activity.json"].get("version") == 1
        )
        return 2 if is_v2 else 3

    def _migration_backup_dir(self) -> Path:
        return self.root / MIGRATION_BACKUP_DIR

    def _prebackup_target_locked(
        self, detected: int, bodies: Mapping[str, bytes]
    ) -> Path:
        """Where this exact source becomes a rollback archive, deterministically."""

        digest = store_report_migration.source_digest(self._decoded_documents(bodies))
        directory = self._migration_backup_dir()
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise StoreCorruptError("migration backup directory is not a directory")
        target = directory / "workstack-premigration-v{}-{}.zip".format(
            detected, digest[7:23]
        )
        if target.is_symlink():
            raise StoreCorruptError("migration backup path is a symlink")
        return target

    def _verify_prebackup_locked(
        self,
        target: Path,
        detected: int,
        bodies: Mapping[str, bytes],
        readiness: StoreReadiness,
        expected_digest: str,
    ) -> str:
        """Prove the rollback archive from the path it would be rolled back from.

        Verification is the shared read-only archive verifier, which takes one
        image of the path and answers every question from it: the container's
        directory, the manifest header, the roster the detected version
        implies, every member digest, and the documents admitted under that
        *old* schema. Nothing is initialized and no authoritative document is
        re-read, so the archive is judged against the very bytes still held
        from the one acquisition. A promise of a verified rollback that was
        never read back is not a verified rollback.

        The verdict is then bound to `expected_digest`, the digest of the bytes
        this migration packed. Validating one image and rolling back from
        another is the same failure as never reading the archive at all, so the
        image that verified must be the image this attempt produced, and the
        digest that proves it is returned for the journal to revalidate.
        """

        try:
            verified = store_report_migration.verify_archive_file(target)
        except ValueError as error:
            raise StoreCorruptError(
                "migration rollback backup did not verify"
            ) from error
        if (
            verified.store_schema_version != detected
            or verified.workspace_id != readiness.workspace_uid
            or verified.bodies != dict(bodies)
            or not secrets.compare_digest(verified.digest, expected_digest)
        ):
            raise StoreCorruptError("migration rollback backup did not verify")
        return verified.digest

    def _persist_prebackup_locked(
        self, detected: int, bodies: Mapping[str, bytes], readiness: StoreReadiness
    ) -> tuple[Path, str]:
        """Write and verify the rollback archive before the journal exists.

        The name is derived from the detected version and the digest of the
        detected bytes, so a retry after a crash lands on the same file instead
        of leaving a new one behind on every attempt. An existing file is
        reused only when it is byte-identical; anything else present under that
        name is a refusal, never an overwrite. Either way the file is then read
        back and verified, so a reused archive is held to the same contract as
        one this attempt wrote. Any refusal here happens before `save_many`, so
        the old generation and every authoritative byte are left as they were.

        The path and the digest of the image that actually verified are both
        returned, because the journal has to be able to say that the archive it
        is about to depend on is still the one that passed.
        """

        target = self._prebackup_target_locked(detected, bodies)
        packed = store_report_migration.pack_backup_archive(
            dict(bodies),
            workspace_id=readiness.workspace_uid,
            store_schema_version=detected,
            created=dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
        )
        if target.exists():
            if target.read_bytes() != packed["body"]:
                raise StoreCorruptError(
                    "an unrelated migration backup already holds this name"
                )
        else:
            self._atomic_write_bytes_locked(target, packed["body"])
        digest = self._verify_prebackup_locked(
            target, detected, bodies, readiness, packed["digest"]
        )
        return target, digest

    def _rebind_prebackup_locked(self, target: Path, verified_digest: str) -> None:
        """Say the archive about to be depended on is still the one that passed.

        Verification held one image and reported its digest; this reads the
        final path once more and refuses unless it is still that same image.
        The journal is what makes the upgrade authoritative, so the last thing
        established before it is that the rollback the journal presumes still
        exists byte for byte at the path a rollback would read.
        """

        try:
            image = target.read_bytes()
        except OSError as error:
            raise StoreCorruptError(
                "migration rollback backup did not verify"
            ) from error
        current = "sha256:" + hashlib.sha256(image).hexdigest()
        if not secrets.compare_digest(current, verified_digest):
            raise StoreCorruptError("migration rollback backup did not verify")

    def _upgrade_locked(
        self,
        detected: int,
        bodies: Mapping[str, bytes],
        values: Mapping[str, dict[str, Any]],
        readiness: StoreReadiness,
    ) -> StoreReadiness:
        digest = store_report_migration.source_digest(values)
        target, verified_digest = self._persist_prebackup_locked(
            detected, bodies, readiness
        )
        writes, operation_id = store_knowledge_migration.plan_upgrade(
            detected, values, digest, now=_utc_stamp()
        )
        self._rebind_prebackup_locked(target, verified_digest)
        self.save_many(writes, operation_id=operation_id)
        return self._validate_ready_state_locked()

    @staticmethod
    def _assert_upgrade_source_bytes_owned(
        manifest: Mapping[str, Any], bodies: Mapping[str, bytes]
    ) -> None:
        """Every held document must still be the byte the manifest recorded."""

        for name, body in bodies.items():
            recorded = manifest["files"].get(name)
            if recorded is None:
                continue
            if recorded != "sha256:" + hashlib.sha256(body).hexdigest():
                raise StoreCorruptError(
                    "upgrade source changed outside Work Stack; "
                    "store left at its detected version"
                )

    @staticmethod
    def _assert_upgrade_source_tasks_owned(
        manifest: Mapping[str, Any], values: Mapping[str, dict[str, Any]]
    ) -> None:
        """The manifest's task baseline must be the one the held backlog means.

        File digests say the bytes are unchanged; they say nothing about the
        baseline recorded beside them. A manifest whose task revisions or task
        digests describe some other backlog is not the baseline of this
        generation, and carrying it into the upgrade would publish revisions
        the held documents never had.

        A backlog whose tasks cannot yield a baseline under this rule is a
        legacy shape that predates the baseline, so it is consistent only with
        a manifest that claims none.
        """

        try:
            expected = _task_semantics(values["backlog.json"])
        except StoreCorruptError:
            expected = {}
        if manifest["tasks"] != expected:
            raise StoreCorruptError(
                "upgrade source task baseline does not match its manifest; "
                "store left at its detected version"
            )

    def _assert_upgrade_source_owned_locked(
        self,
        bodies: Mapping[str, bytes],
        values: Mapping[str, dict[str, Any]],
        readiness: StoreReadiness,
    ) -> None:
        """Refuse to upgrade a generation that already drifted from its baseline.

        The manifest a released build left behind names the workspace Work
        Stack owned, the bytes it last committed and the task baseline those
        bytes meant. All three have to hold. A manifest whose workspace is not
        the one the held documents declare is a baseline for a different store,
        and this migration would silently republish the identity of whichever
        one it happened to read; hash-equal files do not make two workspaces
        the same workspace. When a document this migration just read differs
        from the recorded byte, or the recorded task baseline is not what the
        held backlog means, the difference is an unowned change, and upgrading
        would rewrite it into a new schema, a new journal and new evidence —
        laundering it into accepted history.

        Detection belongs here, before the rollback archive and before the
        journal, so a drifted or foreign store is left exactly as it was found
        and a retry refuses the same way. Every question is answered from
        `bodies` and `values`, the documents this attempt already holds, so the
        generation admitted is the generation judged.
        """

        manifest = self._read_manifest_locked()
        if manifest is None:
            return
        if manifest["store_schema_version"] != readiness.schema_version:
            raise StoreCorruptError(
                "upgrade source manifest schema does not match the detected source; "
                "store left at its detected version"
            )
        if manifest["workspace_id"] != readiness.workspace_uid:
            raise StoreCorruptError(
                "upgrade source manifest belongs to another workspace; "
                "store left at its detected version"
            )
        self._assert_upgrade_source_bytes_owned(manifest, bodies)
        self._assert_upgrade_source_tasks_owned(manifest, values)

    def _admit_or_upgrade_locked(self, existing: set[str]) -> StoreReadiness:
        bodies = self._read_documents_locked(tuple(sorted(existing)))
        values = self._decoded_documents(bodies)
        detected = self._detected_schema(existing, values)
        readiness = validate_document_values(values, schema_version=detected)
        if detected == STORE_SCHEMA_VERSION:
            return readiness
        self._assert_upgrade_source_owned_locked(bodies, values, readiness)
        return self._upgrade_locked(detected, bodies, values, readiness)
