"""The operator's pinned, nonsecret configuration for one source verifier.

This module reads exactly one file: the ``workstack.opendocuments-verifier.v1``
document named by an absolute path. It never opens a key, a network, a Store or
the mapped source itself. ``allowed_root`` values are operator paths; they are
admitted here so :class:`SourceMapping` can be constructed, and they never
appear on a refusal, a ``str``, a ``repr`` or a public observation.

A mapping is one of two exact shapes -- a NAS ``allowed_root`` /
``relative_location`` pair, or a Notion ``page_url`` -- and never a blend of
the two. Admitting a Notion mapping still reads nothing: no token, no network
and no page. It only makes the document *nameable* by the observation path.

Refusals are :class:`VerifierConfigError`, carrying one code from
:data:`VERIFIER_CONFIG_CODES` and nothing else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from integrations.opendocuments.source_access import (
    SourceMapping,
    SourceMappingError,
    SourceMappingRegistry,
)
from workstack.knowledge_ledger_document import CONNECTION_ALIAS_RE
from workstack.knowledge_request import (
    CORPUS_REF_RE,
    MAX_CORPUS_REF_CHARS,
    MAX_CORPUS_REFS,
    MIN_CORPUS_REFS,
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
)

__all__ = [
    "MAX_CONFIG_BYTES",
    "MAX_MAPPINGS",
    "MAX_PAGE_URL_CHARS",
    "MAX_PATH_CHARS",
    "VERIFIER_CONFIG_CODES",
    "VERIFIER_CONFIG_FIELDS",
    "VERIFIER_CONFIG_SCHEMA",
    "VERIFIER_MAPPING_FIELDS",
    "VERIFIER_NAS_MAPPING_FIELDS",
    "VERIFIER_NOTION_MAPPING_FIELDS",
    "OperatorVerifierConfig",
    "VerifierConfigError",
    "load_operator_verifier_config",
]

VERIFIER_CONFIG_SCHEMA = "workstack.opendocuments-verifier.v1"

VERIFIER_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "connection_alias",
        "upstream_workspace_uid",
        "corpus_grants",
        "mappings",
    }
)

# A mapping is one backend or the other, never a blend. The two field sets are
# compared for *exact* equality, so a document carrying both ``allowed_root``
# and ``page_url`` -- or either one plus an unknown key -- is refused rather
# than being silently read as whichever half matched first.
VERIFIER_NAS_MAPPING_FIELDS = frozenset(
    {
        "document_ref",
        "corpus",
        "allowed_root",
        "relative_location",
        "revoked",
    }
)

VERIFIER_NOTION_MAPPING_FIELDS = frozenset(
    {
        "document_ref",
        "corpus",
        "page_url",
        "revoked",
    }
)

#: The original NAS spelling, kept so an existing importer is unchanged.
VERIFIER_MAPPING_FIELDS = VERIFIER_NAS_MAPPING_FIELDS

MAX_CONFIG_BYTES = 64 * 1024
MAX_MAPPINGS = 200
MAX_PATH_CHARS = 4096
MAX_PAGE_URL_CHARS = 1024

VERIFIER_CONFIG_CODES = frozenset(
    {
        "invalid_config_path",
        "config_unreadable",
        "invalid_config",
        "unsupported_config_schema",
        "invalid_connection_alias",
        "invalid_upstream_workspace_uid",
        "invalid_corpus_grants",
        "invalid_mappings",
    }
)

_CONTROL = frozenset(range(0, 32)) | {127}


class VerifierConfigError(ValueError):
    """Closed configuration refusal: one code, never a value."""

    def __init__(self, code: str) -> None:
        if code not in VERIFIER_CONFIG_CODES:
            code = "invalid_config"
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return "VerifierConfigError({!r})".format(self.code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class OperatorVerifierConfig:
    """One immutable snapshot of the operator's verifier document.

    ``config_path`` and the mapping registry are excluded from ``repr``: they
    name filesystem locations of operator material. No file byte is a field.
    """

    connection_alias: str
    upstream_workspace_uid: str
    corpus_grants: tuple[str, ...]
    registry: SourceMappingRegistry = field(repr=False)
    config_path: str = field(repr=False)


def load_operator_verifier_config(path: Any) -> OperatorVerifierConfig:
    """Read and admit the verifier document at an absolute path.

    Reads exactly one file: the one named. It does not consult
    :data:`os.environ`, reach a network, touch a Store or open a mapped source.
    """

    absolute = _admitted_path(path, "invalid_config_path")
    raw = _read_bounded(absolute, MAX_CONFIG_BYTES, "config_unreadable")
    try:
        document = decode_strict_json(raw, maximum_bytes=MAX_CONFIG_BYTES)
    except KnowledgeRequestError as error:
        raise VerifierConfigError("invalid_config") from error
    return _admit_document(document, absolute)


def _admit_document(document: Any, config_path: str) -> OperatorVerifierConfig:
    values = _closed_object(document, VERIFIER_CONFIG_FIELDS, "invalid_config")
    if values["schema"] != VERIFIER_CONFIG_SCHEMA:
        raise VerifierConfigError("unsupported_config_schema")
    alias = values["connection_alias"]
    if not isinstance(alias, str) or not CONNECTION_ALIAS_RE.fullmatch(alias):
        raise VerifierConfigError("invalid_connection_alias")
    try:
        upstream = canonical_uuid(
            values["upstream_workspace_uid"], "upstream_workspace_uid"
        )
    except KnowledgeRequestError as error:
        raise VerifierConfigError("invalid_upstream_workspace_uid") from error
    grants = _admitted_grants(values["corpus_grants"])
    return OperatorVerifierConfig(
        connection_alias=alias,
        upstream_workspace_uid=upstream,
        corpus_grants=grants,
        registry=_admitted_registry(values["mappings"], grants),
        config_path=config_path,
    )


def _closed_object(
    document: Any, fields: frozenset[str], code: str
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise VerifierConfigError(code)
    return document


def _admitted_grants(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise VerifierConfigError("invalid_corpus_grants")
    if not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise VerifierConfigError("invalid_corpus_grants")
    grants: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_CORPUS_REF_CHARS:
            raise VerifierConfigError("invalid_corpus_grants")
        if not CORPUS_REF_RE.fullmatch(entry) or entry in grants:
            raise VerifierConfigError("invalid_corpus_grants")
        grants.append(entry)
    return tuple(grants)


def _admitted_registry(
    value: Any, grants: tuple[str, ...]
) -> SourceMappingRegistry:
    if not isinstance(value, list) or len(value) > MAX_MAPPINGS:
        raise VerifierConfigError("invalid_mappings")
    seen: set[str] = set()
    mappings: list[SourceMapping] = []
    allowed = frozenset(grants)
    for entry in value:
        mapping = _admitted_mapping(entry, allowed)
        if mapping.document_id in seen:
            raise VerifierConfigError("invalid_mappings")
        seen.add(mapping.document_id)
        mappings.append(mapping)
    try:
        return SourceMappingRegistry(mappings)
    except SourceMappingError as error:
        raise VerifierConfigError("invalid_mappings") from error


def _admitted_mapping(value: Any, grants: frozenset[str]) -> SourceMapping:
    """One mapping, admitted as exactly one backend's exact field set."""

    if not isinstance(value, dict):
        raise VerifierConfigError("invalid_mappings")
    fields = set(value)
    if fields == VERIFIER_NAS_MAPPING_FIELDS:
        return _admitted_nas_mapping(value, grants)
    if fields == VERIFIER_NOTION_MAPPING_FIELDS:
        return _admitted_notion_mapping(value, grants)
    raise VerifierConfigError("invalid_mappings")


