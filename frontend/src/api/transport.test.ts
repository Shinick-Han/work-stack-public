import { expect, test, vi } from 'vitest'
import { taskSchema } from '../domain/schemas'
import { jsonResponse, task } from '../test/fixtures'
import { mutateData } from './transport'

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
