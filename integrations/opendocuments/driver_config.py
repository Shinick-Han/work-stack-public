"""The operator's pinned, nonsecret configuration for one OpenDocuments driver.

This module reads exactly two files and confuses them for nothing else.

* :func:`load_operator_driver_config` reads the **nonsecret** driver document
  (``workstack.opendocuments-driver.v1``): which connection alias this driver
  serves, which Work Stack upstream workspace it belongs to, which
  OpenDocuments workspace and origin it talks to, the corpora it covers, the
  operator's own curated source catalog, and the *path* of the key file. It
  never opens that path. Both the launcher and the child read this document.
* :func:`load_driver_key` reads the **key** file, and only the child process
  ever calls it. The launcher and the owner know the path and nothing else.

**What the split does and does not buy.** The launching operator process does
not read the key, so it cannot log it, echo it, pass it in an environment or
put it on an argv. That is the whole claim. The bytes still exist in a file the
same OS user can read, in the child's memory, and in whatever shell the
operator used to write them. This is same-user trust made explicit, not a
sandbox, and not a production credential store: rotation, per-user secret
storage and an installed configuration UI are all still open. The operator owns
the file permissions on ``api_key_file`` and is told so in ``DRIVER.md``.

**No literal key is representable here.** The document has no ``api_key``
field; a document carrying one is refused rather than quietly ignored, so a key
pasted into the nonsecret file is an error the operator sees immediately.

**Nothing is invented.** The origin, the OpenDocuments workspace identifier,
the corpus-only profile and the timeout are admitted by the released
``od_client_config`` predicates -- the same ones the transport will run again --
against a placeholder key, so this module has no second, drifting spelling of
that grammar. The catalog is admitted by the released
:func:`retrieval_mapper_fields.admit_catalog` and additionally required to be
keyed by canonical lowercase UUIDs, because :func:`classify_source` looks a
document id up exactly while :func:`derive_chunk_ref` compares casefolded: an
uppercase key would silently drop every hit instead of failing loudly.

Refusals are :class:`DriverConfigError`, carrying one code from
:data:`DRIVER_CONFIG_CODES` and nothing else. No file content, no path, no key
byte, no decoder message and no exception text reaches a code, a ``str`` or a
``repr``, and neither the key nor the file bytes are ever returned to a caller
that did not ask for exactly one of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from integrations.opendocuments import od_client_config as _backend
from integrations.opendocuments.od_client import TrustedBackendConfig
from integrations.opendocuments.retrieval_mapper_fields import (
    MappingError,
    admit_catalog,
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
    "DRIVER_CONFIG_CODES",
    "DRIVER_CONFIG_SCHEMA",
    "DRIVER_CONFIG_FIELDS",
    "MAX_CONFIG_BYTES",
    "MAX_DRIVER_TIMEOUT_SECONDS",
    "MAX_KEY_BYTES",
    "MAX_PATH_CHARS",
    "DriverConfigError",
    "OperatorDriverConfig",
    "build_backend_config",
    "load_driver_key",
    "load_operator_driver_config",
]

#: The closed nonsecret document this module reads.
DRIVER_CONFIG_SCHEMA = "workstack.opendocuments-driver.v1"

#: Its exact top-level keys. Nothing more is accepted, and nothing is optional.
#: ``api_key`` is deliberately absent: a key may not live in this file.
DRIVER_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "connection_alias",
        "upstream_workspace_uid",
        "od_workspace_id",
        "origin",
        "profile",
        "timeout_seconds",
        "corpus_grants",
        "source_catalog",
        "api_key_file",
    }
)

#: The nonsecret document's byte bound, measured on real UTF-8 octets.
MAX_CONFIG_BYTES = 64 * 1024

#: The key file's byte bound, measured on the key itself. One terminal newline
#: -- ``LF`` or the ``CRLF`` a Windows editor writes -- is permitted and
#: stripped first; what remains must fit the released ``MAX_API_KEY_CHARS``
#: printable bound. A maximal key is therefore 256 octets in a 258-octet file.
MAX_KEY_BYTES = _backend.MAX_API_KEY_CHARS

#: A child's own budget stays below the owner's 75 s transport budget, so the
#: child answers -- or refuses -- before the parent stops waiting for it.
MAX_DRIVER_TIMEOUT_SECONDS = 60.0

#: A pinned absolute path is operator input, never a wire value.
MAX_PATH_CHARS = 4096

#: Every code this module can raise. The set is closed so a caller can render a
#: diagnostic without ever rendering a value.
DRIVER_CONFIG_CODES = frozenset(
    {
        "invalid_config_path",
        "config_unreadable",
        "invalid_config",
        "unsupported_config_schema",
        "invalid_connection_alias",
        "invalid_upstream_workspace_uid",
        "invalid_backend",
        "invalid_corpus_grants",
        "invalid_catalog",
        "invalid_api_key_file",
        "key_unreadable",
        "invalid_api_key",
    }
)

# Admission of the nonsecret half must not need the secret half. This literal
# stands in for the key while the released predicates check the origin, the
# OpenDocuments workspace id, the profile and the timeout; it is never stored
# on the returned snapshot and never sent anywhere.
_PLACEHOLDER_KEY = "placeholder-not-a-key-nonsecret-admission-only"

_CONTROL = frozenset(range(0, 32)) | {127}


class DriverConfigError(ValueError):
    """Closed configuration refusal: one code, never a value."""

    def __init__(self, code: str) -> None:
        if code not in DRIVER_CONFIG_CODES:
            code = "invalid_config"
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return "DriverConfigError({!r})".format(self.code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class OperatorDriverConfig:
    """One immutable snapshot of the operator's nonsecret driver document.

    ``api_key_file`` and ``config_path`` are excluded from ``repr`` on purpose:
    they are filesystem locations of operator material, and a traceback or a
    failing assertion is not a place to publish them. No key byte and no file
    content is a field of this class at all -- :func:`load_driver_key` returns
    the key to its one caller and this snapshot never holds it.
    """

    connection_alias: str
    upstream_workspace_uid: str
    od_workspace_id: str
    origin: str
    profile: str
    timeout_seconds: float
    corpus_grants: tuple[str, ...]
    source_catalog: Mapping[str, Mapping[str, str]] = field(repr=False)
    api_key_file: str = field(repr=False)
    config_path: str = field(repr=False)


def load_operator_driver_config(path: Any) -> OperatorDriverConfig:
    """Read and admit the nonsecret driver document at an absolute path.

    Reads exactly one file: the one named. It does not open ``api_key_file``,
    consult :data:`os.environ`, reach a network, touch a Store or create
    anything. Both the launcher (before it leases a store) and the child (before
    it opens a socket) call this, so the two can never disagree about what an
    alias means.
    """

    absolute = _admitted_path(path, "invalid_config_path")
    raw = _read_bounded(absolute, MAX_CONFIG_BYTES, "config_unreadable")
    try:
        document = decode_strict_json(raw, maximum_bytes=MAX_CONFIG_BYTES)
    except KnowledgeRequestError as error:
        # The decoder's own message can quote the offending input; only the
        # fact that the document was not admissible leaves this module.
        raise DriverConfigError("invalid_config") from error
    return _admit_document(document, absolute)


def load_driver_key(config: OperatorDriverConfig) -> str:
    """Read the operator's key file. **Only the child process calls this.**

    The bytes are read once, admitted against the released printable-key
    grammar, and returned to the caller that is about to build one
    :class:`TrustedBackendConfig`. One terminal newline is tolerated -- ``LF``
    or ``CRLF``, because a Windows editor writes two octets where a POSIX one
    writes one -- and nothing else is stripped, so a key with stray whitespace
    fails loudly instead of being silently repaired into a different key. No
    exception text, path or byte from the file reaches the refusal.
    """

    if not isinstance(config, OperatorDriverConfig):
        raise DriverConfigError("invalid_config")
    # Two octets over the printable bound, so a *maximal* key whose only excess
    # is the editor's terminal ``CRLF`` is read whole rather than refused for
    # being one octet too long to look at. Reading further does not widen what
    # is accepted: the printable bound is re-applied below, to the key alone,
    # after exactly one terminal newline has been removed.
    raw = _read_bounded(config.api_key_file, MAX_KEY_BYTES + 2, "key_unreadable")
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw or len(raw) > MAX_KEY_BYTES:
        # What is left after the one permitted newline is the key. A second
        # newline, a longer key, or an empty file lands here rather than
        # reaching the grammar with something this module widened for it.
        raise DriverConfigError("invalid_api_key")
    try:
        key = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise DriverConfigError("invalid_api_key") from error
    if _backend._API_KEY_RE.match(key) is None:
        raise DriverConfigError("invalid_api_key")
    return key


def build_backend_config(
    config: OperatorDriverConfig, api_key: str
) -> TrustedBackendConfig:
    """Bind the pinned nonsecret facts to the key the child just read.

    The key is a parameter and never a field of :class:`OperatorDriverConfig`,
    so nothing that logs or reprs a config can print it. The returned object is
    the released transport's own type, whose ``repr`` already redacts the key.
    """

    if not isinstance(config, OperatorDriverConfig):
        raise DriverConfigError("invalid_config")
    if not isinstance(api_key, str) or _backend._API_KEY_RE.match(api_key) is None:
        raise DriverConfigError("invalid_api_key")
    return TrustedBackendConfig(
        origin=config.origin,
        api_key=api_key,
        workspace_id=config.od_workspace_id,
        profile=config.profile,
        timeout_seconds=config.timeout_seconds,
    )


# -- admission ------------------------------------------------------------


def _admit_document(document: Any, config_path: str) -> OperatorDriverConfig:
    """Admit the whole closed document, field by field, before returning it."""

    values = _closed_object(document)
    if values["schema"] != DRIVER_CONFIG_SCHEMA:
        raise DriverConfigError("unsupported_config_schema")
    alias = values["connection_alias"]
    if not isinstance(alias, str) or not CONNECTION_ALIAS_RE.fullmatch(alias):
        raise DriverConfigError("invalid_connection_alias")
    try:
        upstream = canonical_uuid(
            values["upstream_workspace_uid"], "upstream_workspace_uid"
        )
    except KnowledgeRequestError as error:
        raise DriverConfigError("invalid_upstream_workspace_uid") from error
    backend = _admitted_backend(values)
    return OperatorDriverConfig(
        connection_alias=alias,
        upstream_workspace_uid=upstream,
        od_workspace_id=backend.workspace_id,
        origin=backend.origin,
        profile=backend.profile,
        timeout_seconds=backend.timeout_seconds,
        corpus_grants=_admitted_grants(values["corpus_grants"]),
        source_catalog=_admitted_catalog(values["source_catalog"]),
        api_key_file=_admitted_path(values["api_key_file"], "invalid_api_key_file"),
        config_path=config_path,
    )


def _closed_object(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise DriverConfigError("invalid_config")
    if set(document) != DRIVER_CONFIG_FIELDS:
        # Unknown *and* missing keys are the same refusal: the document is not
        # this schema. A stray ``api_key`` lands here rather than being ignored.
        raise DriverConfigError("invalid_config")
    return document


def _admitted_backend(values: Mapping[str, Any]) -> TrustedBackendConfig:
    """Run the released transport predicates over the nonsecret half.

    ``od_client_config._bind_config`` and ``_parse_origin`` are the transport's
    own module-private rules, reused here rather than restated: a config this
    function admits cannot then be refused as ``invalid_config`` by the very
    POST it was written for. The placeholder key occupies the one field this
    document may not carry, and the bound object is discarded except for the
    four values copied onto the snapshot.
    """

    bound = _backend._bind_config(
        {
            "origin": values["origin"],
            "api_key": _PLACEHOLDER_KEY,
            "workspace_id": values["od_workspace_id"],
            "profile": values["profile"],
            "timeout_seconds": values["timeout_seconds"],
        }
    )
    if not isinstance(bound, TrustedBackendConfig):
        raise DriverConfigError("invalid_backend")
    if not isinstance(_backend._parse_origin(bound.origin), tuple):
        raise DriverConfigError("invalid_backend")
    if bound.timeout_seconds > MAX_DRIVER_TIMEOUT_SECONDS:
        # The child must finish inside the owner's own transport budget.
        raise DriverConfigError("invalid_backend")
    return bound


def _admitted_grants(value: Any) -> tuple[str, ...]:
    """The corpora this pinned OpenDocuments workspace actually covers.

    The released corpus-alias grammar, applied to a set an operator wrote. The
    child later requires the request's ``corpus_refs`` to equal this set
    exactly, so a duplicate or a miscased entry is refused here rather than
    quietly changing what "equal" means.
    """

    if not isinstance(value, list):
        raise DriverConfigError("invalid_corpus_grants")
    if not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise DriverConfigError("invalid_corpus_grants")
    grants: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_CORPUS_REF_CHARS:
            raise DriverConfigError("invalid_corpus_grants")
        if not CORPUS_REF_RE.fullmatch(entry) or entry in grants:
            raise DriverConfigError("invalid_corpus_grants")
        grants.append(entry)
    return tuple(grants)


def _admitted_catalog(value: Any) -> Mapping[str, Mapping[str, str]]:
    """The operator's curated map, admitted by the released mapper helper.

    ``admit_catalog`` owns the entry shape. The extra rule here is the key: the
    mapper looks a ``documentId`` up *exactly*, so a catalog keyed with
    uppercase or braced UUIDs would admit cleanly and then drop every hit as
    out-of-catalog. This pilot requires canonical lowercase UUID keys and says
    so, rather than casefolding the operator's map behind their back.

    Nothing here is derived from a chat body. A catalog is operator-authored;
    no search result extends it.
    """

    try:
        admit_catalog(value)
    except MappingError as error:
        raise DriverConfigError("invalid_catalog") from error
    entries: dict[str, Mapping[str, str]] = {}
    for key, entry in value.items():
        try:
            canonical_uuid(key, "document_id")
        except KnowledgeRequestError as error:
            raise DriverConfigError("invalid_catalog") from error
        entries[key] = MappingProxyType(dict(entry))
    return MappingProxyType(entries)


# -- files ----------------------------------------------------------------


def _admitted_path(value: Any, code: str) -> str:
    """An absolute, control-free, bounded operator-pinned path.

    Absolute is required for the same reason the transport requires an absolute
    ``argv[0]``: a relative path is resolved against whatever directory the
    process happens to be in, which is not a pin. Nothing is created, resolved
    through a symlink or globbed here.
    """

    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise DriverConfigError(code)
    if any(ord(character) in _CONTROL for character in value):
        raise DriverConfigError(code)
    if not os.path.isabs(value):
        raise DriverConfigError(code)
    return value


def _read_bounded(path: str, maximum_bytes: int, code: str) -> bytes:
    """Read at most ``maximum_bytes`` octets, refusing a longer file.

    One extra octet is requested so an over-long file is detected rather than
    silently truncated into a different document or a different key.
    """

    try:
        with open(path, "rb") as handle:
            raw = handle.read(maximum_bytes + 1)
    except OSError as error:
        # The operating system's message names the path and sometimes the
        # reason; neither is this module's to publish.
        raise DriverConfigError(code) from error
    if len(raw) > maximum_bytes or not raw:
        raise DriverConfigError(code)
    return raw
