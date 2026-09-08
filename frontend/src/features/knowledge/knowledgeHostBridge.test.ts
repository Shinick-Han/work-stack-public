import { afterEach, expect, test, vi } from 'vitest'

import { KnowledgeHostError } from './knowledgeErrors'
import {
  MAX_KNOWLEDGE_REQUEST_BYTES,
  MAX_KNOWLEDGE_RESPONSE_BYTES,
  knowledgeHostAvailable,
  knowledgeHostRequestSchema,
  knowledgeHostResponseSchema,
  requestKnowledge,
} from './knowledgeHostBridge'

interface WebViewMessageEvent extends Event { data?: unknown }

const requestId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const binding = {
  workspace_uid: '22222222-2222-2222-2222-222222222222',
  task_uid: '11111111-1111-1111-8111-111111111111',
  task_id: 'T-0001',
  task_revision: 2,
}
const vault = { vault_id: 'personal-wiki', label: 'notes' }
const sha = 'a'.repeat(64)

function installHost() {
  let listener: ((event: WebViewMessageEvent) => void) | undefined
  const postMessage = vi.fn()
  const removeEventListener = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener,
    postMessage,
  } } })
  return {
    postMessage,
    removeEventListener,
    receive(data: unknown) { listener?.({ data } as WebViewMessageEvent) },
  }
}

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.useRealTimers()
})

test('is absent without a desktop webview host', () => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  expect(knowledgeHostAvailable()).toBe(false)
})

test('sends a bounded status request and resolves catalog data', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('status')
  expect(JSON.parse(host.postMessage.mock.calls[0][0])).toEqual({
    type: 'workstack-knowledge-request',
    schema_version: 1,
    request_id: requestId,
    operation: 'status',
  })
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'status',
    ok: true,
    data: { vaults: [vault], local_only: true },
  })
  await expect(pending).resolves.toEqual({ vaults: [vault], local_only: true })
  expect(host.removeEventListener).toHaveBeenCalled()
})

test('treats a cancelled vault picker as success data, not a registry write', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('choose-vault')
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'choose-vault',
    ok: true,
    data: { cancelled: true },
  })
  await expect(pending).resolves.toEqual({ cancelled: true })
})

test('rejects unknown operations, extra keys, and late mismatched bindings', async () => {
  expect(() => knowledgeHostRequestSchema.parse({
    type: 'workstack-knowledge-request', schema_version: 1, request_id: requestId, operation: 'crawl',
  })).toThrow()
  expect(() => knowledgeHostRequestSchema.parse({
    type: 'workstack-knowledge-request', schema_version: 1, request_id: requestId, operation: 'status', root: 'C:\\vault',
  })).toThrow()
  expect(knowledgeHostResponseSchema.safeParse({
    type: 'workstack-knowledge-response', schema_version: 1, request_id: requestId,
    operation: 'status', ok: true, data: { vaults: [], local_only: true, extra: true },
  }).success).toBe(false)

  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('list-references', { binding })
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'list-references',
    ok: true,
    data: { binding: { ...binding, task_revision: 99 }, references: [], local_only: true },
  })
  await expect(pending).rejects.toMatchObject({ code: 'binding_mismatch' })
  expect(host.removeEventListener).toHaveBeenCalled()
})

test('maps closed host errors and ignores unrelated messages until the correlated reply', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('read-reference', {
    binding,
    vault_id: vault.vault_id,
    document_path: 'projects/review.md',
    start_line: 1,
    end_line: 12,
  })
  host.receive({ type: 'workstack-source-zoom', values: { outlook: 100 } })
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    operation: 'read-reference',
    ok: true,
    data: { binding, reference: {} },
  })
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'read-reference',
    ok: false,
    error: { code: 'registry_unavailable', message: 'Local knowledge registry is unavailable.' },
  })
  await expect(pending).rejects.toEqual(expect.objectContaining({
    code: 'registry_unavailable',
    message: 'Local knowledge registry is unavailable.',
  }))
})

test('times out with listener cleanup', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  vi.useFakeTimers()
  const timedOut = requestKnowledge('status', {}, 200)
  const assertion = expect(timedOut).rejects.toMatchObject({ code: 'timeout' })
  await vi.advanceTimersByTimeAsync(200)
  await assertion
  expect(host.removeEventListener).toHaveBeenCalled()
})

test('cancels an in-flight request and removes the host listener', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const controller = new AbortController()
  const cancelled = requestKnowledge('status', {}, 20_000, controller.signal)
  controller.abort()
  await expect(cancelled).rejects.toBeInstanceOf(KnowledgeHostError)
  await expect(cancelled).rejects.toMatchObject({ code: 'cancelled' })
  expect(host.removeEventListener).toHaveBeenCalled()
})

test('refuses an oversized correlated response and does not send an oversize request', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('status')
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'status',
    ok: true,
    data: { vaults: [], local_only: true, padding: 'x'.repeat(MAX_KNOWLEDGE_RESPONSE_BYTES) },
  })
  await expect(pending).rejects.toMatchObject({ code: 'response_too_large' })
  expect(new TextEncoder().encode(JSON.stringify({
    type: 'workstack-knowledge-request', schema_version: 1, request_id: requestId, operation: 'status',
  })).byteLength).toBeLessThanOrEqual(MAX_KNOWLEDGE_REQUEST_BYTES)
})

