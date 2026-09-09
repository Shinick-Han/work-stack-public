import { expect, test, vi } from 'vitest'
import { ZodError } from 'zod'
import { jsonResponse, workspace } from '../test/fixtures'
import { CROSS_TAB_STORAGE_KEY } from '../integration/planningChangeBus'
import { ApiError } from './transport'
import {
  KNOWLEDGE_CAPTURE_VERIFY_PATH,
  KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE,
  KnowledgeVerificationError,
  classifyKnowledgeVerificationFailure,
  describeKnowledgeVerificationError,
  verifyCaptureSources,
  type ExpectedEvidence,
} from './knowledgeVerification'

const WORKSPACE_UID = workspace.workspace.id
const VERIFICATION_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const DOC_A = 'od-page-7f3ba1d34f50c884600112ab'
const DOC_B = 'od-file-2c19aa4471bd0e33f1005511'

const expectedEvidence: ExpectedEvidence[] = [
  { document_ref: DOC_A, source_type: 'notion.page', expected_source_version: 'od-version-14' },
  { document_ref: DOC_A, source_type: 'notion.page', expected_source_version: 'od-version-14' },
  { document_ref: DOC_B, source_type: 'nas.file', expected_source_version: null },
]

function entry(overrides: Record<string, unknown> = {}) {
  return {
    document_ref: DOC_A,
    source_type: 'notion.page',
    expected_source_version: 'od-version-14',
    observed_source_version: 'od-version-14',
    status: 'current',
    code: 'hash_matched',
    ...overrides,
  }
}

/** The exact success envelope R21 publishes, for the three expected rows above. */
function successEnvelope(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE_UID, capture_id: 'C-0001', capture_revision: 4 },
      result: {
        schema: 'workstack.knowledge-verification.v1',
        verification_id: VERIFICATION_ID,
        checked_at: '2026-09-09T10:15:02Z',
        evidence: [
          entry(),
          entry({ observed_source_version: 'od-version-19', status: 'stale', code: 'hash_differs' }),
          entry({
            document_ref: DOC_B,
            source_type: 'nas.file',
            expected_source_version: null,
            observed_source_version: null,
            status: 'unverifiable',
            code: 'no_expected_version',
          }),
        ],
        ...(overrides.result as Record<string, unknown> | undefined),
      },
      ...(overrides.data as Record<string, unknown> | undefined),
    },
    meta: { outcome: 'verification_ready' },
    ...(overrides.envelope as Record<string, unknown> | undefined),
  }
}

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function verify(overrides: Partial<Parameters<typeof verifyCaptureSources>[0]> = {}) {
  return verifyCaptureSources({
    workspaceUid: WORKSPACE_UID,
    captureId: 'C-0001',
    captureRevision: 4,
    expectedEvidence,
    ...overrides,
  })
}

function verifyCalls(mock: ReturnType<typeof stubFetch>) {
  return mock.mock.calls.filter(([input]) => String(input) === KNOWLEDGE_CAPTURE_VERIFY_PATH)
}

