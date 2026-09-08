import { z } from 'zod'

export const KNOWLEDGE_SCHEMA_VERSION = 1 as const
export const MAX_KNOWLEDGE_REQUEST_BYTES = 16 * 1024
export const MAX_KNOWLEDGE_RESPONSE_BYTES = 128 * 1024
export const MAX_KNOWLEDGE_REASON_CHARS = 500
export const MAX_KNOWLEDGE_EXCERPT_CHARS = 6000
export const MAX_KNOWLEDGE_EXCERPT_LINES = 80
export const DEFAULT_KNOWLEDGE_START_LINE = 1
export const DEFAULT_KNOWLEDGE_END_LINE = 40
export const MAX_KNOWLEDGE_VAULTS = 128
export const MAX_KNOWLEDGE_REFERENCES = 256
export const MAX_KNOWLEDGE_SEARCH_QUERY_CHARS = 1000
export const MAX_KNOWLEDGE_SEARCH_MATCHES = 5
export const KNOWLEDGE_SEARCH_TIMEOUT_MS = 90_000

const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SHA256_SHAPE = /^[0-9a-f]{64}$/
const VAULT_ID_SHAPE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/
const CLOSED_CODE_SHAPE = /^[a-z0-9_]{1,64}$/

export const KNOWLEDGE_OPERATIONS = [
  'status',
  'choose-vault',
  'list-references',
  'read-reference',
  'pin-reference',
  'unpin-reference',
  'search-references',
] as const

export type KnowledgeOperation = (typeof KNOWLEDGE_OPERATIONS)[number]

function withoutControls(value: string) {
  return !/[\0-\x08\x0b\x0c\x0e-\x1f]/.test(value)
}

export const knowledgeUuidSchema = z.string()
  .regex(UUID_SHAPE)
  .refine((value) => value !== NIL_UUID, 'the nil UUID is not a knowledge identity')

export const knowledgeSha256Schema = z.string().regex(SHA256_SHAPE)
export const knowledgeVaultIdSchema = z.string().regex(VAULT_ID_SHAPE)
export const knowledgeReasonSchema = z.string().min(1).max(MAX_KNOWLEDGE_REASON_CHARS)
  .refine(withoutControls, 'Control characters are not allowed')

export const knowledgeSearchQuerySchema = z.string()
  .min(1)
  .max(MAX_KNOWLEDGE_SEARCH_QUERY_CHARS)
  .refine(withoutControls, 'invalid_search_query')
  .refine((value) => value === value.trim(), 'invalid_search_query')

