"""The recovery journal: what a replay may contain, and how it is replayed.

``save_many`` writes this journal before it touches an authoritative document,
so an interrupted commit is finished on the next transaction rather than guessed
at. That only holds while the journal is judged as strictly as the commit was:
every target is a roster member, named once, carrying the digest of the value it
claims to write.

The mixin is the replay itself. It refuses a target holding bytes Work Stack
never wrote, decides whether the baseline it found may be extended or has to be
rebuilt, and leaves the journal in place whenever it stops short. Composed onto
``Store``, it reads and writes through that Store's own held lease.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, Mapping

from .store_document_validation import _compact_json
from .store_errors import StoreCorruptError
from .store_layout import DEFAULTS, STORE_SCHEMA_VERSION, _serialized_json_bytes
from .store_manifest_validation import _validate_recovery_timestamp

__all__ = ["StoreRecoveryMixin", "_recovery_writes", "_validate_recovery_write"]


def _recovery_writes(journal: dict[str, Any]) -> list[dict[str, Any]]:
    if set(journal) != {"version", "operation_id", "created_at", "writes"}:
        raise StoreCorruptError("recovery journal has unknown or missing fields")
    if type(journal["version"]) is not int or journal["version"] != 1:
        raise StoreCorruptError("unsupported recovery journal version")
    operation_id = journal["operation_id"]
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
        raise StoreCorruptError("recovery journal operation_id is invalid")
    _validate_recovery_timestamp(journal["created_at"])
    writes = journal["writes"]
    if not isinstance(writes, list) or not writes:
        raise StoreCorruptError("recovery journal writes must be a non-empty array")
    return writes


def _validate_recovery_write(write: Any, seen: set[str]) -> None:
    if not isinstance(write, dict) or set(write) != {"name", "value", "sha256"}:
        raise StoreCorruptError("recovery journal write entry is invalid")
    name = write["name"]
    if name not in DEFAULTS or name in seen:
        raise StoreCorruptError("recovery journal target is unknown or repeated")
    seen.add(name)
    if not isinstance(write["value"], dict):
        raise StoreCorruptError("recovery journal target value must be an object")
    expected = "sha256:" + hashlib.sha256(_compact_json(write["value"])).hexdigest()
    if not secrets.compare_digest(str(write["sha256"]), expected):
        raise StoreCorruptError("recovery journal value digest mismatch")


class StoreRecoveryMixin:
    """Journal replay for the composed ``Store``.

    Collaborators the composed Store owns: ``journal_path``, ``path``,
    ``_read_json_locked``, ``_atomic_write_locked``, ``_read_manifest_locked``,
    ``_local_baseline_tasks_locked``, ``_write_local_baseline_locked``,
    ``_inspect_sync_locked``, ``_local``, ``_generation`` and
    ``_recovered_files``.
    """

    def _validate_journal(self, journal: dict[str, Any]) -> list[dict[str, Any]]:
        writes = _recovery_writes(journal)
        seen: set[str] = set()
        for write in writes:
            _validate_recovery_write(write, seen)
        return writes

    def _assert_recovery_targets_safe_locked(
        self, manifest: Mapping[str, Any] | None, writes: list[dict[str, Any]]
    ) -> None:
        """Refuse a replay whose targets hold bytes Work Stack never wrote.

        A target the baseline does not name yet — the tenth document, during an
        interrupted upgrade — is safe only while it is absent or already holds
        exactly what the journal intends. Any other content under that name is
        an unowned change and is refused rather than laundered into a
        completed migration.
        """

        if manifest is None:
            return
        for write in writes:
            path = self.path(write["name"])
            current = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
            intended = "sha256:" + hashlib.sha256(
                _serialized_json_bytes(write["value"])
            ).hexdigest()
            if current not in {manifest["files"].get(write["name"]), intended}:
                raise StoreCorruptError(
                    "recovery target changed outside Work Stack; journal retained"
                )

    @staticmethod
    def _replayable_baseline(
        manifest: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """The baseline a replay may extend, or None when it must be rebuilt.

        A manifest an older schema left behind still names the bytes recovery
        is allowed to replace, and it is used for exactly that. It cannot
        describe the roster the journal completes, so carrying it forward would
        publish a baseline claiming the old version over the new generation.
        The manifest is neither deleted nor ignored: publication is deferred to
        `_finish_initialization_locked`, which rebuilds it from the generation
        that actually committed.
        """

        if manifest is None:
            return None
        if manifest["store_schema_version"] != STORE_SCHEMA_VERSION:
            return None
        return manifest

    def _recover_locked(self) -> None:
        if not self.journal_path.exists():
            return
        journal = self._read_json_locked(self.journal_path)
        writes = self._validate_journal(journal)
        baseline = self._read_manifest_locked()
        self._assert_recovery_targets_safe_locked(baseline, writes)
        manifest = self._replayable_baseline(baseline)
        self._local.replace_expectations = {
            write["name"]: (
                "sha256:" + hashlib.sha256(self.path(write["name"]).read_bytes()).hexdigest()
                if self.path(write["name"]).is_file()
                else None
            )
            for write in writes
        }
        try:
            for write in writes:
                self._atomic_write_locked(self.path(write["name"]), write["value"])
        finally:
            self._local.replace_expectations = {}
        recovered_files = sorted(write["name"] for write in writes)
        if manifest is not None:
            expected_hashes = dict(manifest["files"])
            for write in writes:
                expected_hashes[write["name"]] = "sha256:" + hashlib.sha256(
                    _serialized_json_bytes(write["value"])
                ).hexdigest()
            tasks = self._local_baseline_tasks_locked(
                manifest, expected_hashes, recovered_files
            )
            self._write_local_baseline_locked(
                manifest, expected_hashes, tasks, recovered_files
            )
            self.journal_path.unlink()
            self._inspect_sync_locked()
            self._recovered_files = []
            return
        self.journal_path.unlink()
        self._generation += 1
        self._recovered_files = recovered_files
