import { afterEach, expect, test, vi } from 'vitest'
import { jsonResponse, task } from '../test/fixtures'
import {
  MUTATION_NOTICES_PATH,
  MUTATION_UNDO_FALLBACK_MESSAGE,
  MUTATION_UNDO_MESSAGES,
  MUTATION_UNDO_UNREADABLE,
  classifyMutationUndoFailure,
  describeMutationNoticeListFailure,
  describeMutationUndoFailure,
  listMutationNotices,
  mutationNoticeUndoPath,
  mutationNoticesPath,
  mutationUndoTarget,
  undoMutationNotice,
  type MutationNotice,
  type MutationUndoTarget,
} from './mutationNotices'

/**
 * The shipped mutation-notice wire, exercised against a network mock of the exact contract
 * the owner pins in `tests/test_mutation_notice_api.py`. That file is the server half; this
 * one is the client half of the same wire.
 */

const NOTICE_ID = '3b1f5e2a-8c7d-5e4f-9a1b-2c3d4e5f6a7b'
const OTHER_NOTICE_ID = '4c2a6f3b-9d8e-5f60-ab2c-3d4e5f6a7b8c'
const WORKSPACE_UID = '5d3b7a4c-ae9f-5071-bc3d-4e5f6a7b8c9d'
const MUTATION_UID = '6e4c8b5d-bf10-5182-8d4e-5f6a7b8c9d0e'
const CURSOR = `eyJlIjoiRS0wMDAwMDEiLCJ2IjoxLCJ3IjoiJHt3fSJ9${'A'.repeat(8)}`
const KEY = 'workstack:99999999-8888-4777-8666-555555555555'
/** The post-undo row: the notice's before-status, one revision past the one that was sent. */
const RESTORED = { ...task, status: 'open' as const, revision: 3 }

function notice(overrides: Partial<MutationNotice> = {}): MutationNotice {
  return {
    format: 'workstack.mutation-notice',
    schema_version: 1,
    notice_id: NOTICE_ID,
    workspace_uid: WORKSPACE_UID,
    mutation_uid: MUTATION_UID,
    entity_kind: 'task',
    entity_uid: task.uid,
    operation: 'task.status',
    before_revision: 1,
    after_revision: 2,
    source: 'cli',
    actor: 'local.user',
    idempotency_key: 'wire.status.0001',
    commit_state: 'committed',
    summary: 'Task status open to started',
    undoable: true,
    status_before: 'open',
    status_after: 'started',
    ...overrides,
  }
}

