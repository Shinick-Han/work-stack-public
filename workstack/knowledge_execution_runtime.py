"""One owner-configured execution of one already-issued KnowledgeRequest.

This module is the *sequence*, not any of its four steps. The R14 execution
contract chose those steps and had them built and reviewed separately -- the
read-only admission, the per-incarnation attempt guard, the bounded child
transport and the untrusted-output projection -- and what remains is the order
in which one owner process is allowed to perform them:

1. under one outer Store transaction, admit the submitted document against the
   *live* ledger, choose the operator-pinned driver for the admitted alias, and
   consume this incarnation's single attempt;
2. release the transaction, then run the pinned child exactly once, with the
   request on its stdin and nothing else;
3. project the child's untrusted stdout back through the released manual-import
   validators;
4. admit the request *again*, in a new transaction, and refuse to return the
   proposal if the policy, the Task or the window moved while the child ran.

Nothing here stores anything. The returned envelope is a **proposal for the
user to review**, not saved evidence: no Capture is written, no Task is touched
and no response is cached. The existing manual import route is still the only
writer, and the user still confirms there.

**Configuration is the operator's, and it is trusted.** :func:`admit_drivers`
copies a mapping the embedding process supplies -- from ``create_server``, in
practice -- and refuses a malformed one before the server takes its lease. It
reads no environment variable, no configuration file, no SSOT document and no
secret loader, and it has no route: a running server's registry is fixed for
the life of that server. The default is no driver at all, which is the explicit
``knowledge_driver_unavailable`` state rather than a silent no-op.

**The child gets the request and the connection facts, and nothing else.** It
never receives the owner's CSRF token, the Capture bearer token, an ambient
environment or a path to the store. The alias it is told about must be the one
the ledger currently binds to the same upstream workspace the operator pinned;
a mismatch is closed and spawns nothing.

**This is not exactly-once, not a sandbox and not crash resume.** The attempt
is spent before the child starts and is never given back, so an outcome this
owner cannot describe is a question for the operator rather than an automatic
retry. A new owner incarnation has registered nothing, so every request issued
before a restart is ineligible for automatic execution -- deliberately, and
documented as such rather than presented as recovery.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .capture import CaptureValidationError
from .capture_retrieval import CaptureRetrievalError
from .knowledge_attempt_guard import KnowledgeAttemptGuard
from .knowledge_capture_packets import KnowledgeImportError
from .knowledge_driver_exchange import MAX_PAYLOAD_BYTES, run_knowledge_driver
# The transport's own shape predicates, reused rather than restated. They are
# module-private because they are not a public boundary, and this caller is not
# making them one: it is the *same* process pinning the *same* argv, and a
# second spelling of these rules here could drift into accepting a registry the
# transport would then refuse as `invalid_driver_input` at execution time.
from .knowledge_driver_exchange import (  # noqa: PLC2701
    _admissible_command,
    _admissible_environment,
)
from .knowledge_execution_admission import AdmittedExecution, admit_execution_request
from .knowledge_execution_proposal import validate_execution_proposal
from .knowledge_ledger_document import CONNECTION_ALIAS_RE, MAX_CONNECTIONS
from .knowledge_request import KnowledgeRequestError, canonical_uuid
from .knowledge_verification_protocol import KnowledgeVerificationBinding

__all__ = (
    "DRIVER_BINDING_MISMATCH",
    "DRIVER_INPUT_REFUSED",
    "DRIVER_TIMEOUT_SECONDS",
    "DRIVER_UNAVAILABLE",
    "EXECUTE_SCHEMA",
    "KnowledgeDriverBinding",
    "KnowledgeDriverConfigurationError",
    "KnowledgeExecutionError",
    "KnowledgeVerificationBinding",
    "KnowledgeProposalError",
    "MAX_DRIVERS",
    "admit_drivers",
    "execute_knowledge_request",
)


#: The stdin envelope this owner writes. It carries the issued request and the
#: ledger's own connection facts; there is no field for a command, an endpoint,
#: a credential, a token or a corpus grant the caller could add.
EXECUTE_SCHEMA = "workstack.knowledge-execute.v1"

#: At most as many pinned drivers as the ledger may hold connections. The bound
#: is the ledger's, reused rather than restated.
MAX_DRIVERS = MAX_CONNECTIONS

#: The one budget this caller gives a child, at or below the transport default.
DRIVER_TIMEOUT_SECONDS = 75.0

#: Closed codes this module owns. The three transport codes travel unchanged.
DRIVER_UNAVAILABLE = "knowledge_driver_unavailable"
DRIVER_BINDING_MISMATCH = "knowledge_driver_binding_mismatch"
DRIVER_INPUT_REFUSED = "knowledge_driver_input_refused"


class KnowledgeDriverConfigurationError(ValueError):
    """A malformed operator registry, refused before the server exists."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeExecutionError(ValueError):
    """Closed execution refusal: one code, and no submitted value at all."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.details: dict[str, Any] = {}


class KnowledgeProposalError(ValueError):
    """The child answered, and the released validators refused what it said.

    Every refusal the projection can raise arrives here with its own closed
    code and, at most, the *name* of a field in a closed schema. The child's
    bytes, the decoder's message and any value it named are dropped at this
    boundary: the caller learns which rule the answer broke, never what the
    answer contained.
    """

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details: dict[str, Any] = {} if field is None else {"field": field}


def _proposal_refusal(error: Any) -> KnowledgeProposalError:
    """One projection refusal, reduced to a code and a schema field name."""

    field_name = getattr(error, "details", {}).get("field")
    return KnowledgeProposalError(
        str(getattr(error, "code", "invalid_import")),
        field_name if isinstance(field_name, str) else None,
    )


@dataclass(frozen=True)
class KnowledgeDriverBinding:
    """One alias' pinned driver, as the trusted operator configured it.

    ``command`` and ``environment`` are excluded from ``repr`` on purpose: an
    operator's argv holds a filesystem path and its environment may hold
    whatever that adapter needs to reach its own upstream. Neither is a secret
    this module can validate, and neither belongs in a traceback, a log line or
    a test failure message.

    ``verification`` is the R20 opt-in and nothing else. It appends to the end
    of the field order with a ``None`` default, so every caller written before
    it existed -- positional or keyword -- constructs exactly the binding it
    always did. Absence is the default and means *this alias has no verifier*;
    it is never derived from ``command``, and the search command above is not
    read, rewritten or reused to invent one. The verifier's own argv and
    environment are separately pinned by the operator and separately hidden
    from ``repr`` by :class:`KnowledgeVerificationBinding`.
    """

    upstream_workspace_uid: str
    command: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    verification: KnowledgeVerificationBinding | None = None


def _admitted_command(command: Any) -> tuple[str, ...]:
    """The transport's own argv rules, applied before a server ever starts.

    One to sixteen non-empty parts of at most 512 characters, ``argv[0]``
    absolute -- decided by the transport's predicate, not by a copy of it, so a
    registry this function accepts cannot be refused later for its shape. The
    operator pinned the executable; requiring an absolute ``argv[0]`` is what
    keeps that pin from being resolved against a search path. Nothing is
    launched to find out: no child runs during configuration.
    """

    if not _admissible_command(command):
        raise KnowledgeDriverConfigurationError("invalid_driver_command")
    return tuple(command)


def _admitted_environment(environment: Any) -> Mapping[str, str]:
    """An explicit string-to-string mapping, copied into an immutable view.

    Nothing is inherited: the operator states every name and value, and this
    module never consults :data:`os.environ`, a dotenv file or a secret store
    to fill one in. The copy is taken here so a later mutation of the mapping
    the operator handed in cannot change what a running server passes a child.
    """

    if not _admissible_environment(environment):
        raise KnowledgeDriverConfigurationError("invalid_driver_environment")
    return MappingProxyType(dict(environment))


def _admitted_verification(
    verification: Any,
) -> KnowledgeVerificationBinding | None:
    """The alias' optional verifier, held to the driver's own argv rules.

    ``None`` -- the default -- is the explicit "this alias has no verifier"
    state, and it is the only absence there is: an operator who wrote nothing
    gets no verifier, and one who wrote something malformed gets a refusal
    before the server exists rather than a silently disabled check.

    The command and the environment go through the *same* predicates the search
    driver's do, and the copies are taken here, so a later mutation of what the
    operator handed in cannot change what a running server would pass a child.
    Nothing is launched or resolved: no child runs during configuration.
    """

    if verification is None:
        return None
    if not isinstance(verification, KnowledgeVerificationBinding):
        raise KnowledgeDriverConfigurationError("invalid_driver_verification")
    if not _admissible_command(verification.command):
        raise KnowledgeDriverConfigurationError("invalid_verification_command")
    if not _admissible_environment(verification.environment):
        raise KnowledgeDriverConfigurationError("invalid_verification_environment")
    return KnowledgeVerificationBinding(
        command=tuple(verification.command),
        environment=MappingProxyType(dict(verification.environment)),
    )


def admit_drivers(drivers: Any) -> Mapping[str, KnowledgeDriverBinding]:
    """Copy and admit the operator's driver registry, or refuse it.

    Called once, at server construction, before the server takes its store
    lease or opens its socket: an invalid registry is a configuration defect
    the process refuses to start on, not a runtime refusal a request discovers.

    ``None`` -- the default -- is the empty registry, which is the explicit
    "no driver is configured" state every execution then reports.

    An entry may also carry an optional verifier binding. It is copied and
    admitted here under the same argv and environment rules, and its absence
    stays absence: admitting a registry never turns verification on.
    """

    if drivers is None:
        return MappingProxyType({})
    if not isinstance(drivers, Mapping):
        raise KnowledgeDriverConfigurationError("invalid_driver_registry")
    if len(drivers) > MAX_DRIVERS:
        raise KnowledgeDriverConfigurationError("driver_registry_full")
    admitted: dict[str, KnowledgeDriverBinding] = {}
    for alias, binding in drivers.items():
        if not isinstance(alias, str) or not CONNECTION_ALIAS_RE.fullmatch(alias):
            raise KnowledgeDriverConfigurationError("invalid_driver_alias")
        if not isinstance(binding, KnowledgeDriverBinding):
            raise KnowledgeDriverConfigurationError("invalid_driver_binding")
        try:
            upstream = canonical_uuid(
                binding.upstream_workspace_uid, "upstream_workspace_uid"
            )
        except KnowledgeRequestError as error:
            raise KnowledgeDriverConfigurationError(
                "invalid_driver_upstream_workspace_uid"
            ) from error
        admitted[alias] = KnowledgeDriverBinding(
            upstream_workspace_uid=upstream,
            command=_admitted_command(binding.command),
            environment=_admitted_environment(binding.environment),
            # The alias and its upstream identity above are inherited by both
            # operations: one pin, one upstream workspace, whichever child the
            # owner later runs.
            verification=_admitted_verification(binding.verification),
        )
    return MappingProxyType(admitted)


def _pinned_binding(
    drivers: Mapping[str, KnowledgeDriverBinding], admitted: AdmittedExecution
) -> KnowledgeDriverBinding:
    """The driver the operator pinned for exactly this admitted connection.

    An alias with no pinned driver is unavailable, not an error to work around.
    An alias whose pinned upstream workspace is not the one the ledger's roster
    currently names is a *mismatch*: the operator's configuration and the
    owner's policy disagree about where this alias points, and no child runs
    while they do.
    """

    binding = drivers.get(admitted.connection_alias)
    if binding is None:
        raise KnowledgeExecutionError(DRIVER_UNAVAILABLE)
    if binding.upstream_workspace_uid != admitted.upstream_workspace_uid:
        raise KnowledgeExecutionError(DRIVER_BINDING_MISMATCH)
    return binding


def _driver_payload(admitted: AdmittedExecution) -> bytes:
    """The child's whole stdin: the issued request and the ledger's facts.

    Compact canonical UTF-8. The query travels here and nowhere else -- never
    on the argv, never in the environment, never in a log line.
    """

    envelope = {
        "schema": EXECUTE_SCHEMA,
        "request": copy.deepcopy(admitted.document),
        "connection": {
            "alias": admitted.connection_alias,
            "upstream_workspace_uid": admitted.upstream_workspace_uid,
            "policy_revision": admitted.policy_revision,
        },
    }
    payload = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
        # The transport would refuse this the same way, without spawning. The
        # attempt has already been spent by the time we are here, so this is
        # reported as its own configuration-shaped refusal rather than being
        # folded into the launch failure the operator would investigate
        # differently. What keeps it unreached is the issued document's own
        # closed fields -- a query of at most 1000 characters, at most eight
        # corpus refs of at most 64, and bounded identities and timestamps --
        # not the raw 16 KiB HTTP budget, which says nothing about the bytes
        # this wrapper adds. The check above measures the actual encoded
        # payload rather than trusting either bound.
        raise KnowledgeExecutionError(DRIVER_INPUT_REFUSED)
    return payload


def _result_limit(document: Mapping[str, Any]) -> int:
    """The limit the *ledger's* projection carries, not a caller's claim."""

    limit = document["result_limit"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise KnowledgeRequestError("invalid_request", "result_limit")
    return limit


def execute_knowledge_request(
    store: Any,
    request_document: Any,
    *,
    drivers: Mapping[str, KnowledgeDriverBinding],
    guard: KnowledgeAttemptGuard,
    clock: Callable[[], str],
) -> dict[str, Any]:
    """Execute one issued request once and return a proposal for review.

    ``clock`` is the caller's injected clock -- the server's, in production the
    UTC wall clock -- and is read once for the admission before the child and
    once for the admission after it, so the second reading is what decides
    whether the window is still open at the moment the answer would be shown.

    The returned mapping is the released manual-import envelope the user may
    then submit through the existing import route. It is not saved here.
    """

    with store.transaction():
        admitted = admit_execution_request(store, request_document, now=clock())
        binding = _pinned_binding(drivers, admitted)
        # Spending the attempt inside the same transaction that proved the
        # authority is what makes two concurrent executions serialise: the
        # second one finds the identity already attempted and never reaches a
        # child. It is one-way. A failure below -- including one that provably
        # never started a child -- leaves it spent.
        guard.consume(admitted.document["request_id"], admitted.request_digest)
        payload = _driver_payload(admitted)
        result_limit = _result_limit(admitted.document)
    # The transaction is released here, deliberately, before any child I/O:
    # nothing this owner does while waiting on a child holds the store, so an
    # unrelated write can proceed the whole time the child is running.
    outcome = run_knowledge_driver(
        binding.command,
        payload,
        environment=binding.environment,
        timeout_seconds=DRIVER_TIMEOUT_SECONDS,
    )
    if outcome.error_code is not None:
        # The transport's closed code travels unchanged. There is no retry and
        # no second factory call on any branch, and the child's stderr, the
        # command and the environment are not in this exception.
        raise KnowledgeExecutionError(outcome.error_code)
    try:
        proposal = validate_execution_proposal(
            outcome.stdout,
            request_id=admitted.document["request_id"],
            connection_alias=admitted.connection_alias,
            result_limit=result_limit,
            now=clock(),
        )
    except (
        KnowledgeImportError,
        KnowledgeRequestError,
        CaptureValidationError,
        CaptureRetrievalError,
    ) as error:
        # Only the released validators' own closed refusals are converted. A
        # programming error below this line is not a bad proposal and is not
        # caught here.
        raise _proposal_refusal(error) from error
    with store.transaction():
        rechecked = admit_execution_request(store, request_document, now=clock())
        # The policy roster may have been replaced while the child ran; the
        # revision check inside the admission already refuses that, and this
        # says the same thing about the operator's pin so the two can never
        # disagree silently on the way out.
        _pinned_binding(drivers, rechecked)
    return proposal
