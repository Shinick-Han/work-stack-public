import { expect, test } from 'vitest'
import { capture, workspace } from '../../test/fixtures'
import { knowledgeCapture } from './knowledgeCaptureFixture'
import {
  UPDATED_CONTEXT_LAUNCH_LABEL,
  knowledgeLaunchSeedFromCapture,
  sanitizeKnowledgeSeedQuery,
} from './knowledgeLaunchSeed'
import { MAX_QUERY_CHARS, buildKnowledgeRequestDraft } from './knowledgeRequestDraft'

const WORKSPACE_UID = workspace.workspace.id

// Written as code points so no raw control character sits in this source file.
const TAB = String.fromCodePoint(9)
const NUL = String.fromCodePoint(0)
const DEL = String.fromCodePoint(127)
const C1 = String.fromCodePoint(0x85)

test('a 1.0 capture seeds an editable question, the neutral purpose and no scope', () => {
  const seed = knowledgeLaunchSeedFromCapture(capture, WORKSPACE_UID)
  expect(seed.query).toBe('Release review feedback')
  // Never `refresh_capture`: the protocol cannot promise the same document is re-read.
  expect(seed.purpose).toBe('find_context')
  expect(seed.launchLabel).toBe(UPDATED_CONTEXT_LAUNCH_LABEL)
  expect(seed.connectionHint).toBe('personal-outlook')
})

test('a 1.1 knowledge capture seeds the same way and carries the ledger alias as a hint', () => {
  const seed = knowledgeLaunchSeedFromCapture(knowledgeCapture(), WORKSPACE_UID)
  expect(seed.query).toBe('Rollback verification owner')
  expect(seed.purpose).toBe('find_context')
  expect(seed.connectionHint).toBe('personal-outlook')
})

test('nothing but the display title becomes a seed', () => {
  const listed = knowledgeCapture()
  const seed = knowledgeLaunchSeedFromCapture(listed, WORKSPACE_UID)
  const text = JSON.stringify(seed)
  // The summary, the sanitized context, the request and item refs, the digest and the
  // source key are all things this Capture holds. None of them is a starting value.
  for (const held of [
    listed.normalized.summary,
    listed.normalized.context,
    listed.source.container_ref,
    listed.source.object_ref,
    listed.source.version_ref,
    listed.source.fingerprint,
    listed.source_key,
    listed.retrieval.evidence[0].document_ref,
  ]) {
    expect(text).not.toContain(held)
  }
  // The context key is local screen state; the seed carries no binding of any kind.
  expect(Object.keys(seed).sort()).toEqual([
    'connectionHint',
    'contextKey',
    'launchLabel',
    'purpose',
    'query',
  ])
})

test('the context identity separates workspace, capture and revision', () => {
  const first = knowledgeLaunchSeedFromCapture(knowledgeCapture(), WORKSPACE_UID)
  const otherCapture = knowledgeLaunchSeedFromCapture(knowledgeCapture({ id: 'C-0002' }), WORKSPACE_UID)
  const otherRevision = knowledgeLaunchSeedFromCapture(knowledgeCapture({ revision: 9 }), WORKSPACE_UID)
  const otherWorkspace = knowledgeLaunchSeedFromCapture(
    knowledgeCapture(),
    '33333333-3333-4333-8333-333333333333',
  )
  const keys = [first, otherCapture, otherRevision, otherWorkspace].map((seed) => seed.contextKey)
  expect(new Set(keys).size).toBe(4)
})

test('control characters become spaces so the seeded question is usable, not refused', () => {
  // A tab, a NUL, a DEL and a C1 code point, plus whitespace the trim removes.
  const title = ` ${TAB}Rollback${NUL}owner${DEL}review${C1} `
  expect(sanitizeKnowledgeSeedQuery(title)).toBe('Rollback owner review')
  // The draft builder is the authority on the query grammar; the sanitized seed passes it.
  const built = buildKnowledgeRequestDraft({
    options: [{ alias: 'team-nas', label: 'Team NAS' }],
    purpose: 'find_context',
    query: sanitizeKnowledgeSeedQuery(title),
    resultLimit: '5',
    selectedAliases: ['team-nas'],
    workspaceUid: WORKSPACE_UID,
  })
  expect(built.ok).toBe(true)
})

test('a title with nothing usable in it stays empty rather than inventing a question', () => {
  expect(sanitizeKnowledgeSeedQuery('    ')).toBe('')
  expect(sanitizeKnowledgeSeedQuery(NUL + DEL + C1)).toBe('')
  expect(sanitizeKnowledgeSeedQuery(undefined)).toBe('')
  expect(sanitizeKnowledgeSeedQuery(42)).toBe('')
})

test('an over-long title is clipped on whole code points at the released query bound', () => {
  const seeded = sanitizeKnowledgeSeedQuery('\u{1F600}'.repeat(MAX_QUERY_CHARS + 40))
  expect(Array.from(seeded)).toHaveLength(MAX_QUERY_CHARS)
  // A clip that split a surrogate pair would leave a lone half here.
  expect(seeded).toBe('\u{1F600}'.repeat(MAX_QUERY_CHARS))
})

test('a source ref that is not spelled like an alias is no hint at all', () => {
  for (const connectionRef of ['Team NAS', '/etc/passwd', 'UPPER', '', 'ends-']) {
    const seeded = knowledgeCapture({
      source: { ...knowledgeCapture().source, connection_ref: connectionRef },
    })
    expect(knowledgeLaunchSeedFromCapture(seeded, WORKSPACE_UID).connectionHint).toBeNull()
  }
})
