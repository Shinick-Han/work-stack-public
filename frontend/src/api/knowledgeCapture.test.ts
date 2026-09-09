import { expect, test, vi } from 'vitest'
import { jsonResponse } from '../test/fixtures'
import { KNOWLEDGE_CAPTURE_IMPORT_MESSAGES } from '../domain/schemaKnowledgeCapture'
import { ApiError, UnreadableSuccessError } from './transport'
import {
  KNOWLEDGE_CAPTURE_IMPORT_PATH,
  classifyKnowledgeCaptureImportFailure,
  importKnowledgeCaptures,
} from './knowledgeCapture'
import { knowledgeImportEnvelope, REQUEST_ID } from '../features/inbox/knowledgeCaptureFixture'

const successEnvelope = {
  data: {
    request_id: REQUEST_ID,
    capture_ids: ['C-0008'],
    completion_digest: `sha256:${'d'.repeat(64)}`,
    completed_at: '2026-09-08T11:00:00Z',
  },
  meta: { replayed: false, imported_count: 1 },
}

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function headersOf(call: [RequestInfo | URL, (RequestInit | undefined)?]) {
  return (call[1]?.headers ?? {}) as Record<string, string>
}

test('posts the exact admitted envelope to the import route with CSRF and no Idempotency-Key', async () => {
  const body = knowledgeImportEnvelope()
  const mock = stubFetch(async () => (await jsonResponse(successEnvelope)))

  await expect(importKnowledgeCaptures(body)).resolves.toEqual({
    data: successEnvelope.data,
    meta: successEnvelope.meta,
  })

  const call = mock.mock.calls.find(([input]) => String(input) === KNOWLEDGE_CAPTURE_IMPORT_PATH)
  expect(call).toBeDefined()
  expect(call?.[1]?.method).toBe('POST')
  expect(call?.[1]?.credentials).toBe('same-origin')
  expect(call?.[1]?.body).toBe(JSON.stringify(body))
  expect(headersOf(call!).Accept).toBe('application/json')
  expect(headersOf(call!)['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(headersOf(call!)['Idempotency-Key']).toBeUndefined()
})

test('retries after loss reuse the same body object and still omit Idempotency-Key', async () => {
  const body = knowledgeImportEnvelope()
  const mock = stubFetch(async () => (await jsonResponse({
    ...successEnvelope,
    meta: { replayed: true, imported_count: 1 },
  })))

  await importKnowledgeCaptures(body)
  await importKnowledgeCaptures(body)

  const posts = mock.mock.calls.filter(([input]) => String(input) === KNOWLEDGE_CAPTURE_IMPORT_PATH)
  expect(posts).toHaveLength(2)
  expect(posts[0][1]?.body).toBe(posts[1][1]?.body)
  expect(headersOf(posts[0])['Idempotency-Key']).toBeUndefined()
  expect(headersOf(posts[1])['Idempotency-Key']).toBeUndefined()
})

test('does not publish a planning change and does not call Task routes', async () => {
  const mock = stubFetch(async (url) => {
    if (url.includes('/api/v1/tasks')) throw new Error('task mutation is out of scope')
    return jsonResponse(successEnvelope)
  })
  await importKnowledgeCaptures(knowledgeImportEnvelope())
  expect(mock.mock.calls.every(([input]) => {
    const url = String(input)
    return url.startsWith('/api/v1/session') || url === KNOWLEDGE_CAPTURE_IMPORT_PATH
  })).toBe(true)
})

test('closed error codes map to authored copy and never echo submitted values', () => {
  const expired = classifyKnowledgeCaptureImportFailure(
    new ApiError(409, 'request_expired', 'backend exception leaked', { field: 'query' }),
  )
  expect(expired.kind).toBe('definitive')
  expect(expired.message).toMatch(/new search/i)
  expect(expired.message).not.toContain('backend')
  expect(expired.message).not.toContain('query')

  const pending = classifyKnowledgeCaptureImportFailure(new UnreadableSuccessError(200))
  expect(pending.kind).toBe('pending_unknown')
  expect(pending.message).toMatch(/pending/i)
})

test('classifyKnowledgeCaptureImportFailure maps inherited ApiError codes to import_refused and keeps a known allowed code', () => {
  const fallback = KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.import_refused
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
    const failure = classifyKnowledgeCaptureImportFailure(
      new ApiError(500, code, 'raw backend detail: C:\\srv\\nas', { field: 'query' }),
    )
    expect(failure.kind).toBe('refused')
    expect(failure.code).toBe(code)
    expect(failure.message).toBe(fallback)
    expect(typeof failure.message).toBe('string')
    expect(failure.message).not.toContain('nas')
    expect(failure.message).not.toContain('backend')
    expect(failure.message).not.toContain('query')
  }

  const expired = classifyKnowledgeCaptureImportFailure(
    new ApiError(409, 'request_expired', 'backend exception leaked', { field: 'query' }),
  )
  expect(expired).toEqual({
    kind: 'definitive',
    code: 'request_expired',
    message: KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.request_expired,
  })
  expect(expired.message).not.toContain('backend')
  expect(expired.message).not.toContain('query')
})

test('rotated CSRF 403 posts once; explicit retry refreshes the session then posts once more', async () => {
  const body = knowledgeImportEnvelope()
  let sessionIssue = 0
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      sessionIssue += 1
      return jsonResponse({ data: { csrf_token: `csrf-rotated-${sessionIssue}` } })
    }
    if (url === KNOWLEDGE_CAPTURE_IMPORT_PATH) {
      const csrf = ((init?.headers ?? {}) as Record<string, string>)['X-WorkStack-CSRF']
      if (csrf === 'csrf-rotated-1') {
        return jsonResponse({ error: { code: 'forbidden', message: 'stale csrf' } }, 403)
      }
      return jsonResponse(successEnvelope)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', mock)

  await expect(importKnowledgeCaptures(body)).rejects.toBeInstanceOf(ApiError)
  const firstPosts = mock.mock.calls.filter(([input, init]) => (
    String(input) === KNOWLEDGE_CAPTURE_IMPORT_PATH && init?.method === 'POST'
  ))
  expect(firstPosts).toHaveLength(1)
  expect(headersOf(firstPosts[0])['Idempotency-Key']).toBeUndefined()

  await expect(importKnowledgeCaptures(body)).resolves.toEqual({
    data: successEnvelope.data,
    meta: successEnvelope.meta,
  })
  const posts = mock.mock.calls.filter(([input, init]) => (
    String(input) === KNOWLEDGE_CAPTURE_IMPORT_PATH && init?.method === 'POST'
  ))
  const sessions = mock.mock.calls.filter(([input]) => String(input).includes('/api/v1/session'))
  expect(posts).toHaveLength(2)
  expect(sessions.length).toBeGreaterThanOrEqual(2)
  expect(headersOf(posts[0])['X-WorkStack-CSRF']).toBe('csrf-rotated-1')
  expect(headersOf(posts[1])['X-WorkStack-CSRF']).toBe('csrf-rotated-2')
  expect(headersOf(posts[0])['Idempotency-Key']).toBeUndefined()
  expect(headersOf(posts[1])['Idempotency-Key']).toBeUndefined()
  expect(posts[0][1]?.body).toBe(posts[1][1]?.body)
})

test('mismatched or inconsistent success receipts stay unknown and do not mint a new request', async () => {
  const body = knowledgeImportEnvelope()
  const cases = [
    { ...successEnvelope, data: { ...successEnvelope.data, request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' } },
    { ...successEnvelope, meta: { replayed: false, imported_count: 0 } },
    {
      ...successEnvelope,
      data: { ...successEnvelope.data, capture_ids: ['C-0008', 'C-0008'] },
      meta: { replayed: false, imported_count: 2 },
    },
    {
      ...successEnvelope,
      data: { ...successEnvelope.data, capture_ids: ['C-0008', 'C-0009'] },
      meta: { replayed: false, imported_count: 2 },
    },
  ]
  for (const payload of cases) {
    stubFetch(async () => jsonResponse(payload))
    const failure = await importKnowledgeCaptures(body).catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(UnreadableSuccessError)
    expect(classifyKnowledgeCaptureImportFailure(failure).kind).toBe('pending_unknown')
    expect(classifyKnowledgeCaptureImportFailure(failure).message).not.toContain('bbbbbbbb')
    expect(classifyKnowledgeCaptureImportFailure(failure).message).not.toContain('backend')
  }
})
