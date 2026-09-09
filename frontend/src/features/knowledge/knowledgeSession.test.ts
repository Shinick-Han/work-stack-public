import { expect, test } from 'vitest'

import {
  applyPreviewResult,
  applySearchMatch,
  applySearchResult,
  bindKnowledgePreview,
  initialKnowledgeState,
  knowledgePreviewRequest,
  previewMatchesInput,
  previewSpanMatchesRequest,
} from './knowledgeSession'
import type { KnowledgeBinding, KnowledgeReadReference, KnowledgeSearchData } from './knowledgeTypes'

const sha = 'a'.repeat(64)
const vaultId = 'personal-wiki'
const documentPath = 'projects/review.md'

const binding: KnowledgeBinding = {
  workspace_uid: '22222222-2222-2222-2222-222222222222',
  task_uid: '11111111-1111-1111-8111-111111111111',
  task_id: 'T-0001',
  task_revision: 2,
}
const nextRevision: KnowledgeBinding = { ...binding, task_revision: 3 }

function match(overrides: Partial<KnowledgeReadReference> = {}): KnowledgeReadReference {
  return {
    schema_version: 1,
    provider: 'markdown-vault',
    vault_id: vaultId,
    document_path: documentPath,
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
    binding,
    query: 'quality gate',
    corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
    matches: [match()],
    omitted_count: 0,
    ...overrides,
  }
}

function previewing(overrides: Partial<ReturnType<typeof initialKnowledgeState>> = {}) {
  return {
    ...initialKnowledgeState(),
    binding,
    documentPath,
    pending: 'preview' as const,
    selectedVaultId: vaultId,
    ...overrides,
  }
}

const defaultRequest = {
  binding,
  documentPath,
  endLine: 40,
  startLine: 1,
  vaultId,
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
  expect(applied.search?.matches[0]?.document_path).toBe(documentPath)
  expect(applied.search?.omittedCount).toBe(3)
})

test('previewMatchesInput binds the exact request and the returned effective span', () => {
  const clipped = match({ start_line: 1, end_line: 8, excerpt: 'Short note.' })
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '')).toBe(false)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '1', '40')).toBe(false)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '1', '8')).toBe(true)

  const bound = bindKnowledgePreview(clipped, defaultRequest)
  expect(previewMatchesInput(bound, vaultId, documentPath, '', '')).toBe(true)
  expect(previewMatchesInput(bound, vaultId, documentPath, '1', '40')).toBe(true)
  expect(previewMatchesInput(bound, vaultId, documentPath, '1', '8')).toBe(false)
  expect(previewMatchesInput(bound, vaultId, documentPath, '1', '20')).toBe(false)
  expect(previewMatchesInput(bound, vaultId, 'notes/other.md', '', '')).toBe(false)
  expect(previewMatchesInput(bound, 'other-vault', documentPath, '', '')).toBe(false)
})

test('explicit requested ranges match exactly unless the reader clipped that same request', () => {
  const exact = bindKnowledgePreview(
    match({ start_line: 2, end_line: 12 }),
    { binding, documentPath, endLine: 12, startLine: 2, vaultId },
  )
  expect(previewMatchesInput(exact, vaultId, documentPath, '2', '12')).toBe(true)
  expect(previewMatchesInput(exact, vaultId, documentPath, '', '')).toBe(false)
  expect(previewMatchesInput(exact, vaultId, documentPath, '2', '40')).toBe(false)

  const clippedExplicit = bindKnowledgePreview(
    match({ start_line: 2, end_line: 6 }),
    { binding, documentPath, endLine: 12, startLine: 2, vaultId },
  )
  expect(previewMatchesInput(clippedExplicit, vaultId, documentPath, '2', '12')).toBe(true)
  expect(previewMatchesInput(clippedExplicit, vaultId, documentPath, '2', '6')).toBe(false)
  expect(previewSpanMatchesRequest(match({ start_line: 2, end_line: 13 }), {
    documentPath,
    endLine: 12,
    startLine: 2,
    vaultId,
  })).toBe(false)
})

test('applyPreviewResult keeps a clipped short-doc preview only for the current request', () => {
  const clipped = match({ start_line: 1, end_line: 8, excerpt: 'Short note.' })
  const applied = applyPreviewResult(previewing(), clipped, defaultRequest)
  expect(applied.preview?.end_line).toBe(8)
  expect(applied.pending).toBeNull()
  expect(knowledgePreviewRequest(applied.preview!)).toEqual(defaultRequest)
  expect(previewMatchesInput(applied.preview, vaultId, documentPath, '', '')).toBe(true)
})

