import { afterEach, expect, test, vi } from 'vitest'
import { jsonResponse } from '../test/fixtures'
import {
  KNOWLEDGE_CAPTURE_OBSERVATION_PATH,
  KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH,
  KNOWLEDGE_OBSERVATION_MESSAGES,
  OBSERVATION_BINDING_MISMATCH,
  OBSERVATION_HISTORY_UNSUPPORTED,
  OBSERVATION_UNREADABLE,
  classifyKnowledgeObservationFailure,
  describeKnowledgeObservationError,
  fetchCaptureObservation,
  recordCaptureSourceCheck,
} from './knowledgeObservation'
import { KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE } from './knowledgeVerification'

const WORKSPACE = '22222222-2222-2222-2222-222222222222'
const DOC_A = 'od-page-7f3ba1d34f50c884600112ab'
const DOC_B = 'od-file-2c19aa4471bd0e33f1005511'

const EXPECTED = [
  { document_ref: DOC_A, source_type: 'notion.page' as const, expected_source_version: 'od-version-14' },
  { document_ref: DOC_B, source_type: 'nas.file' as const, expected_source_version: null },
]

const INPUT = {
  workspaceUid: WORKSPACE,
  captureId: 'C-0001',
  captureRevision: 3,
  expectedEvidence: EXPECTED,
}

function evidence() {
  return [
    {
      document_ref: DOC_A,
      source_type: 'notion.page',
      expected_source_version: 'od-version-14',
      observed_source_version: 'od-version-14',
      status: 'current',
      code: 'hash_matched',
    },
    {
      document_ref: DOC_B,
      source_type: 'nas.file',
      expected_source_version: null,
      observed_source_version: null,
      status: 'unverifiable',
      code: 'no_expected_version',
    },
  ]
}

function result(overrides: Record<string, unknown> = {}) {
  return {
    schema: 'workstack.knowledge-verification.v1',
    verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    checked_at: '2026-09-08T10:15:02Z',
    evidence: evidence(),
    ...overrides,
  }
}

function unchanged(overrides: Record<string, unknown> = {}) {
  return {
    accepted_at: '2026-09-08T10:15:04Z',
    checked_at: '2026-09-08T10:15:02Z',
    binding_state: 'unchanged',
    result: result(),
    ...overrides,
  }
}

function readyEnvelope(observation: unknown, binding?: Record<string, unknown>) {
  return {
    data: {
      binding: binding ?? { workspace_uid: WORKSPACE, capture_id: 'C-0001', capture_revision: 3 },
      observation,
    },
    meta: { outcome: 'observation_ready' },
  }
}

function recordedEnvelope(observation: unknown) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE, capture_id: 'C-0001', capture_revision: 3 },
      observation,
    },
    meta: { outcome: 'observation_recorded' },
  }
}

/** Answers the session preflight, then the route under test. */
function stubRoute(respond: () => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void init
    if (String(input).includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return respond()
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function routeCalls(mock: ReturnType<typeof stubRoute>, path: string) {
  return mock.mock.calls.filter(([input]) => String(input).startsWith(path))
}

afterEach(() => { vi.unstubAllGlobals() })

test('the history read asks for the canonical target with exactly the three identity keys', async () => {
  const mock = stubRoute(async () => await jsonResponse(readyEnvelope(null)))
  const outcome = await fetchCaptureObservation(INPUT)

  expect(outcome.observation).toBeNull()
  const [target, init] = routeCalls(mock, KNOWLEDGE_CAPTURE_OBSERVATION_PATH)[0]
  const url = new URL(String(target), 'http://127.0.0.1')
  expect(url.pathname).toBe(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)
  expect(url.hash).toBe('')
  expect([...url.searchParams.keys()]).toEqual(['workspace_uid', 'capture_id', 'capture_revision'])
  expect(url.searchParams.get('workspace_uid')).toBe(WORKSPACE)
  expect(url.searchParams.get('capture_id')).toBe('C-0001')
  // Canonical base-10 digits, not `3.0`, not an exponent, not a padded field.
  expect(url.searchParams.get('capture_revision')).toBe('3')
  expect(init?.method ?? 'GET').toBe('GET')
  // A read carries no CSRF preflight and no idempotency key.
  expect(mock.mock.calls.some(([input]) => String(input).includes('/api/v1/session'))).toBe(false)
})

test('an unchanged observation that lines up with the rendered rows is admitted whole', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(unchanged())))
  const outcome = await fetchCaptureObservation(INPUT)
  expect(outcome.observation?.binding_state).toBe('unchanged')
  expect(outcome.observation?.checked_at).toBe('2026-09-08T10:15:02Z')
  expect(outcome.observation?.result?.evidence).toHaveLength(2)
  expect(outcome.observation?.result?.evidence[0].status).toBe('current')
})

