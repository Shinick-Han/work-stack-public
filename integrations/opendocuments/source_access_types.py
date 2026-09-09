"""OpenDocuments source vocabulary, owner mappings and request shaping.

The closed status/code vocabulary, the owner-authored mapping and its registry,
the public result records, the injected-verifier seam, and the one place a
caller's arguments become either a usable mapping or the refusal that replaced
it. Nothing here touches a filesystem, a network or an opener: this module
fixes what an answer is *allowed to say*, while
:mod:`integrations.opendocuments.source_access_nas` and
:mod:`integrations.opendocuments.source_access` decide what it says.

This is an internal module. Every name here that a caller may use is
re-exported from :mod:`integrations.opendocuments.source_access`, which stays
the only supported import site; the underscored names stay exactly as private
as they were when they lived in that module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from integrations.opendocuments.nas_paths import (
    ALWAYS_REFUSED_EXTENSIONS,
    DEFAULT_PERMITTED_EXTENSIONS,
    LOCATION_REFUSAL_CODES,
    MappedLocationError,
    canonical_root,
)


BACKENDS = ("nas", "notion")

# The closed vocabulary of an answer about one document's source.
VERIFICATION_STATUSES = (
    "current",  # actual source bytes hashed and equal to the expected version
    "stale",  # actual source bytes hashed and different
    "missing",  # the root answered, and the file is not in it
    "unavailable",  # the root itself did not answer
    "denied",  # the OS refused the read
    "refused",  # policy said no before any read
    "revoked",  # the owner withdrew the mapping
    "unverifiable",  # the question could not be answered honestly
)
OPEN_STATUSES = VERIFICATION_STATUSES + ("opened", "failed")

VERIFICATION_CODES = (
    "hash_matched",
    "hash_differs",
    "file_absent",
    "root_unavailable",
    "root_access_denied",
    "file_access_denied",
    "indeterminate_access",
    "changed_during_read",
    "file_too_large",
    "no_expected_version",
    "mapping_revoked",
    "unknown_document",
    "invalid_document_id",
    "invalid_expected_version",
    "invalid_indexed_digest",
    "corpus_mismatch",
    "backend_mismatch",
    "no_origin_verifier",
    "verifier_document_mismatch",
    "verifier_no_version",
    "verifier_failed",
    "unsafe_source_url",
    "origin_verified",
) + LOCATION_REFUSAL_CODES
OPEN_CODES = VERIFICATION_CODES + (
    "opener_invoked",
    "opener_failed",
    "invalid_opener",
    "not_current",
)

# Opaque by construction: no separator, no drive colon, no scheme. A caller that
# reaches for a path finds the shape itself rejected.
# ``\Z``, never ``$``: in Python ``$`` also matches immediately before a final
# newline, so a digest or an id carrying one trailing newline would satisfy a
# ``$``-anchored pattern and reach a public projection intact.
_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CORPUS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_OPAQUE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")

# Only the two hosts Notion actually serves pages from. Matching is on the exact
# host or an exact ``.notion.site`` suffix, never a substring: "notion.so.evil"
# has neither shape.
NOTION_ALLOWED_HOSTS = frozenset({"notion.so", "www.notion.so"})
NOTION_ALLOWED_HOST_SUFFIX = ".notion.site"
_NOTION_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,512}\Z")


class SourceMappingError(ValueError):
    """The owner's configuration is wrong. Loud, because a human wrote it."""

    def __init__(self, code: str, document_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.document_id = document_id


@dataclass(frozen=True)
class SourceMapping:
    """One authorised document, written by the owner and keyed by opaque id.

    This is the *only* thing that turns an id into bytes. ``corpus`` is pinned
    here rather than passed by a caller, so a caller asking within corpus A can
    never reach a document the owner filed under corpus B.
    """

    document_id: str
    corpus: str
    backend: str
    allowed_root: Path | None = None
    relative_location: str | None = None
    permitted_extensions: frozenset[str] = DEFAULT_PERMITTED_EXTENSIONS
    page_url: str | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not _DOCUMENT_ID_RE.match(
            self.document_id
        ):
            raise SourceMappingError("invalid_document_id")
        if not isinstance(self.corpus, str) or not _CORPUS_RE.match(self.corpus):
            raise SourceMappingError("invalid_corpus", self.document_id)
        if self.backend not in BACKENDS:
            raise SourceMappingError("invalid_backend", self.document_id)
        if self.backend == "nas":
            self._check_nas()
        else:
            self._check_notion()

    def _check_nas(self) -> None:
        if self.page_url is not None:
            raise SourceMappingError("mapping_field_conflict", self.document_id)
        if self.allowed_root is None or self.relative_location is None:
            raise SourceMappingError("mapping_incomplete", self.document_id)
        if not isinstance(self.permitted_extensions, frozenset):
            raise SourceMappingError("invalid_permitted_extensions", self.document_id)
        if not self.permitted_extensions:
            raise SourceMappingError("invalid_permitted_extensions", self.document_id)
        # An owner may narrow the allow-list; widening it back into a launcher
        # format is refused at configuration time, not at read time.
        if self.permitted_extensions & ALWAYS_REFUSED_EXTENSIONS:
            raise SourceMappingError("executable_extension", self.document_id)
        try:
            root = canonical_root(self.allowed_root)
        except MappedLocationError as error:
            raise SourceMappingError(error.code, self.document_id) from None
        object.__setattr__(self, "allowed_root", root)

    def _check_notion(self) -> None:
        if self.allowed_root is not None or self.relative_location is not None:
            raise SourceMappingError("mapping_field_conflict", self.document_id)
        if not isinstance(self.page_url, str):
            raise SourceMappingError("mapping_incomplete", self.document_id)
        code = unsafe_notion_url_code(self.page_url)
        if code is not None:
            raise SourceMappingError(code, self.document_id)


def _unsafe_url_text_code(url: str) -> str | None:
    """Refuse a URL on its text alone: emptiness, length, control characters.

    Split out of :func:`unsafe_notion_url_code` so the rules that need no parse
    stay separable from the rules that need one. Same code, same order, same
    refusals.
    """

    if not url or len(url) > 1024:
        return "unsafe_source_url"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        return "unsafe_source_url"
    return None


def _unsafe_notion_host_code(host: str | None) -> str | None:
    """The host allow-list, stated once and matched exactly.

    ``hostname`` arrives already case-folded, which is correct: a host name is
    case-insensitive, so "WWW.NOTION.SO" is the allow-listed host, not a
    near-miss of it. Everything else about the URL is compared literally.
    """

    if not host or not host.isascii():
        return "unsafe_source_url"
    if any(label.startswith("xn--") for label in host.split(".")):
        # A punycode label can render as an allow-listed host without being one.
        return "unsafe_source_url"
    if host not in NOTION_ALLOWED_HOSTS and not host.endswith(
        NOTION_ALLOWED_HOST_SUFFIX
    ):
        return "unsafe_source_url"
    if host.endswith(NOTION_ALLOWED_HOST_SUFFIX) and host == NOTION_ALLOWED_HOST_SUFFIX:
        return "unsafe_source_url"
    return None


def unsafe_notion_url_code(url: object) -> str | None:
    """``None`` when a pinned Notion URL is safe to hand an opener, else a code.

    Safe means: HTTPS, an allow-listed host spelled in plain lowercase ASCII, no
    userinfo, no port, no query and no fragment. Query and fragment are refused
    rather than stripped because a pinned URL that needed them was not pinned;
    silently editing an owner's URL would make the mapping mean something the
    owner did not write.
    """

    if not isinstance(url, str):
        return "unsafe_source_url"
    text_code = _unsafe_url_text_code(url)
    if text_code is not None:
        return text_code
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unsafe_source_url"
    if parts.scheme != "https":
        return "unsafe_source_url"
    if parts.query or parts.fragment:
        return "unsafe_source_url"
    if "@" in parts.netloc:  # userinfo, including the "user:pass@" credential form
        return "unsafe_source_url"
    try:
        if parts.port is not None:
            return "unsafe_source_url"
    except ValueError:
        return "unsafe_source_url"
    host_code = _unsafe_notion_host_code(parts.hostname)
    if host_code is not None:
        return host_code
    if not _NOTION_PATH_RE.match(parts.path or "/"):
        return "unsafe_source_url"
    return None


class SourceMappingRegistry:
    """The owner's mapping table, in process and out of the Work Stack SSOT.

    Deliberately not a dict of anything the caller can extend: it is built once
    from configuration and only answers lookups. There is no ``add`` after
    construction, so no request path can widen the authorised set.
    """

    def __init__(self, mappings: object) -> None:
        if isinstance(mappings, Mapping):
            entries = tuple(mappings.values())
        else:
            entries = tuple(mappings)  # type: ignore[arg-type]
        table: dict[str, SourceMapping] = {}
        for entry in entries:
            if not isinstance(entry, SourceMapping):
                raise SourceMappingError("invalid_mapping")
            if entry.document_id in table:
                raise SourceMappingError("duplicate_document_id", entry.document_id)
            table[entry.document_id] = entry
        self._table = table

    def __len__(self) -> int:
        return len(self._table)

    def document_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def lookup(self, document_id: object) -> SourceMapping | None:
        if not isinstance(document_id, str):
            return None
        return self._table.get(document_id)


@dataclass(frozen=True)
class SourceStatus:
    """What is known about one document's source, and nothing more.

    ``source_version`` is a fact this module read; ``expected_source_version``
    and ``indexed_digest`` are what the caller brought. They are kept apart in
    the schema for the same reason ``workstack.capture_retrieval`` keeps
    ``reported_`` apart from verified provenance: collapsing them is how an
    unproven claim becomes an apparent proof.
    """

    document_id: str
    corpus: str | None
    backend: str | None
    status: str
    code: str
    source_version: str | None = None
    expected_source_version: str | None = None
    indexed_digest: str | None = None

    @property
    def open_allowed(self) -> bool:
        return self.status == "current"

    def as_public_dict(self) -> dict[str, object]:
        """The diagnostic projection: safe to log, display or return to a caller.

        No absolute path, no configured root, no directory name and no file
        bytes appear here, which is why it is also the shape a public error is
        built from.
        """

        return {
            "document_id": self.document_id,
            "corpus": self.corpus,
            "backend": self.backend,
            "status": self.status,
            "code": self.code,
            "source_version": self.source_version,
            "expected_source_version": self.expected_source_version,
            "indexed_digest": self.indexed_digest,
            "open_allowed": self.open_allowed,
        }


@dataclass(frozen=True)
class OpenTarget:
    """The private, validated thing an injected opener is allowed to receive.

    ``path`` is set for a NAS mapping and ``url`` for a Notion one, never both.
    This object never reaches a public projection: it is constructed inside
    :func:`authorize_open` immediately after revalidation and passed straight to
    the callback.
    """

    document_id: str
    corpus: str
    backend: str
    source_version: str
    path: Path | None = None
    url: str | None = None


@dataclass(frozen=True)
class OpenDecision:
    """The public answer to "may this be opened, and was it".

    ``opened`` is only ever true when the opener was actually invoked after a
    successful revalidation performed within this call.
    """

    document_id: str
    corpus: str | None
    backend: str | None
    opened: bool
    status: str
    code: str
    source_version: str | None = None
    expected_source_version: str | None = None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "corpus": self.corpus,
            "backend": self.backend,
            "opened": self.opened,
            "status": self.status,
            "code": self.code,
            "source_version": self.source_version,
            "expected_source_version": self.expected_source_version,
        }


