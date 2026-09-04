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

const AUDIT_WORKSPACE = 'f67e2aad-9ed9-4fc7-b1ae-63b240269855'
const AUDIT_CP = `CP-${'a'.repeat(64)}`
const AUDIT_DIGEST = `sha256:${'b'.repeat(64)}`

function auditPayload() {
  return {
    workspace_uid: AUDIT_WORKSPACE,
    entries: [
      {
        locator: {
          workspace_uid: AUDIT_WORKSPACE,
          task_id: 'T-0001',
          date: '2026-09-03',
          ordinal: 0,
          entry_digest: AUDIT_DIGEST,
        },
        checkpoint_id: AUDIT_CP,
        entry: { done: ['shipped'] },
        recorded: {
          type: 'worklog.recorded' as const,
          workspace_uid: AUDIT_WORKSPACE,
          task_id: 'T-0001',
          checkpoint_id: AUDIT_CP,
          date: '2026-09-03',
          ordinal: 0,
          entry_digest: AUDIT_DIGEST,
          origin: 'agent-cli-v1' as const,
        },
        state: 'superseded' as const,
        revision: 1,
        transitions: [transitionEvent()],
      },
    ],
  }
}

function transitionEvent() {
  return {
    type: 'worklog.superseded' as const,
    workspace_uid: AUDIT_WORKSPACE,
    task_id: 'T-0001',
    checkpoint_id: AUDIT_CP,
    date: '2026-09-03',
    ordinal: 0,
    entry_digest: AUDIT_DIGEST,
    state: 'superseded' as const,
    revision: 1,
    reason: { code: 'incorrect', explanation: 'spaced' },
    origin: null,
  }
}

test('reads the whole checkpoint audit with no query parameters', async () => {
  const seen: string[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    seen.push(url)
    if (url.includes('/api/v1/review/checkpoints')) return jsonResponse({ data: auditPayload() })
    throw new Error(`Unexpected request: ${url}`)
  }))

  const audit = await api.getCheckpointAudit()
  expect(audit.entries).toHaveLength(1)
  expect(seen[0]).toBe('/api/v1/review/checkpoints')
  expect(seen[0]).not.toContain('?')
})

test('refuses unknown fields on known audit metadata instead of dropping them', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/review/checkpoints')) {
      const payload = auditPayload()
      return jsonResponse({ data: { ...payload, unexpected: true } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.getCheckpointAudit()).rejects.toBeInstanceOf(Error)
})

test('posts one transition attempt with the verbatim explanation and one key', async () => {
  let attempts = 0
  let sentBody = ''
  let sentKey = ''
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      sentBody = String(init?.body ?? '')
      sentKey = String(new Headers(init?.headers).get('Idempotency-Key') ?? '')
      return jsonResponse({ data: transitionEvent(), meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  const event = await api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: '  spaced  ' },
  }, 'key-frozen-1')

  expect(attempts).toBe(1)
  expect(event.revision).toBe(1)
  // The route is the approved one, with no superseded state alias.
  expect(sentKey).toBe('key-frozen-1')
  // Whitespace survives: the server's normalization is authoritative.
  expect(JSON.parse(sentBody).reason.explanation).toBe('  spaced  ')
})

test('a 409 conflict stays a determinate ApiError with no second attempt', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      return jsonResponse({
        error: {
          code: 'checkpoint_transition_conflict',
          message: 'invalid checkpoint transition input',
          details: { transition_code: 'stale_revision' },
        },
      }, 409)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-conflict')).rejects.toBeInstanceOf(ApiError)
  expect(attempts).toBe(1)
})

