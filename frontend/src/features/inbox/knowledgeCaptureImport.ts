import {
  CANONICAL_UUID,
  CAPTURE_RETRIEVAL_SCHEMA,
  KNOWLEDGE_IMPORT_SCHEMA,
  KnowledgeCaptureImportError,
  MAX_CONTEXT_CHARS,
  MAX_EVIDENCE_ITEMS,
  MAX_ACTION_DETAIL_CHARS,
  MAX_ACTION_ITEMS,
  MAX_IMPORT_ITEMS,
  MAX_KNOWLEDGE_IMPORT_BYTES,
  MAX_RETRIEVAL_BYTES,
  MAX_SUMMARY_CHARS,
  MAX_TAG_CHARS,
  MAX_TAGS,
  MAX_TITLE_CHARS,
  NIL_UUID,
  OPAQUE_REF_PATTERN,
  QUERY_ID_PATTERN,
} from '../../domain/schemaKnowledgeCapture'
import {
  credentialValue,
  decodeForValidation,
  htmlTag,
  recipientAssignment,
  sha256,
  unsafeRawCanary,
} from '../../domain/schemaPrimitives'
import type { RetrievalSourceType } from '../../domain/types'
import type {
  KnowledgeImportAction,
  KnowledgeImportEnvelope,
  KnowledgeImportItem,
  KnowledgeImportNormalized,
  KnowledgeImportRetrievalWire,
  KnowledgeImportWireEvidence,
} from '../../domain/knowledgeImport'

export type {
  KnowledgeImportAction,
  KnowledgeImportEnvelope,
  KnowledgeImportItem,
  KnowledgeImportNormalized,
  KnowledgeImportRetrievalWire,
  KnowledgeImportWireEvidence,
}

const utf8 = new TextEncoder()

const ENVELOPE_KEYS = ['schema', 'request_id', 'items'] as const
const ITEM_KEYS = ['item_id', 'title', 'normalized', 'retrieval'] as const
const NORMALIZED_REQUIRED = ['summary', 'context', 'action_items'] as const
const ACTION_REQUIRED = ['title', 'priority'] as const
const ACTION_OPTIONAL = ['detail', 'due'] as const
const RETRIEVAL_KEYS = [
  'schema',
  'capture_schema_version',
  'request_id',
  'query_id',
  'answer_scope',
  'confidence',
  'evidence',
  'truncated',
] as const
const CONFIDENCE_KEYS = ['level', 'score'] as const
const EVIDENCE_REQUIRED = ['source_type', 'title', 'document_ref'] as const
const EVIDENCE_OPTIONAL = ['chunk_ref', 'source_version', 'indexed_digest', 'web_url'] as const
const SOURCE_TYPES = new Set<RetrievalSourceType>(['notion.page', 'nas.file', 'knowledge.answer'])
const ANSWER_SCOPES = new Set(['single_source', 'synthesized'])
const CONFIDENCE_LEVELS = new Set(['low', 'medium', 'high'])
const PRIORITIES = new Set(['P0', 'P1', 'P2', 'P3'])

const FORBIDDEN_KEYS = new Set([
  'body', 'html', 'content', 'attachments', 'raw', 'transcript', 'recipients',
  'text', 'snippet', 'excerpt', 'passage', 'chunk_text',
  'path', 'file_path', 'filepath', 'source_path', 'share', 'unc_path',
  'url', 'uri', 'href', 'link',
  'token', 'credential', 'credentials', 'secret', 'password', 'api_key',
  'query_raw', 'raw_query', 'metadata', 'properties', 'extra', 'fields',
])

