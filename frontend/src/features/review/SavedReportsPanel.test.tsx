import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, vi } from 'vitest'
import { jsonResponse, workspace } from '../../test/fixtures'
import type { ReviewProjection } from '../../domain/types'
import { DailyReviewPage } from './DailyReviewPage'
import { SavedReportsPanel } from './SavedReportsPanel'
import { REPORT_LIST_PAGE_SIZE } from '../../domain/reportDocuments'

const SRC = dirname(fileURLToPath(import.meta.url))
const WORKSPACE = '0f50a123-3da8-4c82-8f16-8ee1a57260c4'
const WORKSPACE_B = '1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d'
const REPORT_UID = '00000001-abcd-4000-8000-000000000000'
const REPORT_UID_B = '00000002-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const LATER = '2026-09-06T13:00:00Z'
const GENERATED = '2026-09-06T11:59:00Z'
const CURSOR = `A${'b'.repeat(63)}`
const MARKDOWN = '# Hello <script>alert(1)</script>\n\n| a | b |\n'
const KEY_PREFIX = 'workstack:'
/** Whatever the workspace says about a refusal is never the reader's copy. */
const REMOTE_PROSE = 'remote prose that must never be shown'

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

function content(overrides: Record<string, unknown> = {}) {
  return {
    content_revision: 1,
    document_revision: 1,
    markdown: MARKDOWN,
    authored_at: NOW,
    note: null,
    ...overrides,
  }
}

function readDocument(overrides: Record<string, unknown> = {}) {
  return {
    ...listItem(),
    revisions: [content()],
    source_stale: false,
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

function listPage(reports: unknown[], omitted = 0, cursor: string | null = null) {
  return { workspace_uid: WORKSPACE, reports, omitted_count: omitted, cursor }
}

function pageOf(count: number, omitted: number, cursor: string | null) {
  return listPage(
    Array.from({ length: count }, (_, index) => listItem({
      uid: `00000001-abcd-4000-8000-${index.toString(16).padStart(12, '0')}`,
      period: { kind: 'day', date: `2026-07-${String((index % 28) + 1).padStart(2, '0')}` },
      updated_at: `2026-09-06T12:${String(index % 60).padStart(2, '0')}:00Z`,
    })),
    omitted,
    cursor,
  )
}

function sessionFetch(handler: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPanel(workspaceId = WORKSPACE, client?: QueryClient) {
  const queryClient = client ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <SavedReportsPanel workspaceId={workspaceId} />
    </QueryClientProvider>,
  )
  return { queryClient, view }
}

function posts(fetchMock: ReturnType<typeof vi.fn>, fragment: string) {
  return fetchMock.mock.calls.filter(([input, init]) => (
    String(input).includes(fragment) && init?.method === 'POST'
  ))
}

function listUrls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .map(([input]) => String(input))
    .filter((url) => url.startsWith('/api/v1/reports?'))
}

test('owned modules never render HTML, mint weekly writes, or dump JSON helpers', () => {
  const panel = readFileSync(join(SRC, 'SavedReportsPanel.tsx'), 'utf8')
  const hook = readFileSync(join(SRC, 'useSavedReports.ts'), 'utf8')
  const model = readFileSync(join(SRC, 'savedReportsModel.ts'), 'utf8')
  expect(`${panel}${hook}${model}`).not.toMatch(/dangerouslySetInnerHTML/)
  expect(`${panel}${hook}`).not.toMatch(/weekly-preview|createReportDocument|reviseReportDocument/)
  expect(panel).not.toMatch(/JSON\.stringify/)
})

test('an invalid workspace identity does not call the report transport', async () => {
  const fetchMock = sessionFetch(() => {
    throw new Error('report transport should not run')
  })
  renderPanel(workspace.workspace.id)
  expect(await screen.findByRole('heading', { name: 'Saved reports' })).toBeVisible()
  expect(screen.getByText(/Save a local daily draft/)).toBeVisible()
  expect(fetchMock).not.toHaveBeenCalled()
})

test('lists the active workspace with state=active and limit 50, never a stored state', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  expect(await screen.findByText(/Report version 1/)).toBeVisible()
  expect(screen.getAllByText('Draft').length).toBeGreaterThan(0)
  expect(screen.queryByText(REPORT_UID)).toBeNull()
  expect(screen.queryByText(DIGEST)).toBeNull()
  expect(screen.queryByText(WORKSPACE)).toBeNull()
  expect(listUrls(fetchMock)).toEqual([
    `/api/v1/reports?workspace_uid=${WORKSPACE}&state=active&limit=50`,
  ])
})