test('ambiguity becomes CommitUnknownError rather than an automatic retry', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      return jsonResponse({ data: { unexpected: 'shape' } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-ambiguous')).rejects.toBeInstanceOf(CommitUnknownError)
  expect(attempts).toBe(1)
})

test('a well-formed receipt for another checkpoint is commit-unknown, not success', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      return jsonResponse({ data: { ...transitionEvent(), checkpoint_id: `CP-${'d'.repeat(64)}` }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  // Contradictory success: it is never evidence that nothing committed.
  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-wrong-cp')).rejects.toBeInstanceOf(CommitUnknownError)
  expect(attempts).toBe(1)
})

test('a receipt at the wrong next revision is commit-unknown', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      return jsonResponse({ data: { ...transitionEvent(), revision: 7 }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-wrong-rev')).rejects.toBeInstanceOf(CommitUnknownError)
})

test('a receipt in the wrong resulting state is commit-unknown', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      return jsonResponse({ data: { ...transitionEvent(), state: 'active' }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded',
    revision: 0,
    reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-wrong-state')).rejects.toBeInstanceOf(CommitUnknownError)
})

function auditWith(overrides: Record<string, unknown>) {
  const payload = auditPayload() as Record<string, unknown>
  return { ...payload, ...overrides }
}

test('refuses an impossible known locator date instead of repairing it', async () => {
  const payload = auditPayload() as { entries: { locator: Record<string, unknown> }[] }
  payload.entries[0].locator.date = '2026-02-30'
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: payload })))
  await expect(api.getCheckpointAudit()).rejects.toBeInstanceOf(Error)
})

test('refuses an audit whose outer workspace is foreign to its entries', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
    data: auditWith({ workspace_uid: '00000000-0000-4000-8000-000000000009' }),
  })))
  await expect(api.getCheckpointAudit()).rejects.toBeInstanceOf(Error)
})

test('accepts the healthy audit control unchanged', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: auditPayload() })))
  await expect(api.getCheckpointAudit()).resolves.toBeTruthy()
})

test('HTTP 202 on a transition is commit-unknown, not success', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      return jsonResponse({ data: transitionEvent(), meta: { replayed: false } }, 202)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-202')).rejects.toBeInstanceOf(CommitUnknownError)
  expect(attempts).toBe(1)
})

test('a 201 that claims replayed=true is commit-unknown', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) return jsonResponse({ data: transitionEvent(), meta: { replayed: true } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-swapped')).rejects.toBeInstanceOf(CommitUnknownError)
})

test('a historical 200 replay is accepted with no audit lookup', async () => {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) return jsonResponse({ data: transitionEvent(), meta: { replayed: true } }, 200)
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-replay')).resolves.toBeTruthy()
  expect(calls.some((url) => url.endsWith('/api/v1/review/checkpoints'))).toBe(false)
})

test('an unreadable successful body is commit-unknown, not a determinate refusal', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      attempts += 1
      return new Response('{not json', { status: 201, headers: { 'Content-Type': 'application/json' } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  const error = await api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' },
  }, 'key-unreadable').catch((caught: unknown) => caught)
  expect(error).toBeInstanceOf(CommitUnknownError)
  expect(error).not.toBeInstanceOf(ApiError)
  expect(attempts).toBe(1)
})

/** DUCR-F1/F2: the frozen known domain, exactly as the pure module defines it. */
function auditFrom(entryOverrides: Record<string, unknown>) {
  const payload = auditPayload() as { workspace_uid: string; entries: Record<string, unknown>[] }
  payload.entries[0] = { ...payload.entries[0], ...entryOverrides }
  return payload
}

function withPersistedExplanation(text: string) {
  const payload = auditPayload() as { entries: { transitions: Record<string, unknown>[] }[] }
  const first = payload.entries[0].transitions[0]
  if (!first) throw new Error('fixture has no transition to vary')
  first.reason = { ...(first.reason as object), explanation: text }
  return payload
}

async function auditRefuses(payload: unknown) {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: payload })))
  await expect(api.getCheckpointAudit()).rejects.toBeInstanceOf(Error)
}

async function auditAccepts(payload: unknown) {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: payload })))
  await expect(api.getCheckpointAudit()).resolves.toBeTruthy()
}

test('a legitimate NFD persisted explanation is accepted unchanged', async () => {
  // The frozen domain retains NFC and NFD; equality with NFC is not the rule.
  await auditAccepts(withPersistedExplanation('café'))
})

test('U+FEFF at either end is accepted because Python strip does not remove it', async () => {
  await auditAccepts(withPersistedExplanation('﻿wrong day'))
  await auditAccepts(withPersistedExplanation('wrong day﻿'))
})

test('Python-stripped control edges refuse when persisted', async () => {
  // str.strip() removes U+001C and U+0085, so a stored value carrying them
  // at an end was never canonical.
  await auditRefuses(withPersistedExplanation('wrong day'))
  await auditRefuses(withPersistedExplanation('wrong day'))
})

test('a lone surrogate explanation refuses on the strict UTF-8 bound', async () => {
  await auditRefuses(withPersistedExplanation('wrong \uD800 day'))
})

