import { expect, test } from 'vitest'
import {
  buildKnowledgeRequestDraft,
  clipKnowledgeRequestQuery,
  isKnowledgeRequestExpired,
  knowledgeRequestExpiresAtMs,
  knowledgeRequestIdentity,
  knowledgeRequestQueryChars,
  knowledgeRequestReceiptJson,
  validateIssuedKnowledgeRequest,
  KNOWLEDGE_REQUEST_SCHEMA,
  MAX_QUERY_CHARS,
  type IssuedKnowledgeRequest,
  type KnowledgeCorpusOption,
  type KnowledgeRequestDraft,
  type KnowledgeRequestEditorInput,
} from './knowledgeRequestDraft'

const WORKSPACE = '66666666-6666-4666-8666-666666666666'
const TASK_UID = '77777777-7777-4777-8777-777777777777'
const REQUEST_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const REQUESTED_AT = '2026-09-08T09:00:00.000Z'
const EXPIRES_AT = '2026-09-08T09:05:00.000Z'

const OPTIONS: KnowledgeCorpusOption[] = [
  { alias: 'nas-team-share', label: 'Team share' },
  { alias: 'notion-product', label: 'Product notes' },
  { alias: 'wiki-ops', label: 'Operations wiki' },
]

function editor(overrides: Partial<KnowledgeRequestEditorInput> = {}): KnowledgeRequestEditorInput {
  return {
    options: OPTIONS,
    purpose: 'find_context',
    query: 'rollback verification owner',
    resultLimit: '5',
    selectedAliases: ['nas-team-share'],
    task: null,
    workspaceUid: WORKSPACE,
    ...overrides,
  }
}

const TASK = { id: 'T-0033', revision: 2, title: 'Confirm the rollback owner', uid: TASK_UID }

function draftOf(input: KnowledgeRequestEditorInput): KnowledgeRequestDraft {
  const built = buildKnowledgeRequestDraft(input)
  if (!built.ok) throw new Error(`expected a draft, got ${built.code}`)
  return built.draft
}

function issuedFor(draft: KnowledgeRequestDraft, overrides: Record<string, unknown> = {}) {
  return {
    binding: draft.binding,
    corpus_refs: draft.corpus_refs,
    expires_at: EXPIRES_AT,
    purpose: draft.purpose,
    query: draft.query,
    request_id: REQUEST_ID,
    requested_at: REQUESTED_AT,
    result_limit: draft.result_limit,
    schema: KNOWLEDGE_REQUEST_SCHEMA,
    ...overrides,
  }
}

test('a workspace-only request is complete, and an open Task binds identity without any Task text', () => {
  const workspaceOnly = draftOf(editor())
  expect(workspaceOnly.binding).toEqual({ workspace_uid: WORKSPACE })

  const bound = draftOf(editor({ task: TASK }))
  expect(bound.binding).toEqual({
    task_id: 'T-0033',
    task_revision: 2,
    task_uid: TASK_UID,
    workspace_uid: WORKSPACE,
  })
  // The title may seed the editable query, but it is never carried by the draft itself,
  // and the draft never mints identity, a schema string or a window.
  expect(JSON.stringify(bound)).not.toContain('Confirm the rollback owner')
  expect(Object.keys(bound).sort()).toEqual([
    'binding',
    'corpus_refs',
    'purpose',
    'query',
    'result_limit',
  ])
})

test('a partial Task identity refuses instead of being completed from this screen', () => {
  const built = buildKnowledgeRequestDraft(
    editor({ task: { id: 'T-0033', revision: 1.5, uid: TASK_UID } }),
  )
  expect(built).toMatchObject({ code: 'invalid_task_binding', ok: false })
})

test('the query must be non-empty, bounded by Unicode code points and free of control characters', () => {
  expect(buildKnowledgeRequestDraft(editor({ query: '   ' }))).toMatchObject({ code: 'invalid_query' })
  expect(buildKnowledgeRequestDraft(editor({ query: `bad${String.fromCharCode(7)}query` })))
    .toMatchObject({ code: 'invalid_query' })
  expect(buildKnowledgeRequestDraft(editor({ query: 'a'.repeat(MAX_QUERY_CHARS + 1) })))
    .toMatchObject({ code: 'query_too_long' })
  expect(draftOf(editor({ query: '  spaced out  ' })).query).toBe('spaced out')
  const astralNearBound = '😀'.repeat(600)
  expect(draftOf(editor({ query: astralNearBound })).query).toBe(astralNearBound)
  expect(knowledgeRequestQueryChars(draftOf(editor({ query: '😀'.repeat(MAX_QUERY_CHARS) })).query))
    .toBe(MAX_QUERY_CHARS)
  expect(buildKnowledgeRequestDraft(editor({ query: '😀'.repeat(MAX_QUERY_CHARS + 1) })))
    .toMatchObject({ code: 'query_too_long' })
  expect(clipKnowledgeRequestQuery('😀'.repeat(MAX_QUERY_CHARS + 1))).toBe('😀'.repeat(MAX_QUERY_CHARS))
  expect(clipKnowledgeRequestQuery(`${'a'.repeat(MAX_QUERY_CHARS - 1)}😀x`)).toBe(
    `${'a'.repeat(MAX_QUERY_CHARS - 1)}😀`,
  )
})

