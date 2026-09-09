import { expect, test } from 'vitest'
import {
  buildConnectionPolicyBody,
  connectionCorpusOptions,
  connectionPolicyRows,
  parseCorpusRefs,
  type ConnectionPolicyRow,
} from './knowledgeConnectionPolicy'

const UPSTREAM = '66666666-6666-4666-8666-666666666666'

function row(overrides: Partial<ConnectionPolicyRow> = {}): ConnectionPolicyRow {
  return {
    alias: 'team-nas',
    corpusRefs: 'nas-team-share, product-notes',
    upstreamWorkspaceUid: UPSTREAM,
    ...overrides,
  }
}

test('builds the closed policy body with the loaded revision as the compare-and-set guard', () => {
  const result = buildConnectionPolicyBody([row()], 4)
  expect(result.ok).toBe(true)
  if (!result.ok) return
  expect(result.body).toEqual({
    connections: [
      {
        alias: 'team-nas',
        corpus_refs: ['nas-team-share', 'product-notes'],
        upstream_workspace_uid: UPSTREAM,
      },
    ],
    expected_policy_revision: 4,
  })
  // `scope` is the server's, so the body has no field that could widen the grant.
  expect(Object.keys(result.body.connections[0]).sort()).toEqual([
    'alias',
    'corpus_refs',
    'upstream_workspace_uid',
  ])
})

test('refuses an alias that tries to be a location rather than a label', () => {
  for (const alias of [
    'https://intranet.example.invalid/share',
    '\\\\nas\\team',
    'C:/notes',
    'user:secret@host',
    'Team-NAS',
  ]) {
    const result = buildConnectionPolicyBody([row({ alias })], 0)
    expect(result.ok).toBe(false)
    if (result.ok) continue
    expect(result.code).toBe('invalid_alias')
    // The refusal names the problem, never the value.
    expect(result.message).not.toContain(alias)
  }
})

test('refuses a corpus alias that is a path, and a repeated one', () => {
  const path = buildConnectionPolicyBody([row({ corpusRefs: 'nas-team-share, ../secrets' })], 0)
  expect(path).toMatchObject({ code: 'invalid_corpus_ref', ok: false })
  const repeated = buildConnectionPolicyBody([row({ corpusRefs: 'a-corpus a-corpus' })], 0)
  expect(repeated).toMatchObject({ code: 'duplicate_corpus_ref', ok: false })
})

test('refuses an upstream workspace that is not a canonical UUID, and an empty roster', () => {
  expect(buildConnectionPolicyBody([row({ upstreamWorkspaceUid: 'team-nas' })], 0)).toMatchObject({
    code: 'invalid_upstream_workspace_uid',
    ok: false,
  })
  expect(buildConnectionPolicyBody([], 0)).toMatchObject({ code: 'no_connections', ok: false })
  expect(buildConnectionPolicyBody([row(), row()], 0)).toMatchObject({
    code: 'duplicate_alias',
    ok: false,
  })
})

test('splits a typed corpus list on commas and whitespace and drops empty runs', () => {
  expect(parseCorpusRefs('  a-corpus ,, b-corpus\n c-corpus  ')).toEqual([
    'a-corpus',
    'b-corpus',
    'c-corpus',
  ])
  expect(parseCorpusRefs('   ')).toEqual([])
})

test('offers only the corpora the selected connection actually returned', () => {
  const connection = {
    alias: 'team-nas',
    corpus_refs: ['nas-team-share', 'product-notes'],
    scope: 'workspace' as const,
    upstream_workspace_uid: UPSTREAM,
  }
  expect(connectionCorpusOptions(connection)).toEqual([
    { alias: 'nas-team-share', description: null, label: 'nas-team-share' },
    { alias: 'product-notes', description: null, label: 'product-notes' },
  ])
  expect(connectionCorpusOptions(null)).toEqual([])
  expect(connectionPolicyRows([connection])).toEqual([
    {
      alias: 'team-nas',
      corpusRefs: 'nas-team-share, product-notes',
      upstreamWorkspaceUid: UPSTREAM,
    },
  ])
})
