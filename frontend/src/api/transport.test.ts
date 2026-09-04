import { expect, test, vi } from 'vitest'
import { taskSchema } from '../domain/schemas'
import { jsonResponse, task } from '../test/fixtures'
import { z } from 'zod'
import { ApiError, CommitUnknownError, mutateData, mutateIdempotent } from './transport'

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