@dataclass(frozen=True)
class OriginRequest:
    """What the host's trusted origin verifier is asked about.

    It carries the opaque id and the *pinned* URL from the mapping, so a
    verifier cannot be steered at some other page by anything a caller passed.
    """

    document_id: str
    corpus: str
    url: str


@dataclass(frozen=True)
class OriginAttestation:
    """What a trusted verifier attests, in the caller's own words.

    ``source_version`` is ``None`` when the verifier confirmed identity but not
    currentness -- the same honest half-state ``VerifiedSource`` allows, and it
    still does not make a document current.
    """

    document_id: str
    source_version: str | None = None


class OriginVerifier(Protocol):
    """The unimplemented seam. No adapter for it ships in this packet."""

    def __call__(self, request: OriginRequest) -> OriginAttestation | None:
        ...


Opener = Callable[[OpenTarget], None]


@dataclass(frozen=True)
class _Resolved:
    """Internal: either a usable mapping or the refusal that replaced it."""

    mapping: SourceMapping | None = None
    failure: SourceStatus | None = None
    expected: str | None = None
    indexed_digest: str | None = None


def _refusal(
    document_id: object,
    status: str,
    code: str,
    *,
    mapping: SourceMapping | None = None,
    expected: str | None = None,
    indexed_digest: str | None = None,
) -> SourceStatus:
    safe_id = document_id if isinstance(document_id, str) else "<non-text>"
    if len(safe_id) > 128:
        safe_id = "<oversized>"
    if not _DOCUMENT_ID_RE.match(safe_id):
        # Never echo a path, URL or control text back to a caller or a log.
        safe_id = "<rejected>"
    return SourceStatus(
        document_id=safe_id,
        corpus=mapping.corpus if mapping else None,
        backend=mapping.backend if mapping else None,
        status=status,
        code=code,
        expected_source_version=expected,
        indexed_digest=indexed_digest,
    )


