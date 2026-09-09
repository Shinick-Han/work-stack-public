"""The one-shot OpenDocuments knowledge driver: ``python -m ...driver_main``.

This is the child process the owner's execution route spawns. It reads one
``workstack.knowledge-execute.v1`` envelope from stdin, checks it against the
operator's pinned configuration, asks the released
:func:`integrations.opendocuments.manual_query.run_reviewed_query` exactly one
reviewed query, and writes the composed ``workstack.knowledge-import.v1``
envelope to stdout. It is the *process*, not a new retrieval stack: every rule
below this line already exists and is called unedited.

**What the request is, and what it is not.** The document on stdin was already
admitted by the owner against the live ledger, the connection policy, the Task
and the request window; it is trusted internal input from the parent, not a
wire body from a stranger. This module therefore re-checks its **structure and
bounds** -- so a configuration or coding defect refuses instead of reaching a
socket -- and deliberately does **not** rebuild the owner's authority. It never
assembles a :class:`RequestAuthority` out of the request's own fields: doing so
would let a document grant itself the workspace, the Task and the corpus scope
it names, which is exactly the check the parent already performed with state
this process cannot see. The parent remains the ledger and Task authority; this
child mints nothing, writes nothing and imports nothing.

**The operator's policy decides the scope.** The alias and upstream workspace
on the envelope must be the ones pinned in this driver's own configuration
file, and the request's ``corpus_refs`` must equal the operator's
``corpus_grants`` **exactly** -- not be a subset of them. The frozen upstream
chat route carries no collection field (``CLIENT.md``), so a narrower request
is a filter this driver cannot honour: it refuses rather than silently
answering a small question with a whole-workspace search.

**The key is read last, and only here.** Nothing opens the key file until the
input, the configuration and the policy match have all been admitted, so a
malformed request never costs a credential read, let alone a request. The
launching process never reads it at all. This is same-user trust, not a
sandbox: whoever can run this child can read that file.

**One identity, one call, one answer.** The item identity is
``uuid5(OD_DRIVER_ITEM_NAMESPACE, request_id)`` -- derived from the owner's own
request identity and nothing else, so two invocations of the same request
propose the same item and the upstream ``queryId`` (which the driver does not
choose) can never enter it. There is exactly one upstream POST and no retry on
any branch, including ``outcome_unknown``.

**Output is all-or-nothing.** Success is a canonical bounded envelope on stdout
and exit ``0``. Every refusal writes **nothing** to stdout and exits nonzero,
with at most one closed code from :data:`DIAGNOSTIC_CODES` on stderr -- never a
message, a traceback, a response body, a path, a key or the query. The parent
collapses every nonzero exit into its own ``driver_outcome_unknown``; this
module invents no second protocol to argue with that.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Mapping

from integrations.opendocuments.driver_config import (
    DriverConfigError,
    OperatorDriverConfig,
    build_backend_config,
    load_driver_key,
    load_operator_driver_config,
)
from integrations.opendocuments.manual_import import MAX_IMPORT_BYTES
from integrations.opendocuments.manual_query import run_reviewed_query
from integrations.opendocuments.retrieval_mapper_fields import (
    MappingError,
    admit_result_limit,
)
from workstack.knowledge_request import (
    CORPUS_REF_RE,
    MAX_CORPUS_REF_CHARS,
    MAX_CORPUS_REFS,
    MAX_QUERY_CHARS,
    MIN_CORPUS_REFS,
    PURPOSES,
    SCHEMA,
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
    is_expired,
)
from workstack.knowledge_request_issuer import utc_now_rfc3339

__all__ = [
    "CONFIG_ENVIRONMENT_VARIABLE",
    "DIAGNOSTIC_CODES",
    "EXECUTE_SCHEMA",
    "EXIT_OK",
    "EXIT_REFUSED",
    "MAX_STDIN_BYTES",
    "MAX_STDOUT_BYTES",
    "OD_DRIVER_ITEM_NAMESPACE",
    "main",
]

#: The envelope the owner writes to this child's stdin. The value is part of
#: the released execution contract and is spelled here, as ``manual_import``
#: spells the import schema, so this adapter needs no import from the owner's
#: execution runtime. The tests assert the two constants are the same string.
EXECUTE_SCHEMA = "workstack.knowledge-execute.v1"

#: The single environment variable this child reads. The launcher states it
#: explicitly for the child; nothing else about the environment is consulted,
#: and there is no ambient discovery and no default path.
CONFIG_ENVIRONMENT_VARIABLE = "WORKSTACK_OD_DRIVER_CONFIG"

#: The owner transport's stdin bound, measured in real UTF-8 octets.
MAX_STDIN_BYTES = 16 * 1024

#: The owner transport's stdout bound. It is the same 64 KiB the released
#: import envelope is already held to, so the composer's own budget and this
#: one cannot disagree.
MAX_STDOUT_BYTES = MAX_IMPORT_BYTES

#: The fixed application namespace for driver-proposed item identities. It is
#: the constant `uuid.uuid5(uuid.NAMESPACE_URL,
#: "https://work-stack.invalid/opendocuments-driver/v1/item")`, written out so
#: the value is auditable on its own and can never drift with a helper. The
#: same request identity therefore names the same item on every build and every
#: rerun, independently of the answer and of the upstream ``queryId``.
OD_DRIVER_ITEM_NAMESPACE = uuid.UUID("42da54a8-c626-5d03-9f8c-65286472d031")

EXIT_OK = 0
EXIT_REFUSED = 1

#: Every code this child may write to stderr. Each one names the *stage* that
#: closed and nothing about the value that closed it.
DIAGNOSTIC_CODES = frozenset(
    {
        "driver_input_refused",
        "driver_config_refused",
        "driver_policy_refused",
        "driver_key_refused",
        "driver_query_refused",
        "driver_output_refused",
    }
)

_ENVELOPE_FIELDS = frozenset({"schema", "request", "connection"})
_CONNECTION_FIELDS = frozenset({"alias", "upstream_workspace_uid", "policy_revision"})
_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "request_id",
        "binding",
        "purpose",
        "query",
        "corpus_refs",
        "result_limit",
        "requested_at",
        "expires_at",
    }
)
_BINDING_WORKSPACE_ONLY = frozenset({"workspace_uid"})
_BINDING_WITH_TASK = frozenset({"workspace_uid", "task_uid", "task_id", "task_revision"})
_MAX_TIMESTAMP_CHARS = 64


class _Refused(Exception):
    """One stage closed. The code is the whole diagnostic."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in DIAGNOSTIC_CODES else "driver_input_refused"


