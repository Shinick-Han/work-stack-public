"""Frozen document rosters for every released collection-store version.

``workstack.store.DEFAULTS`` answers one question: which documents does *this*
build write, and with which default payload. Several callers ask a different
question — which documents did a *v1*, *v2* or *v3* store contain — and until
now they read that same constant. Sharing works only while the roster never
changes. The accepted report storage contract
(``docs/REPORT-DOCUMENT-STORAGE-CONTRACT.md``) adds a tenth document at schema
5, so the two questions must be answered by two different constants *before*
anything is added: a historical reader that follows ``DEFAULTS`` into a wider
roster would refuse a healthy older store, or claim a document that store
never had.

Every roster below is a historical fact, written out by name and never edited
again. Nothing here is derived from ``DEFAULTS``, and this module imports
nothing from the product, so it can sit underneath every other layer without
a cycle. A future document belongs in a new constant, never inside one of
these.

``V5_DOCUMENT_NAMES`` and ``V6_DOCUMENT_NAMES`` record the two rosters that
followed. Neither is activated by this module or by anything that imports it:
which roster this build writes is ``workstack.store_layout``'s single
statement, and every set here stays readable by a historical caller that has
to judge an older directory as the version it really is.

The one rule applied to a document's *contents* also lives here:
``auxiliary_store_defect`` compares a document against the default payload the
roster carries for it. That check is roster knowledge rather than storage
machinery, it needs no product import, and keeping it beside the names means a
future roster entry arrives with its shape rule in the same file. The caller
still owns the exception type, so the released messages are unchanged.
"""

from __future__ import annotations

from typing import Any, Final, Mapping


REPORTS_DOCUMENT_NAME: Final[str] = "reports.json"

# v1 predates the metadata document; its migration reads exactly these eight.
V1_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
    }
)

V2_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
    }
)

# v3 changed document *payloads*, not the roster, so its file set equals v2's.
# It is spelled out rather than aliased because the two are separate facts and
# a later version may change one without the other.
V3_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
    }
)

KNOWLEDGE_DOCUMENT_NAME: Final[str] = "knowledge.json"

# Schema 4 belongs to ``workstack.ssot``, so the next collection-layout version
# after 3 is 5 and it is the v3 roster plus one document.
V5_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
        "reports.json",
    }
)

# Schema 6 is the v5 roster plus the owner-held knowledge ledger. It is the
# roster this build writes, and like every set above it is a fact written out
# by name: a later document belongs in a new constant, never inside this one.
V6_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
        "reports.json",
        "knowledge.json",
    }
)

# Membership is what the sets state. These tuples additionally fix the order
# the historical callers already had, because some of them build a mapping
# that is hashed or archived, and an order change there would be a silent
# change to a recorded digest.
V1_DOCUMENT_ORDER: Final[tuple[str, ...]] = (
    "workspace.json",
    "backlog.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
)

V2_DOCUMENT_ORDER: Final[tuple[str, ...]] = (
    "workspace.json",
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
)

V3_DOCUMENT_ORDER: Final[tuple[str, ...]] = (
    "workspace.json",
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
)

V5_DOCUMENT_ORDER: Final[tuple[str, ...]] = (
    "workspace.json",
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
    "reports.json",
)

V6_DOCUMENT_ORDER: Final[tuple[str, ...]] = (
    "workspace.json",
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
    "reports.json",
    "knowledge.json",
)

# What "this directory holds a collection store" is decided by. workspace.json
# is excluded because a v4 authority carries one too, so it distinguishes
# nothing; every other v3 name is unique to the collection layout.
V3_LEGACY_MARKER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
    }
)

# The order callers reached through ``sorted(...)``: archive members, source
# digests and refresh notices are all published in this sequence.
V3_SORTED_DOCUMENT_NAMES: Final[tuple[str, ...]] = (
    "activity.json",
    "backlog.json",
    "captures.json",
    "notes.json",
    "okr.json",
    "replies.json",
    "store-meta.json",
    "worklog.json",
    "workspace.json",
)


def _require_roster(order: tuple[str, ...], names: frozenset[str], label: str) -> None:
    """Fail loudly at import if an ordered roster and its set disagree.

    The tuples above repeat the sets by hand so that both order and membership
    are literal. This is the tripwire that keeps the repetition honest.
    """

    if len(order) != len(names) or frozenset(order) != names:
        raise RuntimeError("{} roster order and membership disagree".format(label))


_require_roster(V1_DOCUMENT_ORDER, V1_DOCUMENT_NAMES, "v1")
_require_roster(V2_DOCUMENT_ORDER, V2_DOCUMENT_NAMES, "v2")
_require_roster(V3_DOCUMENT_ORDER, V3_DOCUMENT_NAMES, "v3")
_require_roster(V3_SORTED_DOCUMENT_NAMES, V3_DOCUMENT_NAMES, "v3 sorted")
_require_roster(V5_DOCUMENT_ORDER, V5_DOCUMENT_NAMES, "v5")
_require_roster(V6_DOCUMENT_ORDER, V6_DOCUMENT_NAMES, "v6")

if V3_SORTED_DOCUMENT_NAMES != tuple(sorted(V3_DOCUMENT_NAMES)):
    raise RuntimeError("v3 sorted roster is not in sorted order")

if V1_DOCUMENT_NAMES | {"store-meta.json"} != V2_DOCUMENT_NAMES:
    raise RuntimeError("v2 roster is not v1 plus the metadata document")

if V5_DOCUMENT_NAMES != V3_DOCUMENT_NAMES | {REPORTS_DOCUMENT_NAME}:
    raise RuntimeError("v5 roster is not v3 plus the reports document")

if V6_DOCUMENT_NAMES != V5_DOCUMENT_NAMES | {KNOWLEDGE_DOCUMENT_NAME}:
    raise RuntimeError("v6 roster is not v5 plus the knowledge document")

if V3_LEGACY_MARKER_NAMES != V3_DOCUMENT_NAMES - {"workspace.json"}:
    raise RuntimeError("v3 markers are not the v3 roster without the workspace")


def auxiliary_store_defect(
    name: str, value: Mapping[str, Any], expected: Mapping[str, Any]
) -> str | None:
    """Say how an auxiliary document departs from its default payload shape.

    Returns ``None`` when the document matches. The caller raises, so this
    stays free of any product import while producing exactly the messages the
    released store already produced.
    """

    if set(value) != set(expected) or value.get("version") != expected["version"]:
        return "{} schema is invalid".format(name)
    for key, default_value in expected.items():
        if key == "version":
            continue
        if isinstance(default_value, list) and not isinstance(value.get(key), list):
            return "{}.{} must be an array".format(name, key)
        if isinstance(default_value, dict) and not isinstance(value.get(key), dict):
            return "{}.{} must be an object".format(name, key)
    return None
