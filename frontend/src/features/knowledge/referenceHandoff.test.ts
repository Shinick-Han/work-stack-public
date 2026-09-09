import { expect, test } from 'vitest'

import type { KnowledgeBinding, KnowledgeReadReference, KnowledgeSavedReference } from './knowledgeTypes'
import type { ResumeProgressFacts } from './resumeProgressContract'
import {
  admitPreparedRead,
  knowledgeContextFilename,
  knowledgeContextUtf8Bytes,
  nextHandoffSelection,
  prepareReferenceHandoff,
  ReferenceHandoffError,
  type PreparedReferenceHandoff,
} from './referenceHandoff'

const binding: KnowledgeBinding = {
  workspace_uid: '22222222-2222-2222-2222-222222222222',
  task_uid: '11111111-1111-1111-8111-111111111111',
  task_id: 'T-0001',
  task_revision: 2,
}

/** Narrows an existing vault-selection preparation so its envelope assertions stay exact. */
function vaultHandoff(prepared: PreparedReferenceHandoff) {
  if (prepared.sources !== 'vault-selection') {
    throw new Error(`expected a vault-selection handoff, got ${prepared.sources}`)
  }
  return prepared
}

const sha = 'a'.repeat(64)
const otherSha = 'b'.repeat(64)

const saved: KnowledgeSavedReference = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: 'personal-wiki',
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

const second: KnowledgeSavedReference = {
  ...saved,
  reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  document_path: 'projects/other.md',
}

function readFor(
  item: KnowledgeSavedReference,
  overrides: Partial<KnowledgeReadReference> = {},
): KnowledgeReadReference {
  return {
    schema_version: 1,
    provider: 'markdown-vault',
    vault_id: item.vault_id,
    document_path: item.document_path,
    title: 'Release review',
    source_sha256: item.source_sha256,
    expected_sha256: item.source_sha256,
    freshness: 'unchanged',
    start_line: item.start_line,
    end_line: item.end_line,
    excerpt: 'Make the quality gate measurable.',
    excerpt_truncated: false,
    trust: 'external_reference',
    read_only: true,
    ...overrides,
  }
}

const progressReady: ResumeProgressFacts = {
  status: 'ready',
  snapshot: {
    checkpointId: 'CP-2026-09-07-1',
    recordedDate: '2026-09-07',
    ordinal: 1,
    revision: 4,
    digest: 'digest-one',
    done: ['Split the reference adapter out of the panel.'],
    next: ['Wire the resume brief into the drawer.'],
    blockers: [],
  },
}

function liveReaders(current: readonly KnowledgeSavedReference[] = [saved, second]) {
  return {
    listReferences: async () => current,
    progress: progressReady,
    vaults: [{ vault_id: 'personal-wiki', label: 'notes' }],
    readLiveTask: async () => ({
      id: binding.task_id,
      uid: binding.task_uid,
      revision: binding.task_revision,
      title: 'Define release quality gate',
      detail: 'Make release criteria measurable.',
      status: 'started',
    }),
    readWorkspaceUid: async () => binding.workspace_uid,
  }
}

test('download filename uses the Task identity and omits vault-relative paths', () => {
  expect(knowledgeContextFilename(binding)).toBe('workstack-knowledge-context-T-0001-r2.json')
  expect(knowledgeContextFilename(binding)).not.toContain('projects/review.md')
  expect(knowledgeContextFilename({ ...binding, task_id: 'T/unsafe path' })).not.toContain('/')
  expect(knowledgeContextFilename({ ...binding, task_id: 'T/unsafe path' })).not.toContain(' ')
})

