import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { WeeklyReportPreview } from './WeeklyReportPreview'
import { jsonResponse, workspace } from '../../test/fixtures'
import type { DailyReportContextCatalog } from '../../domain/reporting'
import type { ReviewProjection } from '../../domain/types'
import { shiftIsoDate } from '../../domain/weeklyReporting'
import type { WeeklyReportPreviewResponse } from '../../domain/weeklyReporting'

const UID = '22222222-2222-4222-8222-222222222222'
const END = '2026-08-30'
const START = '2026-08-24'
const WEEK = [
  '2026-08-24',
  '2026-08-25',
  '2026-08-26',
  '2026-08-27',
  '2026-08-28',
  '2026-08-29',
  '2026-08-30',
]
const REVIEW_KEY = ['review', END, 7] as const
const DIGEST = `sha256:${'a'.repeat(64)}`
const MARKDOWN = [
  '# Weekly review 2026-08-24 → 2026-08-30',
  '',
  'Period: 2026-08-24 → 2026-08-30',
  'Generated at: 2026-09-06T01:02:03Z',
  '',
  '## Coverage',
  '',
  ...WEEK.map((day) => `- ${day}: No records.`),
  '',
].join('\n')

function reviewProjection(weekly: ReviewProjection['weekly']): ReviewProjection {
  return {
    day: { date: END, start_time: null, entries: [] },
    weekly,
  }
}

const emptyWeekly: ReviewProjection['weekly'] = {
  range: { start: START, end: END, days: 7 },
  objectives: [],
  projects: [],
}

function previewResponse(
  overrides: Partial<WeeklyReportPreviewResponse> = {},
): WeeklyReportPreviewResponse {
  return {
    workspace_uid: UID,
    source_digest: DIGEST,
    preview: {
      template: 'weekly-v1',
      period: { kind: 'week', start: START, end: END, days: 7 },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        range: { start: START, end: END, days: 7 },
        task_ids: [],
        sources: [],
        coverage: { record_dates: [], no_record_dates: WEEK },
        ignored_keys: [],
      },
      markdown: MARKDOWN,
    },
    ...overrides,
  }
}

function renderPreview(
  fetchMock: ReturnType<typeof vi.fn>,
  props: { sourceUpdatedAt?: number | string; workspaceId?: string } = {},
  client?: QueryClient,
) {
  vi.stubGlobal('fetch', fetchMock)
  const queryClient = client ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview
        endDate={END}
        sourceUpdatedAt={props.sourceUpdatedAt ?? 10}
        workspaceId={props.workspaceId ?? UID}
      />
    </QueryClientProvider>,
  )
  return { queryClient, view }
}

test('generates a readable empty-week document without showing technical ids', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  renderPreview(fetchMock)
  expect(screen.queryByText('No records for this week.')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  expect(screen.getByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeVisible()
  expect(screen.queryByText(UID)).toBeNull()
  expect(screen.queryByText(DIGEST)).toBeNull()
  expect(screen.queryByText('review.weekly.project')).toBeNull()
  const url = String(fetchMock.mock.calls[0][0])
  expect(url).toBe(
    `/api/v1/reports/weekly-preview?end_date=${END}&template=weekly-v1&workspace_uid=${UID}`,
  )
})

test('shows an explicit error and retry when generation fails', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ error: { code: 'unavailable', message: 'Preview unavailable.' } }, 503)
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('Preview unavailable.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Retry weekly report' })).toBeVisible()
})

test('refuses a mismatched workspace payload', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({
        data: previewResponse({ workspace_uid: '00000000-0000-4000-8000-0000000000b0' }),
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeNull()
})

test('clears a displayed document when sourceUpdatedAt or the selected week changes without autogenerating', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByText('No records for this week.')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)

  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  const afterSecond = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate="2026-08-29" sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByText('No records for this week.')).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(afterSecond)
})

test('keeps a displayed document when sourceUpdatedAt is unchanged across a parent rerender', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={10} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.getByText('No records for this week.')).toBeVisible()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)
})

test('discards a late A-B-A completion instead of showing the stale markdown', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
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
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(uidB))
    client.setQueryData(['workspace'], workspaceFor(UID))
  })
  await act(async () => {
    finish?.(await jsonResponse({ data: previewResponse() }))
  })
  await waitFor(() => expect(screen.queryByText('No records for this week.')).toBeNull())
})