function target(overrides: Partial<MutationUndoTarget> = {}): MutationUndoTarget {
  return {
    noticeId: NOTICE_ID,
    entityUid: task.uid,
    revision: 2,
    restoredStatus: 'open',
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function undoRequest(mock: ReturnType<typeof stubFetch>) {
  const call = mock.mock.calls.find(([input]) => String(input).includes('/undo'))
  if (!call) throw new Error('no undo request was made')
  const init = call[1] as RequestInit
  return {
    url: String(call[0]),
    init,
    headers: init.headers as Record<string, string>,
    body: JSON.parse(String(init.body)) as Record<string, unknown>,
  }
}

/** A refusal is the only expected end of these calls; a success here is a test failure. */
async function failureOf(run: () => Promise<unknown>) {
  const outcome = await run().then(
    () => null,
    (error: unknown) => classifyMutationUndoFailure(error),
  )
  if (outcome === null) throw new Error('the undo request unexpectedly succeeded')
  return outcome
}

test('the list query carries only the published keys, and clamps the published limit', () => {
  expect(mutationNoticesPath()).toBe(`${MUTATION_NOTICES_PATH}?limit=20`)
  expect(mutationNoticesPath({ limit: 1 })).toBe(`${MUTATION_NOTICES_PATH}?limit=1`)
  // The owner admits 1..50 and answers 400 outside it, so nothing else may leave here.
  expect(mutationNoticesPath({ limit: 0 })).toBe(`${MUTATION_NOTICES_PATH}?limit=1`)
  expect(mutationNoticesPath({ limit: 5000 })).toBe(`${MUTATION_NOTICES_PATH}?limit=50`)
  expect(mutationNoticesPath({ limit: Number.NaN })).toBe(`${MUTATION_NOTICES_PATH}?limit=20`)
  expect(mutationNoticesPath({ cursor: CURSOR })).toBe(`${MUTATION_NOTICES_PATH}?limit=20&cursor=${CURSOR}`)
  // A cursor that fails the owner's own grammar is dropped, never echoed into a 400.
  expect(mutationNoticesPath({ cursor: 'E-000001' })).toBe(`${MUTATION_NOTICES_PATH}?limit=20`)
  expect(mutationNoticesPath({ cursor: 'short' })).toBe(`${MUTATION_NOTICES_PATH}?limit=20`)
  expect(mutationNoticesPath({ cursor: `${CURSOR}!!` })).toBe(`${MUTATION_NOTICES_PATH}?limit=20`)
})

test('a page parses its items and cursor, and refuses an unpublished field', async () => {
  const mock = stubFetch(() => jsonResponse({
    data: { items: [notice(), notice({ notice_id: OTHER_NOTICE_ID })], next_cursor: CURSOR },
  }))

  const page = await listMutationNotices({ limit: 2 })

  expect(String(mock.mock.calls[0][0])).toBe(`${MUTATION_NOTICES_PATH}?limit=2`)
  expect(page.items.map((item) => item.notice_id)).toEqual([NOTICE_ID, OTHER_NOTICE_ID])
  expect(page.next_cursor).toBe(CURSOR)

  stubFetch(() => jsonResponse({ data: { items: [{ ...notice(), extra: 1 }], next_cursor: null } }))
  await expect(listMutationNotices()).rejects.toThrow()
})

test('a commit_unknown row parses with no after-revision and no invented status pair', async () => {
  stubFetch(() => jsonResponse({
    data: {
      items: [notice({
        operation: 'capture.ingest',
        entity_kind: 'capture',
        after_revision: null,
        commit_state: 'commit_unknown',
        summary: 'Mutation outcome is unverifiable',
        undoable: false,
        status_before: null,
        status_after: null,
      })],
      next_cursor: null,
    },
  }))

  const page = await listMutationNotices()

  expect(page.items[0].after_revision).toBeNull()
  expect(page.items[0].status_before).toBeNull()
  expect(page.next_cursor).toBeNull()
})

test('only a committed, reversible task.status row is offered as an undo target', () => {
  expect(mutationUndoTarget(notice())).toEqual(target())
  // The owner's own verdict leads.
  expect(mutationUndoTarget(notice({ undoable: false }))).toBeNull()
  // `task.permanent_delete` is declared non-undoable by the contract, and reads that way.
  expect(mutationUndoTarget(notice({
    operation: 'task.permanent_delete',
    summary: 'Permanent deletion is not undoable',
    undoable: false,
    status_before: null,
    status_after: null,
  }))).toBeNull()
  // A row with no after-revision has no revision to compare against, so no body to build.
  expect(mutationUndoTarget(notice({ commit_state: 'commit_unknown', after_revision: null }))).toBeNull()
  expect(mutationUndoTarget(notice({ status_before: null }))).toBeNull()
})

test('the undo posts exactly the published path, method, body and headers', async () => {
  const mock = stubFetch(() => jsonResponse({
    data: RESTORED,
    meta: { replayed: false, undone_notice_id: NOTICE_ID },
  }))

  const outcome = await undoMutationNotice(target(), KEY)

  const request = undoRequest(mock)
  expect(request.url).toBe(`/api/v1/mutation-notices/${NOTICE_ID}/undo`)
  expect(request.url).toBe(mutationNoticeUndoPath(NOTICE_ID))
  expect(request.init.method).toBe('POST')
  expect(request.init.credentials).toBe('same-origin')
  // Exactly the one contract field, carrying the notice's own recorded after-revision.
  expect(request.body).toEqual({ revision: 2 })
  expect(request.headers['Idempotency-Key']).toBe(KEY)
  expect(request.headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(outcome.task.status).toBe('open')
  expect(outcome.task.revision).toBe(3)
})

test('one intent is exactly one POST, and a replay is recognised on the same evidence', async () => {
  const mock = stubFetch(() => jsonResponse({
    data: RESTORED,
    meta: { replayed: true, undone_notice_id: NOTICE_ID },
  }))

  const outcome = await undoMutationNotice(target(), KEY)

  expect(outcome.task.revision).toBe(3)
  expect(mock.mock.calls.filter(([input]) => String(input).includes('/undo'))).toHaveLength(1)
})

test('a stale revision is a determinate conflict that undid nothing', async () => {
  stubFetch(() => jsonResponse(
    { error: { code: 'revision_conflict', message: 'remote prose that must never be shown' } },
    409,
  ))

  const outcome = await failureOf(() => undoMutationNotice(target(), KEY))

  expect(outcome.kind).toBe('conflict')
  expect(outcome.message).toBe(MUTATION_UNDO_MESSAGES.revision_conflict)
  expect(outcome.message).toContain('nothing was undone')
})

test('an unreachable owner, a 5xx and an unrecognised 200 are all the same unknown', async () => {
  // A failed fetch: the owner may never have seen it, or may have committed it.
  stubFetch(() => Promise.reject(new TypeError('network down')))
  expect((await failureOf(() => undoMutationNotice(target(), KEY))).kind).toBe('ambiguous')

  stubFetch(() => jsonResponse({ error: { code: 'internal_error' } }, 503))
  const unavailable = await failureOf(() => undoMutationNotice(target(), KEY))
  expect(unavailable.kind).toBe('ambiguous')
  expect(unavailable.code).toBe(MUTATION_UNDO_UNREADABLE)
  expect(unavailable.message).toContain('could not confirm')

  // A schema-valid Task that is not the compensation that was asked for.
  stubFetch(() => jsonResponse({ data: { ...RESTORED, status: 'started' } }))
  expect((await failureOf(() => undoMutationNotice(target(), KEY))).kind).toBe('ambiguous')

  stubFetch(() => jsonResponse({ data: { ...RESTORED, revision: 9 } }))
  expect((await failureOf(() => undoMutationNotice(target(), KEY))).kind).toBe('ambiguous')

  stubFetch(() => jsonResponse({ data: RESTORED }, 202))
  expect((await failureOf(() => undoMutationNotice(target(), KEY))).kind).toBe('ambiguous')
})

test('only the owner receipt naming THIS notice admits an undo', async () => {
  // The exact published envelope, on the write and on the stored replay alike.
  for (const replayed of [false, true]) {
    stubFetch(() => jsonResponse({
      data: RESTORED,
      meta: { replayed, undone_notice_id: NOTICE_ID },
    }))
    expect((await undoMutationNotice(target(), KEY)).task.revision).toBe(3)
  }

  // Every counterexample below carries the SAME proven Task at the same revision and
  // status, so only the receipt separates them. None may be announced as an undo.
  const refused: Array<[string, unknown]> = [
    // No receipt at all: a 200 that never says which change it settled.
    ['no meta', { data: RESTORED }],
    // A receipt for another notice: an answer to a request this row did not make.
    ['another notice', { data: RESTORED, meta: { replayed: false, undone_notice_id: OTHER_NOTICE_ID } }],
    // The contract publishes a boolean, not a string and not an absence.
    ['replayed missing', { data: RESTORED, meta: { undone_notice_id: NOTICE_ID } }],
    ['replayed not boolean', { data: RESTORED, meta: { replayed: 'false', undone_notice_id: NOTICE_ID } }],
    ['notice id missing', { data: RESTORED, meta: { replayed: false } }],
    ['notice id malformed', { data: RESTORED, meta: { replayed: false, undone_notice_id: 'not-a-uuid' } }],
    // An unpublished key, in the receipt and beside it: a different document either way.
    ['extra meta key', { data: RESTORED, meta: { replayed: false, undone_notice_id: NOTICE_ID, undone: true } }],
    ['extra envelope key', { data: RESTORED, meta: { replayed: false, undone_notice_id: NOTICE_ID }, undone: true }],
  ]
  for (const [name, body] of refused) {
    stubFetch(() => jsonResponse(body))
    const outcome = await failureOf(() => undoMutationNotice(target(), KEY))
    expect(outcome.kind, name).toBe('ambiguous')
    expect(outcome.code, name).toBe(MUTATION_UNDO_UNREADABLE)
    expect(outcome.message, name).toContain('could not confirm')
  }
})

test('a malformed intent never reaches the network', async () => {
  const mock = stubFetch(() => jsonResponse({ data: RESTORED }))

  await expect(undoMutationNotice(target(), '')).rejects.toThrow()
  await expect(undoMutationNotice(target({ revision: -1 }), KEY)).rejects.toThrow()

  expect(mock.mock.calls.filter(([input]) => String(input).includes('/undo'))).toHaveLength(0)
})

test('no backend prose reaches the screen, and no inherited member can pose as copy', () => {
  expect(describeMutationUndoFailure('constructor')).toBe(MUTATION_UNDO_FALLBACK_MESSAGE)
  expect(describeMutationUndoFailure('__proto__')).toBe(MUTATION_UNDO_FALLBACK_MESSAGE)
  expect(describeMutationUndoFailure('a_code_nobody_published')).toBe(MUTATION_UNDO_FALLBACK_MESSAGE)
  expect(describeMutationUndoFailure('not_found')).toBe(MUTATION_UNDO_MESSAGES.not_found)
  for (const message of Object.values(MUTATION_UNDO_MESSAGES)) {
    expect(typeof message).toBe('string')
  }
})

test('a read failure separates a workspace that refused from one that was unreachable', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'invalid_query' } }, 400))
  const refused = await listMutationNotices().catch((error: unknown) => describeMutationNoticeListFailure(error))
  expect(refused).toContain('could not read recent changes')

  stubFetch(() => Promise.reject(new TypeError('network down')))
  const unreachable = await listMutationNotices().catch((error: unknown) => describeMutationNoticeListFailure(error))
  expect(unreachable).toContain('could not reach the workspace owner')
})