def main(argv: list[str] | None = None) -> int:
    """Run one request and return this process' exit status.

    Nothing is written to stdout except a composed envelope on the one success
    path, and nothing but a closed code is written to stderr on any path.
    """

    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments:
            # This child takes no options. The request travels on stdin, and an
            # argv nobody designed is a configuration defect, not a mode.
            raise _Refused("driver_input_refused")
        payload = _run(_read_stdin())
    except _Refused as refusal:
        sys.stderr.write(refusal.code + "\n")
        sys.stderr.flush()
        return EXIT_REFUSED
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return EXIT_OK


def _run(raw: bytes) -> bytes:
    """Admit, match, ask once, and compose the bytes stdout will carry."""

    envelope = _admitted_envelope(raw)
    config = _pinned_config()
    request = envelope["request"]
    _require_policy_match(envelope["connection"], request, config)
    key = _driver_key(config)
    result = run_reviewed_query(
        request["query"],
        config=build_backend_config(config, key),
        request_id=request["request_id"],
        item_id=str(uuid.uuid5(OD_DRIVER_ITEM_NAMESPACE, request["request_id"])),
        result_limit=request["result_limit"],
        # The operator's own curated map, handed over unmodified. Nothing here
        # extends it from a search result.
        source_catalog=config.source_catalog,
    )
    if result.get("ok") is not True:
        # The transport's or composer's closed code stays inside this process:
        # the parent's own contract is that a nonzero exit is an unknown
        # outcome, and nothing here tries to say more than that on stdout.
        raise _Refused("driver_query_refused")
    return _canonical_output(result["envelope"])


