import { z } from 'zod'
import { sha256 } from './schemaPrimitives'
import type { CaptureRetrievalProjection } from './types'

export const KNOWLEDGE_IMPORT_SCHEMA = 'workstack.knowledge-import.v1'
export const CAPTURE_RETRIEVAL_SCHEMA = 'workstack.capture-retrieval.v1.1'
export const MAX_KNOWLEDGE_IMPORT_BYTES = 64 * 1024
export const MAX_RETRIEVAL_BYTES = 16 * 1024
export const MAX_IMPORT_ITEMS = 10
export const MAX_EVIDENCE_ITEMS = 10
export const MAX_TITLE_CHARS = 500
export const MAX_SUMMARY_CHARS = 2000
export const MAX_CONTEXT_CHARS = 4000
export const MAX_ACTION_DETAIL_CHARS = 4000
export const MAX_ACTION_ITEMS = 20
export const MAX_TAGS = 50
export const MAX_TAG_CHARS = 100
export const KNOWLEDGE_IMPORT_PROVIDER = 'manual'
export const KNOWLEDGE_IMPORT_RESOURCE_TYPE = 'knowledge.answer'
export const KNOWLEDGE_IMPORT_ADAPTER = 'workstack.knowledge-import'
export const NIL_UUID = '00000000-0000-0000-0000-000000000000'
export const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const QUERY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~:-]{0,127}$/
export const OPAQUE_REF_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~-]*$/

export const canonicalUuidSchema = z.string().regex(CANONICAL_UUID).refine(
  (value) => value !== NIL_UUID,
  'invalid_uuid',
)

const retrievalSourceTypeSchema = z.enum(['notion.page', 'nas.file', 'knowledge.answer'])

const retrievalOriginSchema = z.object({
  document_ref: z.string().min(8).max(256).regex(OPAQUE_REF_PATTERN),
  source_type: retrievalSourceTypeSchema,
}).strict()

const projectionEvidenceSchema = z.object({
  reported_source_type: retrievalSourceTypeSchema,
  title: z.string().min(1).max(MAX_TITLE_CHARS),
  document_ref: z.string().min(8).max(256).regex(OPAQUE_REF_PATTERN),
  chunk_ref: z.string().min(8).max(256).regex(OPAQUE_REF_PATTERN).nullable(),
  reported_source_version: z.string().min(8).max(256).regex(OPAQUE_REF_PATTERN).nullable(),
  version_state: z.enum([
    'unreported',
    'reported_unverified',
    'verified_current',
    'verified_stale',
  ]),
  indexed_digest: sha256.nullable(),
  web_url: z.null(),
}).strict()

function refineListedKnowledgeRetrieval(
  retrieval: CaptureRetrievalProjection,
  context: z.RefinementCtx,
) {
  // stored_retrieval_projection(..., verification=None): origin stays null,
  // capture_source_type stays knowledge.answer, and currentness is never verified.
  if (retrieval.origin !== null) {
    context.addIssue({
      code: 'custom',
      path: ['origin'],
      message: 'Listed knowledge retrieval cannot carry a trusted origin.',
    })
  }
  if (retrieval.capture_source_type !== 'knowledge.answer') {
    context.addIssue({
      code: 'custom',
      path: ['capture_source_type'],
      message: 'Unattested knowledge retrieval is recorded as knowledge.answer.',
    })
  }
  if (retrieval.answer_scope === 'synthesized') {
    if (retrieval.origin_state !== 'synthesized' || retrieval.reported_origin !== null) {
      context.addIssue({
        code: 'custom',
        path: ['origin_state'],
        message: 'Synthesized knowledge retrieval has no origin.',
      })
    }
  } else if (retrieval.origin_state !== 'reported_unverified' || retrieval.reported_origin === null) {
    context.addIssue({
      code: 'custom',
      path: ['origin_state'],
      message: 'Single-source knowledge retrieval stays reported and unverified.',
    })
  } else if (
    retrieval.reported_origin.document_ref !== retrieval.evidence[0]?.document_ref
    || retrieval.reported_origin.source_type !== retrieval.evidence[0]?.reported_source_type
  ) {
    context.addIssue({
      code: 'custom',
      path: ['reported_origin'],
      message: 'Reported origin must match the single evidence document.',
    })
  }
  retrieval.evidence.forEach((item, index) => {
    const expected = item.reported_source_version == null ? 'unreported' : 'reported_unverified'
    if (item.version_state !== expected) {
      context.addIssue({
        code: 'custom',
        path: ['evidence', index, 'version_state'],
        message: 'Listed knowledge evidence cannot claim verified currentness.',
      })
    }
  })
}

