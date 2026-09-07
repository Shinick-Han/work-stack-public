import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { WeeklyReportPreview } from './WeeklyReportPreview'
import { jsonResponse, workspace } from '../../test/fixtures'
import type { ReviewProjection } from '../../domain/types'
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
