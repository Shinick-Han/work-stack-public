import { expect, test } from 'vitest'

import { applySearchResult, initialKnowledgeState } from './knowledgeSession'
import type { KnowledgeReadReference, KnowledgeSearchData } from './knowledgeTypes'

const sha = 'a'.repeat(64)
const vaultId = 'personal-wiki'

function match(overrides: Partial<KnowledgeReadReference> = {}): KnowledgeReadReference {
  return {
    schema_version: 1,
    provider: 'markdown-vault',
    vault_id: vaultId,
    document_path: 'projects/review.md',
    title: 'Release review',
    source_sha256: sha,
    expected_sha256: sha,
    freshness: 'unchanged',
    start_line: 2,
    end_line: 12,
    excerpt: 'Make the quality gate measurable.',
    excerpt_truncated: false,
    trust: 'external_reference',
    read_only: true,
    ...overrides,
  }
}

function data(overrides: Partial<KnowledgeSearchData> = {}): KnowledgeSearchData {
  return {
    binding: {
      workspace_uid: '22222222-2222-2222-2222-222222222222',
      task_uid: '11111111-1111-1111-8111-111111111111',
      task_id: 'T-0001',
      task_revision: 2,
    },
    query: 'quality gate',
    corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
    matches: [match()],
    omitted_count: 0,
    ...overrides,
  }
}

test('applySearchResult ignores stale query, vault, and host echo mismatches', () => {
  const current = {
    ...initialKnowledgeState(),
    searchQuery: 'quality gate',
    selectedVaultId: vaultId,
    pending: 'search' as const,
  }
  expect(applySearchResult({ ...current, searchQuery: 'later query' }, data(), 'quality gate', vaultId).search).toBeNull()
  expect(applySearchResult({ ...current, selectedVaultId: 'other-vault' }, data(), 'quality gate', vaultId).search).toBeNull()
  expect(applySearchResult(current, data({ query: 'host echo' }), 'quality gate', vaultId).search).toBeNull()
  const applied = applySearchResult(current, data({
    matches: [
      match(),
      match({ freshness: 'changed', document_path: 'projects/stale.md' }),
      match({ freshness: 'uncompared', expected_sha256: null, document_path: 'projects/unverified.md' }),
    ],
    omitted_count: 1,
  }), 'quality gate', vaultId)
  expect(applied.search?.matches).toHaveLength(1)
  expect(applied.search?.matches[0]?.document_path).toBe('projects/review.md')
  expect(applied.search?.omittedCount).toBe(3)
})
