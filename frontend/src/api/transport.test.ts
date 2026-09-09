import { expect, test, vi } from 'vitest'
import { taskSchema } from '../domain/schemas'
import { jsonResponse, task } from '../test/fixtures'
import { z } from 'zod'
import { ApiError, CommitUnknownError, getCsrfToken, getData, mutateData, mutateIdempotent, type MutateReceipt, type ResponseReceipt } from './transport'

test('refreshes a rotated CSRF token without changing the logical mutation request', async () => {
  let sessionRequests = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) {
      sessionRequests += 1
      return jsonResponse({
        data: { csrf_token: sessionRequests === 1 ? 'expired-csrf-token' : 'refreshed-csrf-token' },
      })
    }
    if (url.endsWith('/api/v1/tasks')) {
      const csrf = (init?.headers as Record<string, string>)['X-WorkStack-CSRF']
      return csrf === 'expired-csrf-token'
        ? jsonResponse({ error: { code: 'csrf_failed', message: 'rotated' } }, 403)
        : jsonResponse({ data: task }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  await expect(mutateData(
    '/api/v1/tasks',
    'POST',
    { title: task.title },
    taskSchema,
    'workstack:stable-operation',
  )).resolves.toEqual(task)

  const mutationCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/tasks'))
  expect(mutationCalls).toHaveLength(2)
  expect(mutationCalls.map(([, init]) => init?.body)).toEqual([
    JSON.stringify({ title: task.title }),
    JSON.stringify({ title: task.title }),
  ])
  expect(mutationCalls.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])).toEqual([
    'workstack:stable-operation',
    'workstack:stable-operation',
  ])
  expect(mutationCalls.map(([, init]) => (init?.headers as Record<string, string>)['X-WorkStack-CSRF'])).toEqual([
    'expired-csrf-token',
    'refreshed-csrf-token',
  ])
})

/**
 * Fetch-count controls for the D5 single-attempt mode. Both the existing
 * default behaviour and the opt-in mode are asserted at transport level.
 */
const okEnvelope = { data: { ok: true } } as const
const okSchema = z.object({ ok: z.boolean() })

function countingFetch(plan: (attempt: number, url: string) => Response | Promise<Response>) {
  const posts: RequestInit[] = []
  let attempt = 0
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return new Response(JSON.stringify({ data: { csrf_token: 'csrf-token-for-test' } }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })
    }
    attempt += 1
    posts.push(init ?? {})
    return plan(attempt, url)
  })
  vi.stubGlobal('fetch', mock)
  return { posts, postCount: () => attempt }
}