const TITLE_LOCATION_RE = /(?:^\s*[~.]{0,2}\/)|\\|\/\/|(?<![A-Za-z0-9+.-])[A-Za-z]:(?=\S)|(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]+:(?=\S)/i
const FILE_LIKE_SEGMENT_RE = /^[^\s/\\.][^/\\]*\.[A-Za-z0-9]{1,10}$/i
const EMAIL_RE = /[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/
const QUOTED_REPLY_RE = /^>/m

export interface KnowledgeImportEvidencePreview {
  title: string
  reportedSourceType: RetrievalSourceType
  versionState: 'unreported' | 'reported_unverified'
}

export interface KnowledgeImportItemPreview {
  itemId: string
  title: string
  summary: string
  evidenceCount: number
  truncated: boolean
  retrievalScore: number
  retrievalLevel: 'low' | 'medium' | 'high'
  evidence: KnowledgeImportEvidencePreview[]
}

export interface KnowledgeImportPreview {
  requestId: string
  itemCount: number
  items: KnowledgeImportItemPreview[]
}

function fail(code: string): never {
  throw new KnowledgeCaptureImportError(code)
}

export function utf8ByteLength(text: string): number {
  return utf8.encode(text).byteLength
}

function rejectForbidden(value: unknown, depth = 0): void {
  if (depth > 16) fail('invalid_request')
  if (Array.isArray(value)) {
    for (const child of value) rejectForbidden(child, depth + 1)
    return
  }
  if (!value || typeof value !== 'object') return
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_KEYS.has(key.toLowerCase())) fail('forbidden_field')
    rejectForbidden(child, depth + 1)
  }
}

function asObject(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('invalid_request')
  const record = value as Record<string, unknown>
  const allowed = new Set([...required, ...optional])
  for (const key of Object.keys(record)) {
    if (!allowed.has(key)) fail('unknown_field')
  }
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(record, key)) fail('missing_field')
  }
  return record
}

function canonicalUuid(value: unknown): string {
  if (typeof value !== 'string' || !CANONICAL_UUID.test(value) || value === NIL_UUID) fail('invalid_uuid')
  return value
}

function isSourceLocation(decoded: string): boolean {
  if (TITLE_LOCATION_RE.test(decoded)) return true
  for (const token of decoded.match(/\S+(?:\/\S+)+/g) ?? []) {
    const last = token.slice(token.lastIndexOf('/') + 1)
    if (FILE_LIKE_SEGMENT_RE.test(last)) return true
  }
  let tight = 0
  for (let index = 0; index < decoded.length; index += 1) {
    if (decoded[index] !== '/') continue
    const before = index ? decoded[index - 1] : ''
    const after = decoded[index + 1] ?? ''
    if (before && after && before !== ' ' && after !== ' ' && !/\s/.test(before) && !/\s/.test(after)) {
      tight += 1
    }
  }
  return tight > 1
}

function safeTitle(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > MAX_TITLE_CHARS) fail('invalid_text')
  const decoded = decodeForValidation(value)
  if (decoded === null) fail('invalid_text')
  if (credentialValue.test(decoded)) fail('credential_material_suspected')
  if (/[\u0000-\u001F\u007F-\u009F]/.test(decoded)) fail('invalid_text')
  if (EMAIL_RE.test(decoded) || htmlTag.test(decoded) || QUOTED_REPLY_RE.test(decoded) || unsafeRawCanary.test(decoded)) {
    fail('raw_content_suspected')
  }
  if (isSourceLocation(decoded)) fail('source_location_suspected')
  return value
}

function safeDisplayText(value: unknown, maximum: number, required: boolean): string {
  if (typeof value !== 'string') fail('invalid_text')
  if (required && !value) fail('invalid_text')
  if (value.length > maximum) fail('invalid_text')
  if (!value) return value
  const decoded = decodeForValidation(value)
  if (decoded === null) fail('invalid_text')
  if (credentialValue.test(decoded)) fail('credential_material_suspected')
  if (/[\u0000-\u001F\u007F-\u009F]/.test(decoded)) fail('invalid_text')
  if (
    EMAIL_RE.test(decoded)
    || htmlTag.test(decoded)
    || QUOTED_REPLY_RE.test(decoded)
    || unsafeRawCanary.test(decoded)
    || recipientAssignment.test(decoded)
  ) {
    fail('raw_content_suspected')
  }
  return value
}

function parseTags(value: unknown): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length > MAX_TAGS) fail('invalid_item')
  const tags: string[] = []
  for (const tag of value) {
    const safe = safeDisplayText(tag, MAX_TAG_CHARS, true)
    if (!tags.includes(safe)) tags.push(safe)
  }
  return tags
}