test('scope selection refuses empty, oversized, duplicated and un-offered corpora', () => {
  expect(buildKnowledgeRequestDraft(editor({ selectedAliases: [] })))
    .toMatchObject({ code: 'no_corpus_selected' })
  expect(
    buildKnowledgeRequestDraft(
      editor({ selectedAliases: Array.from({ length: 9 }, (_, index) => `alias-${index}`) }),
    ),
  ).toMatchObject({ code: 'too_many_corpora' })
  expect(
    buildKnowledgeRequestDraft(editor({ selectedAliases: ['nas-team-share', 'nas-team-share'] })),
  ).toMatchObject({ code: 'duplicate_corpus_ref' })
  expect(buildKnowledgeRequestDraft(editor({ selectedAliases: ['not-granted'] })))
    .toMatchObject({ code: 'corpus_not_offered' })
})

test('the purpose allowlist and the result limit are both closed', () => {
  expect(buildKnowledgeRequestDraft(editor({ purpose: 'exfiltrate' })))
    .toMatchObject({ code: 'invalid_purpose' })
  for (const limit of ['0', '11', '', '2.5', '-1', 'five', ' ']) {
    expect(buildKnowledgeRequestDraft(editor({ resultLimit: limit })))
      .toMatchObject({ code: 'invalid_result_limit' })
  }
  expect(draftOf(editor({ resultLimit: ' 10 ' })).result_limit).toBe(10)
})

test('no refusal message quotes what was typed', () => {
  const secretish = 'password=hunter2 for corpus not-granted'
  const built = buildKnowledgeRequestDraft(
    editor({ query: secretish, selectedAliases: ['not-granted'] }),
  )
  expect(built.ok).toBe(false)
  if (built.ok) return
  expect(built.message).not.toContain('hunter2')
  expect(built.message).not.toContain('not-granted')
})

test('identity changes for every edit a reader would call a different question', () => {
  const base = editor({ task: TASK })
  const baseline = knowledgeRequestIdentity(base)
  const variants: Partial<KnowledgeRequestEditorInput>[] = [
    { workspaceUid: '55555555-5555-4555-8555-555555555555' },
    { task: { ...TASK, revision: 3 } },
    { task: null },
    { purpose: 'extract_actions' },
    { query: 'rollback verification owner?' },
    { selectedAliases: ['notion-product'] },
    { resultLimit: '6' },
  ]
  for (const variant of variants) {
    expect(knowledgeRequestIdentity({ ...base, ...variant })).not.toBe(baseline)
  }
  // Reordering the same scope, and a different Task title, are the same question.
  expect(knowledgeRequestIdentity({ ...base, selectedAliases: ['nas-team-share'] })).toBe(baseline)
  expect(knowledgeRequestIdentity({ ...base, task: { ...TASK, title: 'Renamed' } })).toBe(baseline)
})

test('a well-formed issued request that answers the draft is accepted whole', () => {
  const draft = draftOf(editor({ selectedAliases: ['nas-team-share', 'notion-product'], task: TASK }))
  // A reordered corpus list is the same scope, so correspondence compares as a set.
  const checked = validateIssuedKnowledgeRequest(
    issuedFor(draft, { corpus_refs: ['notion-product', 'nas-team-share'] }),
    draft,
  )
  expect(checked.ok).toBe(true)
  if (!checked.ok) return
  expect(checked.request.request_id).toBe(REQUEST_ID)
  expect(knowledgeRequestExpiresAtMs(checked.request)).toBe(Date.parse(EXPIRES_AT))
})