function jsonOk(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

test('default idempotent mutation still retries the network and recovers from 403', async () => {
  const control = countingFetch((attempt) => {
    if (attempt === 1) throw new TypeError('network down')
    if (attempt === 2) return jsonOk({ error: { code: 'forbidden', message: 'stale csrf' } }, 403)
    return jsonOk(okEnvelope)
  })

  await expect(mutateIdempotent('/api/v1/thing', { a: 1 }, okSchema, 'key-default', 'unknown'))
    .resolves.toEqual({ ok: true })
  // One network retry plus one 403 recovery: the existing behaviour is intact.
  expect(control.postCount()).toBe(3)
})

test('single-attempt mode sends exactly one POST and does not retry the network', async () => {
  const control = countingFetch(() => { throw new TypeError('network down') })

  await expect(mutateIdempotent(
    '/api/v1/thing', { a: 1 }, okSchema, 'key-single', 'unknown', { singleAttempt: true },
  )).rejects.toBeInstanceOf(CommitUnknownError)
  expect(control.postCount()).toBe(1)
})

test('single-attempt mode does not resend after a 403', async () => {
  const control = countingFetch(() => jsonOk({ error: { code: 'forbidden', message: 'stale csrf' } }, 403))

  await expect(mutateIdempotent(
    '/api/v1/thing', { a: 1 }, okSchema, 'key-403', 'unknown', { singleAttempt: true },
  )).rejects.toBeInstanceOf(ApiError)
  expect(control.postCount()).toBe(1)
})

test('single-attempt 409 stays a determinate ApiError with one POST', async () => {
  const control = countingFetch(() => jsonOk({
    error: {
      code: 'checkpoint_transition_conflict',
      message: 'invalid checkpoint transition input',
      details: { transition_code: 'stale_revision' },
    },
  }, 409))

  await expect(mutateIdempotent(
    '/api/v1/thing', { a: 1 }, okSchema, 'key-409', 'unknown', { singleAttempt: true },
  )).rejects.toBeInstanceOf(ApiError)
  expect(control.postCount()).toBe(1)
})

test('a malformed 2xx is commit-unknown, never evidence the write did not happen', async () => {
  const control = countingFetch(() => jsonOk({ data: { wrong: 'shape' } }, 201))

  const failure = await mutateIdempotent(
    '/api/v1/thing', { a: 1 }, okSchema, 'key-malformed', 'may have committed', { singleAttempt: true },
  ).catch((error: unknown) => error)

  expect(failure).toBeInstanceOf(CommitUnknownError)
  expect(control.postCount()).toBe(1)
})

test('single-attempt keeps the caller key and encoded raw body unchanged', async () => {
  const control = countingFetch(() => jsonOk(okEnvelope, 201))

  await mutateIdempotent(
    '/api/v1/thing',
    { reason: { explanation: '  spaced  ' } },
    okSchema,
    'key-verbatim',
    'unknown',
    { singleAttempt: true },
  )

  expect(control.postCount()).toBe(1)
  const sent = control.posts[0]
  expect(new Headers(sent.headers).get('Idempotency-Key')).toBe('key-verbatim')
  expect(JSON.parse(String(sent.body)).reason.explanation).toBe('  spaced  ')
})

test('DELETE sends a typed If-Match and never lets callers override protected headers', async () => {
  const control = countingFetch(() => jsonOk(okEnvelope))

  await mutateData(
    '/api/v1/tasks/T-0001',
    'DELETE',
    { preview_token: 'token', confirm: 'T-0001' },
    okSchema,
    'workstack:delete-key',
    true,
    { ifMatch: '2' },
  )

  expect(control.postCount()).toBe(1)
  const headers = new Headers(control.posts[0].headers)
  expect(control.posts[0].method).toBe('DELETE')
  expect(headers.get('If-Match')).toBe('2')
  expect(headers.get('Idempotency-Key')).toBe('workstack:delete-key')
  expect(headers.get('Content-Type')).toBe('application/json')
  expect(headers.get('X-WorkStack-CSRF')).toBe('csrf-token-for-test')
  expect(headers.get('Origin')).toBeNull()
  expect([...headers.keys()].sort()).toEqual([
    'accept',
    'content-type',
    'idempotency-key',
    'if-match',
    'x-workstack-csrf',
  ])
})

test('allowed DELETE retry reuses the byte-identical body, If-Match, and idempotency key', async () => {
  let sessionRequests = 0
  let mutations = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) {
      sessionRequests += 1
      return jsonOk({
        data: { csrf_token: sessionRequests === 1 ? 'expired-csrf-token' : 'refreshed-csrf-token' },
      })
    }
    mutations += 1
    if (mutations === 1) throw new TypeError('network down')
    if (mutations === 2) return jsonOk({ error: { code: 'forbidden', message: 'stale csrf' } }, 403)
    return jsonOk(okEnvelope)
  })
  vi.stubGlobal('fetch', fetchMock)
  await getCsrfToken(true)

  await expect(mutateIdempotent(
    '/api/v1/tasks/T-0001',
    { preview_token: 'token', confirm: 'T-0001' },
    okSchema,
    'workstack:stable-delete',
    'unknown',
    { ifMatch: '2', method: 'DELETE' },
  )).resolves.toEqual({ ok: true })

  const mutationCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/tasks/T-0001'))
  expect(mutationCalls).toHaveLength(3)
  expect(mutationCalls.map(([, init]) => init?.method)).toEqual(['DELETE', 'DELETE', 'DELETE'])
  expect(mutationCalls.map(([, init]) => init?.body)).toEqual([
    JSON.stringify({ preview_token: 'token', confirm: 'T-0001' }),
    JSON.stringify({ preview_token: 'token', confirm: 'T-0001' }),
    JSON.stringify({ preview_token: 'token', confirm: 'T-0001' }),
  ])
  expect(mutationCalls.map(([, init]) => new Headers(init?.headers).get('Idempotency-Key'))).toEqual([
    'workstack:stable-delete',
    'workstack:stable-delete',
    'workstack:stable-delete',
  ])
  expect(mutationCalls.map(([, init]) => new Headers(init?.headers).get('If-Match'))).toEqual(['2', '2', '2'])
  expect(mutationCalls.map(([, init]) => new Headers(init?.headers).get('X-WorkStack-CSRF'))).toEqual([
    'expired-csrf-token',
    'expired-csrf-token',
    'refreshed-csrf-token',
  ])
})

/**
 * The opt-in strict success envelope. The shared envelope stays non-strict for every
 * caller that does not ask; a caller that does gets the original payload.
 */