test('changing filter resets the cursor instead of reusing it', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('state=active')) return jsonResponse({ data: pageOf(REPORT_LIST_PAGE_SIZE, 8, CURSOR) })
    if (url.includes('state=archived')) return jsonResponse({ data: listPage([], 0, null) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  expect(await screen.findByRole('button', { name: /Load more \(8 more\)/ })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Archived' }))
  await screen.findByText(/Save a local daily draft/)
  const archived = listUrls(fetchMock).filter((url) => url.includes('state=archived'))
  expect(archived).toHaveLength(1)
  expect(archived[0]).not.toContain('cursor=')
  expect(archived[0]).toContain('state=archived')
  expect(screen.queryByRole('button', { name: /Load more/ })).toBeNull()
})

test('load more uses the cursor once and never walks the rest of history', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('cursor=')) return jsonResponse({ data: listPage([listItem({ uid: '00000001-abcd-4000-8000-0000000000aa' })], 0, null) })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: pageOf(REPORT_LIST_PAGE_SIZE, 4, CURSOR) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  const more = await screen.findByRole('button', { name: /Load more \(4 more\)/ })
  expect(listUrls(fetchMock)).toHaveLength(1)
  await userEvent.click(more)
  await waitFor(() => expect(listUrls(fetchMock)).toHaveLength(2))
  expect(listUrls(fetchMock)[1]).toContain(`cursor=${CURSOR}`)
  expect(listUrls(fetchMock)).toHaveLength(2)
})

test('an invalid cursor keeps shown rows and refresh restarts without the marker', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('cursor=')) {
      return jsonResponse({ error: { code: 'report_cursor_invalid', message: 'cursor invalid' } }, 400)
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: pageOf(REPORT_LIST_PAGE_SIZE, 3, CURSOR) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: /Load more \(3 more\)/ }))
  expect(await screen.findByRole('alert')).toHaveTextContent('page marker is no longer valid')
  expect(screen.getAllByText(/Report version 1/).length).toBeGreaterThan(0)
  await userEvent.click(screen.getByRole('button', { name: 'Refresh list' }))
  await waitFor(() => {
    const lists = listUrls(fetchMock)
    expect(lists.at(-1)).not.toContain('cursor=')
  })
})

test('explicit select reads full revision history as inert text, not HTML', async () => {
  const older = content({ content_revision: 1, document_revision: 1, markdown: 'first body', authored_at: NOW })
  const latest = content({ content_revision: 2, document_revision: 2, markdown: MARKDOWN, authored_at: LATER })
  const fetchMock = sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({
        data: readDocument({ revision: 2, content_revision: 2, revisions: [older, latest] }),
      })
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({ data: listPage([listItem({ revision: 2, content_revision: 2 })]) })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 2/))
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('<script>alert(1)</script>')
  expect(screen.getByLabelText('Authored markdown version 1')).toHaveTextContent('first body')
  expect(screen.getByText('Report version 2', { selector: '.saved-reports__version' })).toBeVisible()
  expect(screen.getAllByText('Draft').length).toBeGreaterThan(0)
  const panel = screen.getByRole('region', { name: 'Saved reports' })
  expect(panel.querySelectorAll('script')).toHaveLength(0)
  expect(panel.innerHTML).not.toContain('<script>alert')
  expect(screen.queryByText(REPORT_UID)).toBeNull()
  expect(String(fetchMock.mock.calls.find(([input]) => String(input).includes(REPORT_UID))?.[0])).toBe(
    `/api/v1/reports/${REPORT_UID}?workspace_uid=${WORKSPACE}`,
  )
})

