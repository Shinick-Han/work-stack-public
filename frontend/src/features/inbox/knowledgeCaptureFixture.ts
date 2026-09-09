import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { CaptureV11, CaptureRetrievalProjection } from '../../domain/types'
import { capture } from '../../test/fixtures'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'

export const REQUEST_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
export const ITEM_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

export const OWNED_R6_PROJECTION_NOTE = 'Oracle R6-CAPTURE-1.1-PROJECTION.fixture.json SHA-256 (LF) d017701b3bd88e9015f38c2e2d857f4bab936d05f620af83f611a96a20b590e3 generator 3ae9586aaf1054776e3faba184e3f710c32aaa9f'

export interface OwnedR6ProjectionFixture {
  request_envelope: unknown
  listed: unknown[]
  stored_retrieval: { evidence: Array<Record<string, unknown>> }
}

export function loadOwnedR6ProjectionFixture(): OwnedR6ProjectionFixture {
  return JSON.parse(readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), 'r6Capture11Projection.owned.json'),
    'utf8',
  )) as OwnedR6ProjectionFixture
}

export function retrievalWire(overrides: Record<string, unknown> = {}) {
  return {
    schema: 'workstack.capture-retrieval.v1.1',
    capture_schema_version: '1.1',
    request_id: REQUEST_ID,
    query_id: 'engine-q-00194f5a',
    answer_scope: 'single_source',
    confidence: { level: 'medium', score: 0.62 },
    evidence: [{
      source_type: 'notion.page',
      title: 'Release quality gate',
      document_ref: 'od-page-7f3ba1d34f50c884600112ab',
      chunk_ref: 'chunk-0004abcd',
      source_version: 'od-version-14',
      indexed_digest: `sha256:${'a'.repeat(64)}`,
      web_url: null,
    }],
    truncated: false,
    ...overrides,
  }
}

export function knowledgeImportEnvelope(overrides: Record<string, unknown> = {}): KnowledgeImportEnvelope {
  const envelope = {
    schema: 'workstack.knowledge-import.v1',
    request_id: REQUEST_ID,
    items: [{
      item_id: ITEM_ID,
      title: 'Rollback verification owner',
      normalized: {
        summary: 'The rollback check still needs an owner.',
        context: 'Sanitized answer context only.',
        action_items: [],
        tags: ['release'],
      },
      retrieval: retrievalWire(),
    }],
    ...overrides,
  }
  return envelope as KnowledgeImportEnvelope
}

export function retrievalProjection(overrides: Partial<CaptureRetrievalProjection> = {}): CaptureRetrievalProjection {
  return {
    schema: 'workstack.capture-retrieval.v1.1',
    capture_schema_version: '1.1',
    request_id: REQUEST_ID,
    query_id: 'engine-q-00194f5a',
    answer_scope: 'single_source',
    confidence: { level: 'medium', score: 0.62 },
    evidence: [{
      reported_source_type: 'notion.page',
      title: 'Release quality gate',
      document_ref: 'od-page-7f3ba1d34f50c884600112ab',
      chunk_ref: 'chunk-0004abcd',
      reported_source_version: 'od-version-14',
      version_state: 'reported_unverified',
      indexed_digest: `sha256:${'a'.repeat(64)}`,
      web_url: null,
    }],
    truncated: false,
    reported_origin: {
      document_ref: 'od-page-7f3ba1d34f50c884600112ab',
      source_type: 'notion.page',
    },
    origin: null,
    origin_state: 'reported_unverified',
    capture_source_type: 'knowledge.answer',
    ...overrides,
  }
}

export function knowledgeCapture(overrides: Partial<CaptureV11> = {}): CaptureV11 {
  return {
    id: capture.id,
    schema_version: '1.1',
    source_key: capture.source_key,
    source: {
      ...capture.source,
      provider: 'manual',
      resource_type: 'knowledge.answer',
      web_url: null,
      display_title: 'Rollback verification owner',
    },
    normalized: capture.normalized,
    task_hints: capture.task_hints,
    provenance: {
      capture_mode: 'manual',
      adapter: 'workstack.knowledge-import',
      adapter_version: '1',
      redaction_policy_version: '1',
      raw_retained: false,
      created_at: capture.created_at,
    },
    status: capture.status,
    linked_task_ids: capture.linked_task_ids,
    converted_task_ids: capture.converted_task_ids,
    revision: capture.revision,
    created_at: capture.created_at,
    updated_at: capture.updated_at,
    retrieval: retrievalProjection(),
    ...overrides,
  }
}