test('prepare reads only the selected references and keeps generated false', async () => {
  const requested: string[] = []
  const prepared = vaultHandoff(await prepareReferenceHandoff({
    binding,
    references: [saved, second],
    selectedIds: [second.reference_id],
    signal: new AbortController().signal,
    ...liveReaders(),
    readReference: async (item) => {
      requested.push(item.reference_id)
      return readFor(item, { excerpt_truncated: true, excerpt: 'Other note' })
    },
  }))
  expect(requested).toEqual([second.reference_id])
  expect(prepared.envelope.schema).toBe('workstack.knowledge-context.v1')
  expect(prepared.envelope.generated).toBe(false)
  expect(Object.keys(prepared.envelope).sort()).toEqual(['binding', 'generated', 'references', 'schema'])
  expect(prepared.briefMarkdown).not.toContain('## Saved Capture sources')
  expect(prepared.envelope.references).toHaveLength(1)
  expect(prepared.envelope.references[0]?.document_path).toBe('projects/other.md')
  expect(prepared.envelope.references[0]?.excerpt_truncated).toBe(true)
  expect(prepared.json).not.toMatch(/C:\\|StateRoot|vault_root/i)
  expect(prepared.briefMarkdown).toContain('Define release quality gate')
  expect(prepared.briefMarkdown).toContain('Make release criteria measurable.')
  expect(prepared.briefMarkdown).toContain('started')
  expect(prepared.briefMarkdown).toContain('untrusted evidence')
  expect(prepared.briefMarkdown).not.toMatch(/C:\\|StateRoot|vault_root/i)
})

test('malformed paired reads and uncompared pinned reads refuse the whole envelope', () => {
  expect(() => admitPreparedRead(saved, readFor(saved, { document_path: 'projects/other.md' }))).toThrow(ReferenceHandoffError)
  expect(() => admitPreparedRead(saved, readFor(saved, { expected_sha256: otherSha, freshness: 'changed', source_sha256: otherSha }))).toThrow(/no longer matches/)
  expect(() => admitPreparedRead(saved, readFor(saved, { freshness: 'uncompared' }))).toThrow(/not compared/)
})

test('UTF-8 envelope cap fails visibly without a truncated export', async () => {
  const hangul = '한'.repeat(6000)
  const first = vaultHandoff(await prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders(),
    readReference: async (item) => readFor(item, { excerpt: '짧다' }),
  }))
  expect(knowledgeContextUtf8Bytes(first.json)).toBeLessThanOrEqual(32 * 1024)

  await expect(prepareReferenceHandoff({
    binding,
    references: [saved, second],
    selectedIds: [saved.reference_id, second.reference_id],
    signal: new AbortController().signal,
    ...liveReaders(),
    readReference: async (item) => readFor(item, { excerpt: hangul }),
  })).rejects.toMatchObject({
    code: 'envelope_too_large',
    message: expect.stringMatching(/Nothing was exported/),
  })
})

test('selection never auto-includes the rest of the list and stops at eight', () => {
  const ids = Array.from({ length: 9 }, (_, index) => `id-${index}`)
  let selected: string[] = []
  selected = nextHandoffSelection(selected, ids[0]!)
  expect(selected).toEqual([ids[0]])
  for (const id of ids.slice(1)) selected = nextHandoffSelection(selected, id)
  expect(selected).toHaveLength(8)
  expect(selected).not.toContain(ids[8])
})


test('prepare reads the freshly listed record, not the cached copy the user selected', async () => {
  const fresh = { ...saved }
  const read: KnowledgeSavedReference[] = []
  await prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders([fresh]),
    readReference: async (item) => {
      read.push(item)
      return readFor(item)
    },
  })
  expect(read).toHaveLength(1)
  expect(read[0]).toBe(fresh)
  expect(read[0]).not.toBe(saved)
})

test('a selected reference removed from the current list refuses before any read', async () => {
  const read: string[] = []
  await expect(prepareReferenceHandoff({
    binding,
    references: [saved, second],
    selectedIds: [saved.reference_id, second.reference_id],
    signal: new AbortController().signal,
    ...liveReaders([second]),
    readReference: async (item) => {
      read.push(item.reference_id)
      return readFor(item)
    },
  })).rejects.toMatchObject({
    code: 'selection_removed',
    message: expect.stringMatching(/Nothing was exported/),
  })
  expect(read).toEqual([])
})

test.each([
  ['repinned hash', { source_sha256: otherSha }],
  ['moved span', { end_line: 20 }],
  ['moved document', { document_path: 'projects/moved.md' }],
  ['different vault', { vault_id: 'other-vault' }],
  ['edited reason', { reason: 'A different saved reason.' }],
])('a selected reference with a %s refuses before any read', async (_label, change) => {
  const read: string[] = []
  await expect(prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders([{ ...saved, ...change }]),
    readReference: async (item) => {
      read.push(item.reference_id)
      return readFor(item)
    },
  })).rejects.toMatchObject({
    code: 'selection_changed',
    message: expect.stringMatching(/Reload the linked references and select again/),
  })
  expect(read).toEqual([])
})