function opaqueRef(value: unknown): string {
  if (typeof value !== 'string' || value.length < 8 || value.length > 256 || !OPAQUE_REF_PATTERN.test(value)) {
    fail('invalid_ref')
  }
  if (credentialValue.test(value)) fail('credential_material_suspected')
  return value
}

function optionalRef(value: unknown): string | null {
  return value == null ? null : opaqueRef(value)
}

function parseEvidenceItem(value: unknown): KnowledgeImportWireEvidence {
  const item = asObject(value, EVIDENCE_REQUIRED, EVIDENCE_OPTIONAL)
  const sourceType = item.source_type
  if (typeof sourceType !== 'string' || !SOURCE_TYPES.has(sourceType as RetrievalSourceType)) {
    fail('invalid_source_type')
  }
  if (item.web_url !== undefined && item.web_url !== null) fail('web_url_not_allowed')
  let indexedDigest: string | null = null
  if (item.indexed_digest != null) {
    const parsed = sha256.safeParse(item.indexed_digest)
    if (!parsed.success) fail('invalid_digest')
    indexedDigest = parsed.data
  }
  return {
    source_type: sourceType as RetrievalSourceType,
    title: safeTitle(item.title),
    document_ref: opaqueRef(item.document_ref),
    chunk_ref: optionalRef(item.chunk_ref),
    source_version: optionalRef(item.source_version),
    indexed_digest: indexedDigest,
    web_url: null,
  }
}

function parseConfidence(value: unknown): { level: 'low' | 'medium' | 'high'; score: number } {
  const confidence = asObject(value, CONFIDENCE_KEYS)
  const level = confidence.level
  const score = confidence.score
  if (typeof level !== 'string' || !CONFIDENCE_LEVELS.has(level)) fail('invalid_confidence')
  if (typeof score === 'boolean' || typeof score !== 'number' || !Number.isFinite(score) || score < 0 || score > 1) {
    fail('invalid_confidence')
  }
  return { level: level as 'low' | 'medium' | 'high', score }
}

function parseRetrievalQueryId(value: unknown, requestId: string): string {
  if (typeof value !== 'string' || !QUERY_ID_PATTERN.test(value)) fail('invalid_text')
  if (credentialValue.test(value)) fail('credential_material_suspected')
  if (value === requestId) fail('query_id_not_distinct')
  return value
}

function parseRetrievalIdentity(root: Record<string, unknown>, requestId: string) {
  if (root.schema !== CAPTURE_RETRIEVAL_SCHEMA || root.capture_schema_version !== '1.1') {
    fail('unsupported_schema')
  }
  const echoed = canonicalUuid(root.request_id)
  if (echoed !== requestId) fail('request_id_mismatch')
  if (typeof root.answer_scope !== 'string' || !ANSWER_SCOPES.has(root.answer_scope)) fail('invalid_answer_scope')
  if (typeof root.truncated !== 'boolean') fail('invalid_truncated')
  return {
    requestId: echoed,
    queryId: parseRetrievalQueryId(root.query_id, requestId),
    answerScope: root.answer_scope as 'single_source' | 'synthesized',
    truncated: root.truncated,
  }
}

function parseEvidenceList(raw: unknown): KnowledgeImportWireEvidence[] {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_EVIDENCE_ITEMS) {
    fail('invalid_evidence')
  }
  const evidence = raw.map(parseEvidenceItem)
  const seen = new Set<string>()
  for (const item of evidence) {
    const key = `${item.document_ref}\0${item.chunk_ref ?? ''}`
    if (seen.has(key)) fail('duplicate_evidence')
    seen.add(key)
  }
  return evidence
}

function assertAnswerScope(
  scope: 'single_source' | 'synthesized',
  evidence: readonly KnowledgeImportWireEvidence[],
) {
  const documents = new Set(evidence.map((item) => item.document_ref))
  const types = new Set(evidence.map((item) => item.source_type))
  if (scope === 'single_source' && (documents.size !== 1 || types.size !== 1)) {
    fail('answer_scope_mismatch')
  }
  if (scope === 'synthesized' && documents.size < 2) fail('answer_scope_mismatch')
}