test('explanations above 240 codepoints refuse, 240 astral codepoints pass', async () => {
  await auditRefuses(withPersistedExplanation('a'.repeat(241)))
  await auditRefuses(withPersistedExplanation('\u{1F600}'.repeat(241)))
  await auditAccepts(withPersistedExplanation('\u{1F600}'.repeat(240)))
})

test('a nil workspace UUID refuses and a version 7 UUID is accepted', async () => {
  const original = auditPayload() as { workspace_uid: string }
  const nil = '00000000-0000-0000-0000-000000000000'
  const nilBound = JSON.parse(
    JSON.stringify(auditPayload()).split(original.workspace_uid).join(nil),
  )
  await auditRefuses(nilBound)

  const v7 = '018f4b1a-7c2d-7f3e-b8a1-2c4d6e8f0a1b'
  const v7Bound = JSON.parse(
    JSON.stringify(auditPayload()).split(original.workspace_uid).join(v7),
  )
  await auditAccepts(v7Bound)
})

test('year 0001 is a real calendar date', async () => {
  const original = auditPayload() as { entries: { locator: { date: string } }[] }
  const rebound = JSON.parse(
    JSON.stringify(auditPayload()).split(original.entries[0].locator.date).join('0001-01-01'),
  )
  await auditAccepts(rebound)
})

test('a supersede carrying a restore reason refuses', async () => {
  const payload = withPersistedExplanation('wrong day') as {
    entries: { transitions: Record<string, unknown>[] }[]
  }
  payload.entries[0].transitions[0].reason = { code: 'restore', explanation: 'wrong day' }
  await auditRefuses(payload)
})

test('the recorded fact is validated as exactly eight bound fields', async () => {
  await auditRefuses(auditFrom({ recorded: {} }))
  await auditRefuses(auditFrom({ recorded: { type: 'worklog.recorded', unexpected: true } }))
})

test('a recorded fact bound to another workspace or slot refuses', async () => {
  const payload = auditPayload() as { entries: { recorded: Record<string, unknown> }[] }
  const recorded = payload.entries[0].recorded
  await auditRefuses(auditFrom({
    recorded: { ...recorded, workspace_uid: '00000000-0000-4000-8000-000000000009' },
  }))
  await auditRefuses(auditFrom({ recorded: { ...recorded, ordinal: 41 } }))
})

test('a null checkpoint requires a null recorded fact and the reverse', async () => {
  const payload = auditPayload() as { entries: { recorded: Record<string, unknown> }[] }
  await auditRefuses(auditFrom({ checkpoint_id: null, recorded: payload.entries[0].recorded }))
  await auditRefuses(auditFrom({ recorded: null }))
})

test('the physical slot is workspace, date and ordinal, not the Task', async () => {
  const payload = auditPayload() as { entries: Record<string, unknown>[] }
  const first = payload.entries[0] as {
    locator: Record<string, unknown>
    recorded: Record<string, unknown>
  }
  const otherTask = 'T-0002'
  const otherCp = `CP-${'e'.repeat(64)}`
  const second = {
    ...first,
    checkpoint_id: otherCp,
    locator: { ...first.locator, task_id: otherTask },
    recorded: { ...first.recorded, task_id: otherTask, checkpoint_id: otherCp },
    state: 'active',
    revision: 0,
    transitions: [],
  }
  // Two Tasks cannot occupy one physical date-local ordinal.
  await auditRefuses({ ...payload, entries: [first, second] })
})

test('invalid known metadata on another day refuses the whole audit', async () => {
  const payload = auditPayload() as { entries: Record<string, unknown>[] }
  const first = payload.entries[0] as { locator: Record<string, unknown> }
  const otherDay = {
    ...first,
    locator: { ...first.locator, date: '2026-02-30', ordinal: 9 },
    state: 'active',
    revision: 0,
    transitions: [],
  }
  await auditRefuses({ ...payload, entries: [first, otherDay] })
})

test('a 201 receipt whose explanation is legitimate NFD resolves', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      return jsonResponse({
        data: { ...transitionEvent(), reason: { code: 'incorrect', explanation: 'café' } },
        meta: { replayed: false },
      }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(api.transitionCheckpoint(AUDIT_CP, {
    state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'café' },
  }, 'key-nfd')).resolves.toBeTruthy()
})

