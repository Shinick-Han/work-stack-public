import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { DailyReportPreview } from './DailyReportPreview'
import { jsonResponse, workspace } from '../../test/fixtures'
import type { ReviewProjection } from '../../domain/types'
import type { DailyReportPreviewResponse } from '../../domain/reporting'
import type { DailyReportDraftEditorProps } from './DailyReportDraftEditor'

const { editorProbe } = vi.hoisted(() => ({
  editorProbe: { props: null as DailyReportDraftEditorProps | null },
}))

function resetEditorProbe() {
  editorProbe.props = null
}

vi.mock('./DailyReportDraftEditor', () => ({
  DailyReportDraftEditor: (props: DailyReportDraftEditorProps) => {
    editorProbe.props = props
    return (
      <div aria-label="Local draft editor" role="dialog">
        <p>{props.coordinate.workspaceUid}</p>
        <p>{props.coordinate.date}</p>
        <p>{props.coordinate.template}</p>
        <p>{props.source.sourceDigest}</p>
        <p>{props.source.generatedAt}</p>
        <pre>{props.source.markdown}</pre>
        <button type="button" onClick={props.onClose}>Close draft</button>
      </div>
    )
  },
}))

const UID = '22222222-2222-4222-8222-222222222222'
const DATE = '2026-08-30'
const REVIEW_KEY = ['review', DATE, 7] as const
const DIGEST = `sha256:${'a'.repeat(64)}`
const MARKDOWN = '# Daily review 2026-08-30\n\n## Day record\n\nNo records.\n'

function reviewProjection(day: ReviewProjection['day']): ReviewProjection {
  return {
    day,
    weekly: { range: { start: DATE, end: DATE, days: 7 }, objectives: [], projects: [] },
  }
}

function previewResponse(overrides: Partial<DailyReportPreviewResponse> = {}): DailyReportPreviewResponse {
  return {
    workspace_uid: UID,
    source_digest: DIGEST,
    preview: {
      template: 'daily-v1',
      period: { kind: 'day', date: DATE },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        date: DATE,
        task_ids: [],
        sources: [],
        weekly_range: { start: DATE, end: DATE, days: 1 },
        ignored_keys: [],
      },
      markdown: MARKDOWN,
    },
    ...overrides,
  }
}

function renderPreview(
  fetchMock: ReturnType<typeof vi.fn>,
  props: { sourceUpdatedAt?: number; workspaceId?: string } = {},
  client?: QueryClient,
) {
  vi.stubGlobal('fetch', fetchMock)
  const queryClient = client ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview
        date={DATE}
        sourceUpdatedAt={props.sourceUpdatedAt ?? 10}
        workspaceId={props.workspaceId ?? UID}
      />
    </QueryClientProvider>,
  )
  return { queryClient, view }
}

test('generates a readable empty-day document without showing technical ids', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeVisible()
  expect(screen.queryByText(UID)).toBeNull()
  expect(screen.queryByText(DIGEST)).toBeNull()
  expect(screen.queryByText('review.day.entry')).toBeNull()
  const url = String(fetchMock.mock.calls[0][0])
  expect(url).toContain('date=2026-08-30')
  expect(url).toContain('template=daily-v1')
  expect(url).toContain(`workspace_uid=${UID}`)
})

test('shows an explicit error and retry when generation fails', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ error: { code: 'unavailable', message: 'Preview unavailable.' } }, 503)
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('Preview unavailable.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
})

test('refuses a mismatched workspace payload', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({
        data: previewResponse({ workspace_uid: '00000000-0000-4000-8000-0000000000b0' }),
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeNull()
})

test('clears a displayed document when review sourceUpdatedAt or the selected day changes without autogenerating', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByText('No records for this day.')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)

  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  const afterSecond = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date="2026-08-29" sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByText('No records for this day.')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(afterSecond)
})

test('keeps a displayed document when sourceUpdatedAt is unchanged across a parent rerender', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={10} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.getByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)
})

test('discards a late A-B-A completion instead of showing the stale markdown', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const workspaceFor = (id: string) => ({
    ...workspace,
    workspace: { ...workspace.workspace, id },
  })
  const uidB = '00000000-0000-4000-8000-0000000000b0'
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(['workspace'], workspaceFor(UID))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(uidB))
    client.setQueryData(['workspace'], workspaceFor(UID))
  })
  await act(async () => {
    finish?.(await jsonResponse({ data: previewResponse() }))
  })
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
})

