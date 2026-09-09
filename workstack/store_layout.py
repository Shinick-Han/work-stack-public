"""The v6 document roster, the on-disk names and the shared serializers.

Every other store module answers questions about *these* names: which documents
a released store carries, what an untouched one holds, and the exact bytes a
value becomes on disk. They live together because the roster assertion below is
the single check that keeps them agreeing, and a check split across callers is a
convention rather than a rule.

Nothing here opens a path or holds a lock. The names are data, so the manifest
validators, the recovery journal and the schema upgrade can each be judged
against the same roster without any of them importing the Store.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from typing import Any

from . import store_rosters
from .knowledge_ledger_document import KNOWLEDGE_DEFAULT, KNOWLEDGE_DOCUMENT_NAME
from .store_document_validation import (
    ACTIVITY_DEFAULT,
    AUXILIARY_DEFAULTS,
    BACKLOG_DEFAULT,
    REPORTS_DEFAULT,
)
from .task_display_id import FIELD as TASK_DISPLAY_ID_HIGH_WATER

__all__ = [
    "CAPTURE_TOKEN_NAME",
    "CHANGE_NOTICE_TYPE",
    "DEFAULTS",
    "JOURNAL_NAME",
    "LOCK_NAME",
    "MAX_COMMIT_EVENTS",
    "MIGRATION_BACKUP_DIR",
    "SERVER_INFO_NAME",
    "STORE_MANIFEST_NAME",
    "STORE_MANIFEST_VERSION",
    "STORE_SCHEMA_VERSION",
    "SYNC_ADOPTION_RECEIPT_NAME",
    "SYNC_REBIND_RECEIPT_NAME",
]


# The one typed change record kind carried beside the legacy sync records.
CHANGE_NOTICE_TYPE = "workstack.change.v1"

# The ceiling one successful commit may emit. Where those events come from is
# recorded in ``store.py``, above the commit whose three successful branches
# the number is derived from.
MAX_COMMIT_EVENTS = 3


# Schema 4 belongs to workstack.ssot, so the collection-layout version after 3
# is 5: the nine released documents plus reports.json. Schema 6 adds the
# eleventh and last one this build knows, knowledge.json, the owner-held policy
# and request ledger. This single statement is what "which roster does this
# build write" means; the frozen sets in store_rosters answer the other
# question.
STORE_SCHEMA_VERSION = 6


_WORKSPACE_REQUIRED_KEYS = frozenset({"version", "id", "name"})
_WORKSPACE_OPTIONAL_KEYS = frozenset({TASK_DISPLAY_ID_HIGH_WATER})


def _workspace_default() -> dict[str, Any]:
    return {"version": 2, "id": str(uuid.uuid4()), "name": "Work Stack"}


def _store_meta_default() -> dict[str, Any]:
    return {
        "version": 2,
        "store_schema_version": STORE_SCHEMA_VERSION,
        "migrations": {
            "identity": {
                "id": "workstack.store.v2",
                "origin": "fresh",
                "source_sha256": None,
            },
            "planning_status": {
                "id": "workstack.planning-status.v1",
                "origin": "fresh",
                "source_sha256": None,
            },
            "reports": {
                "id": "workstack.reports.v5",
                "origin": "fresh",
                "source_sha256": None,
            },
            "knowledge": {
                "id": "workstack.knowledge.v6",
                "origin": "fresh",
                "source_sha256": None,
            },
        },
    }


# Composed from the payload shapes store_document_validation owns, so a shape
# cannot drift between what this build writes and what the historical readers
# accept. The two identity documents are built per store and stay None here.
DEFAULTS: dict[str, dict[str, Any] | None] = {
    "workspace.json": None,
    "backlog.json": copy.deepcopy(BACKLOG_DEFAULT),
    "store-meta.json": None,
    **{name: copy.deepcopy(value) for name, value in AUXILIARY_DEFAULTS.items()},
    "activity.json": copy.deepcopy(ACTIVITY_DEFAULT),
    "reports.json": copy.deepcopy(REPORTS_DEFAULT),
    KNOWLEDGE_DOCUMENT_NAME: copy.deepcopy(KNOWLEDGE_DEFAULT),
}

# This build writes exactly the v6 roster. The check is here so a roster edit
# cannot silently teach the historical readers a document their version never
# had.
if frozenset(DEFAULTS) != store_rosters.V6_DOCUMENT_NAMES:
    raise RuntimeError("store defaults no longer match the frozen v6 roster")

JOURNAL_NAME = ".workstack-journal.json"
LOCK_NAME = ".workstack.lock"
SERVER_INFO_NAME = ".workstack-server.json"
CAPTURE_TOKEN_NAME = ".workstack-capture-token"
STORE_MANIFEST_NAME = ".workstack-store-manifest.json"
SYNC_ADOPTION_RECEIPT_NAME = ".workstack-sync-adoption-receipt.json"
SYNC_REBIND_RECEIPT_NAME = ".workstack-sync-rebind-receipt.json"
STORE_MANIFEST_VERSION = 1
# Rollback archives written before an upgrade commits. The directory is not a
# roster member and never becomes one, so an authority never treats its own
# backup as authoritative data.
MIGRATION_BACKUP_DIR = ".workstack-migration-backups"


def _utc_stamp() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _serialized_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _default_for(name: str) -> dict[str, Any]:
    value = DEFAULTS[name]
    if value is not None:
        return copy.deepcopy(value)
    if name == "workspace.json":
        return _workspace_default()
    if name == "store-meta.json":
        return _store_meta_default()
    raise ValueError("unknown dynamic default: {}".format(name))
