"""Admission rules for the committed store manifest and its task baseline.

The manifest is the baseline every later judgement is made against: which roster
a generation carried, which bytes it committed, and which Task revisions those
bytes meant. Each function decides that from a value alone -- it opens nothing
and holds nothing -- so the store can judge the exact documents it already holds
instead of whatever a path says a moment later.

``_validate_recovery_timestamp`` lives here for the same reason. Both persisted
control records that carry a ``created_at`` -- the recovery journal and the sync
rebind receipt -- are admitted by that one rule, and its message names the
recovery journal because that is the record it was written for.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

from .store_document_validation import (
    MAX_REVISION,
    _canonical_uuid,
    _compact_json,
    _stored_revision,
    supported_roster,
)
from .store_errors import StoreCorruptError
from .store_layout import DEFAULTS, STORE_MANIFEST_VERSION

__all__ = [
    "_task_semantics",
    "_validate_recovery_timestamp",
    "_validate_store_manifest_files",
    "_validate_store_manifest_header",
    "_validate_store_manifest_task",
    "_validate_store_manifest_tasks",
]


def _validate_store_manifest_header(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Admit a baseline manifest and report the roster its version implies.

    A released store that has not been upgraded yet carries the manifest its
    own schema wrote, and that manifest is the baseline the upgrade has to
    honour: it names the bytes Work Stack last committed, and refusing it would
    make the owned migration impossible while doing nothing for safety. So a
    manifest is admitted at any collection version this build can validate, and
    judged against *that* version's roster. Schema 4, an unknown version and a
    version newer than this build are refused exactly as before.
    """

    expected = {
        "version",
        "workspace_id",
        "store_schema_version",
        "generation",
        "files",
        "tasks",
    }
    if set(manifest) != expected or manifest.get("version") != STORE_MANIFEST_VERSION:
        raise StoreCorruptError("store manifest schema is invalid")
    if type(manifest.get("generation")) is not int or manifest["generation"] < 0:
        raise StoreCorruptError("store manifest generation is invalid")
    try:
        roster = supported_roster(manifest.get("store_schema_version"))
    except StoreCorruptError as error:
        raise StoreCorruptError("store manifest schema version is invalid") from error
    _canonical_uuid(manifest.get("workspace_id"), "store_manifest.workspace_id")
    return roster


def _validate_store_manifest_files(
    files: Any, roster: tuple[str, ...] | None = None
) -> None:
    """Judge a manifest roster, against this build's unless told otherwise.

    The released callers outside this module ask the only question they have
    ever asked — is this the roster this build writes — so omitting `roster`
    keeps their answer unchanged. The store passes the roster the manifest's
    own version implies, because a baseline left by an older schema is a
    smaller roster and still a valid baseline.
    """

    if roster is None:
        roster = tuple(sorted(DEFAULTS))
    if not isinstance(files, dict) or set(files) != set(roster):
        raise StoreCorruptError("store manifest file roster is invalid")
    if any(
        not isinstance(value, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        for value in files.values()
    ):
        raise StoreCorruptError("store manifest file digest is invalid")


def _validate_store_manifest_task(task_id: Any, task: Any) -> None:
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"T-[0-9]{4,}", task_id)
        or not isinstance(task, dict)
        or set(task) != {"revision", "digest"}
        or type(task.get("revision")) is not int
        or not 0 <= task["revision"] <= MAX_REVISION
        or not isinstance(task.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", task["digest"])
    ):
        raise StoreCorruptError("store manifest task baseline is invalid")


def _task_semantics(backlog: Any) -> dict[str, dict[str, Any]]:
    """The task baseline a manifest records, computed from one backlog value.

    This is the whole of that computation, so the manifest a commit writes and
    the manifest an upgrade checks are produced by the same rule. It takes the
    decoded backlog rather than reading one, which is what lets a caller judge
    the exact documents it already holds instead of whatever the path says a
    moment later.
    """

    tasks = backlog.get("tasks") if isinstance(backlog, dict) else None
    if not isinstance(tasks, list):
        raise StoreCorruptError("backlog.tasks must be an array")
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise StoreCorruptError("backlog task semantic baseline is invalid")
        result[task["id"]] = {
            "revision": _stored_revision(
                task.get("revision"), "{}.revision".format(task["id"])
            ),
            "digest": "sha256:" + hashlib.sha256(_compact_json(task)).hexdigest(),
        }
    return result


def _validate_store_manifest_tasks(tasks: Any) -> None:
    if not isinstance(tasks, dict):
        raise StoreCorruptError("store manifest task baseline is invalid")
    for task_id, task in tasks.items():
        _validate_store_manifest_task(task_id, task)


def _validate_recovery_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise StoreCorruptError("recovery journal created_at is invalid")
    try:
        parsed = dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise StoreCorruptError("recovery journal created_at is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreCorruptError("recovery journal created_at must include a timezone")