test('discards a late same-batch day A-B-A completion instead of showing the stale markdown', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const dayA = { date: DATE, start_time: null, entries: [] }
  const dayB = { date: DATE, start_time: '09:20', entries: [] }
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(REVIEW_KEY, reviewProjection(dayA))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await act(async () => {
    client.setQueryData(REVIEW_KEY, reviewProjection(dayB))
    client.setQueryData(REVIEW_KEY, reviewProjection(dayA))
  })
  await act(async () => {
    finish?.(await jsonResponse({ data: previewResponse() }))
  })
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
})

test('a weekly-only review cache change keeps the generated daily preview', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const day = { date: DATE, start_time: null, entries: [] as ReviewProjection['day']['entries'] }
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(REVIEW_KEY, reviewProjection(day))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  await act(async () => {
    client.setQueryData(REVIEW_KEY, {
      day,
      weekly: {
        range: { start: DATE, end: DATE, days: 7 },
        objectives: [{ id: 'O-1', objective: 'Weekly only' }],
        projects: [{
          task_id: 'T-0001',
          task: 'Weekly project',
          done: ['rolled up'],
          next: [],
          blockers: [],
          objective_ids: ['O-1'],
          dates: ['2026-08-29'],
          duration_seconds: 120,
        }],
      },
    })
  })
  expect(screen.getByText('No records for this day.')).toBeVisible()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)
})

test('copies the original markdown and surfaces clipboard failure', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  expect(writeText).toHaveBeenCalledWith(MARKDOWN)
  expect(await screen.findByRole('button', { name: 'Markdown copied' })).toBeVisible()

  writeText.mockRejectedValue(new Error('denied'))
  Object.defineProperty(document, 'execCommand', { configurable: true, value: () => false })
  await userEvent.click(screen.getByRole('button', { name: 'Markdown copied' }))
  expect(await screen.findByText('Markdown could not be copied.')).toBeVisible()
})

test('a fresh owner can generate while a stale request is still pending', async () => {
  const pending: Array<(value: Response) => void> = []
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return new Promise<Response>((resolve) => {
        pending.push(resolve)
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(screen.getByRole('button', { name: 'Generating…' })).toBeDisabled()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  const generate = screen.getByRole('button', { name: 'Generate report' })
  expect(generate).toBeEnabled()
  await userEvent.click(generate)
  expect(screen.getByRole('button', { name: 'Generating…' })).toBeDisabled()
  expect(fetchMock).toHaveBeenCalledTimes(2)

  await act(async () => {
    pending[0]?.(await jsonResponse({
      data: previewResponse({
        preview: {
          ...previewResponse().preview,
          markdown: '# Stale owner markdown\n',
          absence: null,
        },
      }),
    }))
  })
  expect(screen.queryByRole('heading', { name: 'Stale owner markdown' })).toBeNull()
  expect(screen.queryByText('No records for this day.')).toBeNull()

  await act(async () => {
    pending[1]?.(await jsonResponse({ data: previewResponse() }))
  })
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  expect(screen.queryByRole('heading', { name: 'Stale owner markdown' })).toBeNull()
})

test('oversized markdown is refused and never shown as a report', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({
        data: previewResponse({
          preview: {
            ...previewResponse().preview,
            markdown: 'm'.repeat(100_001),
          },
        }),
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeNull()
  expect(screen.queryByText('No records for this day.')).toBeNull()
})

test('downloads the original markdown as workstack-daily-<date>.md', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  let filename = ''
  const createObjectURL = vi.fn(() => 'blob:daily')
  const revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  await userEvent.click(screen.getByRole('button', { name: 'Download .md' }))
  expect(createObjectURL).toHaveBeenCalled()
  expect(filename).toBe('workstack-daily-2026-08-30.md')
})

test('a deferred copy does not stamp copied or failure onto a newer preview', async () => {
  let finishCopy: ((ok: boolean) => void) | undefined
  const writeText = vi.fn(() => new Promise<void>((resolve, reject) => {
    finishCopy = (ok) => (ok ? resolve() : reject(new Error('denied')))
  }))
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: () => false })
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  expect(writeText).toHaveBeenCalledWith(MARKDOWN)

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeVisible()

  await act(async () => {
    finishCopy?.(true)
  })
  expect(screen.queryByRole('button', { name: 'Markdown copied' })).toBeNull()

  let finishFail: ((ok: boolean) => void) | undefined
  writeText.mockImplementation(() => new Promise<void>((resolve, reject) => {
    finishFail = (ok) => (ok ? resolve() : reject(new Error('denied')))
  }))
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={12} workspaceId={UID} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await act(async () => {
    finishFail?.(false)
  })
  expect(screen.queryByText('Markdown could not be copied.')).toBeNull()
})

