import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, vi } from 'vitest'
import { jsonResponse } from '../test/fixtures'
import { CROSS_TAB_STORAGE_KEY } from '../integration/planningChangeBus'
import { ApiError, CommitUnknownError, getCsrfToken } from './transport'
import {
  archiveReportDocument,
  createReportDocument,
  finalizeReportDocument,
  listReportDocuments,
  readReportDocument,
  restoreReportDocument,
  reviseReportDocument,
} from './reportDocuments'

const WORKSPACE = '0f50a123-3da8-4c82-8f16-8ee1a57260c4'
const REPORT_UID = '00000001-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const LATER = '2026-09-06T13:00:00Z'
const GENERATED = '2026-09-06T11:59:00Z'
const MARKDOWN = '# body <script>alert(1)</script> | a | b |'
const KEY = 'report-key-0001'
const SRC_DIR = dirname(fileURLToPath(import.meta.url))

function period() {
  return { kind: 'day' as const, date: '2026-09-06' }
}

function listItem(overrides: Record<string, unknown> = {}) {
  return {
    uid: REPORT_UID,
    template: 'daily-v1',
    period: period(),
    state: 'draft',
    revision: 1,
    content_revision: 1,
    source_digest: DIGEST,
    archived_from_state: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

function summary(overrides: Record<string, unknown> = {}) {
  return {
    uid: REPORT_UID,
    workspace_uid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    source_digest: DIGEST,
    source_generated_at: GENERATED,
    state: 'draft',
    revision: 1,
    archived_from_state: null,
    archived_at: null,
    archive_note: null,
    created_at: NOW,
    updated_at: LATER,
    ...overrides,
  }
}

function content(overrides: Record<string, unknown> = {}) {
  return {
    content_revision: 1,
    document_revision: 1,
    markdown: MARKDOWN,
    authored_at: LATER,
    note: null,
    ...overrides,
  }
}

function sessionAnd(handler: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function mutationCalls(fetchMock: ReturnType<typeof vi.fn>, fragment: string) {
  return fetchMock.mock.calls.filter(([input, init]) => (
    String(input).includes(fragment) && init?.method === 'POST'
  ))
}

test('lists with workspace_uid, a filter alias, and limit 50 — never a stored state', async () => {
  const page = {
    workspace_uid: WORKSPACE,
    reports: [listItem()],
    omitted_count: 0,
    cursor: null,
  }
  const fetchMock = sessionAnd((url) => {
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: page })
    throw new Error(`Unexpected request: ${url}`)
  })

  await expect(listReportDocuments({ workspaceUid: WORKSPACE, state: 'active' })).resolves.toEqual(page)
  expect(String(fetchMock.mock.calls[0][0])).toBe(
    `/api/v1/reports?workspace_uid=${WORKSPACE}&state=active&limit=50`,
  )
  expect(String(fetchMock.mock.calls[0][0])).not.toContain('state=draft')
})

test('reads one document with revisions and source_stale on the uid route', async () => {
  const document = {
    ...listItem(),
    revisions: [content({ authored_at: NOW, markdown: MARKDOWN })],
    source_stale: false,
  }
  const fetchMock = sessionAnd((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: document })
    throw new Error(`Unexpected request: ${url}`)
  })

  await expect(readReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
  })).resolves.toEqual(document)
  expect(String(fetchMock.mock.calls[0][0])).toBe(
    `/api/v1/reports/${REPORT_UID}?workspace_uid=${WORKSPACE}`,
  )
  expect(fetchMock.mock.calls[0][1]?.method ?? 'GET').not.toBe('POST')
})

