import { expect, test } from 'vitest'

import {
  LOCAL_MARKDOWN_SOURCE,
  localExcerptView,
  localReferenceView,
  markdownDocumentLocation,
  markdownDocumentTitle,
  markdownLineSpanLabel,
  referenceOriginLabel,
  sourceQualifiedIdentity,
  type KnowledgeReferenceView,
} from './knowledgeSourceView'
import type { KnowledgeReadReference, KnowledgeSavedReference } from './knowledgeTypes'

const sha = 'a'.repeat(64)
const vaults = [{ vault_id: 'personal-wiki', label: 'notes' }]

const saved: KnowledgeSavedReference = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: 'personal-wiki',
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

const read: KnowledgeReadReference = {
  schema_version: 1,
  provider: 'markdown-vault',
  vault_id: saved.vault_id,
  document_path: saved.document_path,
  title: 'Release review',
  source_sha256: sha,
  expected_sha256: sha,
  freshness: 'unchanged',
  start_line: saved.start_line,
  end_line: saved.end_line,
  excerpt: 'Make the quality gate measurable.',
  excerpt_truncated: false,
  trust: 'external_reference',
  read_only: true,
}

test('the local adapter keeps Markdown path and line formatting out of the shared fields', () => {
  const view = localReferenceView(saved, vaults)
  expect(view.connectorName).toBe('Local Markdown')
  expect(view.sourceName).toBe('notes')
  expect(view.documentTitle).toBe('review.md')
  expect(view.documentLocation).toBe('projects')
  expect(view.excerptLocation).toBe('lines 2–12')
  expect(view.linkedReason).toBe(saved.reason)
  expect(view.actions).toEqual(['read', 'unlink'])
  expect(view.scopeNote).toBe(LOCAL_MARKDOWN_SOURCE.scopeNote)
  expect(referenceOriginLabel(view)).toBe('notes · projects · lines 2–12')
})

test('document identity is source-qualified, so a shared title cannot collide across sources', () => {
  const here = localReferenceView(saved, vaults)
  const elsewhere = localReferenceView({ ...saved, vault_id: 'second-vault' }, vaults)
  expect(here.identityKey).not.toBe(elsewhere.identityKey)
  expect(JSON.parse(here.identityKey)).toEqual([
    'markdown-vault',
    'personal-wiki',
    'projects/review.md#2-12',
  ])
})

test('a document key that contains the separator cannot collide with another document', () => {
  const withColonInSource = sourceQualifiedIdentity('demo', 'space:a', 'page-1')
  const withColonInDocument = sourceQualifiedIdentity('demo', 'space', 'a:page-1')
  expect(withColonInSource).not.toBe(withColonInDocument)
  const urlKey = sourceQualifiedIdentity('demo', 'space', 'https://example.test/p/9#b2')
  expect(JSON.parse(urlKey)).toEqual(['demo', 'space', 'https://example.test/p/9#b2'])
})

test('an unlabelled source falls back to its identifier rather than inventing a name', () => {
  expect(localReferenceView(saved, []).sourceName).toBe('personal-wiki')
})

test('the excerpt view carries the read title and the source-supplied provenance', () => {
  const view = localExcerptView(read, vaults)
  expect(view.documentTitle).toBe('Release review')
  // The trail is the folder path only. Both local adapters agree on it, so a card never
  // prints the file it is already titled with a second line down.
  expect(view.documentLocation).toBe('projects')
  expect(view.documentLocation).toBe(localReferenceView(saved, vaults).documentLocation)
  expect(referenceOriginLabel(view)).toBe('notes · projects · lines 2–12')
  expect(view.excerptLocation).toBe('lines 2–12')
  expect(view.trustNote).toContain('not Task permission')
  expect(localExcerptView({ ...read, title: '' as unknown as string }, vaults).documentTitle).toBe('review.md')
})

test('Markdown helpers keep single-line and root-level documents readable', () => {
  expect(markdownDocumentTitle('review.md')).toBe('review.md')
  expect(markdownDocumentLocation('review.md')).toBeNull()
  expect(markdownDocumentLocation('a/b/c.md')).toBe('a / b')
  expect(markdownLineSpanLabel(7, 7)).toBe('line 7')
})

test('a source with no sub-document coordinates renders without empty separators', () => {
  const remote: KnowledgeReferenceView = {
    referenceId: 'r-1',
    identityKey: 'demo:space-1:page-9',
    connectorName: 'Demo source',
    sourceName: 'Team space',
    documentTitle: 'Release plan',
    documentLocation: null,
    excerptLocation: null,
    linkedReason: 'Holds the agreed rollout order.',
    actions: ['read'],
    scopeNote: 'Availability follows the connected source.',
  }
  expect(referenceOriginLabel(remote)).toBe('Team space')
})