const STRICT_ENVELOPE = z.object({ data: z.object({ ok: z.boolean() }).strict() }).strict()

function widenedEnvelopeFetch() {
  return vi.fn((input: RequestInfo | URL) => String(input).endsWith('/api/v1/session')
    ? jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    : jsonResponse({ data: { ok: true }, extra: 'not-published' }))
}

test('an opt-in strict envelope refuses a top-level key the shared envelope would drop', async () => {
  vi.stubGlobal('fetch', widenedEnvelopeFetch())
  await getCsrfToken(true)

  await expect(mutateData(
    '/api/v1/knowledge/captures/verify',
    'POST',
    { probe: true },
    okSchema,
    undefined,
    false,
    { strictEnvelope: STRICT_ENVELOPE },
  )).rejects.toBeInstanceOf(z.ZodError)
})

test('omitting the option leaves the existing non-strict envelope behaviour unchanged', async () => {
  const fetchMock = widenedEnvelopeFetch()
  vi.stubGlobal('fetch', fetchMock)
  await getCsrfToken(true)

  // Byte-identical response, no option: the unknown top-level key is still dropped and
  // the caller still receives its parsed data, exactly as before the seam existed.
  const receipt: MutateReceipt = {}
  await expect(mutateData(
    '/api/v1/tasks',
    'POST',
    { probe: true },
    okSchema,
    undefined,
    false,
    { receipt },
  )).resolves.toEqual({ ok: true })
  expect(receipt.status).toBe(200)

  const mutations = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/tasks'))
  expect(mutations).toHaveLength(1)
})

/**
 * The opt-in GET receipt. The first test is the important one: the option exists so a
 * route may read its own envelope `meta`, and a caller that does not ask for one must be
 * exactly where it was — same single request, same headers, same closed `data` parse.
 */

test('a GET without a receipt is the read it has always been, meta present or not', async () => {
  const control = countingFetch(() =>
    jsonOk({ data: { ok: true }, meta: { occupancy: { request_count: 7 } } }),
  )

  await expect(getData('/api/v1/thing', okSchema)).resolves.toEqual({ ok: true })

  expect(control.postCount()).toBe(1)
  const sent = control.posts[0]
  expect(sent.method).toBeUndefined()
  expect(sent.body).toBeUndefined()
  const headers = new Headers(sent.headers)
  // A read still mints no CSRF token and sends no body headers.
  expect(headers.get('X-WorkStack-CSRF')).toBeNull()
  expect(headers.get('Content-Type')).toBeNull()
  expect(headers.get('Idempotency-Key')).toBeNull()
  expect([...headers.keys()]).toEqual(['accept'])
})

test('an opt-in GET receipt reports the status and meta of that same single answer', async () => {
  const control = countingFetch(() =>
    jsonOk({ data: { ok: true }, meta: { occupancy: { request_count: 7 } } }),
  )
  const receipt: ResponseReceipt = {}

  await expect(getData('/api/v1/thing', okSchema, receipt)).resolves.toEqual({ ok: true })

  expect(receipt).toEqual({ meta: { occupancy: { request_count: 7 } }, status: 200 })
  // The meta is read off the answer already in hand. No second request, no second route.
  expect(control.postCount()).toBe(1)
})

test('a receipt does not soften a refusal or widen the closed data parse', async () => {
  const refused: ResponseReceipt = {}
  const control = countingFetch(() =>
    jsonOk({ error: { code: 'knowledge_backend_unsupported', message: 'refused' } }, 409),
  )

  await expect(getData('/api/v1/thing', okSchema, refused)).rejects.toBeInstanceOf(ApiError)
  expect(refused.status).toBe(409)
  // Nothing was admitted, so nothing was reported as meta either.
  expect(refused.meta).toBeUndefined()
  expect(control.postCount()).toBe(1)

  const malformed: ResponseReceipt = {}
  countingFetch(() => jsonOk({ data: { wrong: 'shape' }, meta: { occupancy: { request_count: 7 } } }))
  // A well-formed meta beside a `data` the caller's schema refuses is still a refusal.
  await expect(getData('/api/v1/thing', okSchema, malformed)).rejects.toThrow()
})

test('a GET receipt on a route that publishes no meta reports none', async () => {
  countingFetch(() => jsonOk({ data: { ok: true } }))
  const receipt: ResponseReceipt = {}

  await expect(getData('/api/v1/thing', okSchema, receipt)).resolves.toEqual({ ok: true })

  expect(receipt.status).toBe(200)
  expect(receipt.meta).toBeUndefined()
})
