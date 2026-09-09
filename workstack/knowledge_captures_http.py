"""The loopback owner surface for importing a manually carried answer.

One canonical route and nothing else:

``POST /api/v1/knowledge/captures/import``  import one ledger-issued request's
answers as Captures.

**Admission is the released one.** The route is not the Capture ingestion
surface and does not borrow its bearer token: it reaches its handler only after
:meth:`~workstack.server_admission.RequestAdmissionMixin._require_browser_mutation`
has proven a loopback ``Host``, a same-origin ``Origin`` and the session CSRF
token. A request carrying only ``Authorization: Bearer …`` is refused
``origin_required`` and performs no read or write of any kind. That is the
whole of the manual mode's authentication, and it is sufficient: the user is
carrying bytes by hand under their own owner session, and there is no
connection to authenticate. An *automated* adapter would need its own approved,
independently authenticated connection, which this slice does not have and does
not pretend to.

**The body bound is this route's alone.** A manual batch carries up to ten
answers, each with its own bounded retrieval evidence, so the exact import path
is given the released 64 KiB Capture budget, measured on the whole raw request
rather than per item. Every other ``/api/v1/knowledge/`` route keeps its 16 KiB
bound, and each individual retrieval wire is still held to its own released
16 KiB limit inside the importer.

**No receipt is cached.** Like the two released knowledge writes, this route is
absent from ``IDEMPOTENT_POST_ROUTES`` and refuses an ``Idempotency-Key``
outright: that mechanism stores the whole response body in ``activity.json``,
and this response names the imported evidence. Retrying is done by sending the
*same* envelope for the *same* request; the ledger recognises it by the
completion digest and returns the original Capture identifiers.

Every refusal carries a closed code and, at most, the *name* of a field in a
closed schema. No title, summary, evidence reference, digest or identifier is
echoed.
"""

from __future__ import annotations

import re
from typing import Any

from .capture_retrieval import CaptureRetrievalError
from .knowledge_capture_import import ImportReceipt, import_knowledge_captures
from .knowledge_capture_packets import KnowledgeImportError
from .knowledge_ledger_document import KnowledgeLedgerError
from .knowledge_request import KnowledgeRequestError
from .knowledge_request_issuer import utc_now_rfc3339
from .knowledge_requests_http import KnowledgeRequestsHttpMixin
from .server_errors import RequestError

__all__ = (
    "IMPORT_BODY_LIMIT",
    "IMPORT_PATH",
    "KnowledgeCapturesHttpMixin",
)


IMPORT_PATH = "/api/v1/knowledge/captures/import"

# The released Capture body budget, measured on the WHOLE raw request. It is
# not a per-item allowance: ten items share it, and each item's retrieval wire
# is separately held to the released 16 KiB extension bound.
IMPORT_BODY_LIMIT = 64 * 1024

_IMPORT_MESSAGE = "the knowledge capture import was refused"

# "The state you expected is not the state that exists." The body was well
# formed and the ledger, the policy, the Task, the clock or the stored evidence
# had moved.
_CONFLICT_CODES = frozenset(
    {
        "capture_record_missing",
        "completion_digest_mismatch",
        "completion_replay_mismatch",
        "ledger_full",
        "policy_revision_changed",
        "request_expired",
        "result_limit_exceeded",
        "source_key_conflict",
        "task_binding_mismatch",
        "task_binding_required",
        "unknown_request",
        "unknown_task",
        "workspace_mismatch",
    }
)


def _refusal(error: Any) -> RequestError:
    """One closed refusal, as an HTTP envelope that echoes no submitted value."""

    status = 409 if error.code in _CONFLICT_CODES else 400
    return RequestError(error.code, _IMPORT_MESSAGE, status, dict(error.details))


def _projected_receipt(receipt: ImportReceipt) -> dict[str, Any]:
    """The bounded answer: identities, a count and the completion facts.

    The imported Capture bodies are deliberately not echoed here. A client
    reads them back through the released capture projection, which re-derives
    the retrieval state on every read rather than trusting anything this
    response said.
    """

    return {
        "data": {
            "request_id": receipt.request_id,
            "capture_ids": list(receipt.capture_ids),
            "completion_digest": receipt.completion_digest,
            "completed_at": receipt.completed_at,
        },
        "meta": {
            "replayed": receipt.replayed,
            "imported_count": len(receipt.capture_ids),
        },
    }


class KnowledgeCapturesHttpMixin(KnowledgeRequestsHttpMixin):
    """The manual knowledge Capture import surface the handler mixes in.

    It extends the released knowledge surface rather than assuming the handler
    happens to mix that in beside it: the canonical-path guard and the
    collection-store requirement are the same two decisions the policy and
    issue routes already make, and inheriting them is what keeps this route
    from growing a second copy that could drift.
    """

    def _post_knowledge_capture_import(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        # The route is the exact path, not a spelling that resolves to it, and
        # the refusal lands before any Store transaction is opened. The helper
        # and the backend check are the released knowledge surface's own.
        self._require_canonical_knowledge_path(IMPORT_PATH)
        self._refuse_unsupported_idempotency_key(
            "the knowledge capture import route replays by request and digest"
        )
        store = self._knowledge_store()
        clock = getattr(self.server, "knowledge_clock", utc_now_rfc3339)
        try:
            receipt = import_knowledge_captures(store, body, now=clock())
        except (
            CaptureRetrievalError,
            KnowledgeImportError,
            KnowledgeLedgerError,
            KnowledgeRequestError,
        ) as error:
            raise _refusal(error) from error
        self.send_json(_projected_receipt(receipt))