export const knowledgeDocumentPathSchema = z.string().min(1).max(1024)
  .refine((value) => withoutControls(value) && !value.includes('\\') && !/[<>:"|?*]/.test(value), 'invalid_document_path')
  .refine((value) => !value.startsWith('/') && !/^[a-zA-Z]:/.test(value), 'invalid_document_path')
  .refine((value) => value.toLowerCase().endsWith('.md'), 'markdown_required')

export const knowledgeVaultLabelSchema = z.string().min(1).max(255)
  .refine((value) => withoutControls(value) && !/[\\/]/.test(value), 'Vault labels are basename only')

export const knowledgeBindingSchema = z.object({
  workspace_uid: knowledgeUuidSchema,
  task_uid: knowledgeUuidSchema,
  task_id: z.string().min(1).max(128).refine(withoutControls, 'invalid_task_identity'),
  task_revision: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
}).strict()

export const knowledgeVaultSchema = z.object({
  vault_id: knowledgeVaultIdSchema,
  label: knowledgeVaultLabelSchema,
}).strict()

export const knowledgeSavedReferenceSchema = z.object({
  reference_id: knowledgeUuidSchema,
  vault_id: knowledgeVaultIdSchema,
  document_path: knowledgeDocumentPathSchema,
  start_line: z.number().int().min(1),
  end_line: z.number().int().min(1),
  source_sha256: knowledgeSha256Schema,
  reason: knowledgeReasonSchema,
}).strict().superRefine(refineLineSpan)

export const knowledgeReadReferenceSchema = z.object({
  schema_version: z.literal(1),
  provider: z.literal('markdown-vault'),
  vault_id: knowledgeVaultIdSchema,
  document_path: knowledgeDocumentPathSchema,
  title: z.string().min(1).max(240).refine(withoutControls),
  source_sha256: knowledgeSha256Schema,
  expected_sha256: knowledgeSha256Schema.nullable(),
  freshness: z.enum(['uncompared', 'unchanged', 'changed']),
  start_line: z.number().int().min(1),
  end_line: z.number().int().min(1),
  excerpt: z.string().max(MAX_KNOWLEDGE_EXCERPT_CHARS).refine((value) => !value.includes('\0')),
  excerpt_truncated: z.boolean(),
  trust: z.literal('external_reference'),
  read_only: z.literal(true),
}).strict().superRefine(refineLineSpan)

const requestEnvelope = {
  type: z.literal('workstack-knowledge-request'),
  schema_version: z.literal(KNOWLEDGE_SCHEMA_VERSION),
  request_id: knowledgeUuidSchema,
}

const responseEnvelope = {
  type: z.literal('workstack-knowledge-response'),
  schema_version: z.literal(KNOWLEDGE_SCHEMA_VERSION),
  request_id: knowledgeUuidSchema,
}

function refineLineSpan(
  value: { start_line: number; end_line: number },
  context: z.RefinementCtx,
) {
  if (value.end_line < value.start_line || value.end_line - value.start_line >= MAX_KNOWLEDGE_EXCERPT_LINES) {
    context.addIssue({ code: 'custom', message: 'invalid_line_range', path: ['end_line'] })
  }
}

export const knowledgeHostRequestSchema = z.discriminatedUnion('operation', [
  z.object({ ...requestEnvelope, operation: z.literal('status') }).strict(),
  z.object({ ...requestEnvelope, operation: z.literal('choose-vault') }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('list-references'),
    binding: knowledgeBindingSchema,
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('read-reference'),
    binding: knowledgeBindingSchema,
    vault_id: knowledgeVaultIdSchema,
    document_path: knowledgeDocumentPathSchema,
    start_line: z.number().int().min(1),
    end_line: z.number().int().min(1),
    expected_sha256: knowledgeSha256Schema.nullable().optional(),
  }).strict().superRefine(refineLineSpan),
  z.object({
    ...requestEnvelope,
    operation: z.literal('pin-reference'),
    binding: knowledgeBindingSchema,
    vault_id: knowledgeVaultIdSchema,
    document_path: knowledgeDocumentPathSchema,
    start_line: z.number().int().min(1),
    end_line: z.number().int().min(1),
    expected_sha256: knowledgeSha256Schema,
    reason: knowledgeReasonSchema,
  }).strict().superRefine(refineLineSpan),
  z.object({
    ...requestEnvelope,
    operation: z.literal('unpin-reference'),
    binding: knowledgeBindingSchema,
    reference_id: knowledgeUuidSchema,
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('search-references'),
    binding: knowledgeBindingSchema,
    vault_id: knowledgeVaultIdSchema,
    query: knowledgeSearchQuerySchema,
  }).strict(),
])

export const knowledgeStatusDataSchema = z.object({
  vaults: z.array(knowledgeVaultSchema).max(MAX_KNOWLEDGE_VAULTS),
  local_only: z.literal(true),
}).strict()

export const knowledgeChooseVaultDataSchema = z.discriminatedUnion('cancelled', [
  z.object({ cancelled: z.literal(true) }).strict(),
  z.object({ cancelled: z.literal(false), vault: knowledgeVaultSchema }).strict(),
])

export const knowledgeListDataSchema = z.object({
  binding: knowledgeBindingSchema,
  references: z.array(knowledgeSavedReferenceSchema).max(MAX_KNOWLEDGE_REFERENCES),
  local_only: z.literal(true),
}).strict()

export const knowledgeReadDataSchema = z.object({
  binding: knowledgeBindingSchema,
  reference: knowledgeReadReferenceSchema,
}).strict()

export const knowledgePinDataSchema = z.object({
  binding: knowledgeBindingSchema,
  reference: knowledgeSavedReferenceSchema,
  local_only: z.literal(true),
}).strict()

export const knowledgeUnpinDataSchema = z.object({
  binding: knowledgeBindingSchema,
  removed: z.literal(true),
  local_only: z.literal(true),
}).strict()

export const knowledgeSearchCorpusSchema = z.object({
  label: z.string().min(1).max(255).refine(withoutControls),
  document_count: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
  indexed_at: z.string().datetime({ offset: true }),
}).strict()

export const knowledgeSearchDataSchema = z.object({
  binding: knowledgeBindingSchema,
  query: knowledgeSearchQuerySchema,
  corpus: knowledgeSearchCorpusSchema,
  matches: z.array(knowledgeReadReferenceSchema).max(MAX_KNOWLEDGE_SEARCH_MATCHES),
  omitted_count: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
}).strict()

const knowledgeSuccessSchema = z.discriminatedUnion('operation', [
  z.object({
    ...responseEnvelope,
    operation: z.literal('status'),
    ok: z.literal(true),
    data: knowledgeStatusDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('choose-vault'),
    ok: z.literal(true),
    data: knowledgeChooseVaultDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('list-references'),
    ok: z.literal(true),
    data: knowledgeListDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('read-reference'),
    ok: z.literal(true),
    data: knowledgeReadDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('pin-reference'),
    ok: z.literal(true),
    data: knowledgePinDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('unpin-reference'),
    ok: z.literal(true),
    data: knowledgeUnpinDataSchema,
  }).strict(),
  z.object({
    ...responseEnvelope,
    operation: z.literal('search-references'),
    ok: z.literal(true),
    data: knowledgeSearchDataSchema,
  }).strict(),
])

export const knowledgeErrorResponseSchema = z.object({
  ...responseEnvelope,
  operation: z.enum(KNOWLEDGE_OPERATIONS),
  ok: z.literal(false),
  error: z.object({
    code: z.string().regex(CLOSED_CODE_SHAPE),
    message: z.string().min(1).max(256).refine(withoutControls),
  }).strict(),
}).strict()

export const knowledgeHostResponseSchema = z.union([
  knowledgeSuccessSchema,
  knowledgeErrorResponseSchema,
])

export type KnowledgeBinding = z.infer<typeof knowledgeBindingSchema>
export type KnowledgeVault = z.infer<typeof knowledgeVaultSchema>
export type KnowledgeSavedReference = z.infer<typeof knowledgeSavedReferenceSchema>
export type KnowledgeReadReference = z.infer<typeof knowledgeReadReferenceSchema>
export type KnowledgeHostRequest = z.infer<typeof knowledgeHostRequestSchema>
export type KnowledgeHostResponse = z.infer<typeof knowledgeHostResponseSchema>
export type KnowledgeStatusData = z.infer<typeof knowledgeStatusDataSchema>
export type KnowledgeChooseVaultData = z.infer<typeof knowledgeChooseVaultDataSchema>
export type KnowledgeListData = z.infer<typeof knowledgeListDataSchema>
export type KnowledgeReadData = z.infer<typeof knowledgeReadDataSchema>
export type KnowledgePinData = z.infer<typeof knowledgePinDataSchema>
export type KnowledgeUnpinData = z.infer<typeof knowledgeUnpinDataSchema>
export type KnowledgeSearchCorpus = z.infer<typeof knowledgeSearchCorpusSchema>
export type KnowledgeSearchData = z.infer<typeof knowledgeSearchDataSchema>

export interface KnowledgeDataByOperation {
  status: KnowledgeStatusData
  'choose-vault': KnowledgeChooseVaultData
  'list-references': KnowledgeListData
  'read-reference': KnowledgeReadData
  'pin-reference': KnowledgePinData
  'unpin-reference': KnowledgeUnpinData
  'search-references': KnowledgeSearchData
}

export interface KnowledgeTaskRef {
  id: string
  uid: string
  revision: number
  title: string
}

export function sameKnowledgeBinding(left: KnowledgeBinding, right: KnowledgeBinding) {
  return left.workspace_uid === right.workspace_uid
    && left.task_uid === right.task_uid
    && left.task_id === right.task_id
    && left.task_revision === right.task_revision
}

export function knowledgeBindingFor(workspaceUid: string, task: KnowledgeTaskRef): KnowledgeBinding {
  return {
    workspace_uid: workspaceUid,
    task_uid: task.uid,
    task_id: task.id,
    task_revision: task.revision,
  }
}

export function peekKnowledgeRequestId(value: unknown) {
  if (!value || typeof value !== 'object' || !('request_id' in value)) return null
  const requestId = (value as { request_id?: unknown }).request_id
  return typeof requestId === 'string' ? requestId : null
}
