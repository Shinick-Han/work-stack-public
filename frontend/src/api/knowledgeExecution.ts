import { z } from 'zod'
import { ApiError, UnreadableSuccessError, getCsrfToken, mutateData, type MutateReceipt } from './transport'
import type { IssuedKnowledgeRequestWire } from './knowledge'

/**
 * Owner execute route for an already-issued KnowledgeRequest.
 *
 * The body is the server-issued document itself. This module does not mint identity,
 * does not add command/config fields, and does not attach an Idempotency-Key (the
 * route refuses one). Success `data` is untrusted JSON: the inbox feature validates it
 * with the existing import parser rather than widening this API layer.
 */

export const KNOWLEDGE_EXECUTE_PATH = '/api/v1/knowledge/requests/execute'

export const knowledgeExecuteMetaSchema = z.strictObject({
  outcome: z.literal('proposal_ready'),
})

const GENERIC_UNKNOWN =
  'The search outcome is not known. Check the connector, or paste an existing result if you have one. This request is not sent again.'

const EXECUTION_COPY: Record<string, string> = {
  knowledge_driver_unavailable:
    'No search connector is configured on this server. The default server does not run one. If you already have a result, paste it into Import.',
  driver_binding_mismatch:
    'This request does not match the connector bound for its connection. It was not run. If you already have a result, paste it into Import.',
  knowledge_driver_binding_mismatch:
    'This request does not match the connector bound for its connection. It was not run. If you already have a result, paste it into Import.',
  request_not_registered:
    'This request is not registered to run on this server. A restarted server does not re-run earlier requests. If you already have a result, paste it into Import.',
  request_already_attempted:
    'This request was already submitted for a connected search. It is not run again. Check the connector, or paste an existing result if you have one.',
  driver_not_started:
    'The search connector did not start. Whether a result exists is not known from here. Check the connector, or paste an existing result if you have one. This request is not sent again.',
  driver_outcome_unknown:
    'The search connector did not return a usable result. The outcome is unknown. Check the connector, or paste an existing result if you have one. This request is not sent again.',
  request_expired:
    'This request is no longer current. The result is not offered for review. If you already have a result, paste it into Import.',
  unknown_request:
    'This request is not in the current ledger. If you already have a result, paste it into Import.',
  policy_revision_changed:
    'The connection policy is no longer the one this request was issued against. The result is not offered for review. If you already have a result, paste it into Import.',
  workspace_mismatch:
    'This request belongs to a different workspace. If you already have a result, paste it into Import.',
  task_binding_mismatch:
    'This request is bound to a different Task revision. If you already have a result, paste it into Import.',
  task_binding_required:
    'This request requires a Task binding. If you already have a result, paste it into Import.',
  unknown_task:
    'This request names a Task that is not in this workspace. If you already have a result, paste it into Import.',
  request_digest_mismatch:
    'This request no longer matches what the server holds. If you already have a result, paste it into Import.',
  knowledge_backend_unsupported:
    'This workspace cannot run a connected search. If you already have a result, paste it into Import.',
  invalid_csrf:
    'This browser session is no longer recognised. Reload Work Stack. This request is not sent again. If you already have a result, paste it into Import.',
  origin_required:
    'This browser session is no longer recognised. Reload Work Stack. This request is not sent again. If you already have a result, paste it into Import.',
}

export function describeKnowledgeExecutionFailure(error: unknown): string {
  if (error instanceof ApiError) {
    const message = Object.hasOwn(EXECUTION_COPY, error.code) ? EXECUTION_COPY[error.code] : undefined
    // Own-property + string admission: constructor/toString/__proto__ must not replace GENERIC_UNKNOWN.
    return typeof message === 'string' ? message : GENERIC_UNKNOWN
  }
  return GENERIC_UNKNOWN
}

/**
 * POST the issued KnowledgeRequest once. Callers must lock before invoking this so a
 * double click cannot overlap CSRF + POST. There is no automatic resend: not on 403,
 * transport loss, malformed success, or timeout.
 */
export async function executeKnowledgeRequest(request: IssuedKnowledgeRequestWire): Promise<unknown> {
  await getCsrfToken(true)
  const receipt: MutateReceipt = {}
  const data = await mutateData(
    KNOWLEDGE_EXECUTE_PATH,
    'POST',
    request,
    z.unknown(),
    undefined,
    false,
    { receipt, singleAttempt: true },
  )
  try {
    knowledgeExecuteMetaSchema.parse(receipt.meta ?? {})
  } catch {
    throw new UnreadableSuccessError(receipt.status ?? 200)
  }
  return data
}