test('applyPreviewResult ignores a late preview after path, vault, or span inputs change', () => {
  const clipped = match({ start_line: 1, end_line: 8, excerpt: 'LATE SHORT DOC' })
  expect(applyPreviewResult(
    previewing({ documentPath: 'notes/other.md' }),
    clipped,
    defaultRequest,
  ).preview).toBeNull()
  expect(applyPreviewResult(
    previewing({ selectedVaultId: 'other-vault' }),
    clipped,
    defaultRequest,
  ).preview).toBeNull()
  expect(applyPreviewResult(
    previewing({ endLine: '20' }),
    clipped,
    defaultRequest,
  ).preview).toBeNull()
  expect(applyPreviewResult(
    previewing({ startLine: '2' }),
    clipped,
    defaultRequest,
  ).preview).toBeNull()
  expect(applyPreviewResult(
    previewing({ documentPath: '' }),
    clipped,
    defaultRequest,
  ).preview).toBeNull()
})

test('applyPreviewResult rejects a returned span that is not a clip of the bound request', () => {
  const wider = applyPreviewResult(
    previewing(),
    match({ start_line: 1, end_line: 41 }),
    defaultRequest,
  )
  expect(wider.preview).toBeNull()
  expect(wider.error).toMatch(/does not match the requested document range/)
  expect(wider.errorCode).toBe('invalid_line_range')

  const movedStart = applyPreviewResult(
    previewing(),
    match({ start_line: 2, end_line: 8 }),
    defaultRequest,
  )
  expect(movedStart.preview).toBeNull()
  expect(movedStart.errorCode).toBe('invalid_line_range')

  const otherDoc = applyPreviewResult(
    previewing(),
    match({ document_path: 'notes/other.md', start_line: 1, end_line: 8 }),
    defaultRequest,
  )
  expect(otherDoc.preview).toBeNull()
})

test('applySearchMatch binds the match span so Link uses that exact returned range', () => {
  const selected = match({ start_line: 2, end_line: 12 })
  const current = {
    ...initialKnowledgeState(),
    binding,
    selectedVaultId: vaultId,
  }
  const applied = applySearchMatch(current, selected)
  expect(applied.startLine).toBe('2')
  expect(applied.endLine).toBe('12')
  expect(previewMatchesInput(applied.preview, vaultId, documentPath, '2', '12')).toBe(true)
  expect(previewMatchesInput(applied.preview, vaultId, documentPath, '', '')).toBe(false)
  expect(knowledgePreviewRequest(applied.preview!)).toEqual({
    binding,
    documentPath,
    endLine: 12,
    startLine: 2,
    vaultId,
  })
})

test('a preview bound to one Task revision cannot authorize a pin on the next one', () => {
  const clipped = bindKnowledgePreview(
    match({ start_line: 1, end_line: 8, excerpt: 'Short note.' }),
    defaultRequest,
  )
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '')).toBe(true)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '', binding)).toBe(true)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '', nextRevision)).toBe(false)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '', {
    ...binding,
    workspace_uid: '33333333-3333-3333-3333-333333333333',
  })).toBe(false)
  expect(previewMatchesInput(clipped, vaultId, documentPath, '', '', null)).toBe(false)

  const unbound = match({ start_line: 1, end_line: 8, excerpt: 'No receipt.' })
  expect(previewMatchesInput(unbound, vaultId, documentPath, '1', '8')).toBe(true)
  expect(previewMatchesInput(unbound, vaultId, documentPath, '1', '8', binding)).toBe(false)
})

test('applyPreviewResult drops a read that settles after the Task revision moved on', () => {
  const clipped = match({ start_line: 1, end_line: 8, excerpt: 'LATE AFTER REVISION' })
  const late = applyPreviewResult(previewing({ binding: nextRevision }), clipped, defaultRequest)
  expect(late.preview).toBeNull()
  expect(late.pending).toBeNull()
  expect(late.error).toBeNull()
  expect(applyPreviewResult(previewing({ binding: null }), clipped, defaultRequest).preview).toBeNull()
  expect(applyPreviewResult(previewing(), clipped, defaultRequest).preview?.end_line).toBe(8)
})

test('applySearchMatch refuses to bind a preview without a Task snapshot', () => {
  const applied = applySearchMatch(
    { ...initialKnowledgeState(), selectedVaultId: vaultId },
    match({ start_line: 2, end_line: 12 }),
  )
  expect(applied.preview).toBeNull()
  expect(previewMatchesInput(applied.preview, vaultId, documentPath, '2', '12', null)).toBe(false)
})