test('Edit local draft opens the editor with the owned source snapshot', async () => {
  resetEditorProbe()
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  await userEvent.click(screen.getByRole('button', { name: 'Edit local draft' }))
  expect(screen.getByRole('dialog', { name: 'Local draft editor' })).toBeVisible()
  expect(editorProbe.props?.coordinate).toEqual({
    workspaceUid: UID,
    date: DATE,
    template: 'daily-v1',
  })
  expect(editorProbe.props?.source).toEqual({
    sourceDigest: DIGEST,
    generatedAt: '2026-09-06T01:02:03Z',
    markdown: MARKDOWN,
  })
  expect(DIGEST.startsWith('sha256:')).toBe(true)
})

test('an old owner cannot open the editor after a same-batch owner swap', async () => {
  resetEditorProbe()
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const workspaceFor = (id: string) => ({
    ...workspace,
    workspace: { ...workspace.workspace, id },
  })
  const uidB = '00000000-0000-4000-8000-0000000000b0'
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(['workspace'], workspaceFor(UID))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  const edit = screen.getByRole('button', { name: 'Edit local draft' })
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(uidB))
    client.setQueryData(['workspace'], workspaceFor(UID))
    edit.click()
  })
  expect(screen.queryByRole('dialog', { name: 'Local draft editor' })).toBeNull()
  expect(editorProbe.props).toBeNull()
})

test('source invalidation leaves an open editor pinned and close still works', async () => {
  resetEditorProbe()
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('No records for this day.')
  await userEvent.click(screen.getByRole('button', { name: 'Edit local draft' }))
  expect(screen.getByRole('dialog', { name: 'Local draft editor' })).toBeVisible()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date={DATE} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByText('No records for this day.')).toBeNull()
  expect(screen.getByRole('dialog', { name: 'Local draft editor' })).toBeVisible()
  expect(editorProbe.props?.coordinate.date).toBe(DATE)
  expect(editorProbe.props?.source.markdown).toBe(MARKDOWN)
  expect(editorProbe.props?.source.sourceDigest).toBe(DIGEST)

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date="2026-08-29" sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.getByRole('dialog', { name: 'Local draft editor' })).toBeVisible()
  expect(editorProbe.props?.coordinate).toEqual({
    workspaceUid: UID,
    date: DATE,
    template: 'daily-v1',
  })

  await userEvent.click(screen.getByRole('button', { name: 'Close draft' }))
  expect(screen.queryByRole('dialog', { name: 'Local draft editor' })).toBeNull()
})

const CATALOG_TASKS = ['T-0001', 'T-0002']

function withCatalog(
  catalog: Record<string, unknown> | undefined,
  date: string = DATE,
) {
  const base = previewResponse()
  const response: Record<string, unknown> = {
    ...base,
    preview: {
      ...base.preview,
      period: { kind: 'day', date },
      absence: null,
      provenance: { ...base.preview.provenance, date, task_ids: CATALOG_TASKS },
    },
  }
  if (catalog) response.context_catalog = catalog
  return response
}

function readyCatalog(overrides: Record<string, unknown> = {}) {
  return {
    captured_at: '2026-09-06T01:02:03Z',
    items: [{
      capture_id: 'C-0001',
      capture_revision: 1,
      title: 'Synthetic context',
      linked_task_ids: ['T-0001'],
      status: 'linked',
    }],
    omitted_count: 0,
    ...overrides,
  }
}

function previewFetch(body: unknown) {
  return vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/daily-preview?')) return jsonResponse({ data: body })
    throw new Error(`Unexpected request: ${String(input)}`)
  })
}