def _admitted_shared(entry: Mapping[str, Any], grants: frozenset[str]) -> tuple[str, str, bool]:
    """The three fields both backends spell the same way."""

    document_ref = entry["document_ref"]
    corpus = entry["corpus"]
    revoked = entry["revoked"]
    if not isinstance(document_ref, str) or not document_ref:
        raise VerifierConfigError("invalid_mappings")
    if not isinstance(corpus, str) or corpus not in grants:
        raise VerifierConfigError("invalid_mappings")
    if type(revoked) is not bool:
        raise VerifierConfigError("invalid_mappings")
    return document_ref, corpus, revoked


def _admitted_nas_mapping(
    entry: Mapping[str, Any], grants: frozenset[str]
) -> SourceMapping:
    document_ref, corpus, revoked = _admitted_shared(entry, grants)
    relative = entry["relative_location"]
    if not isinstance(relative, str) or not relative:
        raise VerifierConfigError("invalid_mappings")
    root = _admitted_path(entry["allowed_root"], "invalid_mappings")
    try:
        return SourceMapping(
            document_id=document_ref,
            corpus=corpus,
            backend="nas",
            allowed_root=Path(root),
            relative_location=relative,
            revoked=revoked,
        )
    except SourceMappingError as error:
        raise VerifierConfigError("invalid_mappings") from error


def _admitted_notion_mapping(
    entry: Mapping[str, Any], grants: frozenset[str]
) -> SourceMapping:
    """A pinned Notion page URL, admitted by the *released* allow-list.

    The URL is not re-parsed here and no second host rule is written: passing it
    to :class:`SourceMapping` is what applies ``unsafe_notion_url_code``, so
    there is exactly one spelling of "which Notion URLs exist" in this process.
    Whether a page id can be extracted from it is an observation-time question,
    not a configuration one -- an operator whose URL names no page gets
    ``refused``/``source_refused`` for that document, not a dead verifier file.
    """

    document_ref, corpus, revoked = _admitted_shared(entry, grants)
    page_url = entry["page_url"]
    if not isinstance(page_url, str) or not page_url:
        raise VerifierConfigError("invalid_mappings")
    if len(page_url) > MAX_PAGE_URL_CHARS:
        raise VerifierConfigError("invalid_mappings")
    try:
        return SourceMapping(
            document_id=document_ref,
            corpus=corpus,
            backend="notion",
            page_url=page_url,
            revoked=revoked,
        )
    except SourceMappingError as error:
        raise VerifierConfigError("invalid_mappings") from error


def _admitted_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise VerifierConfigError(code)
    if any(ord(character) in _CONTROL for character in value):
        raise VerifierConfigError(code)
    if not os.path.isabs(value):
        raise VerifierConfigError(code)
    return value


def _read_bounded(path: str, maximum_bytes: int, code: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(maximum_bytes + 1)
    except OSError as error:
        raise VerifierConfigError(code) from error
    if len(raw) > maximum_bytes or not raw:
        raise VerifierConfigError(code)
    return raw
