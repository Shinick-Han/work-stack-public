import type { KnowledgeEvidenceInput, KnowledgeEvidenceRowInput } from './knowledgeEvidenceView'

/**
 * Presentation fixtures for the evidence panel: component tests and the local render
 * check read the same shapes. Every label here is the kind of safe, human label a parent
 * is expected to supply — no source path, query, credential or provider product name.
 */

export function evidenceRow(index: number, versioned = true): KnowledgeEvidenceRowInput {
  return {
    id: `evidence-${index}`,
    title: `Rollback step ${index} agreed for the 4.2 release`,
    typeLabel: index % 2 === 0 ? 'Page' : 'Answer',
    versionLabel: versioned ? `v${index}` : null,
    note: index === 1 ? 'Cited in the release checklist.' : null,
  }
}

export function evidenceRows(count: number, versioned = true): KnowledgeEvidenceRowInput[] {
  return Array.from({ length: count }, (_, index) => evidenceRow(index + 1, versioned))
}

/** A clean import: source checked, every item versioned, confidence supplied. */
export const verifiedEvidence: KnowledgeEvidenceInput = {
  sourceLabel: 'Team knowledge base',
  requestLabel: 'Which rollback steps did we agree for the 4.2 release?',
  retrievedAt: '2026-09-08T02:15:00Z',
  evidenceCount: 3,
  confidenceLabel: 'High',
  confidenceScore: 0.82,
  verification: 'current',
  rows: evidenceRows(3),
}

/** The same import from a source that never told us which version it read. */
export const unversionedEvidence: KnowledgeEvidenceInput = {
  ...verifiedEvidence,
  confidenceLabel: null,
  confidenceScore: null,
  rows: evidenceRows(3, false),
}

/** A crowded, partial result from a source that could not be reached for a check. */
export const crowdedEvidence: KnowledgeEvidenceInput = {
  sourceLabel: 'Shared drive index',
  requestLabel: 'Everything we hold on rollback verification ownership',
  retrievedAt: '2026-09-08T02:40:00Z',
  evidenceCount: 24,
  confidenceLabel: 'Mixed',
  confidenceScore: 0.41,
  verification: 'offline',
  resultsConflict: true,
  resultsTruncated: true,
  rows: evidenceRows(12),
}

/** Nothing came back, and nothing may be implied from that. */
export const emptyEvidence: KnowledgeEvidenceInput = {
  sourceLabel: 'Team knowledge base',
  requestLabel: 'Rollback owner for the 4.2 release',
  retrievedAt: null,
  evidenceCount: 0,
  verification: 'access-denied',
  rows: [],
}