test('discards a late same-batch weekly A-B-A completion instead of showing the stale markdown', async () => {
  let finish: ((value: Response) => void) | undefined
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return new Promise<Response>((resolve) => {
        finish = resolve
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const weekB: ReviewProjection['weekly'] = {
    ...emptyWeekly,
    objectives: [{ id: 'O-1', objective: 'Weekly only' }],
  }
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(REVIEW_KEY, reviewProjection(emptyWeekly))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await act(async () => {
    client.setQueryData(REVIEW_KEY, reviewProjection(weekB))
    client.setQueryData(REVIEW_KEY, reviewProjection(emptyWeekly))
  })
  await act(async () => {
    finish?.(await jsonResponse({ data: previewResponse() }))
  })
  await waitFor(() => expect(screen.queryByText('No records for this week.')).toBeNull())
})

test('a day-only review cache change keeps the generated weekly preview', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(REVIEW_KEY, reviewProjection(emptyWeekly))
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  const afterGenerate = fetchMock.mock.calls.length
  await act(async () => {
    client.setQueryData(REVIEW_KEY, {
      day: {
        date: END,
        start_time: '09:20',
        entries: [{
          task_id: 'T-0001',
          task: 'Day record',
          done: ['logged'],
          next: [],
          blockers: [],
        }],
      },
      weekly: emptyWeekly,
    })
  })
  expect(screen.getByText('No records for this week.')).toBeVisible()
  expect(fetchMock).toHaveBeenCalledTimes(afterGenerate)
})

test('copies the original markdown and surfaces clipboard failure', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await screen.findByText('No records for this week.')
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
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return new Promise<Response>((resolve) => {
        pending.push(resolve)
      })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(screen.getByRole('button', { name: 'Generating…' })).toBeDisabled()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  const generate = screen.getByRole('button', { name: 'Generate weekly report' })
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
          provenance: {
            ...previewResponse().preview.provenance,
            task_ids: ['T-0001'],
            sources: [{ kind: 'review.weekly.project', task_id: 'T-0001', dates: ['2026-08-30'] }],
            coverage: {
              record_dates: ['2026-08-30'],
              no_record_dates: WEEK.filter((day) => day !== '2026-08-30'),
            },
          },
        },
      }),
    }))
  })
  expect(screen.queryByRole('heading', { name: 'Stale owner markdown' })).toBeNull()
  expect(screen.queryByText('No records for this week.')).toBeNull()

  await act(async () => {
    pending[1]?.(await jsonResponse({ data: previewResponse() }))
  })
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  expect(screen.queryByRole('heading', { name: 'Stale owner markdown' })).toBeNull()
})

test('oversized markdown is refused and never shown as a report', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
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
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeNull()
  expect(screen.queryByText('No records for this week.')).toBeNull()
})

test('downloads the original markdown bytes as workstack-weekly-START-to-END.md', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  let filename = ''
  let blobParts: BlobPart[] | undefined
  const RealBlob = globalThis.Blob
  vi.stubGlobal('Blob', class extends RealBlob {
    constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
      super(parts, options)
      blobParts = parts
    }
  })
  const createObjectURL = vi.fn(() => 'blob:weekly')
  const revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await screen.findByText('No records for this week.')
  await userEvent.click(screen.getByRole('button', { name: 'Download .md' }))
  expect(blobParts).toEqual([MARKDOWN])
  expect(createObjectURL).toHaveBeenCalled()
  expect(filename).toBe('workstack-weekly-2026-08-24-to-2026-08-30.md')
})

test('a deferred copy does not stamp copied or failure onto a newer preview', async () => {
  let finishCopy: ((ok: boolean) => void) | undefined
  const writeText = vi.fn(() => new Promise<void>((resolve, reject) => {
    finishCopy = (ok) => (ok ? resolve() : reject(new Error('denied')))
  }))
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: () => false })
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: previewResponse() })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await screen.findByText('No records for this week.')
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  expect(writeText).toHaveBeenCalledWith(MARKDOWN)

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
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
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={12} workspaceId={UID} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  await act(async () => {
    finishFail?.(false)
  })
  expect(screen.queryByText('Markdown could not be copied.')).toBeNull()
})

