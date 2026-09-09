"""The one-shot OpenDocuments source verifier: stdin in, one observation out.

This is the child process an owner-side admission path may spawn. It reads one
``workstack.knowledge-verify.v1`` document from stdin, checks it against the
operator's pinned verifier configuration, asks the released
:func:`integrations.opendocuments.source_access.verify_source` for each NAS
evidence entry and
:func:`integrations.opendocuments.source_verifier_notion.observe_notion_mapping`
for each Notion one, and writes one ``workstack.knowledge-verification.v1``
document to stdout. It is not a retrieval stack and not an opener.

The Notion half is an *access* observation and nothing else: it never returns
``current`` or ``stale``, it always echoes the request's
``expected_source_version`` with ``observed_source_version`` null, and it reads
no page content. Reaching a page is not the same fact as the captured text
still being right, and this module never lets the first stand in for the second.

Request and result *shape* belong to
``workstack.knowledge_verification_protocol``. This module does not reimplement
those validators. It pins connection alias and upstream identity, requires the
request corpora to be a subset of the operator grants, maps richer source_access
codes onto the protocol's coarse pairs, and keeps paths and file bytes out of
every public observation.

Success is a canonical bounded document on stdout and exit ``0``. Every refusal
writes **nothing** to stdout and exits ``1``, with at most one closed code from
:data:`DIAGNOSTIC_CODES` on stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Mapping

from integrations.opendocuments.source_access import SourceStatus, verify_source
from integrations.opendocuments.source_verifier_config import (
    OperatorVerifierConfig,
    VerifierConfigError,
    load_operator_verifier_config,
)
from integrations.opendocuments.source_verifier_notion import (
    NotionBatch,
    observe_notion_mapping,
)
from workstack.knowledge_request import KnowledgeRequestError, decode_strict_json
from workstack.knowledge_request_issuer import utc_now_rfc3339

__all__ = [
    "CONFIG_ENVIRONMENT_VARIABLE",
    "DIAGNOSTIC_CODES",
    "EXIT_OK",
    "EXIT_REFUSED",
    "MAX_STDIN_BYTES",
    "MAX_STDOUT_BYTES",
    "NOTION_SOURCE_TYPE",
    "RESULT_SCHEMA",
    "VERIFY_SCHEMA",
    "check_verifier_policy",
    "main",
    "observe_verification_evidence",
]

VERIFY_SCHEMA = "workstack.knowledge-verify.v1"
RESULT_SCHEMA = "workstack.knowledge-verification.v1"

CONFIG_ENVIRONMENT_VARIABLE = "WORKSTACK_OD_VERIFIER_CONFIG"

MAX_STDIN_BYTES = 16 * 1024
MAX_STDOUT_BYTES = 16 * 1024

EXIT_OK = 0
EXIT_REFUSED = 1

DIAGNOSTIC_CODES = frozenset(
    {
        "verifier_input_refused",
        "verifier_config_refused",
        "verifier_policy_refused",
        "verifier_output_refused",
    }
)

NAS_SOURCE_TYPE = "nas.file"
NOTION_SOURCE_TYPE = "notion.page"

# Capture/protocol opaque versions cannot contain ``:``. The only NAS byte-hash
# spelling that may cross this adapter is ``sha256-<64 lowercase hex>``. The
# released ``verify_source`` helper still uses internal ``sha256:<hex>``.
_NAS_WIRE_HASH_RE = re.compile(r"^sha256-[0-9a-f]{64}\Z")
_NAS_INTERNAL_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_UNUSABLE_EXPECTED = object()

_EXACT_PAIRS = {
    ("current", "hash_matched"): ("current", "hash_matched"),
    ("stale", "hash_differs"): ("stale", "hash_differs"),
    ("missing", "file_absent"): ("missing", "file_absent"),
    ("unavailable", "root_unavailable"): ("unavailable", "root_unavailable"),
    ("revoked", "mapping_revoked"): ("revoked", "mapping_revoked"),
    ("unverifiable", "no_expected_version"): ("unverifiable", "no_expected_version"),
    ("unverifiable", "no_origin_verifier"): ("unverifiable", "no_origin_verifier"),
}

_CURRENT_OR_STALE = frozenset({"current", "stale"})


class _Refused(Exception):
    """One stage closed. The code is the whole diagnostic."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in DIAGNOSTIC_CODES else "verifier_input_refused"


