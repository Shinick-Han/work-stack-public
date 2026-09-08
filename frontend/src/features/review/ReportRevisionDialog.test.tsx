import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, vi } from 'vitest'
import { jsonResponse, workspace } from '../../test/fixtures'
import { SavedReportsPanel } from './SavedReportsPanel'

const SRC = dirname(fileURLToPath(import.meta.url))
const WORKSPACE = '0f50a123-3da8-4c82-8f16-8ee1a57260c4'
const WORKSPACE_B = '1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d'
const REPORT_UID = '00000001-abcd-4000-8000-000000000000'
const REPORT_UID_B = '00000002-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const LATER = '2026-09-06T13:00:00Z'
const GENERATED = '2026-09-06T11:59:00Z'
const MARKDOWN = '# Hello <script>alert(1)</script>\n\n| a | b |\n'
const KEY_PREFIX = 'workstack:'

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
    revision: 2,
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

function editor() {
  return screen.getByRole('textbox', { name: 'Report markdown' })
}

function captureDownloads() {
  const blobs: Blob[] = []
  const names: string[] = []
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      blobs.push(blob)
      return 'blob:fixture'
    },
    revokeObjectURL: () => undefined,
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    names.push(this.download)
  })
  return { blobs, names }
}

test('owned revision modules keep authored text inert and never mint weekly writes', () => {
  const dialog = readFileSync(join(SRC, 'ReportRevisionDialog.tsx'), 'utf8')
  const hook = readFileSync(join(SRC, 'useReportRevision.ts'), 'utf8')
  const model = readFileSync(join(SRC, 'reportRevisionModel.ts'), 'utf8')
  const panel = readFileSync(join(SRC, 'SavedReportsPanel.tsx'), 'utf8')
  expect(`${dialog}${hook}${model}${panel}`).not.toMatch(/dangerouslySetInnerHTML/)
  expect(panel).not.toMatch(/reviseReportDocument|createReportDocument|weekly-preview/)
  expect(`${dialog}${hook}`).not.toMatch(/localStorage/)
})

test('editing a finalized report saves a new authored version and reopens it as a draft', async () => {
  const next = '# second version'
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/revisions?')) {
      return jsonResponse({
        data: {
          ...summary({ state: 'draft', revision: 3 }),
          content_entry: content({
            content_revision: 2,
            document_revision: 3,
            markdown: next,
            authored_at: LATER,
            note: null,
          }),
          reopened: true,
          source_stale: false,
        },
        meta: { replayed: false },
      })
    }
    if (url.includes('/finalize?')) throw new Error('revision must not auto-finalize')
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({
        data: readDocument({
          state: 'finalized',
          revision: 2,
          content_revision: 1,
          revisions: [content({ markdown: MARKDOWN, document_revision: 2 })],
        }),
      })
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({ data: listPage([listItem({ state: 'finalized', revision: 2 })]) })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  expect(editor()).toHaveValue(MARKDOWN)
  expect(screen.getByText(/finalized report becomes a draft/i)).toBeVisible()
  fireEvent.change(editor(), { target: { value: next } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(await screen.findByText(/finalized report is now a draft/)).toBeVisible()
  expect(posts(fetchMock, '/finalize?')).toHaveLength(0)
  const [, init] = posts(fetchMock, '/revisions?')[0]
  expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(new RegExp(`^${KEY_PREFIX}`))
  expect(JSON.parse(String(init?.body))).toEqual({
    workspace_uid: WORKSPACE,
    expected_revision: 2,
    markdown: next,
    note: null,
  })
  expect(String(posts(fetchMock, '/revisions?')[0][0])).toContain(`/reports/${REPORT_UID}/revisions?`)
  expect(screen.getByLabelText('Authored markdown version 1')).toHaveTextContent('<script>alert(1)</script>')
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => (
    String(input).startsWith(`/api/v1/reports/${REPORT_UID}?`) && !String(input).includes('/revisions')
  ))).toBe(true))
})

test('an archived report cannot be revised until it is restored', async () => {
  sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({
        data: readDocument({
          state: 'archived',
          archived_from_state: 'finalized',
          revision: 3,
        }),
      })
    }
    if (url.includes('state=archived')) {
      return jsonResponse({
        data: listPage([listItem({ state: 'archived', archived_from_state: 'finalized', revision: 3 })]),
      })
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: 'Archived' }))
  await userEvent.click(await screen.findByText(/Report version 1/))
  expect(await screen.findByRole('button', { name: 'Edit report' })).toBeDisabled()
  expect(screen.queryByRole('textbox', { name: 'Report markdown' })).toBeNull()
})