test('finalize posts expected_revision with a stable key and does not claim success from the click', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await screen.findByRole('button', { name: 'Finalize' })
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))
  expect(screen.getByRole('button', { name: 'Working…' })).toBeDisabled()
  expect(screen.queryByText('Finalized')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Working…' }))
  expect(posts(fetchMock, '/finalize?')).toHaveLength(1)
  await act(async () => {
    finish?.(await jsonResponse({
      data: summary({ state: 'finalized', revision: 2, source_stale: false }),
      meta: { replayed: false },
    }))
  })
  expect((await screen.findAllByText('Finalized')).length).toBeGreaterThan(0)
  const [, init] = posts(fetchMock, '/finalize?')[0]
  expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(new RegExp(`^${KEY_PREFIX}`))
  expect(JSON.parse(String(init?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
  })
  expect(String(posts(fetchMock, '/finalize?')[0][0])).toContain(`/reports/${REPORT_UID}/finalize?`)
})

test('archive and restore send the last confirmed revision and never delete', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/archive?')) {
      return jsonResponse({
        data: summary({
          state: 'archived',
          revision: 2,
          archived_from_state: 'draft',
          archived_at: LATER,
          archive_note: null,
        }),
        meta: { replayed: false },
      })
    }
    if (url.includes('/restore?')) {
      return jsonResponse({ data: summary({ state: 'draft', revision: 3 }), meta: { replayed: false } })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Archive' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))
  expect(JSON.parse(String(posts(fetchMock, '/archive?')[0][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
    note: null,
  })
  expect(JSON.parse(String(posts(fetchMock, '/restore?')[0][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 2,
  })
  expect(posts(fetchMock, '/delete')).toHaveLength(0)
})

test('an ambiguous finalize keeps draft, retries the same key, and never auto-retries a new one', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) return jsonResponse({ data: { unexpected: true }, meta: { replayed: false } })
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Finalize' }))
  expect(await screen.findByRole('button', { name: 'Retry the same request' })).toBeVisible()
  expect(screen.queryByText('Finalized')).toBeNull()
  const firstKey = (posts(fetchMock, '/finalize?')[0][1]?.headers as Record<string, string>)['Idempotency-Key']
  await userEvent.click(screen.getByRole('button', { name: 'Retry the same request' }))
  await waitFor(() => expect(posts(fetchMock, '/finalize?')).toHaveLength(2))
  expect((posts(fetchMock, '/finalize?')[1][1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(firstKey)
  expect(JSON.parse(String(posts(fetchMock, '/finalize?')[1][1]?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 1,
  })
})

test('a revision conflict keeps the read body and requires an explicit refresh before another write', async () => {
  let reads = 0
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) {
      return jsonResponse({
        error: { code: 'report_revision_conflict', message: 'revision moved' },
      }, 409)
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      reads += 1
      return jsonResponse({ data: readDocument({ revision: reads === 1 ? 1 : 4 }) })
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('<script>alert(1)</script>')
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))
  expect(await screen.findByText(/no longer matches the revision last confirmed/)).toBeVisible()
  expect(screen.getByLabelText('Current authored markdown')).toHaveTextContent('<script>alert(1)</script>')
  expect(screen.getByRole('button', { name: 'Finalize' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Refresh this report' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Finalize' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))
  expect(JSON.parse(String(posts(fetchMock, '/finalize?')[1][1]?.body)).expected_revision).toBe(4)
})

/**
 * `assertOk` copies `error.code` out of the envelope with no allowlist, so the workspace
 * can answer with a code that names an `Object.prototype` member. On an ordinary lookup
 * table `__proto__` returned the prototype OBJECT, and `transitionError` — rendered as
 * `{model.transitionError}` — is a React child: an object child throws and takes the
 * whole saved-report panel down; `constructor` returned a function, which renders blank.
 */
test.each([
  '__proto__',
  'constructor',
  'toString',
  'hasOwnProperty',
  'valueOf',
  'report_code_no_table_holds',
])('a transition refused with the code %s stays mounted and reads as authored copy', async (code) => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) {
      return jsonResponse({ error: { code, message: REMOTE_PROSE } }, 409)
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('<script>alert(1)</script>')
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))

  // The panel is still mounted and the one authored fallback is what the reader gets.
  const alert = await screen.findByText(/This saved report could not be updated, so nothing was written\./)
  expect(alert).toBeVisible()
  expect(screen.getByLabelText('Current authored markdown')).toHaveTextContent('<script>alert(1)</script>')
  expect(document.body.textContent).not.toContain('native code')
  expect(document.body.textContent).not.toContain('[object Object]')
  expect(document.body.textContent).not.toContain(REMOTE_PROSE)

  // A refusal wrote nothing: the row keeps its confirmed state and only one write left.
  expect(screen.queryByText('Finalized')).toBeNull()
  expect(posts(fetchMock, '/finalize?')).toHaveLength(1)
})

test('a workspace A-B-A swap drops the in-flight list instead of showing stale rows', async () => {
  const pending: Array<(value: Response) => void> = []
  const fetchMock = sessionFetch((url) => {
    if (url.startsWith('/api/v1/reports?')) {
      return new Promise<Response>((resolve) => pending.push(resolve))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const workspaceFor = (id: string) => ({ ...workspace, workspace: { ...workspace.workspace, id } })
  client.setQueryData(['workspace'], workspaceFor(WORKSPACE))
  renderPanel(WORKSPACE, client)
  await waitFor(() => expect(pending.length).toBe(1))
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(WORKSPACE_B))
    client.setQueryData(['workspace'], workspaceFor(WORKSPACE))
  })
  await waitFor(() => expect(pending.length).toBe(2))
  await act(async () => {
    pending[0]?.(await jsonResponse({
      data: listPage([listItem({ content_revision: 9, period: { kind: 'day', date: '2026-01-01' } })]),
    }))
  })
  expect(screen.queryByText(/Report version 9/)).toBeNull()
  await act(async () => {
    pending[1]?.(await jsonResponse({ data: listPage([listItem()]) }))
  })
  expect(await screen.findByText(/Report version 1/)).toBeVisible()
  expect(screen.queryByText(/Report version 9/)).toBeNull()
})

test('unmounting drops a late read so it cannot paint after teardown', async () => {
  let finish: ((value: Response) => void) | undefined
  sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  const { view } = renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  view.unmount()
  await act(async () => {
    finish?.(await jsonResponse({ data: readDocument() }))
  })
  expect(screen.queryByLabelText('Current authored markdown')).toBeNull()
})

test('Daily Review still owns the date control and does not autogenerate previews when saved reports load', async () => {
  const emptyReview: ReviewProjection = {
    day: { date: '2026-08-30', start_time: null, entries: [] },
    weekly: { range: { start: '2026-08-24', end: '2026-08-30', days: 7 }, objectives: [], projects: [] },
  }
  let previewCalls = 0
  const fetchMock = sessionFetch((url) => {
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.includes('preview')) {
      previewCalls += 1
      throw new Error(`preview should wait for a click: ${url}`)
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: WORKSPACE } }}
      />
    </QueryClientProvider>,
  )
  await screen.findByText('Not yet')
  expect(screen.getByRole('textbox', { name: 'Review date' })).toHaveValue('2026-08-30')
  expect(screen.getByRole('heading', { name: 'Saved reports' })).toBeVisible()
  expect(screen.getByRole('button', { name: /^Generate report$/ })).toBeVisible()
  expect(previewCalls).toBe(0)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'August 29, 2026' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('2026-08-29'))).toBe(true))
  expect(listUrls(fetchMock).every((url) => url.includes(`workspace_uid=${WORKSPACE}`))).toBe(true)
  expect(listUrls(fetchMock).length).toBeGreaterThan(0)
  expect(previewCalls).toBe(0)
})

