"""OpenDocuments source verification and a separate, revalidated safe open.

This is an *external adapter library*. It is not an HTTP endpoint, not a
connector, and it launches no document of its own: the only thing that can open
anything is a callback the host injects, and this module hands it a validated
``Path`` argument -- never a command string, never a shell fragment. It imports
nothing from ``workstack``; the Work Stack Capture contract is the reason this
module exists, not a dependency of it. ``workstack.capture_retrieval`` is the
*consumer* of the facts produced here, and the vocabulary is deliberately
parallel: what a document or an index says about itself is a claim, and only a
fact this module read at the source becomes a version.

The trust boundary, stated once
-------------------------------

A caller says **which document**, never **which file**. The only input that
selects bytes is an opaque ``document_id`` that must already exist in an
owner-authored :class:`SourceMapping`. A path, a UNC share, a drive letter or a
URL arriving as a ``document_id`` is refused as malformed -- not tried, not
normalised, not "cleaned up". Nothing from an index chunk, a ``sourcePath``
field or model output is ever promoted to authority; those live on the
``reported_`` side of the Capture split and this module does not read them.

The mapping registry is host configuration held in process. It is not Work
Stack SSOT, it is not exported, and it is not written anywhere by this module.
Correspondingly, the public result of every operation carries the opaque id, a
corpus, a closed status and code, and a version string -- and no absolute path,
no root, no directory listing and no file bytes. The validated path exists only
inside :class:`OpenTarget`, which is handed to the injected opener and to
nobody else.

What "current" means here
-------------------------

Exactly one thing: this module read the permitted file's actual bytes under a
bounded read, hashed them to ``sha256:<hex>``, and that hash equals the
``expected_source_version`` the caller supplied. Every other outcome has its own
name. An index digest never makes a document current, no matter what it matches;
a *canonically shaped* one is echoed back as ``indexed_digest`` and takes no
part in the decision, and anything else is refused as
``invalid_indexed_digest`` without the offered text appearing in the answer. A
question the filesystem could not answer stays ``unverifiable`` -- it never
degrades into ``missing``, and it never hardens into a specific finding such as
"a reparse point" or "the share is offline", because "I could not tell" and "it
is gone" lead a human to opposite actions.

The version an open is authorised under is the version of the file that was
resolved and hashed in that same call, and the opener is handed that very path:
:func:`authorize_open` resolves the mapped target once and never re-resolves it
after the comparison. What is still not promised is what happens *after* the
callback is entered -- see "What this module does not promise".

What this module does not promise
---------------------------------

It is not a sandbox and does not serialise the filesystem. Containment,
reparse-point and extension checks are made at the moment they are made; a
process running as the same OS user with write access inside the approved root
can still change a component afterwards. The open path in particular has an
irreducible gap between the final revalidation and the external application's
own read, which is documented rather than hidden -- see ``SOURCE-ACCESS.md``.

Notion here is a *seam*, not an integration. There is no API token, no client
and no network call in this packet: a pinned HTTPS URL is validated against a
host allow-list, and currentness comes only from a trusted verifier the host
injects. Without that verifier the answer is ``unverifiable`` and opening is
disabled -- never an invented verification.

Where the code lives
--------------------

One contract, three files, split only so each stays measurable. The vocabulary,
the owner's mapping and registry, the public result records and the request
shaping are in ``source_access_types``; reaching the mapped file, bounding the
read and hashing it are in ``source_access_nas``; the Notion seam, the three
public entry points and the open authorisation are here. Both are internal
modules: this module re-exports every supported name, so ``from
integrations.opendocuments.source_access import ...`` is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from integrations.opendocuments.nas_paths import (
    ALWAYS_REFUSED_EXTENSIONS,
    COMPONENT_ACCESS_DENIED,
    COMPONENT_INDETERMINATE,
    DEFAULT_PERMITTED_EXTENSIONS,
    LOCATION_REFUSAL_CODES,
    MappedLocationError,
    canonical_root,
    resolve_mapped_target,
)
from integrations.opendocuments.source_access_nas import (
    MAX_VERIFIED_BYTES,
    READ_CHUNK_BYTES,
    _verify_nas,
)
from integrations.opendocuments.source_access_types import (
    BACKENDS,
    NOTION_ALLOWED_HOST_SUFFIX,
    NOTION_ALLOWED_HOSTS,
    OPEN_CODES,
    OPEN_STATUSES,
    VERIFICATION_CODES,
    VERIFICATION_STATUSES,
    OpenDecision,
    Opener,
    OpenTarget,
    OriginAttestation,
    OriginRequest,
    OriginVerifier,
    SourceMapping,
    SourceMappingError,
    SourceMappingRegistry,
    SourceStatus,
    _OPAQUE_VERSION_RE,
    _refusal,
    _resolve_request,
    unsafe_notion_url_code,
)


def _verify_notion(
    mapping: SourceMapping,
    expected: str | None,
    indexed_digest: str | None,
    origin_verifier: OriginVerifier | None,
) -> SourceStatus:
    def result(status: str, code: str, version: str | None = None) -> SourceStatus:
        return SourceStatus(
            document_id=mapping.document_id,
            corpus=mapping.corpus,
            backend=mapping.backend,
            status=status,
            code=code,
            source_version=version,
            expected_source_version=expected,
            indexed_digest=indexed_digest,
        )

    assert mapping.page_url is not None
    unsafe = unsafe_notion_url_code(mapping.page_url)
    if unsafe is not None:  # pragma: no cover - construction already refuses
        return result("refused", unsafe)
    if origin_verifier is None:
        # No connector, no token, no network in this packet. Saying anything
        # else here would be inventing a verification.
        return result("unverifiable", "no_origin_verifier")
    if not callable(origin_verifier):
        return result("unverifiable", "no_origin_verifier")
    request = OriginRequest(
        document_id=mapping.document_id, corpus=mapping.corpus, url=mapping.page_url
    )
    try:
        attestation = origin_verifier(request)
    except Exception:
        # A verifier that raised attested nothing. Its message is not echoed.
        return result("unverifiable", "verifier_failed")
    if attestation is None:
        return result("unverifiable", "no_origin_verifier")
    if not isinstance(attestation, OriginAttestation):
        return result("unverifiable", "verifier_failed")
    if attestation.document_id != mapping.document_id:
        # An attestation about some other document is not about this one.
        return result("unverifiable", "verifier_document_mismatch")
    version = attestation.source_version
    if version is None:
        return result("unverifiable", "verifier_no_version")
    if not isinstance(version, str) or not _OPAQUE_VERSION_RE.match(version):
        return result("unverifiable", "verifier_failed")
    if expected is None:
        return result("unverifiable", "no_expected_version", version)
    if version == expected:
        return result("current", "origin_verified", version)
    return result("stale", "hash_differs", version)


def verify_source(
    document_id: object,
    *,
    registry: SourceMappingRegistry,
    expected_source_version: object = None,
    indexed_digest: object = None,
    corpus: object = None,
    origin_verifier: OriginVerifier | None = None,
) -> SourceStatus:
    """Say what is actually known about one authorised document's source.

    ``document_id`` is opaque and must already be mapped; ``corpus``, when given,
    must equal the corpus the owner pinned on that mapping. ``indexed_digest``
    must be canonical ``sha256:<64 lowercase hex>`` or absent -- anything else is
    ``refused``/``invalid_indexed_digest`` and is not echoed back -- and even a
    well-formed one is accepted only so it can be carried beside the answer as a
    claim. It never influences the status, because a digest of what an index
    holds is not a version of the source.
    """

    resolved = _resolve_request(
        registry,
        document_id,
        corpus,
        expected_source_version,
        indexed_digest,
        None,
    )
    if resolved.failure is not None:
        return resolved.failure
    mapping = resolved.mapping
    assert mapping is not None
    digest = resolved.indexed_digest
    if mapping.backend == "nas":
        return _verify_nas(mapping, resolved.expected, digest)[0]
    return _verify_notion(mapping, resolved.expected, digest, origin_verifier)


def resolve_notion_source(
    document_id: object,
    *,
    registry: SourceMappingRegistry,
    origin_verifier: OriginVerifier | None = None,
    expected_source_version: object = None,
    corpus: object = None,
) -> SourceStatus:
    """The Notion half on its own, for a caller that wants the backend pinned.

    Identical semantics to :func:`verify_source`, except that a NAS mapping is
    refused as ``backend_mismatch`` instead of silently taking the filesystem
    path.
    """

    resolved = _resolve_request(
        registry, document_id, corpus, expected_source_version, None, "notion"
    )
    if resolved.failure is not None:
        return resolved.failure
    mapping = resolved.mapping
    assert mapping is not None
    return _verify_notion(mapping, resolved.expected, None, origin_verifier)


def _open_decision(
    status: str,
    code: str,
    *,
    mapping: SourceMapping | None,
    version: str | None = None,
    expected: str | None = None,
    opened: bool = False,
    safe_id: str | None = None,
) -> OpenDecision:
    """The public answer to an open, built so no path or root can reach it."""

    return OpenDecision(
        document_id=safe_id
        if safe_id is not None
        else (mapping.document_id if mapping else "<rejected>"),
        corpus=mapping.corpus if mapping else None,
        backend=mapping.backend if mapping else None,
        opened=opened,
        status=status,
        code=code,
        source_version=version,
        expected_source_version=expected,
    )


def _decision_from_status(status: SourceStatus) -> OpenDecision:
    """Carry a refusal through unchanged. An open adds nothing to a refusal."""

    return OpenDecision(
        document_id=status.document_id,
        corpus=status.corpus,
        backend=status.backend,
        opened=False,
        status=status.status,
        code=status.code,
        source_version=status.source_version,
        expected_source_version=status.expected_source_version,
    )


def _open_target(
    mapping: SourceMapping, source_version: str, verified_path: Path | None
) -> OpenTarget:
    """The private, validated argument: a hashed path, or the pinned URL.

    For a NAS mapping this is the very path :func:`_verify_nas` resolved,
    stat-ed and hashed in the same call. That is what keeps ``source_version``
    a statement about the bytes the callback is about to receive, rather than
    about some other file that answered to the same location afterwards.
    """

    if mapping.backend == "nas":
        assert verified_path is not None
        return OpenTarget(
            document_id=mapping.document_id,
            corpus=mapping.corpus,
            backend=mapping.backend,
            source_version=source_version,
            path=verified_path,
        )
    return OpenTarget(
        document_id=mapping.document_id,
        corpus=mapping.corpus,
        backend=mapping.backend,
        source_version=source_version,
        url=mapping.page_url,
    )


def authorize_open(
    document_id: object,
    *,
    registry: SourceMappingRegistry,
    opener: object,
    expected_source_version: object = None,
    corpus: object = None,
    origin_verifier: OriginVerifier | None = None,
) -> OpenDecision:
    """Revalidate everything, then hand a validated argument to the opener.

    This is a *separate operation* from :func:`verify_source`, and it does not
    trust an earlier verification: mapping, corpus, revocation, containment,
    accessibility and the expected source version are all checked again inside
    this call, immediately before the callback runs. Anything short of
    ``current`` -- offline, denied, revoked, stale, missing, unverified --
    disables the open.

    Ordering matters as much as the checks do. The mapped target is resolved
    exactly once, and the version comparison is the *last* thing that happens to
    it: the path handed to the opener is the same path that was resolved,
    stat-ed and hashed, so ``source_version`` always describes the file the
    callback receives. An ordinary edit landing while the target is being
    resolved is therefore seen by the hash that follows it, and the answer is
    ``stale`` with the callback never called.

    The opener receives an :class:`OpenTarget` holding a validated ``Path`` (or,
    for Notion, a pinned HTTPS URL). No command string is built and no shell is
    involved anywhere in this module; a string passed as ``opener`` is refused
    outright rather than interpreted.

    Remaining exposure, stated plainly and not narrowed by the ordering above:
    a write that lands after the final hash -- while the callback runs, or after
    the external application has the path -- is not detected. A process running
    as the same OS user with write access inside the approved root can replace
    the file at that point. That gap is inherent to handing a path to another
    program: closing it would require path-to-external-application semantics the
    filesystem does not offer. This is not atomic sandboxing and must not be
    described as such.
    """

    if isinstance(opener, str) or not callable(opener):
        # A command string is not an opener. Refusing the shape is what keeps
        # shell interpolation from ever being a question here.
        refusal = _refusal(document_id, "refused", "invalid_opener")
        return _open_decision(
            "refused", "invalid_opener", mapping=None, safe_id=refusal.document_id
        )

    # The full verification runs again here, inside this call -- scope, corpus,
    # revocation, containment, accessibility and the expected version. What it
    # does *not* do is resolve the mapped target twice. The NAS verification
    # returns the very path it hashed, and that path is what the opener is
    # handed, so the version in the decision and the bytes behind the argument
    # come from one coherent resolution. Resolving again after the comparison
    # would mean a file first observed *after* the hash could be paired with the
    # version of the file observed before it.
    resolved = _resolve_request(
        registry, document_id, corpus, expected_source_version, None, None
    )
    if resolved.failure is not None:
        return _decision_from_status(resolved.failure)
    mapping = resolved.mapping
    assert mapping is not None

    verified_path: Path | None = None
    if mapping.backend == "nas":
        status, verified_path = _verify_nas(mapping, resolved.expected, None)
    else:
        status = _verify_notion(mapping, resolved.expected, None, origin_verifier)
    if status.status != "current":
        return _decision_from_status(status)
    assert status.source_version is not None

    target = _open_target(mapping, status.source_version, verified_path)

    try:
        opener(target)
    except Exception:
        # The opener's own failure is its business; its message may name the
        # path and must not reach a public result.
        return _open_decision(
            "failed",
            "opener_failed",
            mapping=mapping,
            version=status.source_version,
            expected=status.expected_source_version,
        )
    return _open_decision(
        "opened",
        "opener_invoked",
        mapping=mapping,
        version=status.source_version,
        expected=status.expected_source_version,
        opened=True,
    )


__all__ = [
    "ALWAYS_REFUSED_EXTENSIONS",
    "BACKENDS",
    "DEFAULT_PERMITTED_EXTENSIONS",
    "MAX_VERIFIED_BYTES",
    "NOTION_ALLOWED_HOSTS",
    "NOTION_ALLOWED_HOST_SUFFIX",
    "OPEN_CODES",
    "OPEN_STATUSES",
    "OpenDecision",
    "OpenTarget",
    "Opener",
    "OriginAttestation",
    "OriginRequest",
    "OriginVerifier",
    "SourceMapping",
    "SourceMappingError",
    "SourceMappingRegistry",
    "SourceStatus",
    "VERIFICATION_CODES",
    "VERIFICATION_STATUSES",
    "authorize_open",
    "resolve_notion_source",
    "unsafe_notion_url_code",
    "verify_source",
]