/** DUD-F1: the frozen domain constrains canonical/nonnil/variant, not version. */
const NIBBLE_UUIDS = [
  '018f4b1a-7c2d-0f3e-b8a1-2c4d6e8f0a1b',
  '018f4b1a-7c2d-9f3e-b8a1-2c4d6e8f0a1b',
  '018f4b1a-7c2d-af3e-b8a1-2c4d6e8f0a1b',
  '018f4b1a-7c2d-ff3e-b8a1-2c4d6e8f0a1b',
]

test('every RFC variant UUID is accepted whatever its version nibble', async () => {
  const original = auditPayload() as { workspace_uid: string }
  for (const uid of NIBBLE_UUIDS) {
    const rebound = JSON.parse(
      JSON.stringify(auditPayload()).split(original.workspace_uid).join(uid),
    )
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: rebound })))
    await expect(api.getCheckpointAudit()).resolves.toBeTruthy()
  }
})

test('the durable event schema accepts the same version nibbles', async () => {
  for (const uid of NIBBLE_UUIDS) {
    const event = { ...transitionEvent(), workspace_uid: uid }
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
      if (url.includes('/transitions')) return jsonResponse({ data: event, meta: { replayed: false } }, 201)
      throw new Error(`Unexpected request: ${url}`)
    }))
    await expect(api.transitionCheckpoint(AUDIT_CP, {
      state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' },
    }, `key-${uid}`)).resolves.toBeTruthy()
  }
})

test('the nil UUID and a non-RFC variant still refuse', async () => {
  const original = auditPayload() as { workspace_uid: string }
  for (const uid of ['00000000-0000-0000-0000-000000000000', '018f4b1a-7c2d-4f3e-c8a1-2c4d6e8f0a1b']) {
    const rebound = JSON.parse(
      JSON.stringify(auditPayload()).split(original.workspace_uid).join(uid),
    )
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: rebound })))
    await expect(api.getCheckpointAudit()).rejects.toBeInstanceOf(Error)
  }
})


test('the keyed status helper sends one PATCH with the caller key and preserves the raw receipt', async () => {
  const calls: { method: string; key: string | null; body: unknown; url: string }[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    const headers = (init?.headers ?? {}) as Record<string, string>
    calls.push({
      body: init?.body ? JSON.parse(String(init.body)) : null,
      key: headers['Idempotency-Key'] ?? null,
      method: init?.method ?? 'GET',
      url,
    })
    // A full Task plus permitted unknown metadata: nothing may be stripped here.
    return jsonResponse({
      data: { ...task, status: 'started', revision: (task.revision ?? 0) + 1, server_only: { note: 1 } },
      meta: { replayed: true, trace: 'abc' },
    })
  }))

  const receipt = await api.patchTaskStatusIntent('T-0001', { revision: 3, status: 'started' }, 'workstack:key-1')

  expect(calls).toHaveLength(1)
  expect(calls[0].method).toBe('PATCH')
  expect(calls[0].url).toContain('/api/v1/tasks/T-0001')
  expect(calls[0].key).toBe('workstack:key-1')
  expect(calls[0].body).toEqual({ revision: 3, status: 'started' })
  expect(receipt.status).toBe(200)
  expect(receipt.body.meta).toEqual({ replayed: true, trace: 'abc' })
  // The raw payload survives: no wrapper unwrapping, no synthesized defaults.
  expect((receipt.body.data as { server_only: unknown }).server_only).toEqual({ note: 1 })
  expect((receipt.body.data as { revision: number }).revision).toBe((task.revision ?? 0) + 1)
})

test('the keyed status helper answers a determinate 409 instead of rejecting, and never resends', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    attempts += 1
    return jsonResponse({ error: { code: 'conflict', message: 'Revision conflict.' } }, 409)
  }))

  const receipt = await api.patchTaskStatusIntent('T-0001', { revision: 3, status: 'started' }, 'workstack:key-2')

  expect(receipt.status).toBe(409)
  expect(receipt.body.data).toBeUndefined()
  expect(attempts).toBe(1)
})

test('the ordinary patchTask keeps its unkeyed default behaviour', async () => {
  const headersSeen: (string | null)[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    const headers = (init?.headers ?? {}) as Record<string, string>
    headersSeen.push(headers['Idempotency-Key'] ?? null)
    return jsonResponse({ data: { ...task, status: 'done' } })
  }))

  await api.patchTask('T-0001', { revision: 3, status: 'done' })

  expect(headersSeen).toEqual([null])
})