/** jsdom's Blob has no .text(); FileReader is the supported reader here. */
function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

function contextPanel() {
  return screen.queryByRole('region', { name: 'Related task context' })
}

test('shows the related context panel beside an admitted preview and asks for nothing more', async () => {
  const fetchMock = previewFetch(withCatalog(readyCatalog()))
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  const panel = await screen.findByRole('region', { name: 'Related task context' })
  expect(within(panel).getByText('C-0001')).toBeVisible()
  expect(within(panel).getByText('Synthetic context')).toBeVisible()
  expect(within(panel).getByText('linked')).toBeVisible()
  expect(within(panel).getByText('Linked tasks: T-0001')).toBeVisible()
  expect(within(panel).queryAllByRole('button')).toHaveLength(0)
  expect(within(panel).queryAllByRole('link')).toHaveLength(0)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('hides the panel entirely when a pre-R44 server omits the catalogue', async () => {
  const fetchMock = previewFetch(withCatalog(undefined))
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeVisible()
  expect(contextPanel()).toBeNull()
})

test('states an empty catalogue plainly', async () => {
  const fetchMock = previewFetch(withCatalog(readyCatalog({ items: [] })))
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('No context is linked to these tasks.')).toBeVisible()
  expect(screen.getByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeVisible()
})

test('keeps a truncated catalogue visible without making the report unavailable', async () => {
  const full = Array.from({ length: 32 }, (unused, index) => ({
    capture_id: `C-${String(index + 1).padStart(4, '0')}`,
    capture_revision: 0,
    title: `Context ${index + 1}`,
    linked_task_ids: ['T-0002'],
    status: 'linked',
  }))
  const fetchMock = previewFetch(withCatalog(readyCatalog({ items: full, omitted_count: 4 })))
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText(
    '4 more linked captures are not shown here. Review Task context for the full list.',
  )).toBeVisible()
  expect(screen.getByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeVisible()
})

test('refuses a malformed catalogue instead of showing a laundered panel', async () => {
  const fetchMock = previewFetch(withCatalog(readyCatalog({
    items: [{
      capture_id: 'C-0001',
      capture_revision: 1,
      title: 'Synthetic context',
      linked_task_ids: ['T-0001'],
      status: 'linked',
      source_url: 'https://example.invalid/doc',
    }],
  })))
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(contextPanel()).toBeNull()
  expect(screen.queryByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeNull()
  expect(screen.queryByText('https://example.invalid/doc')).toBeNull()
})

test('never keeps the previous day catalogue after the preview is invalidated', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (!url.startsWith('/api/v1/reports/daily-preview?')) {
      throw new Error(`Unexpected request: ${url}`)
    }
    const yesterday = url.includes('date=2026-08-29')
    return jsonResponse({
      data: yesterday
        ? withCatalog(readyCatalog({ items: [{
          capture_id: 'C-0002',
          capture_revision: 2,
          title: 'Yesterday context',
          linked_task_ids: ['T-0002'],
          status: 'dismissed',
        }] }), '2026-08-29')
        : withCatalog(readyCatalog()),
    })
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('Synthetic context')).toBeVisible()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <DailyReportPreview date="2026-08-29" sourceUpdatedAt={10} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(contextPanel()).toBeNull()
  expect(screen.queryByText('Synthetic context')).toBeNull()

  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  expect(await screen.findByText('Yesterday context')).toBeVisible()
  expect(screen.getByText('dismissed')).toBeVisible()
  expect(screen.queryByText('Synthetic context')).toBeNull()
})

test('copy and download still carry exactly the preview markdown when a catalogue is present', async () => {
  const fetchMock = previewFetch(withCatalog(readyCatalog()))
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  let filename = ''
  const blobs: Blob[] = []
  const createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob)
    return 'blob:daily'
  })
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
  await screen.findByText('Synthetic context')

  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  expect(writeText).toHaveBeenCalledWith(MARKDOWN)

  await userEvent.click(screen.getByRole('button', { name: 'Download .md' }))
  expect(await blobText(blobs[0])).toBe(MARKDOWN)
  expect(filename).toBe('workstack-daily-2026-08-30.md')
})