test('the returned envelope is closed and its own fields are checked', () => {
  const draft = draftOf(editor())
  const cases: Array<[string, unknown]> = [
    ['malformed_response', 'not an object'],
    ['malformed_response', null],
    ['unknown_field', issuedFor(draft, { provider: 'opendocuments.ask' })],
    ['unsupported_schema', issuedFor(draft, { schema: 'workstack.knowledge-request.v2' })],
    ['invalid_request_id', issuedFor(draft, { request_id: `urn:uuid:${REQUEST_ID}` })],
    ['invalid_request_id', issuedFor(draft, { request_id: REQUEST_ID.toUpperCase() })],
    ['invalid_binding', issuedFor(draft, { binding: { workspace_uid: WORKSPACE, extra: 1 } })],
    ['invalid_timestamp', issuedFor(draft, { requested_at: '2026-09-08 09:00:00' })],
    ['invalid_request_window', issuedFor(draft, { expires_at: '2026-09-08T09:06:00.000Z' })],
    ['invalid_request_window', issuedFor(draft, { expires_at: REQUESTED_AT })],
    ['invalid_corpus_refs', issuedFor(draft, { corpus_refs: ['NAS-Team-Share'] })],
    ['invalid_corpus_refs', issuedFor(draft, { corpus_refs: [] })],
    ['invalid_result_limit', issuedFor(draft, { result_limit: 11 })],
    ['invalid_result_limit', issuedFor(draft, { result_limit: true })],
  ]
  for (const [code, response] of cases) {
    expect(validateIssuedKnowledgeRequest(response, draft)).toMatchObject({ code, ok: false })
  }

  const missing = issuedFor(draft) as Record<string, unknown>
  delete missing.expires_at
  expect(validateIssuedKnowledgeRequest(missing, draft)).toMatchObject({ code: 'missing_field' })

  expect(
    validateIssuedKnowledgeRequest(
      issuedFor(draft, { requested_at: '2026-02-30T09:00:00Z', expires_at: '2026-02-30T09:01:00Z' }),
      draft,
    ),
  ).toMatchObject({ code: 'invalid_timestamp' })
  expect(
    validateIssuedKnowledgeRequest(
      issuedFor(draft, { requested_at: '2026-02-29T09:00:00Z', expires_at: '2026-02-29T09:01:00Z' }),
      draft,
    ),
  ).toMatchObject({ code: 'invalid_timestamp' })
  expect(
    validateIssuedKnowledgeRequest(
      issuedFor(draft, {
        requested_at: '2026-09-08T09:00:00.000000001Z',
        expires_at: '2026-09-08T09:00:00.000000002Z',
      }),
      draft,
    ),
  ).toMatchObject({ ok: true })
  expect(
    validateIssuedKnowledgeRequest(
      issuedFor(draft, { requested_at: '2024-02-29T23:55:00Z', expires_at: '2024-03-01T00:00:00Z' }),
      draft,
    ),
  ).toMatchObject({ ok: true })
  const astral = '😀'.repeat(600)
  const astralDraft = draftOf(editor({ query: astral }))
  expect(
    validateIssuedKnowledgeRequest(issuedFor(astralDraft, { query: astral }), astralDraft),
  ).toMatchObject({ ok: true })
})

test('a well-formed envelope that answers a different question is refused', () => {
  const draft = draftOf(editor({ selectedAliases: ['nas-team-share'], task: TASK }))
  const otherTask = {
    task_id: 'T-0099',
    task_revision: 7,
    task_uid: '88888888-8888-4888-8888-888888888888',
    workspace_uid: WORKSPACE,
  }
  const cases: Array<[string, unknown]> = [
    ['binding_mismatch', issuedFor(draft, { binding: otherTask })],
    ['binding_mismatch', issuedFor(draft, { binding: { workspace_uid: WORKSPACE } })],
    ['purpose_mismatch', issuedFor(draft, { purpose: 'refresh_capture' })],
    ['query_mismatch', issuedFor(draft, { query: 'rollback verification owner and secrets' })],
    ['corpus_mismatch', issuedFor(draft, { corpus_refs: ['wiki-ops'] })],
    ['corpus_mismatch', issuedFor(draft, { corpus_refs: ['nas-team-share', 'wiki-ops'] })],
    ['result_limit_mismatch', issuedFor(draft, { result_limit: 9 })],
  ]
  for (const [code, response] of cases) {
    const refused = validateIssuedKnowledgeRequest(response, draft)
    expect(refused).toMatchObject({ code, ok: false })
    if (refused.ok) continue
    // The refusal explains the family; it never quotes what came back.
    expect(refused.message).not.toContain('T-0099')
    expect(refused.message).not.toContain('wiki-ops')
    expect(refused.message).not.toContain('secrets')
  }
})

test('expiry is read against the supplied clock and never moved', () => {
  const draft = draftOf(editor())
  const checked = validateIssuedKnowledgeRequest(issuedFor(draft), draft)
  expect(checked.ok).toBe(true)
  if (!checked.ok) return
  const request: IssuedKnowledgeRequest = checked.request
  const expiry = Date.parse(EXPIRES_AT)
  expect(isKnowledgeRequestExpired(request, expiry - 1)).toBe(false)
  expect(isKnowledgeRequestExpired(request, expiry)).toBe(true)
  expect(isKnowledgeRequestExpired(request, expiry + 60_000)).toBe(true)
  // Reading it twice does not extend it.
  expect(request.expires_at).toBe(EXPIRES_AT)
})

test('the receipt text is the issued document itself, in the contract field order', () => {
  const draft = draftOf(editor({ task: TASK }))
  const checked = validateIssuedKnowledgeRequest(issuedFor(draft), draft)
  expect(checked.ok).toBe(true)
  if (!checked.ok) return
  const text = knowledgeRequestReceiptJson(checked.request)
  expect(Object.keys(JSON.parse(text) as Record<string, unknown>)).toEqual([
    'schema',
    'request_id',
    'binding',
    'purpose',
    'query',
    'corpus_refs',
    'result_limit',
    'requested_at',
    'expires_at',
  ])
  expect(JSON.parse(text)).toEqual(issuedFor(draft))
})