def _validate_claims(
    mapping: SourceMapping,
    expected_source_version: object,
    indexed_digest: object,
) -> tuple[str | None, str | None, str | None]:
    """Shape the caller's two claims into ``(expected, digest, refusal code)``.

    Both are things the caller brought, and neither is authority. This says only
    whether each has the shape of the thing it claims to be, in the same order
    and under the same closed codes as before.
    """

    expected: str | None = None
    if expected_source_version is not None:
        if not isinstance(expected_source_version, str):
            return None, None, "invalid_expected_version"
        pattern = (
            _SHA256_VERSION_RE if mapping.backend == "nas" else _OPAQUE_VERSION_RE
        )
        if not pattern.match(expected_source_version):
            return None, None, "invalid_expected_version"
        expected = expected_source_version
    digest: str | None = None
    if indexed_digest is not None:
        # An index digest is a *claim*, and a claim still has to have the shape
        # of the thing it claims to be. Anything that is not a canonical
        # ``sha256:<64 lowercase hex>`` -- a path, a URL, a bare hex string, an
        # uppercased digest, a non-string -- is refused here under the closed
        # ``invalid_indexed_digest`` code, and the offered text is deliberately
        # not carried into the refusal: a public projection must never become a
        # way to get an absolute path echoed back.
        if not isinstance(indexed_digest, str) or not _SHA256_VERSION_RE.match(
            indexed_digest
        ):
            return None, None, "invalid_indexed_digest"
        # Well-formed, and still only a claim: it is echoed beside the answer
        # and is never freshness authority.
        digest = indexed_digest
    return expected, digest, None


