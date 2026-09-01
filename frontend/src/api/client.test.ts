import { expect, test, vi } from 'vitest'
import { ApiError, CommitUnknownError, api } from './client'
import type { CapturePacket, ReplyCommand, ReplyReceipt } from '../domain/types'
import { capture, jsonResponse, task, workspace } from '../test/fixtures'
import { CROSS_TAB_STORAGE_KEY } from '../integration/planningChangeBus'

test('publishes cross-tab refresh only after a confirmed mutation, never after a read', async () => {
  window.localStorage.removeItem(CROSS_TAB_STORAGE_KEY)
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/tasks')) return jsonResponse({ data: task }, 201)
    throw new Error(`Unexpected request: ${url}`)
  }))

  await api.getWorkspace()
  expect(window.localStorage.getItem(CROSS_TAB_STORAGE_KEY)).toBeNull()

  await api.createTask({ title: task.title })
  expect(JSON.parse(window.localStorage.getItem(CROSS_TAB_STORAGE_KEY) ?? '{}')).toMatchObject({ version: 1 })
})

test('retains one Idempotency-Key across a network retry', async () => {
  let captureAttempts = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/api/v1/captures')) {
      captureAttempts += 1
      if (captureAttempts === 1) return Promise.reject(new TypeError('temporary connection reset'))
      return jsonResponse({ data: { capture } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  const packet: CapturePacket = {
    schema_version: capture.schema_version,
    source_key: capture.source_key,
    source: capture.source,
    normalized: {
      ...capture.normalized,
      action_items: capture.normalized.action_items.map(({ id: _id, task_id: _taskId, ...action }) => action),
    },
    task_hints: capture.task_hints,
    provenance: capture.provenance,
  }
  await expect(api.ingestCapture(packet)).resolves.toMatchObject({ id: 'C-0001' })

  const captureCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/api/v1/captures'))
  expect(captureCalls).toHaveLength(2)
  const firstHeaders = captureCalls[0][1]?.headers as Record<string, string>
  const secondHeaders = captureCalls[1][1]?.headers as Record<string, string>
  expect(firstHeaders['Idempotency-Key']).toMatch(/^workstack:/)
  expect(secondHeaders['Idempotency-Key']).toBe(firstHeaders['Idempotency-Key'])
})

test('uses protected idempotent browser routes for source tasks, approved replies, and receipts', async () => {
  const bodyDigest = `sha256:${'c'.repeat(64)}`
  const targetDigest = `sha256:${'d'.repeat(64)}`
  const command: ReplyCommand = {
    id: 'R-0001',
    task_id: task.id,
    capture_id: capture.id,
    capture_revision: capture.revision,
    provider: 'microsoft-outlook',
    capability: 'outlook.reply',
    target: {
      resource_type: capture.source.resource_type,
      connection_ref: capture.source.connection_ref,
      container_ref: capture.source.container_ref,
      object_ref: capture.source.object_ref,
      version_ref: capture.source.version_ref,
    },
    body: 'Approved response.',
    body_digest: bodyDigest,
    target_digest: targetDigest,
    state: 'approved',
    approved_at: '2026-08-29T09:00:00Z',
    receipt: null,
    created_at: '2026-08-29T09:00:00Z',
    updated_at: '2026-08-29T09:00:00Z',
  }
  const receipt: ReplyReceipt = {
    schema_version: '1.0',
    reply_id: command.id,
    provider: command.provider,
    outcome: 'sent',
    occurred_at: '2026-08-29T09:05:00Z',
    body_digest: bodyDigest,
    target_digest: targetDigest,
  }
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith(`/captures/${capture.id}/task`)) return jsonResponse({ data: { task } }, 201)
    if (url.endsWith(`/replies/${command.id}/receipt`)) return jsonResponse({ data: { reply: { ...command, state: 'sent', receipt, updated_at: receipt.occurred_at } } })
    if (url.endsWith('/api/v1/replies')) return jsonResponse({ data: { reply: command } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  const sourceTaskIntentId = '11111111-1111-4111-8111-111111111111'
  const sourceTaskOperationId = `workstack:${sourceTaskIntentId}`
  await expect(api.createTaskFromCapture(
    capture.id,
    { title: 'Task from source', intent_id: sourceTaskIntentId },
    sourceTaskOperationId,
  )).resolves.toMatchObject({ id: task.id })
  await expect(api.createReply({ task_id: task.id, capture_id: capture.id, body: command.body, approved: true })).resolves.toMatchObject({ id: command.id, state: 'approved' })
  await expect(api.importReplyReceipt(command.id, receipt)).resolves.toMatchObject({ id: command.id, state: 'sent' })

  const mutationCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(mutationCalls).toHaveLength(3)
  for (const [, init] of mutationCalls) {
    expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
    expect((init?.headers as Record<string, string>)['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  }
  expect((mutationCalls[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(sourceTaskOperationId)
  expect(JSON.parse(String(mutationCalls[0][1]?.body))).toEqual({
    title: 'Task from source',
    intent_id: sourceTaskIntentId,
  })
  expect(JSON.parse(String(mutationCalls[1][1]?.body))).toEqual({
    task_id: task.id,
    capture_id: capture.id,
    body: command.body,
    approved: true,
  })
})

test('returns a validated Task from one v1 idempotent create POST', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/tasks')) return jsonResponse({ data: task, meta: { replayed: false } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  await expect(api.createTask({ title: task.title })).resolves.toEqual(task)

  const createCalls = fetchMock.mock.calls.filter(([input, init]) => (
    String(input).endsWith('/api/v1/tasks') && init?.method === 'POST'
  ))
  expect(createCalls).toHaveLength(1)
  expect((createCalls[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(createCalls[0][1]?.body))).toEqual({ title: task.title })
})

test('retains one task-creation Idempotency-Key across response-loss retry', async () => {
  let attempts = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/tasks')) {
      attempts += 1
      if (attempts === 1) return Promise.reject(new TypeError('response lost after commit'))
      return jsonResponse({ data: task, meta: { replayed: true } }, 200)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  await expect(api.createTask({ title: task.title })).resolves.toEqual(task)

  const calls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/tasks'))
  expect(calls).toHaveLength(2)
  const firstHeaders = calls[0][1]?.headers as Record<string, string>
  const secondHeaders = calls[1][1]?.headers as Record<string, string>
  expect(secondHeaders['Idempotency-Key']).toBe(firstHeaders['Idempotency-Key'])
})

test('marks an invalid successful create response as commit-unknown', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/tasks')) return jsonResponse({ data: { id: task.id } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  await expect(api.createTask({ title: task.title })).rejects.toBeInstanceOf(CommitUnknownError)

  const createCalls = fetchMock.mock.calls.filter(([input, init]) => (
    String(input).endsWith('/api/v1/tasks') && init?.method === 'POST'
  ))
  expect(createCalls).toHaveLength(1)
})

test('preserves an HTTP API failure from v1 task creation', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/tasks')) return jsonResponse({
      error: { code: 'invalid_request', message: 'title is required', details: { field: 'title' } },
    }, 400)
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  try {
    await api.createTask({ title: task.title })
    throw new Error('Expected task creation to fail')
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(CommitUnknownError)
    expect(error).toMatchObject({ status: 400, code: 'invalid_request' })
  }
})

test('adopts validated external SSOT changes only against the reviewed coordinate and stable operation key', async () => {
  const digest = `sha256:${'e'.repeat(64)}`
  const status = {
    state: 'in-sync',
    workspace_id: 'workspace-test',
    candidate_workspace_id: 'workspace-test',
    generation: 8,
    manifest_digest: digest,
    changed_files: [],
    reason: null,
    rebind_available: false,
  }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/sync/adopt')) return jsonResponse({ data: status })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  const operationKey = 'workstack:sync-review-operation'
  await expect(api.adoptSyncChanges(7, digest, operationKey)).resolves.toEqual(status)
  const [, init] = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/sync/adopt'))!
  expect(init?.method).toBe('POST')
  expect((init?.headers as Record<string, string>)['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect((init?.headers as Record<string, string>)['Idempotency-Key']).toBe(operationKey)
  expect(JSON.parse(String(init?.body))).toEqual({
    expected_generation: 7,
    expected_manifest_digest: digest,
  })
})