def main(argv: list[str] | None = None) -> int:
    """Run one verification and return this process' exit status."""

    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments:
            raise _Refused("verifier_input_refused")
        payload = _run(_read_stdin())
    except _Refused as refusal:
        sys.stderr.write(refusal.code + "\n")
        sys.stderr.flush()
        return EXIT_REFUSED
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return EXIT_OK


def observe_verification_evidence(
    request: Mapping[str, Any],
    config: OperatorVerifierConfig,
    *,
    _notion_batch: NotionBatch | None = None,
) -> list[dict[str, Any]]:
    """Map one already-shaped request's evidence through source_access.

    This does not validate the protocol document. The CLI path asks protocol A
    to admit the request first, then calls this, then asks A to admit the
    result. Callers that are not the CLI must not treat this as a wire
    validator.

    ``_notion_batch`` is the private test seam for the Notion transport and
    clock. One batch is built per call, so every Notion entry in one request
    shares one rate limit and one wall budget rather than getting its own.
    """

    corpora = _corpus_set(request.get("corpus_refs"))
    entries = request.get("evidence")
    if not isinstance(entries, (list, tuple)):
        return []
    batch = NotionBatch() if _notion_batch is None else _notion_batch
    return [_observe_one(entry, corpora, config, batch) for entry in entries]


def check_verifier_policy(
    connection: Mapping[str, Any],
    corpus_refs: Any,
    config: OperatorVerifierConfig,
) -> bool:
    """True when this operator file is the pin for this admitted request."""

    if connection.get("alias") != config.connection_alias:
        return False
    if connection.get("upstream_workspace_uid") != config.upstream_workspace_uid:
        return False
    granted = set(config.corpus_grants)
    try:
        requested = set(corpus_refs)
    except TypeError:
        return False
    return bool(requested) and requested <= granted


def _run(raw: bytes) -> bytes:
    request = _admitted_request(raw, utc_now_rfc3339())
    config = _pinned_config()
    connection = request["connection"]
    if not check_verifier_policy(connection, request["corpus_refs"], config):
        raise _Refused("verifier_policy_refused")
    evidence = observe_verification_evidence(request, config)
    # The clock is read *after* the observation, and that one reading is both
    # the reported ``checked_at`` and the ``now`` the result is validated at. A
    # Notion batch may spend real seconds on the wire, and a slow check that
    # overran its window has to be refused on the way out rather than backdated
    # to the moment the request was admitted.
    checked_at = utc_now_rfc3339()
    result = {
        "schema": RESULT_SCHEMA,
        "verification_id": request["verification_id"],
        "checked_at": checked_at,
        "evidence": evidence,
    }
    admitted = _admitted_result(result, request, checked_at)
    return _canonical_output(admitted)


def _read_stdin() -> bytes:
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    except OSError as error:
        raise _Refused("verifier_input_refused") from error
    if not raw or len(raw) > MAX_STDIN_BYTES:
        raise _Refused("verifier_input_refused")
    return raw


def _admitted_request(raw: bytes, now: str) -> dict[str, Any]:
    try:
        document = decode_strict_json(raw, maximum_bytes=MAX_STDIN_BYTES)
    except KnowledgeRequestError as error:
        raise _Refused("verifier_input_refused") from error
    protocol = _protocol()
    try:
        admitted = protocol.validate_verification_request(document, now=now)
    except protocol.VerificationError as error:
        raise _Refused("verifier_input_refused") from error
    if not isinstance(admitted, dict):
        raise _Refused("verifier_input_refused")
    return admitted


def _admitted_result(
    document: Mapping[str, Any], request: Mapping[str, Any], now: str
) -> Mapping[str, Any]:
    protocol = _protocol()
    try:
        admitted = protocol.validate_verification_result(
            document, request=request, now=now
        )
    except protocol.VerificationError as error:
        raise _Refused("verifier_output_refused") from error
    if not isinstance(admitted, dict):
        raise _Refused("verifier_output_refused")
    return admitted


def _protocol() -> Any:
    from workstack import knowledge_verification_protocol as protocol

    return protocol


def _pinned_config() -> OperatorVerifierConfig:
    path = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    if not path:
        raise _Refused("verifier_config_refused")
    try:
        return load_operator_verifier_config(path)
    except VerifierConfigError as error:
        raise _Refused("verifier_config_refused") from error


def _observe_one(
    entry: Any,
    corpora: frozenset[str],
    config: OperatorVerifierConfig,
    batch: NotionBatch,
) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        return _blank_entry(entry, "unverifiable", "verification_unavailable")
    source_type = entry.get("source_type")
    if source_type == NAS_SOURCE_TYPE:
        return _observe_nas(entry, corpora, config)
    if source_type == NOTION_SOURCE_TYPE:
        return _observe_notion(entry, corpora, config, batch)
    return _result_entry(entry, "unverifiable", "unsupported_source_type", None)