test('a current list that gained references leaves the existing selection alone', async () => {
  const added: KnowledgeSavedReference = {
    ...saved,
    reference_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    document_path: 'projects/added.md',
  }
  const read: string[] = []
  const prepared = await prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders([saved, added]),
    readReference: async (item) => {
      read.push(item.reference_id)
      return readFor(item)
    },
  })
  expect(read).toEqual([saved.reference_id])
  expect(prepared.referenceIds).toEqual([saved.reference_id])
})

test('the resume brief uses the saved live Task fields and the frozen progress record', async () => {
  const prepared = await prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders(),
    readLiveTask: async () => ({
      id: binding.task_id,
      uid: binding.task_uid,
      revision: binding.task_revision,
      title: 'Saved title from API',
      detail: 'Saved detail from API',
      status: 'started',
    }),
    readReference: async (item) => readFor(item),
  })
  expect(prepared.briefMarkdown).toContain('Saved title from API')
  expect(prepared.briefMarkdown).toContain('Saved detail from API')
  expect(prepared.briefMarkdown).toContain(saved.reason)
  expect(prepared.briefMarkdown).toContain(saved.source_sha256)
  expect(prepared.briefMarkdown).not.toContain('unsaved draft')
  expect(prepared.briefMarkdown).toMatch(/^# Resume brief/m)
  expect(prepared.briefMarkdown).toContain('## Recorded progress')
  expect(prepared.briefMarkdown).toContain('CP-2026-09-07-1')
  expect(prepared.briefMarkdown).toContain('2026-09-07')
  expect(prepared.briefMarkdown).toContain('Wire the resume brief into the drawer.')
  expect(prepared.briefMarkdown).toContain('No blockers recorded in this checkpoint.')
  expect(prepared.progressKey).toBe('record:digest-one')
})

test('the final live Task-detail context wins and stays out of the vault JSON envelope', async () => {
  const early = [{
    id: 'C-0001',
    status: 'linked',
    source: { provider: 'manual', resource_type: 'knowledge.answer', display_title: 'Early row' },
    ref: { kind: 'capture', id: 'C-0001' },
    connections: [{ target: { kind: 'task', id: binding.task_id }, reasons: ['capture-link'] }],
  }]
  const final = [{
    id: 'C-0002',
    status: 'linked',
    source: { provider: 'manual', resource_type: 'knowledge.answer', display_title: 'Final live row' },
    ref: { kind: 'capture', id: 'C-0002' },
    connections: [{ target: { kind: 'task', id: binding.task_id }, reasons: ['capture-link'] }],
  }]
  let reads = 0
  const prepared = vaultHandoff(await prepareReferenceHandoff({
    binding,
    references: [saved],
    selectedIds: [saved.reference_id],
    signal: new AbortController().signal,
    ...liveReaders(),
    readLiveTask: async () => {
      reads += 1
      return {
        id: binding.task_id,
        uid: binding.task_uid,
        revision: binding.task_revision,
        title: 'Define release quality gate',
        detail: 'Make release criteria measurable.',
        status: 'started',
        context: reads === 1 ? early : final,
      }
    },
    readReference: async (item) => readFor(item),
  }))
  expect(reads).toBe(2)
  expect(prepared.briefMarkdown).toContain('C-0002')
  expect(prepared.briefMarkdown).toContain('Final live row')
  expect(prepared.briefMarkdown).not.toContain('C-0001')
  expect(prepared.briefMarkdown).not.toContain('Early row')
  const payload = JSON.parse(prepared.json) as { schema: string; references: unknown[] }
  expect(payload.schema).toBe('workstack.knowledge-context.v1')
  expect(Object.keys(payload).sort()).toEqual(['binding', 'generated', 'references', 'schema'])
  expect(prepared.json).not.toContain('C-0002')
  expect(prepared.json).not.toContain('Final live row')
})