test('a changed observation keeps its times and carries no evidence at all', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope({
    accepted_at: '2026-09-08T10:15:04Z',
    checked_at: '2026-09-08T10:15:02Z',
    binding_state: 'changed',
    result: null,
  })))
  const outcome = await fetchCaptureObservation(INPUT)
  expect(outcome.observation?.binding_state).toBe('changed')
  expect(outcome.observation?.result).toBeNull()
})

test('a changed observation that still carries the old result is refused, not shown', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(unchanged({ binding_state: 'changed' }))))
  await expect(fetchCaptureObservation(INPUT)).rejects.toBeDefined()
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ name: 'ZodError' })
})

test('an unchanged observation is refused when it disagrees with its own result about the time', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(unchanged({ checked_at: '2026-09-08T10:15:03Z' }))))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ name: 'ZodError' })
})

test('an unchanged observation with no result is refused', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(unchanged({ result: null }))))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ name: 'ZodError' })
})

test('saved evidence that does not line up positionally with the rows on screen is refused', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(unchanged({
    result: result({ evidence: [evidence()[1], evidence()[0]] }),
  }))))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ code: OBSERVATION_BINDING_MISMATCH })
})

test('a saved check about another capture is refused', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(
    unchanged(),
    { workspace_uid: WORKSPACE, capture_id: 'C-0002', capture_revision: 3 },
  )))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ code: OBSERVATION_BINDING_MISMATCH })
})

test('an unpublished top-level key or a wrong outcome is refused rather than read', async () => {
  stubRoute(async () => await jsonResponse({ ...readyEnvelope(null), warnings: ['x'] }))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ name: 'ZodError' })

  stubRoute(async () => await jsonResponse({ ...readyEnvelope(null), meta: { outcome: 'verification_ready' } }))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ name: 'ZodError' })
})

test('a well-formed body returned as 206 is not the one published success status', async () => {
  stubRoute(async () => await jsonResponse(readyEnvelope(null), 206))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ code: OBSERVATION_UNREADABLE })
})

test('only a plain not_found 404 diagnoses an old server', async () => {
  stubRoute(async () => await jsonResponse({ error: { code: 'not_found', message: 'no route' } }, 404))
  await expect(fetchCaptureObservation(INPUT)).rejects.toMatchObject({ code: OBSERVATION_HISTORY_UNSUPPORTED })

  // This 404 is about the capture, not about the route: it must not send the UI back to
  // the old readonly check.
  stubRoute(async () => await jsonResponse({ error: { code: 'unknown_capture', message: 'gone' } }, 404))
  const capture404 = await fetchCaptureObservation(INPUT).catch((error: unknown) => error)
  expect(classifyKnowledgeObservationFailure(capture404).code).toBe('unknown_capture')

  // Neither is an unreadable 404 body.
  stubRoute(async () => new Response('<html>404</html>', { status: 404 }))
  const html404 = await fetchCaptureObservation(INPUT).catch((error: unknown) => error)
  expect(classifyKnowledgeObservationFailure(html404).code).not.toBe(OBSERVATION_HISTORY_UNSUPPORTED)
})

test('a read failure renders closed copy and never the backend sentence', async () => {
  stubRoute(async () => await jsonResponse(
    { error: { code: 'observation_read_unavailable', message: 'sqlite journal /srv/store.db busy' } },
    503,
  ))
  const failure = await fetchCaptureObservation(INPUT).catch((error: unknown) => error)
  const closed = classifyKnowledgeObservationFailure(failure)
  expect(closed.code).toBe('observation_read_unavailable')
  expect(closed.message).toBe('The saved source check could not be read.')
  expect(closed.message).not.toContain('sqlite')
})