def _resolve_request(
    registry: SourceMappingRegistry,
    document_id: object,
    corpus: object,
    expected_source_version: object,
    indexed_digest: object,
    required_backend: str | None,
) -> _Resolved:
    """Scope, identity and argument shape, before anything touches a source."""

    if not isinstance(document_id, str) or not _DOCUMENT_ID_RE.match(document_id):
        # A path, UNC share, drive letter or URL lands here: refused as
        # malformed rather than looked up, so no lookup ever sees one.
        return _Resolved(
            failure=_refusal(document_id, "refused", "invalid_document_id")
        )
    mapping = registry.lookup(document_id)
    if mapping is None:
        return _Resolved(failure=_refusal(document_id, "refused", "unknown_document"))
    if corpus is not None and corpus != mapping.corpus:
        return _Resolved(
            failure=_refusal(
                document_id, "refused", "corpus_mismatch", indexed_digest=None
            )
        )
    if required_backend is not None and mapping.backend != required_backend:
        return _Resolved(
            failure=_refusal(
                document_id, "refused", "backend_mismatch", mapping=mapping
            )
        )
    if mapping.revoked:
        # A revoked mapping denies even when the bytes are still on the share:
        # the withdrawal is the authority, not the file's continued existence.
        return _Resolved(
            failure=_refusal(
                document_id, "revoked", "mapping_revoked", mapping=mapping
            )
        )

    expected, digest, invalid = _validate_claims(
        mapping, expected_source_version, indexed_digest
    )
    if invalid is not None:
        return _Resolved(
            failure=_refusal(document_id, "refused", invalid, mapping=mapping)
        )
    return _Resolved(mapping=mapping, expected=expected, indexed_digest=digest)