test('an X-B-X reselect drops the first read so actions freeze the newest revision', async () => {
  const pending: Array<(value: Response) => void> = []
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) {
      return jsonResponse({ data: summary({ state: 'finalized', revision: 5 }), meta: { replayed: false } })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`) || url.startsWith(`/api/v1/reports/${REPORT_UID_B}?`)) {
      return new Promise<Response>((resolve) => pending.push(resolve))
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({
        data: listPage([
          listItem(),
          listItem({ uid: REPORT_UID_B, content_revision: 7, period: { kind: 'day', date: '2026-09-05' } }),
        ]),
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(screen.getByText(/Report version 7/))
  await userEvent.click(screen.getByText(/Report version 1/))
  expect(pending).toHaveLength(3)
  await act(async () => {
    pending[2]?.(await jsonResponse({
      data: readDocument({
        revision: 4,
        content_revision: 2,
        revisions: [
          content({ content_revision: 1, document_revision: 1, markdown: 'old body' }),
          content({ content_revision: 2, document_revision: 4, markdown: 'second body' }),
        ],
      }),
    }))
  })
  await act(async () => {
    pending[0]?.(await jsonResponse({
      data: readDocument({ revisions: [content({ markdown: 'first body' })] }),
    }))
  })
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('second body')
  expect(screen.queryByText('first body')).toBeNull()
  expect(screen.getByText('Report version 2', { selector: '.saved-reports__version' })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))
  await waitFor(() => expect(posts(fetchMock, '/finalize?')).toHaveLength(1))
  expect(JSON.parse(String(posts(fetchMock, '/finalize?')[0][1]?.body)).expected_revision).toBe(4)
})

test('a stale read failure cannot erase the newer read that already painted', async () => {
  const pending: Array<(value: Response) => void> = []
  sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`) || url.startsWith(`/api/v1/reports/${REPORT_UID_B}?`)) {
      return new Promise<Response>((resolve) => pending.push(resolve))
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({
        data: listPage([
          listItem(),
          listItem({ uid: REPORT_UID_B, content_revision: 7, period: { kind: 'day', date: '2026-09-05' } }),
        ]),
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(screen.getByText(/Report version 7/))
  await userEvent.click(screen.getByText(/Report version 1/))
  await act(async () => {
    pending[2]?.(await jsonResponse({
      data: readDocument({
        revision: 4,
        content_revision: 2,
        revisions: [
          content({ content_revision: 1, document_revision: 1, markdown: 'old body' }),
          content({ content_revision: 2, document_revision: 4, markdown: 'second body' }),
        ],
      }),
    }))
  })
  await act(async () => {
    pending[0]?.(await jsonResponse({ error: { code: 'report_not_found', message: 'gone' } }, 404))
  })
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('second body')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('button', { name: 'Finalize' })).toBeEnabled()
})