test('posts exactly the three contract body fields once, with CSRF and no Idempotency-Key', async () => {
  const envelope = successEnvelope()
  const mock = stubFetch(async () => await jsonResponse(envelope))

  const outcome = await verify()
  expect(outcome).toEqual(envelope.data)

  const calls = verifyCalls(mock)
  expect(calls).toHaveLength(1)
  const [, init] = calls[0]
  expect(init?.method).toBe('POST')
  expect(init?.credentials).toBe('same-origin')
  expect(JSON.parse(String(init?.body))).toEqual({
    workspace_uid: WORKSPACE_UID,
    capture_id: 'C-0001',
    capture_revision: 4,
  })
  const headers = (init?.headers ?? {}) as Record<string, string>
  expect(headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(headers['Idempotency-Key']).toBeUndefined()
})

test('a single attempt does not resend on a network fault or on a 403', async () => {
  const lost = stubFetch(async () => { throw new TypeError('Failed to fetch') })
  await expect(verify()).rejects.toBeInstanceOf(TypeError)
  expect(verifyCalls(lost)).toHaveLength(1)

  const forbidden = stubFetch(async () => await jsonResponse({ error: { code: 'csrf_invalid' } }, 403))
  await expect(verify()).rejects.toBeInstanceOf(ApiError)
  expect(verifyCalls(forbidden)).toHaveLength(1)
})

test('refuses a well-formed answer bound to another capture, revision or workspace', async () => {
  for (const binding of [
    { workspace_uid: '33333333-3333-4333-8333-333333333333', capture_id: 'C-0001', capture_revision: 4 },
    { workspace_uid: WORKSPACE_UID, capture_id: 'C-0002', capture_revision: 4 },
    { workspace_uid: WORKSPACE_UID, capture_id: 'C-0001', capture_revision: 5 },
  ]) {
    stubFetch(async () => await jsonResponse(successEnvelope({ data: { binding } })))
    await expect(verify()).rejects.toMatchObject({ code: 'verification_binding_mismatch' })
  }
})

test('refuses evidence that is reordered, resized or bound to another document', async () => {
  const base = successEnvelope().data.result.evidence
  for (const evidence of [
    [base[2], base[0], base[1]],
    [base[0], base[1]],
    [entry({ document_ref: DOC_B, source_type: 'notion.page' }), base[1], base[2]],
    [entry({ expected_source_version: 'od-version-77', observed_source_version: 'od-version-77' }), base[1], base[2]],
    [entry({ source_type: 'nas.file' }), base[1], base[2]],
  ]) {
    stubFetch(async () => await jsonResponse(successEnvelope({ result: { evidence } })))
    await expect(verify()).rejects.toMatchObject({ code: 'verification_binding_mismatch' })
  }
})

test('duplicate document refs are accepted positionally, not as a set', async () => {
  const evidence = successEnvelope().data.result.evidence
  // Rows 0 and 1 are the same document. Only their positions distinguish the answers.
  stubFetch(async () => await jsonResponse(successEnvelope({ result: { evidence } })))
  const outcome = await verify()
  expect(outcome.result.evidence.map((item) => item.status)).toEqual(['current', 'stale', 'unverifiable'])
})

test('refuses status and code pairs the protocol does not publish', async () => {
  const rest = successEnvelope().data.result.evidence.slice(1)
  for (const first of [
    entry({ code: 'root_unavailable' }),
    entry({ code: 'made_up_code' }),
    entry({ status: 'current', observed_source_version: 'od-version-19' }),
    entry({ status: 'stale', code: 'hash_differs', observed_source_version: 'od-version-14' }),
    entry({ status: 'stale', code: 'hash_differs', observed_source_version: null }),
    entry({ status: 'denied', code: 'access_denied', observed_source_version: 'od-version-14' }),
    entry({ status: 'unknown_status', code: 'hash_matched' }),
  ]) {
    stubFetch(async () => await jsonResponse(successEnvelope({ result: { evidence: [first, ...rest] } })))
    await expect(verify()).rejects.toBeInstanceOf(ZodError)
  }
})

test('refuses a widened envelope, a wrong schema id, a bad clock and a wrong meta outcome', async () => {
  const cases: Record<string, unknown>[] = [
    { result: { schema: 'workstack.knowledge-verification.v2' } },
    { result: { checked_at: 'yesterday' } },
    { result: { checked_at: '2026-13-45T99:99:99Z' } },
    { result: { verification_id: '00000000-0000-0000-0000-000000000000' } },
    { result: { evidence: [] } },
    { result: { note: 'extra' } },
    { data: { extra: 'field' } },
    { envelope: { meta: { outcome: 'verification_pending' } } },
    { envelope: { meta: {} } },
  ]
  for (const override of cases) {
    stubFetch(async () => await jsonResponse(successEnvelope(override)))
    await expect(verify()).rejects.toBeInstanceOf(ZodError)
  }
})

test('refuses locally, without a request, when the capture cannot be checked', async () => {
  const mock = stubFetch(async () => await jsonResponse(successEnvelope()))
  await expect(verify({ expectedEvidence: [] })).rejects.toMatchObject({ code: 'verification_not_checkable' })
  await expect(verify({
    expectedEvidence: Array.from({ length: 11 }, () => expectedEvidence[0]),
  })).rejects.toMatchObject({ code: 'verification_not_checkable' })
  await expect(verify({ captureId: 'C-1' })).rejects.toBeInstanceOf(ZodError)
  await expect(verify({ captureRevision: -1 })).rejects.toBeInstanceOf(ZodError)
  await expect(verify({ workspaceUid: 'not-a-uuid' })).rejects.toBeInstanceOf(ZodError)
  expect(verifyCalls(mock)).toHaveLength(0)
})

test('every closed backend code has its own copy and nothing else borrows backend prose', () => {
  const closed = [
    'knowledge_verifier_unavailable',
    'verification_busy',
    'verification_cleanup_unsettled',
    'verification_binding_mismatch',
    'capture_revision_changed',
    'unknown_capture',
    'capture_not_verifiable',
    'verification_authority_changed',
    'verification_result_refused',
    'driver_not_started',
    'driver_outcome_unknown',
    'invalid_request',
  ]
  const seen = new Set<string>()
  for (const code of closed) {
    const failure = classifyKnowledgeVerificationFailure(new ApiError(503, code, 'raw backend detail: C:\\srv\\nas'))
    expect(failure).toEqual({ code, message: describeKnowledgeVerificationError(code) })
    expect(failure.message).not.toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
    expect(failure.message).not.toContain('nas')
    seen.add(failure.message)
  }
  expect(seen.size).toBe(closed.length)

  const surprise = classifyKnowledgeVerificationFailure(new ApiError(500, 'internal_error', 'Traceback (most recent call last)'))
  expect(surprise.message).toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
  expect(classifyKnowledgeVerificationFailure(new TypeError('Failed to fetch')).message)
    .toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
  expect(classifyKnowledgeVerificationFailure(new KnowledgeVerificationError('verification_unreadable')).code)
    .toBe('verification_unreadable')
})

/**
 * The three response-admission corrections. Each one drives the real
 * `verifyCaptureSources` request path, not a schema in isolation, because the defect
 * being closed is what the mounted panel would have displayed.
 */

test('refuses a success-shaped 201 or 202: R21 publishes exactly one success status', async () => {
  for (const status of [201, 202]) {
    const mock = stubFetch(async () => await jsonResponse(successEnvelope(), status))
    await expect(verify()).rejects.toMatchObject({ code: 'verification_unreadable' })
    expect(verifyCalls(mock)).toHaveLength(1)
  }
})

test('refuses an unpublished top-level envelope key, which the shared envelope would drop', async () => {
  const mock = stubFetch(async () => await jsonResponse(successEnvelope({
    envelope: { extra: 'not-published' },
  })))
  await expect(verify()).rejects.toBeInstanceOf(ZodError)
  expect(verifyCalls(mock)).toHaveLength(1)

  // The same body without the extra key is the accepted success, so the refusal above is
  // the top-level key itself and not some other property of this fixture.
  stubFetch(async () => await jsonResponse(successEnvelope()))
  await expect(verify()).resolves.toBeTruthy()
})

test('refuses a checked_at the calendar does not have, even when Date.parse normalises it', async () => {
  // `Date.parse` reports a finite number for every value in this list.
  for (const checked_at of [
    '2026-02-31T10:15:02Z',
    '2026-02-29T10:15:02Z',
    '2100-02-29T10:15:02Z',
    '2026-04-31T10:15:02Z',
    '2026-00-10T10:15:02Z',
    '2026-09-00T10:15:02Z',
    '0000-09-09T10:15:02Z',
    '2026-09-09T24:00:00Z',
    '2026-09-09T10:60:02Z',
    '2026-09-09T10:15:60Z',
    '2026-09-09T10:15:02+24:00',
    '2026-09-09T10:15:02-00:60',
  ]) {
    const mock = stubFetch(async () => await jsonResponse(successEnvelope({ result: { checked_at } })))
    await expect(verify()).rejects.toBeInstanceOf(ZodError)
    expect(verifyCalls(mock)).toHaveLength(1)
  }
})

test('accepts a real leap day and the offset forms RFC3339 publishes', async () => {
  for (const checked_at of [
    '2024-02-29T10:15:02Z',
    '2000-02-29T23:59:59.999999999+14:00',
    '2026-12-31T00:00:00-00:00',
    '2026-09-09T10:15:02.5+09:30',
  ]) {
    const mock = stubFetch(async () => await jsonResponse(successEnvelope({ result: { checked_at } })))
    const outcome = await verify()
    expect(outcome.result.checked_at).toBe(checked_at)
    expect(verifyCalls(mock)).toHaveLength(1)
  }
})

test('a backend error code named after an Object.prototype member still gets the fallback', async () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
    const mock = stubFetch(async () => await jsonResponse(
      { error: { code, message: 'raw backend detail: C:\srv\nas' } },
      500,
    ))
    const failure = await verify().then(
      () => { throw new Error('the check must not succeed') },
      (error: unknown) => classifyKnowledgeVerificationFailure(error),
    )
    expect(failure).toEqual({ code, message: KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE })
    expect(typeof failure.message).toBe('string')
    expect(failure.message).not.toContain('nas')
    expect(verifyCalls(mock)).toHaveLength(1)

    // Both exported helpers, plus the error this module raises itself.
    expect(describeKnowledgeVerificationError(code)).toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
    expect(new KnowledgeVerificationError(code).message).toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
  }
})

test('an accepted check publishes no planning change', async () => {
  window.localStorage.removeItem(CROSS_TAB_STORAGE_KEY)
  const mock = stubFetch(async () => await jsonResponse(successEnvelope()))
  await expect(verify()).resolves.toBeTruthy()
  expect(verifyCalls(mock)).toHaveLength(1)
  expect(window.localStorage.getItem(CROSS_TAB_STORAGE_KEY)).toBeNull()
})
