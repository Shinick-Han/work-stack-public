import type { RetrievalSourceType } from './types'
import { CAPTURE_RETRIEVAL_SCHEMA, KNOWLEDGE_IMPORT_SCHEMA } from './schemaKnowledgeCapture'

/**
 * Closed knowledge-import envelope types. Kept in domain so the API layer can
 * name the envelope without importing the inbox parser.
 */

export interface KnowledgeImportAction {
  title: string
  detail: string
  priority: 'P0' | 'P1' | 'P2' | 'P3'
  due: string | null
}

export interface KnowledgeImportNormalized {
  summary: string
  context: string
  action_items: KnowledgeImportAction[]
  tags: string[]
}

export interface KnowledgeImportWireEvidence {
  source_type: RetrievalSourceType
  title: string
  document_ref: string
  chunk_ref: string | null
  source_version: string | null
  indexed_digest: string | null
  web_url: null
}

export interface KnowledgeImportRetrievalWire {
  schema: typeof CAPTURE_RETRIEVAL_SCHEMA
  capture_schema_version: '1.1'
  request_id: string
  query_id: string
  answer_scope: 'single_source' | 'synthesized'
  confidence: { level: 'low' | 'medium' | 'high'; score: number }
  evidence: KnowledgeImportWireEvidence[]
  truncated: boolean
}

export interface KnowledgeImportItem {
  item_id: string
  title: string
  normalized: KnowledgeImportNormalized
  retrieval: KnowledgeImportRetrievalWire
}

export interface KnowledgeImportEnvelope {
  schema: typeof KNOWLEDGE_IMPORT_SCHEMA
  request_id: string
  items: KnowledgeImportItem[]
}