test('overlapping refreshes of the same report keep the newest body and revision', async () => {
  const pending: Array<(value: Response) => void> = []
  let reads = 0
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/finalize?')) {
      return jsonResponse({ error: { code: 'report_revision_conflict', message: 'revision moved' } }, 409)
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      reads += 1
      if (reads === 1) return jsonResponse({ data: readDocument() })
      return new Promise<Response>((resolve) => pending.push(resolve))
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Finalize' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Refresh this report' }))
  await userEvent.click(screen.getByRole('button', { name: 'Refresh this report' }))
  expect(pending).toHaveLength(2)
  await act(async () => {
    pending[1]?.(await jsonResponse({
      data: readDocument({
        revision: 4,
        content_revision: 2,
        revisions: [
          content({ content_revision: 1, document_revision: 1, markdown: 'old body' }),
          content({ content_revision: 2, document_revision: 4, markdown: 'newest body' }),
        ],
      }),
    }))
  })
  await act(async () => {
    pending[0]?.(await jsonResponse({
      data: readDocument({
        revision: 2,
        content_revision: 2,
        revisions: [
          content({ content_revision: 1, document_revision: 1, markdown: 'old body' }),
          content({ content_revision: 2, document_revision: 2, markdown: 'older body' }),
        ],
      }),
    }))
  })
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('newest body')
  expect(screen.queryByText('older body')).toBeNull()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Finalize' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: 'Finalize' }))
  await waitFor(() => expect(posts(fetchMock, '/finalize?')).toHaveLength(2))
  expect(JSON.parse(String(posts(fetchMock, '/finalize?')[1][1]?.body)).expected_revision).toBe(4)
})