test('captures proven host listeners so addEventListener keeps its this binding', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const listeners = new Set<(event: WebViewMessageEvent) => void>()
  const postMessage = vi.fn()
  const webview = {
    addEventListener(this: { postMessage: typeof postMessage }, _type: string, next: (event: WebViewMessageEvent) => void) {
      if (typeof this.postMessage !== 'function') throw new Error('addEventListener lost this')
      listeners.add(next)
    },
    removeEventListener(this: { postMessage: typeof postMessage }, _type: string, next: (event: WebViewMessageEvent) => void) {
      if (typeof this.postMessage !== 'function') throw new Error('removeEventListener lost this')
      listeners.delete(next)
    },
    postMessage,
  }
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview } })
  const pending = requestKnowledge('status')
  const payload = {
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'status',
    ok: true,
    data: { vaults: [vault], local_only: true },
  }
  for (const listener of listeners) listener({ data: payload } as WebViewMessageEvent)
  await expect(pending).resolves.toEqual({ vaults: [vault], local_only: true })
  expect(listeners.size).toBe(0)
})

test('ignores a matching request id when the error operation differs', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('pin-reference', {
    binding,
    vault_id: vault.vault_id,
    document_path: 'projects/review.md',
    start_line: 1,
    end_line: 8,
    expected_sha256: sha,
    reason: 'Release criteria live in this note.',
  })
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'unpin-reference',
    ok: false,
    error: { code: 'registry_unavailable', message: 'Local knowledge registry is unavailable.' },
  })
  await expect(pending).rejects.toMatchObject({ code: 'operation_mismatch' })
  expect(host.removeEventListener).toHaveBeenCalled()
})

test('accepts pin and unpin envelopes bound to the requested snapshot', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('pin-reference', {
    binding,
    vault_id: vault.vault_id,
    document_path: 'projects/review.md',
    start_line: 1,
    end_line: 8,
    expected_sha256: sha,
    reason: 'Release criteria live in this note.',
  })
  const reference = {
    reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    vault_id: vault.vault_id,
    document_path: 'projects/review.md',
    start_line: 1,
    end_line: 8,
    source_sha256: sha,
    reason: 'Release criteria live in this note.',
  }
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'pin-reference',
    ok: true,
    data: { binding, reference, local_only: true },
  })
  await expect(pending).resolves.toEqual({ binding, reference, local_only: true })
})

test('sends a bounded search-references request and rejects command extras or control characters', async () => {
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('search-references', {
    binding,
    vault_id: vault.vault_id,
    query: 'quality gate',
  }, 90_000)
  expect(JSON.parse(host.postMessage.mock.calls[0][0])).toEqual({
    type: 'workstack-knowledge-request',
    schema_version: 1,
    request_id: requestId,
    operation: 'search-references',
    binding,
    vault_id: vault.vault_id,
    query: 'quality gate',
  })
  expect(() => knowledgeHostRequestSchema.parse({
    type: 'workstack-knowledge-request',
    schema_version: 1,
    request_id: requestId,
    operation: 'search-references',
    binding,
    vault_id: vault.vault_id,
    query: 'quality gate',
    command: ['C:\\\\unsafe.exe'],
  })).toThrow()
  expect(() => knowledgeHostRequestSchema.parse({
    type: 'workstack-knowledge-request',
    schema_version: 1,
    request_id: requestId,
    operation: 'search-references',
    binding,
    vault_id: vault.vault_id,
    query: '\u0007inject',
  })).toThrow()
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'search-references',
    ok: true,
    data: {
      binding,
      query: 'quality gate',
      corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
      matches: [{
        schema_version: 1,
        provider: 'markdown-vault',
        vault_id: vault.vault_id,
        document_path: 'projects/review.md',
        title: 'Release review',
        source_sha256: sha,
        expected_sha256: sha,
        freshness: 'unchanged',
        start_line: 2,
        end_line: 12,
        excerpt: 'Make the quality gate measurable.',
        excerpt_truncated: false,
        trust: 'external_reference',
        read_only: true,
      }],
      omitted_count: 1,
    },
  })
  await expect(pending).resolves.toEqual(expect.objectContaining({
    query: 'quality gate',
    omitted_count: 1,
    corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
  }))
})

test('search-references rejects extra response keys and a late mismatched binding', async () => {
  expect(knowledgeHostResponseSchema.safeParse({
    type: 'workstack-knowledge-response', schema_version: 1, request_id: requestId,
    operation: 'search-references', ok: true, data: {
      binding, query: 'q', corpus: { label: 'notes', document_count: 1, indexed_at: '2026-09-08T00:00:00Z' },
      matches: [], omitted_count: 0, extra: true,
    },
  }).success).toBe(false)
  vi.spyOn(window.crypto, 'randomUUID').mockReturnValue(requestId)
  const host = installHost()
  const pending = requestKnowledge('search-references', {
    binding,
    vault_id: vault.vault_id,
    query: 'quality gate',
  }, 90_000)
  host.receive({
    type: 'workstack-knowledge-response',
    schema_version: 1,
    request_id: requestId,
    operation: 'search-references',
    ok: true,
    data: {
      binding: { ...binding, task_revision: 99 },
      query: 'quality gate',
      corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
      matches: [],
      omitted_count: 0,
    },
  })
  await expect(pending).rejects.toMatchObject({ code: 'binding_mismatch' })
})
