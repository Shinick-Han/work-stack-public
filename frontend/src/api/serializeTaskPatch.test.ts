import { expect, test, vi } from 'vitest'

import { api, serializeTaskPatch } from './client'
import type { TaskPatch } from '../domain/types'
import { jsonResponse, task } from '../test/fixtures'

test('serializeTaskPatch omits unset key_result_refs and keeps an explicit empty list', () => {
  expect(serializeTaskPatch({ revision: 3, title: 'x' })).not.toHaveProperty('key_result_refs')
  expect(
    serializeTaskPatch({ revision: 3, key_result_refs: undefined } as TaskPatch),
  ).not.toHaveProperty('key_result_refs')
  expect(serializeTaskPatch({ revision: 3, key_result_refs: [] }).key_result_refs).toEqual([])
  expect(
    JSON.stringify(
      serializeTaskPatch({
        revision: 3,
        key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
      }),
    ),
  ).toContain('"key_result_refs"')
})

test('patchTask sends omitted vs empty key_result_refs through serializeTaskPatch', async () => {
  const bodies: string[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    bodies.push(String(init?.body ?? ''))
    return jsonResponse({ data: task })
  }))

  await api.patchTask('T-0001', { revision: 3, title: 'omit refs' })
  await api.patchTask('T-0001', { revision: 3, key_result_refs: [] })

  expect(JSON.parse(bodies[0])).not.toHaveProperty('key_result_refs')
  expect(JSON.parse(bodies[1]).key_result_refs).toEqual([])
})