test('a same-batch owner swap refuses copy and download of the stale document', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
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
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const createObjectURL = vi.fn(() => 'blob:weekly')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  renderPreview(fetchMock, { workspaceId: UID }, client)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  await screen.findByText('No records for this week.')
  const copy = screen.getByRole('button', { name: 'Copy Markdown' })
  const download = screen.getByRole('button', { name: 'Download .md' })
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(uidB))
    client.setQueryData(['workspace'], workspaceFor(UID))
    copy.click()
    download.click()
  })
  expect(writeText).not.toHaveBeenCalled()
  expect(createObjectURL).not.toHaveBeenCalled()
  expect(screen.queryByText('No records for this week.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Copy Markdown' })).toBeNull()
})

const GENERATED_AT = '2026-09-06T01:02:03Z'
const RECORDED_MARKDOWN = [
  '# Weekly review 2026-08-24 → 2026-08-30',
  '',
  '## Projects',
  '',
  '- Ship the review aid',
  '',
].join('\n')

function contextItem(
  overrides: Partial<DailyReportContextCatalog['items'][number]> = {},
): DailyReportContextCatalog['items'][number] {
  return {
    capture_id: 'C-0001',
    capture_revision: 1,
    title: 'Synthetic context',
    linked_task_ids: ['T-0001'],
    status: 'linked',
    ...overrides,
  }
}

function contextCatalog(
  overrides: Partial<DailyReportContextCatalog> = {},
): DailyReportContextCatalog {
  return {
    captured_at: GENERATED_AT,
    items: [contextItem()],
    omitted_count: 0,
    ...overrides,
  }
}

/**
 * A recorded week (absence null) whose provenance names `taskIds`, optionally
 * carrying the R45 catalogue sibling. Built from the existing previewResponse
 * fixture so only the catalogue differs from the pre-R45 weekly payload.
 */
function recordedResponse(
  catalog?: DailyReportContextCatalog,
  taskIds: string[] = ['T-0001', 'T-0002'],
): WeeklyReportPreviewResponse {
  const base = previewResponse()
  const response: WeeklyReportPreviewResponse = {
    ...base,
    preview: {
      ...base.preview,
      absence: null,
      markdown: RECORDED_MARKDOWN,
      provenance: {
        ...base.preview.provenance,
        task_ids: taskIds,
        sources: taskIds.map((taskId) => ({
          kind: 'review.weekly.project' as const,
          task_id: taskId,
          dates: ['2026-08-30'],
        })),
        coverage: {
          record_dates: ['2026-08-30'],
          no_record_dates: WEEK.filter((day) => day !== '2026-08-30'),
        },
      },
    },
  }
  return catalog ? { ...response, context_catalog: catalog } : response
}

function respondWith(body: unknown) {
  return vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: body })
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
}

async function generateWeekly() {
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  return screen.findByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })
}

function contextPanel() {
  return screen.getByRole('region', { name: 'Related task context' })
}