function parseRetrieval(value: unknown, requestId: string): KnowledgeImportRetrievalWire {
  const encoded = JSON.stringify(value)
  if (utf8ByteLength(encoded) > MAX_RETRIEVAL_BYTES) fail('retrieval_too_large')
  const root = asObject(value, RETRIEVAL_KEYS)
  const identity = parseRetrievalIdentity(root, requestId)
  const evidence = parseEvidenceList(root.evidence)
  assertAnswerScope(identity.answerScope, evidence)
  return {
    schema: CAPTURE_RETRIEVAL_SCHEMA,
    capture_schema_version: '1.1',
    request_id: identity.requestId,
    query_id: identity.queryId,
    answer_scope: identity.answerScope,
    confidence: parseConfidence(root.confidence),
    evidence,
    truncated: identity.truncated,
  }
}

function parseAction(value: unknown): KnowledgeImportAction {
  const action = asObject(value, ACTION_REQUIRED, ACTION_OPTIONAL)
  if (typeof action.priority !== 'string' || !PRIORITIES.has(action.priority)) fail('invalid_item')
  if (action.due !== undefined && action.due !== null && typeof action.due !== 'string') fail('invalid_item')
  const detail = action.detail === undefined ? '' : safeDisplayText(action.detail, MAX_ACTION_DETAIL_CHARS, false)
  return {
    title: safeTitle(action.title),
    detail,
    priority: action.priority as KnowledgeImportAction['priority'],
    due: action.due === undefined ? null : action.due as string | null,
  }
}

function parseNormalized(value: unknown): KnowledgeImportNormalized {
  const normalized = asObject(value, NORMALIZED_REQUIRED, ['tags'])
  if (!Array.isArray(normalized.action_items) || normalized.action_items.length > MAX_ACTION_ITEMS) {
    fail('invalid_item')
  }
  return {
    summary: safeDisplayText(normalized.summary, MAX_SUMMARY_CHARS, true),
    context: safeDisplayText(normalized.context, MAX_CONTEXT_CHARS, true),
    action_items: normalized.action_items.map(parseAction),
    tags: parseTags(normalized.tags),
  }
}

function parseItem(value: unknown, requestId: string): KnowledgeImportItem {
  const item = asObject(value, ITEM_KEYS)
  return {
    item_id: canonicalUuid(item.item_id),
    title: safeTitle(item.title),
    normalized: parseNormalized(item.normalized),
    retrieval: parseRetrieval(item.retrieval, requestId),
  }
}

export function parseKnowledgeImportText(text: string): KnowledgeImportEnvelope {
  if (utf8ByteLength(text) > MAX_KNOWLEDGE_IMPORT_BYTES) fail('import_too_large')
  let parsed: unknown
  try {
    parsed = JSON.parse(text) as unknown
  } catch {
    fail('invalid_json')
  }
  rejectForbidden(parsed)
  const root = asObject(parsed, ENVELOPE_KEYS)
  if (root.schema !== KNOWLEDGE_IMPORT_SCHEMA) fail('unsupported_schema')
  const requestId = canonicalUuid(root.request_id)
  if (!Array.isArray(root.items) || root.items.length < 1 || root.items.length > MAX_IMPORT_ITEMS) {
    fail('invalid_item')
  }
  const items = root.items.map((item) => parseItem(item, requestId))
  const seen = new Set<string>()
  for (const item of items) {
    if (seen.has(item.item_id)) fail('duplicate_item_id')
    seen.add(item.item_id)
  }
  return { schema: KNOWLEDGE_IMPORT_SCHEMA, request_id: requestId, items }
}

export function previewKnowledgeImport(envelope: KnowledgeImportEnvelope): KnowledgeImportPreview {
  return {
    requestId: envelope.request_id,
    itemCount: envelope.items.length,
    items: envelope.items.map((item) => ({
      itemId: item.item_id,
      title: item.title,
      summary: item.normalized.summary,
      evidenceCount: item.retrieval.evidence.length,
      truncated: item.retrieval.truncated,
      retrievalScore: item.retrieval.confidence.score,
      retrievalLevel: item.retrieval.confidence.level,
      evidence: item.retrieval.evidence.map((entry) => ({
        title: entry.title,
        reportedSourceType: entry.source_type,
        versionState: entry.source_version ? 'reported_unverified' : 'unreported',
      })),
    })),
  }
}