test('a CAS conflict keeps the buffer, offers fallbacks, and does not rebase', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const { blobs, names } = captureDownloads()
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/revisions?')) {
      return jsonResponse({ error: { code: 'report_revision_conflict', message: 'revision moved' } }, 409)
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'kept locally' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/no longer matches the revision last confirmed/)
  expect(editor()).toHaveValue('kept locally')
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('kept locally'))
  await userEvent.click(screen.getByRole('button', { name: 'Download .md' }))
  expect(names).toEqual(['workstack-daily-2026-09-06-revision.md'])
  expect(blobs).toHaveLength(1)
  expect(JSON.parse(String(posts(fetchMock, '/revisions?')[0][1]?.body)).expected_revision).toBe(1)
  expect(screen.queryByRole('button', { name: 'Retry the same request' })).toBeNull()
})

test('commit-unknown retries the same key and frozen payload, not newer typed text', async () => {
  const fetchMock = sessionFetch((url) => {
    if (url.includes('/revisions?')) return jsonResponse({ data: { unexpected: true }, meta: { replayed: false } })
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'frozen body' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(await screen.findByRole('button', { name: 'Retry the same request' })).toBeVisible()
  fireEvent.change(editor(), { target: { value: 'newer typed body' } })
  const first = posts(fetchMock, '/revisions?')[0]
  const firstKey = (first[1]?.headers as Record<string, string>)['Idempotency-Key']
  await userEvent.click(screen.getByRole('button', { name: 'Retry the same request' }))
  await waitFor(() => expect(posts(fetchMock, '/revisions?')).toHaveLength(2))
  const second = posts(fetchMock, '/revisions?')[1]
  expect((second[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(firstKey)
  expect(JSON.parse(String(second[1]?.body))).toEqual(JSON.parse(String(first[1]?.body)))
  expect(JSON.parse(String(second[1]?.body)).markdown).toBe('frozen body')
  expect(editor()).toHaveValue('newer typed body')
})

test('text typed during an in-flight save is kept and only the written snapshot is cleared', async () => {
  let finish: ((value: Response) => void) | undefined
  sessionFetch((url) => {
    if (url.includes('/revisions?')) {
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
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'first' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()
  fireEvent.change(editor(), { target: { value: 'first extra' } })
  await act(async () => {
    finish?.(await jsonResponse({
      data: {
        ...summary({ revision: 2 }),
        content_entry: content({
          content_revision: 2,
          document_revision: 2,
          markdown: 'first',
          authored_at: LATER,
        }),
        reopened: false,
        source_stale: false,
      },
      meta: { replayed: false },
    }))
  })
  expect(editor()).toHaveValue('first extra')
  expect(screen.getByText(/unsaved changes/)).toBeVisible()
  expect(screen.getByText(/Saved as a new authored version/)).toBeVisible()
  expect(screen.queryByText(/finalized report is now a draft/)).toBeNull()
})

test('a workspace A-B-A swap drops the in-flight save and keeps the original buffer labeled', async () => {
  let finish: ((value: Response) => void) | undefined
  sessionFetch((url) => {
    if (url.includes('/revisions?')) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`) || url.startsWith(`/api/v1/reports/${REPORT_UID_B}?`)) {
      return jsonResponse({ data: readDocument() })
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const workspaceFor = (id: string) => ({ ...workspace, workspace: { ...workspace.workspace, id } })
  client.setQueryData(['workspace'], workspaceFor(WORKSPACE))
  renderPanel(WORKSPACE, client)
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'original report text' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(WORKSPACE_B))
    client.setQueryData(['workspace'], workspaceFor(WORKSPACE))
  })
  expect(await screen.findByText(/still holds the original report/)).toBeVisible()
  expect(screen.getByLabelText('Original report markdown')).toHaveTextContent('original report text')
  expect(screen.getByRole('button', { name: 'Save new version' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Download .md' })).toBeDisabled()
  await act(async () => {
    finish?.(await jsonResponse({
      data: {
        ...summary({ revision: 9 }),
        content_entry: content({
          content_revision: 2,
          document_revision: 9,
          markdown: 'original report text',
          authored_at: LATER,
        }),
        reopened: false,
        source_stale: false,
      },
      meta: { replayed: false },
    }))
  })
  expect(screen.queryByText(/Saved as a new authored version/)).toBeNull()
  expect(screen.getByLabelText('Original report markdown')).toHaveTextContent('original report text')
})

test('selecting another report keeps the original buffer and does not let it bleed into the new report', async () => {
  sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({ data: readDocument({ revisions: [content({ markdown: 'report A body' })] }) })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID_B}?`)) {
      return jsonResponse({
        data: readDocument({
          uid: REPORT_UID_B,
          content_revision: 2,
          revision: 2,
          period: { kind: 'day', date: '2026-09-05' },
          revisions: [
            content({ markdown: 'old B', content_revision: 1, document_revision: 1 }),
            content({ markdown: 'report B body', content_revision: 2, document_revision: 2 }),
          ],
        }),
      })
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({
        data: listPage([
          listItem(),
          listItem({ uid: REPORT_UID_B, content_revision: 2, period: { kind: 'day', date: '2026-09-05' } }),
        ]),
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'typed against A' } })
  await userEvent.click(screen.getByText(/Report version 2/))
  expect(await screen.findByText(/still holds the original report/)).toBeVisible()
  expect(editor()).toHaveValue('typed against A')
  expect(editor()).not.toHaveValue('report B body')
  expect(screen.getByLabelText('Original report markdown')).toHaveTextContent('typed against A')
  expect(await screen.findByLabelText('Current authored markdown')).toHaveTextContent('report B body')
  expect(screen.getByRole('button', { name: 'Save new version' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeDisabled()
})

test('closing with unsaved text asks first and does not discard on cancel', async () => {
  sessionFetch((url) => {
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) return jsonResponse({ data: readDocument() })
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'unsaved buffer' } })
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  const prompt = screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })
  expect(prompt).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
  expect(editor()).toHaveValue('unsaved buffer')
  expect(screen.queryByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  await userEvent.click(screen.getByRole('button', { name: 'Discard changes and close' }))
  expect(screen.queryByRole('textbox', { name: 'Report markdown' })).toBeNull()
})

test('authored markdown stays inert text and fallbacks stay enabled after a refused write', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  sessionFetch((url) => {
    if (url.includes('/revisions?')) {
      return jsonResponse({ error: { code: 'report_revision_limit', message: 'limit' } }, 409)
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({
        data: readDocument({ source_stale: true, revisions: [content({ markdown: MARKDOWN })] }),
      })
    }
    if (url.startsWith('/api/v1/reports?')) return jsonResponse({ data: listPage([listItem()]) })
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPanel()
  await userEvent.click(await screen.findByText(/Report version 1/))
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  expect(editor()).toHaveValue(MARKDOWN)
  const dialog = screen.getByRole('dialog', { name: 'Edit saved report' })
  expect(dialog).toHaveTextContent(/day this report was based on has changed/)
  expect(dialog.querySelectorAll('script')).toHaveLength(0)
  expect(dialog.innerHTML).not.toContain('<script>alert')
  fireEvent.change(editor(), { target: { value: MARKDOWN + 'more' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/cannot take another authored version/)
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(MARKDOWN + 'more'))
})

test('an invalid workspace identity never opens a revision write', async () => {
  const fetchMock = sessionFetch(() => {
    throw new Error('report transport should not run')
  })
  renderPanel(workspace.workspace.id)
  expect(await screen.findByRole('heading', { name: 'Saved reports' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Edit report' })).toBeNull()
  expect(fetchMock).not.toHaveBeenCalled()
})

async function selectRow(label: RegExp) {
  const list = await screen.findByRole('list')
  await userEvent.click(within(list).getByText(label))
}

/** Report A on 2026-09-06 (version 1) and report B on 2026-09-05 (version 2). */
function twoReportSession(revisions: (url: string) => Promise<Response> | Response) {
  return sessionFetch((url) => {
    if (url.includes('/revisions?')) return revisions(url)
    if (url.startsWith(`/api/v1/reports/${REPORT_UID}?`)) {
      return jsonResponse({ data: readDocument({ revisions: [content({ markdown: 'report A body' })] }) })
    }
    if (url.startsWith(`/api/v1/reports/${REPORT_UID_B}?`)) {
      return jsonResponse({
        data: readDocument({
          uid: REPORT_UID_B,
          content_revision: 2,
          revision: 2,
          period: { kind: 'day', date: '2026-09-05' },
          revisions: [content({ markdown: 'report B body', content_revision: 2, document_revision: 2 })],
        }),
      })
    }
    if (url.startsWith('/api/v1/reports?')) {
      return jsonResponse({
        data: listPage([
          listItem(),
          listItem({ uid: REPORT_UID_B, content_revision: 2, period: { kind: 'day', date: '2026-09-05' } }),
        ]),
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
}

/** A pending native write plus a fallback probe that records the text it would copy. */
function pendingNativeClipboard() {
  let rejectWrite: ((reason: unknown) => void) | undefined
  const writeText = vi.fn(() => new Promise<void>((_, reject) => {
    rejectWrite = reject
  }))
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const fallbackText: (string | null)[] = []
  const execCommand = vi.fn(() => {
    const field = document.querySelector('textarea[readonly]')
    fallbackText.push(field instanceof HTMLTextAreaElement ? field.value : null)
    return true
  })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
  return {
    execCommand,
    fallbackText,
    rejectNative: (reason: unknown) => rejectWrite?.(reason),
    writeText,
  }
}

async function settleClipboard() {
  await act(async () => {
    for (let tick = 0; tick < 8; tick += 1) await Promise.resolve()
  })
}

test('a report A-B-A swap drops the in-flight save callback and keeps the frozen request retryable', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = twoReportSession(() => new Promise<Response>((resolve) => {
    finish = resolve
  }))
  renderPanel()
  await selectRow(/Report version 1/)
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'typed against A' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  await waitFor(() => expect(posts(fetchMock, '/revisions?')).toHaveLength(1))
  const first = posts(fetchMock, '/revisions?')[0]
  const firstKey = (first[1]?.headers as Record<string, string>)['Idempotency-Key']

  await selectRow(/Report version 2/)
  expect(await screen.findByText(/still holds the original report/)).toBeVisible()
  await selectRow(/Report version 1/)
  await waitFor(() => expect(screen.queryByText(/still holds the original report/)).toBeNull())

  // Returning to A restores the coordinate but not the generation: the pre-cycle
  // callback cannot publish a success or run onSaved.
  await act(async () => {
    finish?.(await jsonResponse({
      data: {
        ...summary({ revision: 2 }),
        content_entry: content({
          content_revision: 2,
          document_revision: 2,
          markdown: 'typed against A',
          authored_at: LATER,
        }),
        reopened: false,
        source_stale: false,
      },
      meta: { replayed: false },
    }))
  })
  expect(screen.queryByText(/Saved as a new authored version/)).toBeNull()
  expect(editor()).toHaveValue('typed against A')

  // The write stayed unknown rather than determinate, so the exact frozen request
  // is still replayable under its original idempotency key.
  await userEvent.click(screen.getByRole('button', { name: 'Retry the same request' }))
  await waitFor(() => expect(posts(fetchMock, '/revisions?')).toHaveLength(2))
  const second = posts(fetchMock, '/revisions?')[1]
  expect((second[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(firstKey)
  expect(JSON.parse(String(second[1]?.body))).toEqual(JSON.parse(String(first[1]?.body)))
  expect(JSON.parse(String(second[1]?.body)).markdown).toBe('typed against A')
  expect(String(second[0])).toContain(`/reports/${REPORT_UID}/revisions?`)
})

test('a native clipboard rejection after another report is selected never runs the fallback', async () => {
  const clipboard = pendingNativeClipboard()
  twoReportSession(() => {
    throw new Error('no revision write should run')
  })
  renderPanel()
  await selectRow(/Report version 1/)
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'report A clipboard body' } })
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith('report A clipboard body'))

  await selectRow(/Report version 2/)
  expect(await screen.findByText(/still holds the original report/)).toBeVisible()
  clipboard.rejectNative(new Error('blocked by permission policy'))
  await settleClipboard()

  expect(clipboard.execCommand).not.toHaveBeenCalled()
  expect(clipboard.fallbackText).toEqual([])
  expect(screen.queryByText(/clipboard is unavailable/)).toBeNull()
  expect(screen.queryByText(/was copied as Markdown/)).toBeNull()
})

test('a native clipboard rejection after an A-B-A report swap never runs the fallback', async () => {
  const clipboard = pendingNativeClipboard()
  twoReportSession(() => {
    throw new Error('no revision write should run')
  })
  renderPanel()
  await selectRow(/Report version 1/)
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'report A clipboard body' } })
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith('report A clipboard body'))

  await selectRow(/Report version 2/)
  expect(await screen.findByText(/still holds the original report/)).toBeVisible()
  await selectRow(/Report version 1/)
  await waitFor(() => expect(screen.queryByText(/still holds the original report/)).toBeNull())
  clipboard.rejectNative(new Error('blocked by permission policy'))
  await settleClipboard()

  // The coordinate came back, but the copy operation token did not.
  expect(clipboard.execCommand).not.toHaveBeenCalled()
  expect(clipboard.fallbackText).toEqual([])
  expect(screen.queryByText(/clipboard is unavailable/)).toBeNull()
  expect(screen.queryByText(/was copied as Markdown/)).toBeNull()
})

test('a native clipboard rejection still falls back while the same report stays selected', async () => {
  const clipboard = pendingNativeClipboard()
  twoReportSession(() => {
    throw new Error('no revision write should run')
  })
  renderPanel()
  await selectRow(/Report version 1/)
  await userEvent.click(await screen.findByRole('button', { name: 'Edit report' }))
  fireEvent.change(editor(), { target: { value: 'report A clipboard body' } })
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith('report A clipboard body'))

  clipboard.rejectNative(new Error('blocked by permission policy'))
  await settleClipboard()

  expect(clipboard.fallbackText).toEqual(['report A clipboard body'])
  expect(await screen.findByText(/was copied as Markdown/)).toBeVisible()
})
