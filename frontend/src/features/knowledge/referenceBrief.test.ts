import { expect, test } from 'vitest'

import { buildResumeBriefMarkdown, maybeResumeBriefMarkdown, taskBriefUtf8Bytes } from './referenceBrief'
import type { ResumeProgressFacts } from './resumeProgressContract'
import type { KnowledgeBinding, KnowledgeReadReference, KnowledgeSavedReference } from './knowledgeTypes'

const binding: KnowledgeBinding = {
  workspace_uid: '22222222-2222-2222-2222-222222222222',
  task_uid: '11111111-1111-1111-8111-111111111111',
  task_id: 'T-0001',
  task_revision: 2,
}

const sha = 'a'.repeat(64)

const noProgress: ResumeProgressFacts = { status: 'none' }

const saved: KnowledgeSavedReference = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: 'personal-wiki',
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

function readFor(excerpt: string): KnowledgeReadReference {
  return {
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
    excerpt,
    excerpt_truncated: false,
    trust: 'external_reference',
    read_only: true,
  }
}

test('omits a brief that would exceed the 32KiB clipboard cap', () => {
  const hangul = '한'.repeat(6000)
  const savedMany = Array.from({ length: 8 }, (_, index) => ({
    ...saved,
    reference_id: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa${index}`,
    document_path: `projects/review-${index}.md`,
  }))
  const reads = savedMany.map((item) => ({ ...readFor(hangul), document_path: item.document_path }))
  const markdown = maybeResumeBriefMarkdown({
    binding,
    captureContext: [{
      id: 'C-0001',
      status: 'linked',
      source: { provider: 'manual', resource_type: 'knowledge.answer', display_title: 'Catalog row' },
      ref: { kind: 'capture', id: 'C-0001' },
      connections: [{ target: { kind: 'task', id: binding.task_id }, reasons: ['capture-link'] }],
    }],
    progress: noProgress,
    vaults: [],
    reads,
    saved: savedMany,
    task: {
      id: binding.task_id,
      uid: binding.task_uid,
      revision: binding.task_revision,
      title: 'Define release quality gate',
      detail: 'Make release criteria measurable.',
      status: 'started',
    },
  })
  expect(markdown).toBeNull()
  const one = maybeResumeBriefMarkdown({
    binding,
    progress: noProgress,
    vaults: [],
    reads: [readFor('Make the quality gate measurable.')],
    saved: [saved],
    task: {
      id: binding.task_id,
      uid: binding.task_uid,
      revision: binding.task_revision,
      title: 'Define release quality gate',
      detail: 'Make release criteria measurable.',
      status: 'started',
    },
  })
  expect(one).toContain('Define release quality gate')
  expect(taskBriefUtf8Bytes(one!)).toBeLessThanOrEqual(32 * 1024)
})

const savedTask = {
  id: binding.task_id,
  uid: binding.task_uid,
  revision: binding.task_revision,
  title: 'Define release quality gate',
  detail: 'Make release criteria measurable.',
  status: 'started',
}

const snapshot = {
  checkpointId: 'CP-2026-09-07-1',
  recordedDate: '2026-09-07',
  ordinal: 1,
  revision: 4,
  digest: 'digest-one',
  done: ['Split the adapter out of the panel.'],
  next: ['Wire the brief into the drawer.'],
  blockers: [],
}

function briefWith(progress: ResumeProgressFacts) {
  return buildResumeBriefMarkdown({
    binding,
    progress,
    reads: [readFor('Make the quality gate measurable.')],
    saved: [saved],
    task: savedTask,
    vaults: [{ vault_id: 'personal-wiki', label: 'notes' }],
  })
}

test('a readable checkpoint is frozen with its identity and its own empty-field copy', () => {
  const markdown = briefWith({ status: 'ready', snapshot })
  expect(markdown).toContain('## Recorded progress')
  expect(markdown).toContain('- Record: Checkpoint CP-2026-09-07-1')
  expect(markdown).toContain('- Recorded date: 2026-09-07')
  expect(markdown).toContain('- Ordinal: 1')
  expect(markdown).toContain('- Revision: 4')
  expect(markdown).toContain('Split the adapter out of the panel.')
  expect(markdown).toContain('Wire the brief into the drawer.')
  expect(markdown).toContain('No blockers recorded in this checkpoint.')
  expect(markdown).not.toMatch(/all clear|nothing blocking/i)
})

test('an unrecorded ordinal or revision says so instead of defaulting to zero', () => {
  const markdown = briefWith({
    status: 'ready',
    snapshot: { ...snapshot, ordinal: null, revision: null, next: [] },
  })
  expect(markdown).toContain('- Ordinal: not recorded')
  expect(markdown).toContain('- Revision: not recorded')
  expect(markdown).toContain('No next step recorded in this checkpoint.')
})

test('a partially readable record names the scope it could not read', () => {
  const markdown = briefWith({ status: 'partial', snapshot, missing: ['blockers'] })
  expect(markdown).toContain('could not be read in full')
  expect(markdown).toContain('Unavailable: blockers.')
  expect(markdown).toContain('- Record: Checkpoint CP-2026-09-07-1')
})

test('a legacy record without checkpoint identity is named by its recorded slot', () => {
  const markdown = briefWith({
    status: 'ready',
    snapshot: { ...snapshot, checkpointId: null, ordinal: 2 },
  })
  expect(markdown).toContain('- Record: Legacy record · 2026-09-07 #2')
  expect(markdown).not.toContain('CP-')
})

test('a multi-line record keeps every recorded line instead of collapsing it', () => {
  const markdown = briefWith({
    status: 'ready',
    snapshot: { ...snapshot, next: ['Wire the brief.', 'Then screenshot both themes.'] },
  })
  expect(markdown).toContain('Wire the brief.')
  expect(markdown).toContain('Then screenshot both themes.')
})

test('absent, unavailable and still-loading progress each stay honest in the brief', () => {
  expect(briefWith({ status: 'none' })).toContain('No progress recorded yet.')
  const unavailable = briefWith({ status: 'unavailable', reason: 'The checkpoint audit could not be read.' })
  expect(unavailable).toContain('The checkpoint audit could not be read.')
  expect(unavailable).toContain('does not include a recorded progress snapshot')
  expect(briefWith({ status: 'loading' })).toContain('had not finished loading')
})

test('the brief names the source rather than presenting every document as a Markdown file', () => {
  const markdown = briefWith({ status: 'none' })
  expect(markdown).toContain('- Source: Local Markdown · notes')
  expect(markdown).toContain('- Document: notes · projects/review.md · lines 2–12')
  expect(markdown).toContain('- Linked reason: Quality gate source of truth.')
  expect(markdown).toContain('Copy this brief into your agent session. Nothing is sent automatically.')
  expect(markdown).not.toContain('## Saved Capture sources')
})

test('an explicit empty Capture context is named; omitted context leaves the prior brief unchanged', () => {
  const without = briefWith({ status: 'none' })
  const empty = buildResumeBriefMarkdown({
    binding,
    captureContext: [],
    progress: { status: 'none' },
    reads: [readFor('Make the quality gate measurable.')],
    saved: [saved],
    task: savedTask,
    vaults: [{ vault_id: 'personal-wiki', label: 'notes' }],
  })
  expect(empty).toContain('## Saved Capture sources')
  expect(empty).toContain('No linked Capture sources included.')
  expect(without).not.toContain('## Saved Capture sources')
})