test('posts create/revise/finalize/archive/restore with query workspace, body workspace, and the caller key', async () => {
  const created = { ...summary(), content_entry: content(), source_stale: false }
  const revised = {
    ...summary({ revision: 2 }),
    content_entry: content({ content_revision: 2, document_revision: 2, markdown: '# second' }),
    reopened: false,
    source_stale: false,
  }
  const finalized = { ...summary({ state: 'finalized', revision: 2 }), source_stale: false }
  const archived = summary({
    state: 'archived',
    revision: 2,
    archived_from_state: 'draft',
    archived_at: LATER,
    archive_note: null,
  })
  const restored = summary({ revision: 2 })
  const fetchMock = sessionAnd((url) => {
    if (url === `/api/v1/reports?workspace_uid=${WORKSPACE}`) return jsonResponse({ data: created, meta: { replayed: false } }, 201)
    if (url.endsWith(`/revisions?workspace_uid=${WORKSPACE}`)) return jsonResponse({ data: revised, meta: { replayed: false } })
    if (url.endsWith(`/finalize?workspace_uid=${WORKSPACE}`)) return jsonResponse({ data: finalized, meta: { replayed: false } })
    if (url.endsWith(`/archive?workspace_uid=${WORKSPACE}`)) return jsonResponse({ data: archived, meta: { replayed: false } })
    if (url.endsWith(`/restore?workspace_uid=${WORKSPACE}`)) return jsonResponse({ data: restored, meta: { replayed: false } })
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  await expect(createReportDocument({
    workspaceUid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    sourceDigest: DIGEST,
    sourceGeneratedAt: GENERATED,
    markdown: MARKDOWN,
  }, KEY)).resolves.toEqual(created)
  await expect(reviseReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
    markdown: '# second',
    note: null,
  }, `${KEY}-rev`)).resolves.toEqual(revised)
  await expect(finalizeReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
  }, `${KEY}-fin`)).resolves.toEqual(finalized)
  await expect(archiveReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
    note: null,
  }, `${KEY}-arc`)).resolves.toEqual(archived)
  await expect(restoreReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
  }, `${KEY}-res`)).resolves.toEqual(restored)

  const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts.map(([input]) => String(input))).toEqual([
    `/api/v1/reports?workspace_uid=${WORKSPACE}`,
    `/api/v1/reports/${REPORT_UID}/revisions?workspace_uid=${WORKSPACE}`,
    `/api/v1/reports/${REPORT_UID}/finalize?workspace_uid=${WORKSPACE}`,
    `/api/v1/reports/${REPORT_UID}/archive?workspace_uid=${WORKSPACE}`,
    `/api/v1/reports/${REPORT_UID}/restore?workspace_uid=${WORKSPACE}`,
  ])
  expect(JSON.parse(String(posts[0][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    source_digest: DIGEST,
    source_generated_at: GENERATED,
    markdown: MARKDOWN,
  })
  expect(JSON.parse(String(posts[1][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
    markdown: '# second',
    note: null,
  })
  expect(JSON.parse(String(posts[2][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
  })
  expect(JSON.parse(String(posts[3][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
    note: null,
  })
  expect(posts.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])).toEqual([
    KEY, `${KEY}-rev`, `${KEY}-fin`, `${KEY}-arc`, `${KEY}-res`,
  ])
})

test('retries the identical create path, body, and caller key after a network drop', async () => {
  const created = { ...summary(), content_entry: content(), source_stale: false }
  let attempts = 0
  const fetchMock = sessionAnd((url) => {
    if (!url.startsWith('/api/v1/reports?')) throw new Error(`Unexpected request: ${url}`)
    attempts += 1
    if (attempts === 1) return Promise.reject(new TypeError('temporary connection reset'))
    return jsonResponse({ data: created, meta: { replayed: true } }, 201)
  })
  await getCsrfToken(true)

  await expect(createReportDocument({
    workspaceUid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    sourceDigest: DIGEST,
    sourceGeneratedAt: GENERATED,
    markdown: MARKDOWN,
  }, KEY)).resolves.toEqual(created)

  const posts = mutationCalls(fetchMock, '/api/v1/reports?')
  expect(posts).toHaveLength(2)
  expect(posts.map(([, init]) => init?.body)).toEqual([
    posts[0][1]?.body,
    posts[0][1]?.body,
  ])
  expect(posts.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])).toEqual([KEY, KEY])
  expect(JSON.parse(String(posts[1][1]?.body)).markdown).toBe(MARKDOWN)
})

test('surfaces commit ambiguity for a malformed 201 and does not mint another key', async () => {
  const fetchMock = sessionAnd((url) => {
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: { uid: REPORT_UID }, meta: { replayed: false } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  await expect(createReportDocument({
    workspaceUid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    sourceDigest: DIGEST,
    sourceGeneratedAt: GENERATED,
    markdown: MARKDOWN,
  }, KEY)).rejects.toBeInstanceOf(CommitUnknownError)
  expect(mutationCalls(fetchMock, '/api/v1/reports?')).toHaveLength(1)
  expect((mutationCalls(fetchMock, '/api/v1/reports?')[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(KEY)
})

test('treats a transition answered at an unasserted revision as commit-unknown', async () => {
  const answers: Record<string, unknown> = {
    revisions: {
      ...summary({ revision: 777 }),
      content_entry: content({
        content_revision: 2,
        document_revision: 777,
        markdown: '# second',
      }),
      reopened: false,
      source_stale: false,
    },
    finalize: { ...summary({ state: 'finalized', revision: 777 }), source_stale: false },
    archive: summary({
      state: 'archived',
      revision: 777,
      archived_from_state: 'draft',
      archived_at: LATER,
      archive_note: null,
    }),
    restore: summary({ revision: 777 }),
  }
  const fetchMock = sessionAnd((url) => {
    for (const [verb, data] of Object.entries(answers)) {
      if (url.endsWith(`/${verb}?workspace_uid=${WORKSPACE}`)) {
        return jsonResponse({ data, meta: { replayed: false } })
      }
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  const target = { workspaceUid: WORKSPACE, reportUid: REPORT_UID, expectedRevision: 1 }
  await expect(reviseReportDocument({ ...target, markdown: '# second', note: null }, `${KEY}-rev`))
    .rejects.toBeInstanceOf(CommitUnknownError)
  await expect(finalizeReportDocument(target, `${KEY}-fin`)).rejects.toBeInstanceOf(CommitUnknownError)
  await expect(archiveReportDocument({ ...target, note: null }, `${KEY}-arc`))
    .rejects.toBeInstanceOf(CommitUnknownError)
  await expect(restoreReportDocument(target, `${KEY}-res`)).rejects.toBeInstanceOf(CommitUnknownError)

  // One POST each, on the caller's own key: an ambiguous commit is never
  // retried under a freshly minted one.
  const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts).toHaveLength(4)
  expect(posts.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])).toEqual([
    `${KEY}-rev`, `${KEY}-fin`, `${KEY}-arc`, `${KEY}-res`,
  ])
})

test('treats a 201 naming a period, digest, or source stamp the create never sent as commit-unknown', async () => {
  const answers = [
    summary({ period: { kind: 'day', date: '2026-09-07' } }),
    summary({ source_digest: `sha256:${'1'.repeat(64)}` }),
    summary({ source_generated_at: '2026-09-06T10:00:00Z' }),
  ]
  let answered = 0
  const fetchMock = sessionAnd((url) => {
    if (!url.startsWith('/api/v1/reports?')) throw new Error(`Unexpected request: ${url}`)
    const data = { ...answers[answered], content_entry: content(), source_stale: false }
    answered += 1
    return jsonResponse({ data, meta: { replayed: false } }, 201)
  })
  await getCsrfToken(true)

  for (let index = 0; index < answers.length; index += 1) {
    await expect(createReportDocument({
      workspaceUid: WORKSPACE,
      template: 'daily-v1',
      period: period(),
      sourceDigest: DIGEST,
      sourceGeneratedAt: GENERATED,
      markdown: MARKDOWN,
    }, `${KEY}-${index}`)).rejects.toBeInstanceOf(CommitUnknownError)
  }
  expect(mutationCalls(fetchMock, '/api/v1/reports?')).toHaveLength(answers.length)
  expect(mutationCalls(fetchMock, '/api/v1/reports?')
    .map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key']))
    .toEqual([`${KEY}-0`, `${KEY}-1`, `${KEY}-2`])
})

test('accepts a replayed receipt because it answers the same asserted revision', async () => {
  // A replay returns the body recorded for a byte-identical request, so the
  // step it names is still exactly the one this caller asserted.
  const finalized = { ...summary({ state: 'finalized', revision: 2 }), source_stale: false }
  const created = { ...summary(), content_entry: content(), source_stale: false }
  const fetchMock = sessionAnd((url) => {
    if (url.endsWith(`/finalize?workspace_uid=${WORKSPACE}`)) {
      return jsonResponse({ data: finalized, meta: { replayed: true } })
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({ data: created, meta: { replayed: true } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  await expect(finalizeReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
  }, `${KEY}-fin`)).resolves.toEqual(finalized)
  await expect(createReportDocument({
    workspaceUid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    sourceDigest: DIGEST,
    sourceGeneratedAt: GENERATED,
    markdown: MARKDOWN,
  }, KEY)).resolves.toEqual(created)

  const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts).toHaveLength(2)
  expect(posts.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key']))
    .toEqual([`${KEY}-fin`, KEY])
})

test('preserves determinate report failures instead of wrapping them as commit-unknown', async () => {
  sessionAnd((url) => {
    if (url.includes('/revisions?')) {
      return jsonResponse({
        error: { code: 'report_revision_conflict', message: 'report revision does not match the expected revision' },
      }, 409)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  const failure = await reviseReportDocument({
    workspaceUid: WORKSPACE,
    reportUid: REPORT_UID,
    expectedRevision: 1,
    markdown: '# second',
    note: null,
  }, KEY).catch((error: unknown) => error)
  expect(failure).toBeInstanceOf(ApiError)
  expect(failure).not.toBeInstanceOf(CommitUnknownError)
  expect((failure as ApiError).code).toBe('report_revision_conflict')
  expect((failure as ApiError).status).toBe(409)
})

test('does not publish a planning-change signal for a pure report create', async () => {
  window.localStorage.removeItem(CROSS_TAB_STORAGE_KEY)
  const created = { ...summary(), content_entry: content(), source_stale: false }
  sessionAnd((url) => {
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: created, meta: { replayed: false } }, 201)
    throw new Error(`Unexpected request: ${url}`)
  })
  await getCsrfToken(true)

  await createReportDocument({
    workspaceUid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    sourceDigest: DIGEST,
    sourceGeneratedAt: GENERATED,
    markdown: MARKDOWN,
  }, KEY)
  expect(window.localStorage.getItem(CROSS_TAB_STORAGE_KEY)).toBeNull()
})

test('refuses a malformed list envelope from the same GET transport', async () => {
  sessionAnd((url) => {
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({
        data: { workspace_uid: WORKSPACE, reports: [listItem({ state: 'active' })], omitted_count: 0, cursor: null },
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })

  await expect(listReportDocuments({ workspaceUid: WORKSPACE })).rejects.toThrow()
})

test('never reads or writes localStorage from the owned report client modules', () => {
  const api = readFileSync(join(SRC_DIR, 'reportDocuments.ts'), 'utf8')
  const domain = readFileSync(join(SRC_DIR, '..', 'domain', 'reportDocuments.ts'), 'utf8')
  expect(api).not.toMatch(/localStorage/)
  expect(domain).not.toMatch(/localStorage/)
  expect(api).not.toMatch(/createIdempotencyKey/)
  expect(api).not.toMatch(/publishPlanningChange/)
})