test('an explicit record posts the exact contract body exactly once and keeps what it saved', async () => {
  const mock = stubRoute(async () => await jsonResponse(recordedEnvelope(unchanged())))
  const recorded = await recordCaptureSourceCheck(INPUT)

  expect(recorded.observation.binding_state).toBe('unchanged')
  expect(recorded.observation.result?.evidence[0].status).toBe('current')
  const posts = routeCalls(mock, KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)
  expect(posts).toHaveLength(1)
  const [target, init] = posts[0]
  expect(String(target)).toBe(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)
  expect(init?.method).toBe('POST')
  expect(JSON.parse(String(init?.body))).toEqual({
    workspace_uid: WORKSPACE,
    capture_id: 'C-0001',
    capture_revision: 3,
  })
  const headers = init?.headers as Record<string, string>
  expect(headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(headers['Idempotency-Key']).toBeUndefined()
})

test('a record answered with no observation, a changed one, or a 201 is refused', async () => {
  stubRoute(async () => await jsonResponse(recordedEnvelope(null)))
  await expect(recordCaptureSourceCheck(INPUT)).rejects.toMatchObject({ code: OBSERVATION_UNREADABLE })

  stubRoute(async () => await jsonResponse(recordedEnvelope({
    accepted_at: '2026-09-08T10:15:04Z',
    checked_at: '2026-09-08T10:15:02Z',
    binding_state: 'changed',
    result: null,
  })))
  await expect(recordCaptureSourceCheck(INPUT)).rejects.toMatchObject({ code: OBSERVATION_UNREADABLE })

  stubRoute(async () => await jsonResponse(recordedEnvelope(unchanged()), 201))
  await expect(recordCaptureSourceCheck(INPUT)).rejects.toMatchObject({ code: OBSERVATION_UNREADABLE })

  // The ready meta belongs to the read. A record that answers with it did not record.
  stubRoute(async () => await jsonResponse({ ...recordedEnvelope(unchanged()), meta: { outcome: 'observation_ready' } }))
  await expect(recordCaptureSourceCheck(INPUT)).rejects.toMatchObject({ name: 'ZodError' })
})

test('a record failure never repeats itself and does not fall back to the old route', async () => {
  const mock = stubRoute(async () => await jsonResponse(
    { error: { code: 'observation_save_unknown', message: 'OSError 28 on /srv/store.db' } },
    503,
  ))
  const failure = await recordCaptureSourceCheck(INPUT).catch((error: unknown) => error)
  const closed = classifyKnowledgeObservationFailure(failure)

  expect(closed.code).toBe('observation_save_unknown')
  // It must say the save is unconfirmed, and it must not claim nothing was written.
  expect(closed.message).toContain('could not be confirmed')
  expect(closed.message).not.toMatch(/not saved|nothing was saved|was discarded/i)
  expect(closed.message).not.toContain('OSError')
  expect(routeCalls(mock, KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toHaveLength(1)
  expect(routeCalls(mock, '/api/v1/knowledge/captures/verify')).toHaveLength(0)
  expect(routeCalls(mock, KNOWLEDGE_CAPTURE_OBSERVATION_PATH)).toHaveLength(0)
})

test('a capture with no checkable evidence never reaches the record route', async () => {
  const mock = stubRoute(async () => await jsonResponse(recordedEnvelope(unchanged())))
  await expect(recordCaptureSourceCheck({ ...INPUT, expectedEvidence: [] }))
    .rejects.toMatchObject({ code: 'verification_not_checkable' })
  expect(mock).not.toHaveBeenCalled()
})

test('a prototype-named backend code reaches one fixed sentence, never an inherited member', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    const described = describeKnowledgeObservationError(code)
    expect(typeof described).toBe('string')
    expect(described).toBe(KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE)
  }
  expect(Object.getPrototypeOf(KNOWLEDGE_OBSERVATION_MESSAGES)).toBeNull()
  // R21's own reviewed sentences are reused rather than re-spelled here.
  expect(describeKnowledgeObservationError('verification_busy'))
    .toBe('Another source check is already running. Try again once it finishes.')
})