/** Read-only sanitized projection. Wire retrieval (source_type, no origin) must not parse here. */
export const captureRetrievalProjectionSchema: z.ZodType<CaptureRetrievalProjection> = z.object({
  schema: z.literal(CAPTURE_RETRIEVAL_SCHEMA),
  capture_schema_version: z.literal('1.1'),
  request_id: canonicalUuidSchema,
  query_id: z.string().min(1).max(128).regex(QUERY_ID_PATTERN),
  answer_scope: z.enum(['single_source', 'synthesized']),
  confidence: z.object({
    level: z.enum(['low', 'medium', 'high']),
    score: z.number().finite().gte(0).lte(1),
  }).strict(),
  evidence: z.array(projectionEvidenceSchema).min(1).max(MAX_EVIDENCE_ITEMS),
  truncated: z.boolean(),
  reported_origin: retrievalOriginSchema.nullable(),
  origin: retrievalOriginSchema.nullable(),
  origin_state: z.enum(['synthesized', 'reported_unverified', 'verified']),
  capture_source_type: retrievalSourceTypeSchema,
}).strict().superRefine(refineListedKnowledgeRetrieval)

export const knowledgeCaptureImportDataSchema = z.object({
  request_id: canonicalUuidSchema,
  capture_ids: z.array(z.string().min(1).max(64)).min(1).max(MAX_IMPORT_ITEMS),
  completion_digest: sha256,
  completed_at: z.string().min(1),
}).strict()

export const knowledgeCaptureImportMetaSchema = z.object({
  replayed: z.boolean(),
  imported_count: z.number().int().min(0).max(MAX_IMPORT_ITEMS),
}).strict()

export type KnowledgeCaptureImportData = z.infer<typeof knowledgeCaptureImportDataSchema>
export type KnowledgeCaptureImportMeta = z.infer<typeof knowledgeCaptureImportMetaSchema>

export interface KnowledgeCaptureImportOutcome {
  data: KnowledgeCaptureImportData
  meta: KnowledgeCaptureImportMeta
}

export function knowledgeImportOutcomeMatchesEnvelope(
  envelope: { request_id: string; items: readonly unknown[] },
  outcome: KnowledgeCaptureImportOutcome,
): boolean {
  const ids = outcome.data.capture_ids
  return outcome.data.request_id === envelope.request_id
    && new Set(ids).size === ids.length
    && ids.length === outcome.meta.imported_count
    && ids.length === envelope.items.length
}

