import { expect, test, vi } from 'vitest'
import { jsonResponse } from '../test/fixtures'
import { ApiError, UnreadableSuccessError } from './transport'
import {
  KNOWLEDGE_EXECUTE_PATH,
  describeKnowledgeExecutionFailure,
  executeKnowledgeRequest,
} from './knowledgeExecution'
import type { IssuedKnowledgeRequestWire } from './knowledge'

const REQUEST: IssuedKnowledgeRequestWire = {
  schema: 'workstack.knowledge-request.v1',
  request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  binding: { workspace_uid: '22222222-2222-2222-2222-222222222222' },
  purpose: 'find_context',
  query: 'rollback verification owner',
  corpus_refs: ['nas-team-share'],
  result_limit: 5,
  requested_at: '2026-09-08T09:32:07Z',
  expires_at: '2099-09-08T09:37:07Z',
}

const proposal = {
  schema: 'workstack.knowledge-import.v1',
  request_id: REQUEST.request_id,
  items: [{ item_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }],
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

test('posts the exact issued request with CSRF, no Idempotency-Key, and no Task route', async () => {
  const mock = stubFetch(async (url) => {
    if (url.includes('/api/v1/tasks')) throw new Error('task mutation is out of scope')
    return jsonResponse({ data: proposal, meta: { outcome: 'proposal_ready' } })
  })

  await expect(executeKnowledgeRequest(REQUEST)).resolves.toEqual(proposal)

  const call = mock.mock.calls.find(([input]) => String(input) === KNOWLEDGE_EXECUTE_PATH)
  expect(call).toBeDefined()
  expect(call?.[1]?.method).toBe('POST')
  expect(call?.[1]?.credentials).toBe('same-origin')
  expect(call?.[1]?.body).toBe(JSON.stringify(REQUEST))
  expect(headersOf(call!).Accept).toBe('application/json')
  expect(headersOf(call!)['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(headersOf(call!)['Idempotency-Key']).toBeUndefined()
  expect(mock.mock.calls.every(([input]) => {
    const url = String(input)
    return url.startsWith('/api/v1/session') || url === KNOWLEDGE_EXECUTE_PATH
  })).toBe(true)
})

test('rotated CSRF 403 posts once and does not resend', async () => {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-stale' } })
    }
    if (url === KNOWLEDGE_EXECUTE_PATH) {
      return jsonResponse({ error: { code: 'forbidden', message: 'stale csrf' } }, 403)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', mock)

  await expect(executeKnowledgeRequest(REQUEST)).rejects.toBeInstanceOf(ApiError)
  const posts = mock.mock.calls.filter(([input, init]) => (
    String(input) === KNOWLEDGE_EXECUTE_PATH && init?.method === 'POST'
  ))
  expect(posts).toHaveLength(1)
  expect(headersOf(posts[0])['Idempotency-Key']).toBeUndefined()
})

test('malformed success meta stays unreadable and is not a retry signal', async () => {
  const cases = [
    { data: proposal },
    { data: proposal, meta: { outcome: 'stored' } },
    { data: proposal, meta: { outcome: 'proposal_ready', extra: true } },
  ]
  for (const payload of cases) {
    stubFetch(async () => jsonResponse(payload))
    const failure = await executeKnowledgeRequest(REQUEST).catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(UnreadableSuccessError)
    expect(describeKnowledgeExecutionFailure(failure)).not.toContain('backend')
    expect(describeKnowledgeExecutionFailure(failure)).not.toMatch(/run again|new request/i)
  }
})

test('unavailable and unknown codes use closed copy and never echo server text or the query', () => {
  const unavailable = describeKnowledgeExecutionFailure(
    new ApiError(503, 'knowledge_driver_unavailable', 'Notion adapter leaked', { query: REQUEST.query }),
  )
  expect(unavailable).toMatch(/does not run one/i)
  expect(unavailable).not.toMatch(/Notion|NAS already connected/i)
  expect(unavailable).not.toContain('leaked')
  expect(unavailable).not.toContain(REQUEST.query)

  const unknown = describeKnowledgeExecutionFailure(
    new ApiError(502, 'child_stderr_leaked', 'traceback: secret', { field: 'query' }),
  )
  expect(unknown).toMatch(/not known/i)
  expect(unknown).not.toContain('traceback')
  expect(unknown).not.toContain('secret')
  expect(unknown).not.toMatch(/automatic|sent again with a new request/i)
})

const EXECUTION_FALLBACK =
  'The search outcome is not known. Check the connector, or paste an existing result if you have one. This request is not sent again.'
const DRIVER_UNAVAILABLE_COPY =
  'No search connector is configured on this server. The default server does not run one. If you already have a result, paste it into Import.'

test('inherited Object.prototype names and an ordinary unknown code return the fallback string', () => {
  const known = describeKnowledgeExecutionFailure(
    new ApiError(503, 'knowledge_driver_unavailable', 'Notion adapter leaked', { query: REQUEST.query }),
  )
  expect(known).toBe(DRIVER_UNAVAILABLE_COPY)
  expect(typeof known).toBe('string')

  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf', 'child_stderr_leaked']) {
    const message = describeKnowledgeExecutionFailure(
      new ApiError(500, code, 'raw backend detail: C:\\srv\\nas', { field: 'query', token: 'secret' }),
    )
    expect(message).toBe(EXECUTION_FALLBACK)
    expect(typeof message).toBe('string')
    expect(message).not.toMatch(/native code|\[object Object\]|function /)
    expect(message).not.toContain('nas')
    expect(message).not.toContain('backend')
    expect(message).not.toContain('query')
    expect(message).not.toContain('secret')
    expect(message).not.toContain(REQUEST.query)
  }
})