test('shows the related task context beside an admitted weekly preview without any action', async () => {
  const fetchMock = respondWith(recordedResponse(contextCatalog({
    items: [
      contextItem(),
      contextItem({
        capture_id: 'C-0002',
        title: 'Second context',
        linked_task_ids: ['T-0001', 'T-0002'],
        status: 'dismissed',
      }),
    ],
  })))
  renderPreview(fetchMock)
  await generateWeekly()
  const panel = contextPanel()
  expect(panel).toBeVisible()
  expect(within(panel).getByText(
    "A snapshot of context linked to this report's tasks when the preview was generated."
    + ' It is not included in the report and does not verify individual claims.',
  )).toBeVisible()
  expect(within(panel).getByText('C-0001')).toBeVisible()
  expect(within(panel).getByText('Synthetic context')).toBeVisible()
  expect(within(panel).getByText('linked')).toBeVisible()
  expect(within(panel).getByText('dismissed')).toBeVisible()
  expect(within(panel).getByText('Linked tasks: T-0001, T-0002')).toBeVisible()
  expect(within(panel).queryAllByRole('button')).toHaveLength(0)
  expect(within(panel).queryAllByRole('link')).toHaveLength(0)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('renders a pre-R45 weekly response with no context panel at all', async () => {
  const fetchMock = respondWith(recordedResponse())
  renderPreview(fetchMock)
  await generateWeekly()
  expect(screen.queryByRole('region', { name: 'Related task context' })).toBeNull()
  expect(screen.queryByText('Related task context')).toBeNull()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeVisible()
})

test('states an empty catalogue and the omitted count without hiding the weekly report', async () => {
  const empty = respondWith(recordedResponse(contextCatalog({ items: [] })))
  const { view } = renderPreview(empty)
  await generateWeekly()
  expect(within(contextPanel()).getByText('No context is linked to these tasks.')).toBeVisible()
  view.unmount()

  const truncated = respondWith(recordedResponse(contextCatalog({
    items: Array.from({ length: 32 }, (_, index) => contextItem({
      capture_id: `C-${String(index + 1).padStart(4, '0')}`,
    })),
    omitted_count: 4,
  })))
  renderPreview(truncated)
  await generateWeekly()
  expect(within(contextPanel()).getByText(
    '4 more linked captures are not shown here. Review Task context for the full list.',
  )).toBeVisible()
  expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Download .md' })).toBeVisible()
})

test('renders a catalogue title as inert text rather than markup', async () => {
  const hostile = '<img src=x onerror=alert(1)> [open](https://example.invalid) **bold**'
  const fetchMock = respondWith(recordedResponse(contextCatalog({
    items: [contextItem({ title: hostile })],
  })))
  const { view } = renderPreview(fetchMock)
  await generateWeekly()
  const panel = contextPanel()
  expect(within(panel).getByText(hostile)).toBeVisible()
  expect(panel.querySelector('img')).toBeNull()
  expect(panel.querySelector('a')).toBeNull()
  expect(panel.querySelector('strong')?.textContent).toBe('C-0001')
  expect(view.container.querySelectorAll('input')).toHaveLength(0)
})

test('refuses a malformed weekly catalogue instead of showing a partial panel', async () => {
  const fetchMock = respondWith({
    ...recordedResponse(contextCatalog()),
    context_catalog: {
      ...contextCatalog(),
      items: [{ ...contextItem(), source_url: 'https://example.invalid/leak' }],
    },
  })
  renderPreview(fetchMock)
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Related task context' })).toBeNull()
  expect(screen.queryByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeNull()
  expect(screen.queryByText('https://example.invalid/leak')).toBeNull()
  expect(screen.queryByText('Synthetic context')).toBeNull()
})

test('drops the previous week catalogue when the owned weekly preview is invalidated', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/reports/weekly-preview?end_date=2026-08-29')) {
      return jsonResponse({ data: previewResponse({
        preview: {
          ...previewResponse().preview,
          period: { kind: 'week', start: '2026-08-23', end: '2026-08-29', days: 7 },
          provenance: {
            ...previewResponse().preview.provenance,
            range: { start: '2026-08-23', end: '2026-08-29', days: 7 },
            coverage: {
              record_dates: [],
              no_record_dates: WEEK.map((day) => shiftIsoDate(day, -1) as string),
            },
          },
        },
      }) })
    }
    if (url.startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({ data: recordedResponse(contextCatalog()) })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  const { queryClient, view } = renderPreview(fetchMock, { sourceUpdatedAt: 10 })
  await generateWeekly()
  expect(contextPanel()).toBeVisible()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate={END} sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  expect(screen.queryByRole('region', { name: 'Related task context' })).toBeNull()
  expect(screen.queryByText('Synthetic context')).toBeNull()

  view.rerender(
    <QueryClientProvider client={queryClient}>
      <WeeklyReportPreview endDate="2026-08-29" sourceUpdatedAt={11} workspaceId={UID} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Generate weekly report' }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Related task context' })).toBeNull()
  expect(screen.queryByText('Synthetic context')).toBeNull()
})

test('copy and download still carry exactly the weekly markdown when a catalogue is present', async () => {
  const fetchMock = respondWith(recordedResponse(contextCatalog()))
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  let filename = ''
  let blobParts: BlobPart[] | undefined
  const RealBlob = globalThis.Blob
  vi.stubGlobal('Blob', class extends RealBlob {
    constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
      super(parts, options)
      blobParts = parts
    }
  })
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:weekly'), revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download
  })
  renderPreview(fetchMock)
  await generateWeekly()
  expect(contextPanel()).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }))
  expect(writeText).toHaveBeenCalledWith(RECORDED_MARKDOWN)
  await userEvent.click(screen.getByRole('button', { name: 'Download .md' }))
  expect(blobParts).toEqual([RECORDED_MARKDOWN])
  expect(filename).toBe('workstack-weekly-2026-08-24-to-2026-08-30.md')
})
