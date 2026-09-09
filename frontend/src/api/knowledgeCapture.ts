import { ZodError } from 'zod'
import { ApiError, UnreadableSuccessError, getCsrfToken, mutateData, type MutateReceipt } from './transport'
import {
  DEFINITIVE_NEW_SEARCH_CODES,
  KnowledgeCaptureImportError,
  describeKnowledgeCaptureImportError,
  knowledgeCaptureImportDataSchema,
  knowledgeCaptureImportMetaSchema,
  knowledgeImportOutcomeMatchesEnvelope,
  type KnowledgeCaptureImportOutcome,
} from '../domain/schemaKnowledgeCapture'
import type { KnowledgeImportEnvelope } from '../domain/knowledgeImport'

export type KnowledgeCaptureImportFailureKind = 'pending_unknown' | 'definitive' | 'refused'

export function classifyKnowledgeCaptureImportFailure(error: unknown): {
  kind: KnowledgeCaptureImportFailureKind
  code: string
  message: string
} {
  if (error instanceof KnowledgeCaptureImportError) {
    return { kind: 'refused', code: error.code, message: error.message }
  }
  if (error instanceof UnreadableSuccessError || error instanceof ZodError) {
    return {
      kind: 'pending_unknown',
      code: 'import_pending',
      message: describeKnowledgeCaptureImportError('import_pending'),
    }
  }
  if (error instanceof ApiError) {
    const kind = DEFINITIVE_NEW_SEARCH_CODES.has(error.code) ? 'definitive' : 'refused'
    return {
      kind,
      code: error.code,
      message: describeKnowledgeCaptureImportError(error.code),
    }
  }
  return {
    kind: 'pending_unknown',
    code: 'import_pending',
    message: describeKnowledgeCaptureImportError('import_pending'),
  }
}

export const KNOWLEDGE_CAPTURE_IMPORT_PATH = '/api/v1/knowledge/captures/import'

/**
 * POST the frozen knowledge-import envelope.
 *
 * No Idempotency-Key: the route refuses one. Replay is the same request_id plus
 * the same admitted body. This is not a planning mutation, so the planning bus
 * is not published; the parent invalidates caches only after a successful parse.
 */
export async function importKnowledgeCaptures(
  envelope: KnowledgeImportEnvelope,
): Promise<KnowledgeCaptureImportOutcome> {
  // Read-only session refresh so an explicit retry can pick up rotated CSRF.
  // The mutation itself is single-attempt: one POST per click, no auto-resend.
  await getCsrfToken(true)
  const receipt: MutateReceipt = {}
  const data = await mutateData(
    KNOWLEDGE_CAPTURE_IMPORT_PATH,
    'POST',
    envelope,
    knowledgeCaptureImportDataSchema,
    undefined,
    false,
    { receipt, singleAttempt: true },
  )
  const outcome = {
    data,
    meta: knowledgeCaptureImportMetaSchema.parse(receipt.meta ?? {}),
  }
  if (!knowledgeImportOutcomeMatchesEnvelope(envelope, outcome)) {
    throw new UnreadableSuccessError(receipt.status ?? 200)
  }
  return outcome
}