export const KNOWLEDGE_CAPTURE_IMPORT_MESSAGES: Record<string, string> = {
  import_too_large: 'The knowledge result exceeds the 64 KiB limit.',
  retrieval_too_large: 'A retrieval extension exceeds the 16 KiB limit.',
  invalid_json: 'This knowledge result is not valid JSON.',
  unknown_field: 'This knowledge result includes a field that is not allowed.',
  missing_field: 'This knowledge result is missing a required field.',
  forbidden_field: 'This knowledge result includes a forbidden content field.',
  invalid_uuid: 'This knowledge result has an invalid request or item identity.',
  invalid_text: 'This knowledge result includes unsafe display text.',
  invalid_request: 'This knowledge result is not a closed knowledge-import envelope.',
  unsupported_schema: 'This knowledge result uses an unsupported schema.',
  source_location_suspected: 'A title reads as a source path or URL and was refused.',
  credential_material_suspected: 'Credential-shaped material is not allowed in a knowledge result.',
  raw_content_suspected: 'Raw content is not allowed in a knowledge result.',
  invalid_ref: 'An evidence handle is not an opaque reference.',
  invalid_digest: 'An indexed digest is not a SHA-256 digest.',
  invalid_source_type: 'A reported source type is not allowed.',
  invalid_evidence: 'The evidence list is not a closed 1–10 item set.',
  duplicate_evidence: 'Two evidence items share the same document and chunk handles.',
  duplicate_item_id: 'Two import items share the same item identity.',
  invalid_answer_scope: 'The answer scope is not allowed.',
  answer_scope_mismatch: 'The answer scope does not match the evidence.',
  invalid_confidence: 'The retrieval score is not a finite 0–1 ranking score.',
  invalid_truncated: 'The truncated flag must be a JSON boolean.',
  request_id_mismatch: 'A retrieval request identity does not match the envelope.',
  query_id_not_distinct: 'The engine query identity must not copy the request identity.',
  web_url_not_allowed: 'Evidence must not carry a source URL.',
  invalid_item: 'An import item is not a closed knowledge-import item.',
  origin_required: 'This import must be made from the signed-in Work Stack session.',
  unsupported_idempotency_key: 'This import does not accept an idempotency key. Retry the same envelope.',
  knowledge_backend_unsupported: 'This workspace backend cannot import knowledge captures.',
  request_expired: 'This knowledge request has expired. Issue a new search; do not reuse this envelope.',
  policy_revision_changed: 'Knowledge policy changed. Issue a new search; this envelope cannot be overwritten.',
  unknown_request: 'This knowledge request is not in the current ledger. Issue a new search.',
  workspace_mismatch: 'This knowledge request belongs to a different workspace. Issue a new search.',
  task_binding_mismatch: 'This knowledge request is bound to a different Task revision. Issue a new search.',
  task_binding_required: 'This knowledge request requires a Task binding. Issue a new search.',
  unknown_task: 'This knowledge request names a Task that is not in this workspace. Issue a new search.',
  completion_digest_mismatch: 'This envelope does not match the completed import. Issue a new search.',
  completion_replay_mismatch: 'This retry does not match the completed import. Issue a new search.',
  source_key_conflict: 'An imported item collides with an existing capture. Issue a new search.',
  ledger_full: 'The knowledge ledger cannot accept another import right now.',
  result_limit_exceeded: 'This envelope has more items than the request allowed. Issue a new search.',
  capture_record_missing: 'A completed import could not be replayed because a capture is missing. Issue a new search.',
  import_pending: 'The import outcome may still be pending. Retry the same envelope. Do not issue a new search yet.',
  import_refused: 'The knowledge capture import was refused.',
}

export const DEFINITIVE_NEW_SEARCH_CODES = new Set([
  'request_expired',
  'policy_revision_changed',
  'unknown_request',
  'workspace_mismatch',
  'task_binding_mismatch',
  'task_binding_required',
  'unknown_task',
  'completion_digest_mismatch',
  'completion_replay_mismatch',
  'source_key_conflict',
  'ledger_full',
  'result_limit_exceeded',
  'capture_record_missing',
  'knowledge_backend_unsupported',
])

export function describeKnowledgeCaptureImportError(code: string): string {
  const fallback = KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.import_refused
  const message = Object.hasOwn(KNOWLEDGE_CAPTURE_IMPORT_MESSAGES, code)
    ? KNOWLEDGE_CAPTURE_IMPORT_MESSAGES[code]
    : undefined
  // Own-property admission only: constructor/toString/__proto__ must not replace import_refused.
  return typeof message === 'string' ? message : fallback
}

export class KnowledgeCaptureImportError extends Error {
  readonly code: string

  constructor(code: string) {
    super(describeKnowledgeCaptureImportError(code))
    this.name = 'KnowledgeCaptureImportError'
    this.code = code
  }
}
