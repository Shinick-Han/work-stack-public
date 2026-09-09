"""Planning the collection-store upgrade to schema 6.

Schema 6 is schema 5 plus one document: ``knowledge.json``, the owner-held
policy and request ledger. Nothing else about a v5 store changes, so this
module is deliberately small and deliberately separate from
:mod:`workstack.store_report_migration`.

Separate, because the v5 planner is a *historical fact*: it says what
upgrading a v1, v2 or v3 directory to schema 5 produced, and it is still the
only description of that step. Widening it to also add a knowledge ledger
would rewrite that fact and would leave no module able to answer "what did the
report migration do". So this module owns exactly the v5-to-v6 step and,
for a store older than 5, composes the released planner with it:

    v1 / v2 / v3  --store_report_migration.plan_upgrade-->  v5  --here-->  v6
    v5                                                          --here-->  v6

The evidence written for the knowledge document names the version actually
detected on disk and digests the documents actually found there — the same
`digest` the v5 planner was given — never the intermediate v5 mapping this
module derives on the way. A rollback restores the detected bytes, so the
evidence has to point at those.

Nothing here opens a path, reads a clock or writes a document. The caller
supplies the documents it already holds under the writer lease and commits the
returned mapping through one journalled ``save_many``, exactly as the v5
upgrade already did.
"""

from __future__ import annotations

import copy
from typing import Any, Final, Mapping

from . import store_report_migration, store_rosters
from .knowledge_ledger_document import KNOWLEDGE_DEFAULT, KNOWLEDGE_DOCUMENT_NAME
from .store_errors import StoreCorruptError

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "KNOWLEDGE_EVIDENCE_IDS",
    "SUPPORTED_SOURCE_VERSIONS",
    "plan_upgrade",
]

# The collection version this build writes. ``store_layout.STORE_SCHEMA_VERSION``
# is the same number; the test suite asserts the two stay equal rather than one
# importing the other, because a store module may not depend on the layout it
# sits underneath.
CURRENT_SCHEMA_VERSION: Final[int] = 6

# Which detected versions this planner can carry to 6. Schema 4 is
# ``workstack.ssot``'s and is not a collection store, so it is absent here for
# the same reason it is absent from every roster.
SUPPORTED_SOURCE_VERSIONS: Final[tuple[int, ...]] = (1, 2, 3, 5)

# The evidence record the knowledge document is introduced with, per detected
# origin. A fresh v6 store says so; every upgrade says it was migrated and
# carries the digest of the bytes that were detected.
KNOWLEDGE_EVIDENCE_IDS: Final[dict[str, str]] = {
    "fresh": "workstack.knowledge.v6",
    "migrated_v1": "workstack.knowledge.v5-to-v6",
    "migrated_v2": "workstack.knowledge.v5-to-v6",
    "migrated_v3": "workstack.knowledge.v5-to-v6",
    "migrated_v5": "workstack.knowledge.v5-to-v6",
}


def _plan_v5_to_v6(
    values: Mapping[str, dict[str, Any]], digest: str, origin: str
) -> dict[str, dict[str, Any]]:
    """The v5 roster carried forward untouched, plus an empty ledger.

    Every existing document is copied as it was read. The only edits are the
    metadata record's own version and the knowledge evidence beside it, so a
    store's tasks, activity, planning status and reports come through the
    upgrade byte-identical in meaning.
    """

    metadata = copy.deepcopy(values["store-meta.json"])
    migrations = metadata["migrations"]
    metadata["store_schema_version"] = CURRENT_SCHEMA_VERSION
    migrations["knowledge"] = {
        "id": KNOWLEDGE_EVIDENCE_IDS[origin],
        "origin": origin,
        "source_sha256": digest,
    }
    writes = {
        name: copy.deepcopy(values[name]) for name in store_rosters.V5_DOCUMENT_ORDER
    }
    writes["store-meta.json"] = metadata
    writes[KNOWLEDGE_DOCUMENT_NAME] = copy.deepcopy(KNOWLEDGE_DEFAULT)
    return writes


def plan_upgrade(
    detected: int,
    values: Mapping[str, dict[str, Any]],
    digest: str,
    *,
    now: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Every v6 document to write, and the operation id that names the upgrade.

    ``values`` are the documents detected on disk, already admitted as
    ``detected``. ``digest`` is their digest, and it is what every evidence
    record written here points at, whichever intermediate shape the documents
    pass through.

    The operation id is derived from the detected version and that digest, so a
    retry after a crash replays the same journal entry rather than opening a
    second one.
    """

    if detected not in SUPPORTED_SOURCE_VERSIONS:
        raise StoreCorruptError("store schema version is not supported")
    if detected == 5:
        writes = _plan_v5_to_v6(values, digest, "migrated_v5")
    else:
        intermediate, _operation = store_report_migration.plan_upgrade(
            detected, values, digest, now=now
        )
        writes = _plan_v5_to_v6(
            intermediate, digest, "migrated_v{}".format(detected)
        )
    if set(writes) != set(store_rosters.V6_DOCUMENT_NAMES):
        raise StoreCorruptError("upgrade did not produce the v6 roster")
    operation_id = "store-migrate-v{}-v6-{}".format(detected, digest[7:23])
    return writes, operation_id