# -- stdin ----------------------------------------------------------------


def _read_stdin() -> bytes:
    """Read at most one octet past the bound, so a long payload is refused."""

    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    except OSError as error:
        raise _Refused("driver_input_refused") from error
    if not raw or len(raw) > MAX_STDIN_BYTES:
        raise _Refused("driver_input_refused")
    return raw


def _admitted_envelope(raw: bytes) -> dict[str, Any]:
    """Decode the closed stdin envelope and admit the request's shape.

    The decoder is the released strict one: bounded UTF-8, no duplicate keys,
    no non-finite numbers, bounded depth. The field admission below is
    structural only -- it proves the parent handed over the document shape this
    driver knows how to run, and asserts no authority of its own.
    """

    try:
        document = decode_strict_json(raw, maximum_bytes=MAX_STDIN_BYTES)
    except KnowledgeRequestError as error:
        raise _Refused("driver_input_refused") from error
    envelope = _closed_object(document, _ENVELOPE_FIELDS)
    if envelope["schema"] != EXECUTE_SCHEMA:
        raise _Refused("driver_input_refused")
    return {
        "schema": EXECUTE_SCHEMA,
        "connection": _admitted_connection(envelope["connection"]),
        "request": _admitted_request(envelope["request"]),
    }


def _closed_object(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        # Missing and unknown keys are the same refusal: this is not the
        # document the owner's runtime composes.
        raise _Refused("driver_input_refused")
    return value


def _admitted_connection(value: Any) -> dict[str, Any]:
    """The ledger facts the owner read, admitted as facts and not as authority.

    ``policy_revision`` is an echoed integer. It is not consulted to decide
    anything here, and it grants nothing: the owner re-admits the request under
    the live policy after this child returns.
    """

    connection = _closed_object(value, _CONNECTION_FIELDS)
    alias = connection["alias"]
    if not isinstance(alias, str) or not CORPUS_REF_RE.fullmatch(alias):
        raise _Refused("driver_input_refused")
    if len(alias) > MAX_CORPUS_REF_CHARS:
        raise _Refused("driver_input_refused")
    revision = connection["policy_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise _Refused("driver_input_refused")
    return {
        "alias": alias,
        "upstream_workspace_uid": _uuid(connection["upstream_workspace_uid"]),
        "policy_revision": revision,
    }


def _admitted_request(value: Any) -> dict[str, Any]:
    """The issued KnowledgeRequest v1 projection, admitted structurally.

    Identities go through the released :func:`canonical_uuid`, the result limit
    through the released :func:`admit_result_limit`, and the corpus list through
    the released alias grammar. The query is bounded here and validated in full
    by the transport's own predicate before any socket is opened, so there is no
    second query grammar in this file.
    """

    request = _closed_object(value, _REQUEST_FIELDS)
    if request["schema"] != SCHEMA or request["purpose"] not in PURPOSES:
        raise _Refused("driver_input_refused")
    query = request["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise _Refused("driver_input_refused")
    try:
        result_limit = admit_result_limit(request["result_limit"])
    except MappingError as error:
        raise _Refused("driver_input_refused") from error
    return {
        "schema": SCHEMA,
        "request_id": _uuid(request["request_id"]),
        "binding": _admitted_binding(request["binding"]),
        "purpose": request["purpose"],
        "query": query,
        "corpus_refs": _admitted_corpus_refs(request["corpus_refs"]),
        "result_limit": result_limit,
        "requested_at": _timestamp(request["requested_at"]),
        "expires_at": _timestamp(request["expires_at"]),
    }


def _admitted_binding(value: Any) -> dict[str, Any]:
    """Workspace identity, with the Task trio all-or-nothing, as released.

    The binding is carried, never acted on: this driver attaches nothing to a
    Task and reads no Task state. It is admitted so a malformed document is a
    refusal rather than a surprise inside the composer.
    """

    if not isinstance(value, dict):
        raise _Refused("driver_input_refused")
    keys = set(value)
    if keys not in (_BINDING_WORKSPACE_ONLY, _BINDING_WITH_TASK):
        raise _Refused("driver_input_refused")
    _uuid(value["workspace_uid"])
    if keys == _BINDING_WITH_TASK:
        _uuid(value["task_uid"])
        revision = value["task_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise _Refused("driver_input_refused")
        if not isinstance(value["task_id"], str) or not value["task_id"]:
            raise _Refused("driver_input_refused")
    return dict(value)


def _admitted_corpus_refs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise _Refused("driver_input_refused")
    refs: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_CORPUS_REF_CHARS:
            raise _Refused("driver_input_refused")
        if not CORPUS_REF_RE.fullmatch(entry) or entry in refs:
            raise _Refused("driver_input_refused")
        refs.append(entry)
    return tuple(refs)


def _uuid(value: Any) -> str:
    try:
        return canonical_uuid(value, "identifier")
    except KnowledgeRequestError as error:
        raise _Refused("driver_input_refused") from error


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP_CHARS:
        raise _Refused("driver_input_refused")
    return value


# -- policy ---------------------------------------------------------------


def _pinned_config() -> OperatorDriverConfig:
    """Load the operator's nonsecret document from the one named variable."""

    path = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    if not path:
        raise _Refused("driver_config_refused")
    try:
        return load_operator_driver_config(path)
    except DriverConfigError as error:
        raise _Refused("driver_config_refused") from error


def _require_policy_match(
    connection: Mapping[str, Any],
    request: Mapping[str, Any],
    config: OperatorDriverConfig,
) -> None:
    """Refuse, before any socket, unless the owner and the operator agree.

    Three facts must line up: the alias this driver serves, the Work Stack
    upstream workspace that alias belongs to, and the corpus set. The corpus
    rule is set **equality**, not containment -- see the module docstring -- and
    the request window is re-read against this process' own clock so a request
    that lapsed between the owner's admission and this spawn is not run.
    """

    if connection["alias"] != config.connection_alias:
        raise _Refused("driver_policy_refused")
    if connection["upstream_workspace_uid"] != config.upstream_workspace_uid:
        raise _Refused("driver_policy_refused")
    if set(request["corpus_refs"]) != set(config.corpus_grants):
        raise _Refused("driver_policy_refused")
    try:
        expired = is_expired(dict(request), utc_now_rfc3339())
    except KnowledgeRequestError as error:
        raise _Refused("driver_input_refused") from error
    if expired:
        # Expiry is terminal here as everywhere: this driver has no renew, no
        # extend and no reissue, and a lapsed request needs a fresh one the
        # user reviews.
        raise _Refused("driver_policy_refused")


def _driver_key(config: OperatorDriverConfig) -> str:
    """Read the key file -- the first and only time this process touches it."""

    try:
        return load_driver_key(config)
    except DriverConfigError as error:
        raise _Refused("driver_key_refused") from error


# -- stdout ---------------------------------------------------------------


def _canonical_output(envelope: Any) -> bytes:
    """One compact, sorted, bounded UTF-8 document, or nothing at all."""

    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _Refused("driver_output_refused") from error
    if not 0 < len(payload) <= MAX_STDOUT_BYTES:
        # A partial or oversized answer is not a success. The parent would
        # refuse it anyway; refusing here keeps stdout empty on every failure.
        raise _Refused("driver_output_refused")
    return payload


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