def _observe_nas(
    entry: Mapping[str, Any],
    corpora: frozenset[str],
    config: OperatorVerifierConfig,
) -> dict[str, Any]:
    expected = _internal_nas_expected(entry.get("expected_source_version"))
    if expected is _UNUSABLE_EXPECTED:
        return _result_entry(entry, "unverifiable", "verification_unavailable", None)
    document_ref = entry.get("document_ref")
    mapping = config.registry.lookup(document_ref)
    if mapping is not None and mapping.backend != "nas":
        # The owner asked about a file and the operator filed this id as a
        # page. Answering from the other backend would answer a question
        # nobody asked.
        return _result_entry(entry, "refused", "source_refused", None)
    if mapping is not None and mapping.corpus not in corpora:
        return _result_entry(entry, "refused", "source_refused", None)
    status = verify_source(
        document_ref,
        registry=config.registry,
        expected_source_version=expected,
        indexed_digest=None,
        corpus=None if mapping is None else mapping.corpus,
    )
    return _from_source_status(entry, status)


def _observe_notion(
    entry: Mapping[str, Any],
    corpora: frozenset[str],
    config: OperatorVerifierConfig,
    batch: NotionBatch,
) -> dict[str, Any]:
    """One Notion access observation, with ``observed_source_version`` null.

    The mapping is looked up and its backend checked here, before anything in
    ``source_verifier_notion`` runs, so an unmapped or NAS-filed id never
    reaches the token or the socket. ``expected_source_version`` is passed
    through unread except for "is it null", and is echoed back exactly as the
    request wrote it.
    """

    mapping = config.registry.lookup(entry.get("document_ref"))
    if mapping is None or mapping.backend != "notion":
        return _result_entry(entry, "refused", "source_refused", None)
    status, code = observe_notion_mapping(
        mapping,
        in_request_corpus=mapping.corpus in corpora,
        expected_source_version=entry.get("expected_source_version"),
        batch=batch,
    )
    return _result_entry(entry, status, code, None)


def _internal_nas_expected(value: object) -> object:
    """Translate wire ``sha256-<hex>`` to internal ``sha256:<hex>``, or refuse."""

    if value is None:
        return None
    if isinstance(value, str) and _NAS_WIRE_HASH_RE.fullmatch(value):
        return "sha256:" + value[7:]
    return _UNUSABLE_EXPECTED


def _wire_nas_observed(value: str | None) -> str | None:
    if isinstance(value, str) and _NAS_INTERNAL_HASH_RE.fullmatch(value):
        return "sha256-" + value[7:]
    return None


def _from_source_status(entry: Mapping[str, Any], status: SourceStatus) -> dict[str, Any]:
    public_status, public_code = _public_pair(status.status, status.code)
    if public_status not in _CURRENT_OR_STALE:
        return _result_entry(entry, public_status, public_code, None)
    observed = _wire_nas_observed(status.source_version)
    if observed is None:
        return _result_entry(entry, "unverifiable", "verification_unavailable", None)
    return _result_entry(entry, public_status, public_code, observed)


def _public_pair(status: str, code: str) -> tuple[str, str]:
    exact = _EXACT_PAIRS.get((status, code))
    if exact is not None:
        return exact
    if status == "denied":
        return "denied", "access_denied"
    if status == "refused":
        return "refused", "source_refused"
    return "unverifiable", "verification_unavailable"


def _result_entry(
    entry: Mapping[str, Any], status: str, code: str, observed: str | None
) -> dict[str, Any]:
    return {
        "document_ref": entry.get("document_ref"),
        "source_type": entry.get("source_type"),
        "expected_source_version": entry.get("expected_source_version"),
        "observed_source_version": observed,
        "status": status,
        "code": code,
    }


def _blank_entry(_entry: Any, status: str, code: str) -> dict[str, Any]:
    return {
        "document_ref": None,
        "source_type": None,
        "expected_source_version": None,
        "observed_source_version": None,
        "status": status,
        "code": code,
    }


def _corpus_set(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple)):
        return frozenset(item for item in value if isinstance(item, str))
    return frozenset()


def _canonical_output(envelope: Any) -> bytes:
    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _Refused("verifier_output_refused") from error
    if not 0 < len(payload) <= MAX_STDOUT_BYTES:
        raise _Refused("verifier_output_refused")
    return payload


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
