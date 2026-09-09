import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { ReviewProjection } from '../../domain/types'
import { task, workspace } from '../../test/fixtures'
import { DailyReviewPage, type DailyReviewPageProps } from './DailyReviewPage'
import { REVIEW_COPY, strandedIdentityLabel } from './DailyReviewPageModel'

function jsonResponse(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}

function emptySavedReportsList(url: string) {
  if (!url.startsWith('/api/v1/reports?')) return null
  const workspaceUid = new URL(url, 'https://workstack.local').searchParams.get('workspace_uid') ?? ''
  return jsonResponse({
    data: { workspace_uid: workspaceUid, reports: [], omitted_count: 0, cursor: null },
  })
}

/**
 * The Recent changes panel mounts with the page, so its one bounded list request is
 * answered here the same way the saved-reports list already is. It stays empty: this file
 * asserts the review surface, and the panel's own behaviour has its own tests.
 */
function emptyMutationNotices(url: string) {
  if (!url.startsWith('/api/v1/mutation-notices')) return null
  return jsonResponse({ data: { items: [], next_cursor: null } })
}

const emptyReview: ReviewProjection = {
  day: { date: '2026-08-30', start_time: null, entries: [] },
  weekly: {
    range: { start: '2026-08-24', end: '2026-08-30', days: 7 },
    objectives: [],
    projects: [],
  },
}

function renderPage(
  fetchMock: ReturnType<typeof vi.fn>,
  onNotice = vi.fn(),
  extra: Partial<DailyReviewPageProps> = {},
) {
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const onOpenTask = extra.onOpenTask ?? vi.fn()
  const view = render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={onNotice}
        onOpenTask={onOpenTask}
        today="2026-08-30"
        workspace={workspace}
        {...extra}
      />
    </QueryClientProvider>,
  )
  return { onNotice, onOpenTask, client, unmount: view.unmount }
}

test('review calendar changes the selected date query and rejects impossible or future drafts', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    if (String(input).startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    const saved = emptySavedReportsList(String(input))
    if (saved) return saved
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  renderPage(fetchMock)
  await screen.findByText('Not yet')
  const date = screen.getByRole('textbox', { name: 'Review date' })
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(screen.getByRole('button', { name: 'August 31, 2026' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'August 29, 2026' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('2026-08-29'))).toBe(true))
  const count = fetchMock.mock.calls.length
  fireEvent.change(date, { target: { value: '2026-02-30' } })
  fireEvent.change(date, { target: { value: '2026-09-01' } })
  expect(date).toHaveAttribute('aria-invalid', 'true')
  expect(fetchMock).toHaveBeenCalledTimes(count)
  await userEvent.click(screen.getByRole('button', { name: 'Clear date' }))
  expect(date).toHaveValue('')
  expect(fetchMock.mock.calls.every(([, init]) => !init || (init as RequestInit).method !== 'POST')).toBe(true)
})

test('adds one idempotent Done/Next/Blocker entry and refreshes the day', async () => {
  let review: any = emptyReview
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: review })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: workspace.workspace.id, entries: [] } })
    }
    if (url === '/api/v1/review/entries' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      const entry = { task_id: task.id, task: task.title, done: body.done, next: body.next, blockers: body.blockers }
      review = { ...review, day: { ...review.day, entries: [entry] } }
      return jsonResponse({ data: { date: body.date, ...entry }, meta: { replayed: false } }, 201)
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  const { onNotice } = renderPage(fetchMock, vi.fn(), { initialTaskId: task.id })

  const doneInput = await screen.findByLabelText(REVIEW_COPY.changed)
  await userEvent.type(doneInput, 'Closed the release gate')
  await userEvent.type(screen.getByLabelText(REVIEW_COPY.next), 'Open Daily Review')
  await userEvent.type(screen.getByLabelText(REVIEW_COPY.blockers), 'Waiting on dogfood')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))

  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Daily review entry added'))
  const mutation = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/review/entries')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
    date: '2026-08-30',
    task_id: task.id,
    done: ['Closed the release gate'],
    next: ['Open Daily Review'],
    blockers: ['Waiting on dogfood'],
  })
  expect(await screen.findAllByText('Closed the release gate')).not.toHaveLength(0)
})

test('checks in with an idempotent local time intent', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url === '/api/v1/review/checkin' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      return jsonResponse({ data: { date: body.date, start_time: body.time }, meta: { replayed: false } }, 201)
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  const { onNotice } = renderPage(fetchMock)

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: 'Check in now' }))

  await waitFor(() => expect(onNotice).toHaveBeenCalledWith(expect.stringMatching(/^Checked in at \d{2}:\d{2}$/)))
  const mutation = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/review/checkin')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
})

test('shows reviewed work-session duration in the day record and weekly roll-up', async () => {
  const review = {
    day: {
      date: '2026-08-30',
      start_time: null,
      entries: [{
        task_id: task.id,
        task: task.title,
        done: ['Focused result'],
        next: [],
        blockers: [],
        session_id: 'WS-000001',
        duration_seconds: 3900,
      }],
    },
    weekly: {
      range: { start: '2026-08-24', end: '2026-08-30', days: 7 },
      objectives: [],
      projects: [{
        task_id: task.id,
        task: task.title,
        done: ['Focused result'],
        next: [],
        blockers: [],
        objective_ids: [],
        dates: ['2026-08-30'],
        duration_seconds: 3900,
      }],
    },
  }
  renderPage(vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/review?')) return jsonResponse({ data: review })
    const saved = emptySavedReportsList(String(input))
    if (saved) return saved
    throw new Error(`Unexpected request: ${String(input)}`)
  }))

  expect(await screen.findByText('1h 5m focused · WS-000001')).toBeVisible()
  expect(screen.getByText('1h 5m focused · 1 active day')).toBeVisible()
})

test('a workspace that returns to A does not resurrect the old staged intent', async () => {
  const user = userEvent.setup()
  const CP = `CP-${'a'.repeat(64)}`
  const DIGEST = `sha256:${'b'.repeat(64)}`
  // The shared fixture UID is not RFC-conformant, so this case uses real ones.
  const uidA = '22222222-2222-4222-8222-222222222222'
  const uidB = '00000000-0000-4000-8000-0000000000b0'
  const auditFor = (uid: string) => ({
    workspace_uid: uid,
    entries: [{
      locator: { workspace_uid: uid, task_id: 'T-0001', date: '2026-08-30', ordinal: 0, entry_digest: DIGEST },
      checkpoint_id: CP,
      entry: { done: ['shipped'] },
      recorded: {
        type: 'worklog.recorded',
        workspace_uid: uid,
        task_id: 'T-0001',
        checkpoint_id: CP,
        date: '2026-08-30',
        ordinal: 0,
        entry_digest: DIGEST,
        origin: 'agent-cli-v1',
      },
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  })

  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: auditFor(url.includes('x') ? uidB : uidA) })
    }
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const page = (uid: string) => (
    <DailyReviewPage
      onNotice={vi.fn()}
      onOpenTask={vi.fn()}
      today="2026-08-30"
      workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
    />
  )
  const view = render(<QueryClientProvider client={client}>{page(uidA)}</QueryClientProvider>)

  await user.click(await screen.findByRole('button', { name: `Supersede ${CP}` }))
  await user.type(screen.getByLabelText('Explanation'), 'staged in A')

  // A -> B -> A: the coordinate returns, the interaction generation does not.
  view.rerender(<QueryClientProvider client={client}>{page(uidB)}</QueryClientProvider>)
  view.rerender(<QueryClientProvider client={client}>{page(uidA)}</QueryClientProvider>)

  await waitFor(() => expect(screen.queryByLabelText('Explanation')).toBeNull())
})

test('a cache owner change in the same batch refuses the staged Confirm before rerender', async () => {
  const user = userEvent.setup()
  const CP = `CP-${'a'.repeat(64)}`
  const DIGEST = `sha256:${'b'.repeat(64)}`
  const uidA = '22222222-2222-4222-8222-222222222222'
  const uidB = '00000000-0000-4000-8000-0000000000b0'
  const auditFor = (uid: string) => ({
    workspace_uid: uid,
    entries: [{
      locator: { workspace_uid: uid, task_id: 'T-0001', date: '2026-08-30', ordinal: 0, entry_digest: DIGEST },
      checkpoint_id: CP,
      entry: { done: ['shipped'] },
      recorded: {
        type: 'worklog.recorded',
        workspace_uid: uid,
        task_id: 'T-0001',
        checkpoint_id: CP,
        date: '2026-08-30',
        ordinal: 0,
        entry_digest: DIGEST,
        origin: 'agent-cli-v1',
      },
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  })

  let transitions = 0
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      transitions += 1
      return jsonResponse({ data: {}, meta: { replayed: false } }, 201)
    }
    if (url.endsWith('/api/v1/review/checkpoints')) return jsonResponse({ data: auditFor(uidA) })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const workspaceFor = (uid: string) => ({
    ...workspace,
    workspace: { ...workspace.workspace, id: uid },
  })
  client.setQueryData(['workspace'], workspaceFor(uidA))
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={workspaceFor(uidA)}
      />
    </QueryClientProvider>,
  )

  await user.click(await screen.findByRole('button', { name: `Supersede ${CP}` }))
  await user.type(screen.getByLabelText('Explanation'), 'staged in A')

  const confirm = screen.getByRole('button', { name: 'Confirm supersede' })
  // One React batch: ownership leaves and returns, then the stale control fires
  // before any rerender could hide it.
  await act(async () => {
    client.setQueryData(['workspace'], workspaceFor(uidB))
    client.setQueryData(['workspace'], workspaceFor(uidA))
    confirm.click()
  })

  expect(transitions).toBe(0)
})

test('a same-date review day cache change keeps the unknown-outcome retry and frozen explanation', async () => {
  const user = userEvent.setup()
  const CP = `CP-${'a'.repeat(64)}`
  const DIGEST = `sha256:${'b'.repeat(64)}`
  const uid = '22222222-2222-4222-8222-222222222222'
  const explanation = 'keep this explanation'
  const audit = {
    workspace_uid: uid,
    entries: [{
      locator: { workspace_uid: uid, task_id: 'T-0001', date: '2026-08-30', ordinal: 0, entry_digest: DIGEST },
      checkpoint_id: CP,
      entry: { done: ['shipped'] },
      recorded: {
        type: 'worklog.recorded',
        workspace_uid: uid,
        task_id: 'T-0001',
        checkpoint_id: CP,
        date: '2026-08-30',
        ordinal: 0,
        entry_digest: DIGEST,
        origin: 'agent-cli-v1',
      },
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  }
  const transitionKeys: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      transitionKeys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      return jsonResponse({ data: { unexpected: 'shape' } }, 201)
    }
    if (url.endsWith('/api/v1/review/checkpoints')) return jsonResponse({ data: audit })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await user.click(await screen.findByRole('button', { name: `Supersede ${CP}` }))
  await user.type(screen.getByLabelText('Explanation'), explanation)
  await user.click(screen.getByRole('button', { name: 'Confirm supersede' }))
  expect(await screen.findByRole('button', { name: 'Retry the same request' })).toBeVisible()
  expect(screen.getByLabelText('Frozen explanation')).toHaveValue(explanation)
  expect(transitionKeys).toHaveLength(1)
  const frozenKey = transitionKeys[0]
  expect(frozenKey).toMatch(/^workstack:/)

  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], {
      ...emptyReview,
      day: {
        date: '2026-08-30',
        start_time: '09:20',
        entries: [{
          task_id: 'T-0001',
          task: 'Day record',
          done: ['logged'],
          next: [],
          blockers: [],
        }],
      },
    })
  })

  expect(screen.getByRole('button', { name: 'Retry the same request' })).toBeVisible()
  expect(screen.getByLabelText('Frozen explanation')).toHaveValue(explanation)
  await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
  await waitFor(() => expect(transitionKeys).toHaveLength(2))
  expect(transitionKeys[1]).toBe(frozenKey)
})

test('generating a report then checking in clears the stale preview without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  let review: ReviewProjection = emptyReview
  let previewCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: review })
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      previewCalls += 1
      return jsonResponse({
        data: {
          workspace_uid: uid,
          source_digest: `sha256:${'a'.repeat(64)}`,
          preview: {
            template: 'daily-v1',
            period: { kind: 'day', date: '2026-08-30' },
            generated_at: '2026-09-06T01:02:03Z',
            absence: 'no records',
            provenance: {
              date: '2026-08-30',
              task_ids: [],
              sources: [],
              weekly_range: { start: '2026-08-30', end: '2026-08-30', days: 1 },
              ignored_keys: [],
            },
            markdown: '# Daily review 2026-08-30\n\nNo records.\n',
          },
        },
      })
    }
    if (url === '/api/v1/review/checkin' && init?.method === 'POST') {
      review = {
        ...review,
        day: { ...review.day, start_time: '09:20' },
      }
      return jsonResponse({ data: { date: '2026-08-30', start_time: '09:20' }, meta: { replayed: false } }, 201)
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Check in now' }))
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
  expect(previewCalls).toBe(1)
})

test('an unchanged review refetch keeps the generated preview without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  let previewCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      previewCalls += 1
      return jsonResponse({
        data: {
          workspace_uid: uid,
          source_digest: `sha256:${'a'.repeat(64)}`,
          preview: {
            template: 'daily-v1',
            period: { kind: 'day', date: '2026-08-30' },
            generated_at: '2026-09-06T01:02:03Z',
            absence: 'no records',
            provenance: {
              date: '2026-08-30',
              task_ids: [],
              sources: [],
              weekly_range: { start: '2026-08-30', end: '2026-08-30', days: 1 },
              ignored_keys: [],
            },
            markdown: '# Daily review 2026-08-30\n\nNo records.\n',
          },
        },
      })
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  const dataUpdatedAt = client.getQueryState(['review', '2026-08-30', 7])?.dataUpdatedAt ?? 0
  await act(async () => {
    await client.invalidateQueries({ queryKey: ['review', '2026-08-30', 7] })
  })
  await waitFor(() => {
    expect(client.getQueryState(['review', '2026-08-30', 7])?.dataUpdatedAt ?? 0).toBeGreaterThan(dataUpdatedAt)
  })
  expect(screen.getByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  expect(previewCalls).toBe(1)
})

function dailyPreviewEnvelope(uid: string) {
  return {
    workspace_uid: uid,
    source_digest: `sha256:${'a'.repeat(64)}`,
    preview: {
      template: 'daily-v1',
      period: { kind: 'day', date: '2026-08-30' },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        date: '2026-08-30',
        task_ids: [],
        sources: [],
        weekly_range: { start: '2026-08-30', end: '2026-08-30', days: 1 },
        ignored_keys: [],
      },
      markdown: '# Daily review 2026-08-30\n\nNo records.\n',
    },
  }
}

const WEEK_DATES = [
  '2026-08-24',
  '2026-08-25',
  '2026-08-26',
  '2026-08-27',
  '2026-08-28',
  '2026-08-29',
  '2026-08-30',
]

function weeklyPreviewEnvelope(uid: string) {
  return {
    workspace_uid: uid,
    source_digest: `sha256:${'b'.repeat(64)}`,
    preview: {
      template: 'weekly-v1',
      period: { kind: 'week', start: '2026-08-24', end: '2026-08-30', days: 7 },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        range: { start: '2026-08-24', end: '2026-08-30', days: 7 },
        task_ids: [],
        sources: [],
        coverage: { record_dates: [], no_record_dates: WEEK_DATES },
        ignored_keys: [],
      },
      markdown: '# Weekly review 2026-08-24 → 2026-08-30\n\nNo records.\n',
    },
  }
}

function weeklyOnlyReview(review: ReviewProjection): ReviewProjection {
  return {
    ...review,
    weekly: {
      ...review.weekly,
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
  }
}

function dayOnlyReview(review: ReviewProjection): ReviewProjection {
  return {
    ...review,
    day: {
      date: '2026-08-30',
      start_time: null,
      entries: [{
        task_id: 'T-0001',
        task: 'Day record',
        done: ['logged'],
        next: [],
        blockers: [],
      }],
    },
  }
}

test('a weekly-only review cache change keeps the generated preview without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  let previewCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      previewCalls += 1
      return jsonResponse({ data: dailyPreviewEnvelope(uid) })
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], {
      ...emptyReview,
      weekly: {
        ...emptyReview.weekly,
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
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  expect(previewCalls).toBe(1)
})

test('a changed review day clears the generated preview without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  let previewCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      previewCalls += 1
      return jsonResponse({ data: dailyPreviewEnvelope(uid) })
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], {
      ...emptyReview,
      day: {
        date: '2026-08-30',
        start_time: null,
        entries: [{
          task_id: 'T-0001',
          task: 'Day record',
          done: ['logged'],
          next: [],
          blockers: [],
        }],
      },
    })
  })
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
  expect(screen.queryByRole('button', { name: 'Edit local draft' })).toBeNull()
  expect(previewCalls).toBe(1)
})

test('an unsaved draft survives review refetch error and date-query loading', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const markdown = '# Daily review 2026-08-30\n\nNo records.\n'
  let reviewMode: 'ok' | 'fail' = 'ok'
  let previewCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: uid, entries: [] } })
    }
    if (url.startsWith('/api/v1/review?')) {
      if (url.includes('date=2026-08-29')) {
        return new Promise<Response>(() => undefined)
      }
      if (reviewMode === 'fail') {
        return jsonResponse({ error: { code: 'unavailable', message: 'Review source unavailable.' } }, 503)
      }
      return jsonResponse({ data: emptyReview })
    }
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      previewCalls += 1
      return jsonResponse({
        data: {
          workspace_uid: uid,
          source_digest: `sha256:${'a'.repeat(64)}`,
          preview: {
            template: 'daily-v1',
            period: { kind: 'day', date: '2026-08-30' },
            generated_at: '2026-09-06T01:02:03Z',
            absence: 'no records',
            provenance: {
              date: '2026-08-30',
              task_ids: [],
              sources: [],
              weekly_range: { start: '2026-08-30', end: '2026-08-30', days: 1 },
              ignored_keys: [],
            },
            markdown,
          },
        },
      })
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  window.localStorage.clear()
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )

  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Edit local draft' }))
  await waitFor(() => expect(screen.queryByText('Opening the saved draft…')).toBeNull())
  const editor = await screen.findByLabelText('Report markdown')
  expect(editor).toHaveValue(markdown)
  await userEvent.type(editor, ' keep')
  const unsaved = `${markdown} keep`
  expect(editor).toHaveValue(unsaved)

  reviewMode = 'fail'
  await act(async () => {
    await client.invalidateQueries({ queryKey: ['review', '2026-08-30', 7] })
  })
  expect(await screen.findByText('Review source unavailable.')).toBeVisible()
  expect(screen.queryByRole('button', { name: /^Generate report$/ })).toBeNull()
  expect(screen.getByLabelText('Report markdown')).toHaveValue(unsaved)
  expect(screen.getByRole('dialog', { name: 'Edit local report draft' })).toBeVisible()

  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'August 29, 2026' }))
  expect(await screen.findByText('Opening the review…')).toBeVisible()
  expect(screen.getByLabelText('Report markdown')).toHaveValue(unsaved)
  expect(previewCalls).toBe(1)

  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeVisible()
  expect(screen.getByLabelText('Report markdown')).toHaveValue(unsaved)
})

function renderReview(fetchMock: ReturnType<typeof vi.fn>, uid: string, client?: QueryClient) {
  vi.stubGlobal('fetch', fetchMock)
  const queryClient = client ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <DailyReviewPage
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={{ ...workspace, workspace: { ...workspace.workspace, id: uid } }}
      />
    </QueryClientProvider>,
  )
  return queryClient
}

function dualPreviewFetch(uid: string, review: { current: ReviewProjection }) {
  let dailyCalls = 0
  let weeklyCalls = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: review.current })
    if (url.startsWith('/api/v1/reports/daily-preview?')) {
      dailyCalls += 1
      return jsonResponse({ data: dailyPreviewEnvelope(uid) })
    }
    if (url.startsWith('/api/v1/reports/weekly-preview?')) {
      weeklyCalls += 1
      return jsonResponse({ data: weeklyPreviewEnvelope(uid) })
    }
    if (url === '/api/v1/review/checkin' && init?.method === 'POST') {
      review.current = {
        ...review.current,
        day: { ...review.current.day, start_time: '09:20' },
      }
      return jsonResponse({ data: { date: '2026-08-30', start_time: '09:20' }, meta: { replayed: false } }, 201)
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  return {
    fetchMock,
    dailyCalls: () => dailyCalls,
    weeklyCalls: () => weeklyCalls,
  }
}

async function generateBothReports() {
  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: /^Generate weekly report$/ }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
}

test('a weekly-only review cache change keeps the unknown-outcome retry and frozen explanation', async () => {
  const user = userEvent.setup()
  const CP = `CP-${'a'.repeat(64)}`
  const DIGEST = `sha256:${'b'.repeat(64)}`
  const uid = '22222222-2222-4222-8222-222222222222'
  const explanation = 'keep this weekly-untouched explanation'
  const audit = {
    workspace_uid: uid,
    entries: [{
      locator: { workspace_uid: uid, task_id: 'T-0001', date: '2026-08-30', ordinal: 0, entry_digest: DIGEST },
      checkpoint_id: CP,
      entry: { done: ['shipped'] },
      recorded: {
        type: 'worklog.recorded',
        workspace_uid: uid,
        task_id: 'T-0001',
        checkpoint_id: CP,
        date: '2026-08-30',
        ordinal: 0,
        entry_digest: DIGEST,
        origin: 'agent-cli-v1',
      },
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  }
  const transitionKeys: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/transitions')) {
      transitionKeys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      return jsonResponse({ data: { unexpected: 'shape' } }, 201)
    }
    if (url.endsWith('/api/v1/review/checkpoints')) return jsonResponse({ data: audit })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  renderReview(fetchMock, uid, client)

  await user.click(await screen.findByRole('button', { name: `Supersede ${CP}` }))
  await user.type(screen.getByLabelText('Explanation'), explanation)
  await user.click(screen.getByRole('button', { name: 'Confirm supersede' }))
  expect(await screen.findByRole('button', { name: 'Retry the same request' })).toBeVisible()
  const frozenKey = transitionKeys[0]

  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], weeklyOnlyReview(emptyReview))
  })

  expect(screen.getByRole('button', { name: 'Retry the same request' })).toBeVisible()
  expect(screen.getByLabelText('Frozen explanation')).toHaveValue(explanation)
  await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
  await waitFor(() => expect(transitionKeys).toHaveLength(2))
  expect(transitionKeys[1]).toBe(frozenKey)
})

test('generates a weekly report only after an explicit click and never dumps technical ids', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const review = { current: emptyReview }
  const { fetchMock, weeklyCalls } = dualPreviewFetch(uid, review)
  renderReview(fetchMock, uid)
  await screen.findByText('Not yet')
  expect(weeklyCalls()).toBe(0)
  expect(screen.getByRole('button', { name: /^Generate weekly report$/ })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: /^Generate weekly report$/ }))
  expect(await screen.findByText('No records for this week.')).toBeVisible()
  expect(screen.getByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeVisible()
  expect(screen.queryByText(uid)).toBeNull()
  expect(screen.queryByText(`sha256:${'b'.repeat(64)}`)).toBeNull()
  expect(screen.queryByText('review.weekly.project')).toBeNull()
  expect(weeklyCalls()).toBe(1)
  expect(String(fetchMock.mock.calls.find(([input]) => String(input).includes('weekly-preview'))?.[0])).toBe(
    `/api/v1/reports/weekly-preview?end_date=2026-08-30&template=weekly-v1&workspace_uid=${uid}`,
  )
})

test('a weekly-only review cache change clears weekly and keeps daily without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const review = { current: emptyReview }
  const { fetchMock, dailyCalls, weeklyCalls } = dualPreviewFetch(uid, review)
  const client = renderReview(fetchMock, uid)
  await generateBothReports()
  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], weeklyOnlyReview(emptyReview))
  })
  expect(screen.getByText('No records for this day.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  await waitFor(() => expect(screen.queryByText('No records for this week.')).toBeNull())
  expect(screen.getByRole('button', { name: /^Generate weekly report$/ })).toBeVisible()
  expect(dailyCalls()).toBe(1)
  expect(weeklyCalls()).toBe(1)
})

test('a day-only review cache change and check-in clear daily and keep weekly without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const review = { current: emptyReview }
  const { fetchMock, dailyCalls, weeklyCalls } = dualPreviewFetch(uid, review)
  const client = renderReview(fetchMock, uid)
  await generateBothReports()
  await act(async () => {
    client.setQueryData(['review', '2026-08-30', 7], dayOnlyReview(emptyReview))
  })
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
  expect(screen.getByText('No records for this week.')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Edit local draft' })).toBeNull()
  expect(dailyCalls()).toBe(1)
  expect(weeklyCalls()).toBe(1)

  await userEvent.click(screen.getByRole('button', { name: /^Generate report$/ }))
  expect(await screen.findByText('No records for this day.')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Check in now' }))
  await waitFor(() => expect(screen.queryByText('No records for this day.')).toBeNull())
  expect(screen.getByText('No records for this week.')).toBeVisible()
  expect(dailyCalls()).toBe(2)
  expect(weeklyCalls()).toBe(1)
})

test('an unchanged review refetch keeps both generated previews without autogenerating', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const review = { current: emptyReview }
  const { fetchMock, dailyCalls, weeklyCalls } = dualPreviewFetch(uid, review)
  const client = renderReview(fetchMock, uid)
  await generateBothReports()
  const dataUpdatedAt = client.getQueryState(['review', '2026-08-30', 7])?.dataUpdatedAt ?? 0
  await act(async () => {
    await client.invalidateQueries({ queryKey: ['review', '2026-08-30', 7] })
  })
  await waitFor(() => {
    expect(client.getQueryState(['review', '2026-08-30', 7])?.dataUpdatedAt ?? 0).toBeGreaterThan(dataUpdatedAt)
  })
  expect(screen.getByText('No records for this day.')).toBeVisible()
  expect(screen.getByText('No records for this week.')).toBeVisible()
  expect(within(screen.getByRole('region', { name: 'Daily report' })).getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  expect(dailyCalls()).toBe(1)
  expect(weeklyCalls()).toBe(1)
})

test('refuses a malformed weekly preview wire without showing markdown', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.startsWith('/api/v1/reports/weekly-preview?')) {
      return jsonResponse({
        data: {
          ...weeklyPreviewEnvelope(uid),
          workspace_uid: '00000000-0000-4000-8000-0000000000b0',
        },
      })
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  renderReview(fetchMock, uid)
  await screen.findByText('Not yet')
  await userEvent.click(screen.getByRole('button', { name: /^Generate weekly report$/ }))
  expect(await within(screen.getByRole('region', { name: 'Weekly report' })).findByRole('alert')).toBeVisible()
  expect(screen.queryByRole('heading', { level: 1, name: 'Weekly review 2026-08-24 → 2026-08-30' })).toBeNull()
  expect(screen.queryByText('No records for this week.')).toBeNull()
})

const longTitle = 'Host-side search worker isolation for Knowledge retrieval across Local Markdown and future remote sources'
const otherTask = {
  ...task,
  id: 'T-0033',
  uid: '33333333-3333-4333-8333-333333333333',
  title: longTitle,
}
const twoTaskWorkspace = { ...workspace, tasks: [task, otherTask] }

function reviewSurfaceFetch(onPost?: (init?: RequestInit) => Response | Promise<Response>) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: workspace.workspace.id, entries: [] } })
    }
    if (url === '/api/v1/review/entries' && init?.method === 'POST' && onPost) return onPost(init)
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    const notices = emptyMutationNotices(url)
    if (notices) return notices
    throw new Error(`Unexpected request: ${url}`)
  })
}

test('independent Review requires an explicit Task and never writes the first id', async () => {
  const fetchMock = reviewSurfaceFetch()
  renderPage(fetchMock, vi.fn(), { workspace: twoTaskWorkspace })
  await screen.findByText('Not yet')
  expect(screen.getByLabelText('Task')).toHaveValue('')
  expect(screen.getByRole('button', { name: REVIEW_COPY.save })).toBeDisabled()
  expect(screen.getByText(REVIEW_COPY.missingTarget)).toBeVisible()
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toBeDisabled()
  expect(fetchMock.mock.calls.every(([url, init]) => (
    String(url) !== '/api/v1/review/entries' || (init as RequestInit | undefined)?.method !== 'POST'
  ))).toBe(true)
})

test('an unknown initialTaskId stays a selection-required state', async () => {
  renderPage(reviewSurfaceFetch(), vi.fn(), { initialTaskId: 'T-9999', workspace: twoTaskWorkspace })
  await screen.findByText('Not yet')
  expect(screen.getByLabelText('Task')).toHaveValue('')
  expect(screen.getByRole('alert')).toHaveTextContent(REVIEW_COPY.invalidTarget)
  expect(screen.queryByText(`Recording progress for ${task.id}`)).toBeNull()
})

test('Record progress binds the intended Task title and open callback', async () => {
  const fetchMock = reviewSurfaceFetch()
  const { onOpenTask } = renderPage(fetchMock, vi.fn(), {
    initialTaskId: otherTask.id,
    workspace: twoTaskWorkspace,
  })
  await screen.findByText('Not yet')
  expect(screen.getByText(`Recording progress for ${otherTask.id}`)).toBeVisible()
  expect(screen.getByText(longTitle)).toBeVisible()
  expect(screen.getByRole('heading', { name: REVIEW_COPY.recent })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: `Open ${otherTask.id} planning detail` }))
  expect(onOpenTask).toHaveBeenCalledWith(otherTask.id)
})

test('a parent initialTaskId change does not overwrite an in-progress draft', async () => {
  const fetchMock = reviewSurfaceFetch()
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        initialTaskId={task.id}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={twoTaskWorkspace}
      />
    </QueryClientProvider>,
  )
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Keep this draft')
  view.rerender(
    <QueryClientProvider client={client}>
      <DailyReviewPage
        initialTaskId={otherTask.id}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={twoTaskWorkspace}
      />
    </QueryClientProvider>,
  )
  expect(screen.getByLabelText('Task')).toHaveValue(task.id)
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('Keep this draft')
  expect(screen.getByText(`Recording progress for ${task.id}`)).toBeVisible()
})

test('changing Task with a draft keeps or discards through an explicit guard', async () => {
  renderPage(reviewSurfaceFetch(), vi.fn(), { initialTaskId: task.id, workspace: twoTaskWorkspace })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Unsaved line')
  await userEvent.selectOptions(screen.getByLabelText('Task'), otherTask.id)
  const prompt = screen.getByRole('alertdialog', { name: REVIEW_COPY.guardTitle })
  expect(prompt).toBeVisible()
  await userEvent.click(within(prompt).getByRole('button', { name: REVIEW_COPY.keepDraft }))
  expect(screen.queryByRole('alertdialog')).toBeNull()
  expect(screen.getByLabelText('Task')).toHaveValue(task.id)
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('Unsaved line')
  await userEvent.selectOptions(screen.getByLabelText('Task'), otherTask.id)
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discardDraft }))
  expect(screen.getByLabelText('Task')).toHaveValue(otherTask.id)
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('')
  expect(screen.getByText(`Recording progress for ${otherTask.id}`)).toBeVisible()
})

test('a refused save keeps the draft and retries the same idempotency key', async () => {
  const keys: string[] = []
  const fetchMock = reviewSurfaceFetch((init) => {
    keys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
    return jsonResponse({ error: { code: 'conflict', message: 'Review entry refused.' } }, 409)
  })
  const { onNotice } = renderPage(fetchMock, vi.fn(), { initialTaskId: task.id })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Review entry refused.', 'error'))
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.retryUnchanged }))
  await waitFor(() => expect(keys).toHaveLength(2))
  expect(keys[0]).toMatch(/^workstack:/)
  expect(keys[1]).toBe(keys[0])
})

test('commit_unknown retries only the frozen entry identity', async () => {
  const keys: string[] = []
  const fetchMock = reviewSurfaceFetch((init) => {
    keys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
    return jsonResponse({ data: { unexpected: 'shape' } }, 201)
  })
  renderPage(fetchMock, vi.fn(), { initialTaskId: task.id })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  expect(await screen.findByRole('button', { name: REVIEW_COPY.retrySame })).toBeVisible()
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.retrySame }))
  await waitFor(() => expect(keys).toHaveLength(2))
  expect(keys[1]).toBe(keys[0])
})

function acceptedEntryPost(init?: RequestInit) {
  const body = JSON.parse(String(init?.body))
  return jsonResponse({
    data: {
      date: body.date,
      task_id: body.task_id,
      task: task.title,
      done: body.done,
      next: body.next,
      blockers: body.blockers,
    },
    meta: { replayed: false },
  }, 201)
}

test('onNavigationLockChange tracks a dirty draft and clears on unmount', async () => {
  const onLock = vi.fn()
  const { unmount } = renderPage(reviewSurfaceFetch(), vi.fn(), {
    initialTaskId: task.id,
    onNavigationLockChange: onLock,
  })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Keep this draft')
  await waitFor(() => expect(onLock).toHaveBeenCalledWith(true))
  unmount()
  expect(onLock).toHaveBeenLastCalledWith(false)
})

test('replacing onNavigationLockChange while locked does not unlock', async () => {
  const first = vi.fn()
  const second = vi.fn()
  const fetchMock = reviewSurfaceFetch()
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = (onNavigationLockChange: (locked: boolean) => void) => (
    <QueryClientProvider client={client}>
      <DailyReviewPage
        initialTaskId={task.id}
        onNavigationLockChange={onNavigationLockChange}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={twoTaskWorkspace}
      />
    </QueryClientProvider>
  )
  const view = render(tree(first))
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Keep this draft')
  await waitFor(() => expect(first).toHaveBeenCalledWith(true))
  const firstCalls = first.mock.calls.map((call) => call[0])
  view.rerender(tree(second))
  expect(first.mock.calls.map((call) => call[0])).toEqual(firstCalls)
  expect(second).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discard }))
  await waitFor(() => expect(second).toHaveBeenCalledWith(false))
  expect(first.mock.calls.filter((call) => call[0] === false)).toHaveLength(
    firstCalls.filter((value) => value === false).length,
  )
})

test('Discard draft clears ordinary unsaved text and unlocks navigation', async () => {
  const onLock = vi.fn()
  renderPage(reviewSurfaceFetch(), vi.fn(), {
    initialTaskId: task.id,
    onNavigationLockChange: onLock,
    workspace: twoTaskWorkspace,
  })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Unsaved line')
  await waitFor(() => expect(onLock).toHaveBeenCalledWith(true))
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discard }))
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('')
  expect(screen.queryByRole('button', { name: REVIEW_COPY.discard })).toBeNull()
  await waitFor(() => expect(onLock).toHaveBeenLastCalledWith(false))
})

test('a pending progress write keeps navigation locked until success', async () => {
  let release!: (value: Response) => void
  const held = new Promise<Response>((resolve) => { release = resolve })
  const onLock = vi.fn()
  renderPage(reviewSurfaceFetch(() => held), vi.fn(), {
    initialTaskId: task.id,
    onNavigationLockChange: onLock,
  })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await waitFor(() => expect(onLock).toHaveBeenCalledWith(true))
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  expect(await screen.findByRole('button', { name: REVIEW_COPY.saving })).toBeDisabled()
  expect(screen.queryByRole('button', { name: REVIEW_COPY.discard })).toBeNull()
  expect(onLock).toHaveBeenLastCalledWith(true)
  await act(async () => {
    release((await acceptedEntryPost({
      body: JSON.stringify({
        date: '2026-08-30',
        task_id: task.id,
        done: ['Closed the release gate'],
        next: [],
        blockers: [],
      }),
    })) as Response)
  })
  await waitFor(() => expect(onLock).toHaveBeenLastCalledWith(false))
})

test('commit_unknown stays frozen for its owner, then strands recoverably when the workspace is replaced', async () => {
  const keys: string[] = []
  const posted: unknown[] = []
  let workspaceUid = workspace.workspace.id
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: workspaceUid, entries: [] } })
    }
    if (url === '/api/v1/review/entries' && init?.method === 'POST') {
      keys.push((init.headers as Record<string, string>)['Idempotency-Key'])
      posted.push(JSON.parse(String(init.body)))
      return jsonResponse({ data: { unexpected: 'shape' } }, 201)
    }
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onLock = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = (boundWorkspace: typeof twoTaskWorkspace) => (
    <QueryClientProvider client={client}>
      <DailyReviewPage
        initialTaskId={task.id}
        onNavigationLockChange={onLock}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={boundWorkspace}
      />
    </QueryClientProvider>
  )
  const view = render(tree(twoTaskWorkspace))
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  expect(await screen.findByRole('button', { name: REVIEW_COPY.retrySame })).toBeVisible()
  expect(screen.queryByRole('button', { name: REVIEW_COPY.discard })).toBeNull()
  await userEvent.selectOptions(screen.getByLabelText('Task'), otherTask.id)
  const frozenPrompt = screen.getByRole('alertdialog', { name: REVIEW_COPY.frozenGuardTitle })
  expect(within(frozenPrompt).queryByRole('button', { name: REVIEW_COPY.discardDraft })).toBeNull()
  await userEvent.click(within(frozenPrompt).getByRole('button', { name: REVIEW_COPY.keepFrozen }))
  expect(screen.getByLabelText('Task')).toHaveValue(task.id)
  expect(keys).toHaveLength(1)
  const frozenKey = keys[0]
  // Same owner: the frozen retry stays available and keeps its identity.
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.retrySame }))
  await waitFor(() => expect(keys).toHaveLength(2))
  expect(keys[1]).toBe(frozenKey)
  await waitFor(() => expect(onLock).toHaveBeenLastCalledWith(true))

  workspaceUid = '00000000-0000-4000-8000-0000000000b0'
  view.rerender(tree({
    ...twoTaskWorkspace,
    workspace: { ...workspace.workspace, id: workspaceUid },
  }))

  // Named unresolved state: old Task, date, workspace and payload inspectable.
  const unresolved = screen.getByRole('group', { name: REVIEW_COPY.strandedHeading })
  expect(within(unresolved).getByText(
    strandedIdentityLabel(task.id, '2026-08-30', workspace.workspace.id),
  )).toBeVisible()
  expect(within(unresolved).getByText('Closed the release gate')).toBeVisible()
  expect(within(unresolved).getByText(REVIEW_COPY.strandedExplanation)).toBeVisible()

  // Retry is disabled with the reason stated, not enabled and inert.
  expect(screen.queryByRole('button', { name: REVIEW_COPY.retrySame })).toBeNull()
  const unavailable = screen.getByRole('button', { name: REVIEW_COPY.retryUnavailable })
  expect(unavailable).toBeDisabled()
  fireEvent.click(unavailable)
  expect(keys).toHaveLength(2)
  expect(onLock).toHaveBeenLastCalledWith(true)

  // Explicit acknowledge and release: nothing replays, navigation unlocks.
  await userEvent.click(within(unresolved).getByRole('button', { name: REVIEW_COPY.releaseStranded }))
  await waitFor(() => expect(onLock).toHaveBeenLastCalledWith(false))
  expect(screen.queryByRole('group', { name: REVIEW_COPY.strandedHeading })).toBeNull()
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('')
  expect(screen.getByRole('button', { name: REVIEW_COPY.save })).toBeDisabled()

  // Zero cross-owner POSTs: both writes carry the original identity and key.
  expect(keys).toEqual([frozenKey, frozenKey])
  expect(posted).toEqual([
    { date: '2026-08-30', task_id: task.id, done: ['Closed the release gate'], next: [], blockers: [] },
    { date: '2026-08-30', task_id: task.id, done: ['Closed the release gate'], next: [], blockers: [] },
  ])
})

test('a target arriving while the draft is blocked is applied after the draft is discarded', async () => {
  const posted: Array<{ task_id: string }> = []
  const fetchMock = reviewSurfaceFetch((init) => {
    posted.push(JSON.parse(String(init?.body)))
    return acceptedEntryPost(init)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = (boundInitialTaskId: string) => (
    <QueryClientProvider client={client}>
      <DailyReviewPage
        initialTaskId={boundInitialTaskId}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        today="2026-08-30"
        workspace={twoTaskWorkspace}
      />
    </QueryClientProvider>
  )
  const view = render(tree(task.id))
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Keep this draft')
  view.rerender(tree(otherTask.id))
  // The blocked draft stays bound to the Task it was written for.
  expect(screen.getByLabelText('Task')).toHaveValue(task.id)
  expect(screen.getByText(`Recording progress for ${task.id}`)).toBeVisible()

  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discard }))
  await waitFor(() => expect(screen.getByLabelText('Task')).toHaveValue(otherTask.id))
  expect(screen.getByText(`Recording progress for ${otherTask.id}`)).toBeVisible()

  await userEvent.type(screen.getByLabelText(REVIEW_COPY.changed), 'Recorded after the target moved')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  await waitFor(() => expect(posted).toHaveLength(1))
  expect(posted[0].task_id).toBe(otherTask.id)
})

test('Discard draft and guard discard both clear a refused save error', async () => {
  const fetchMock = reviewSurfaceFetch(() => (
    jsonResponse({ error: { code: 'conflict', message: 'Review entry refused.' } }, 409)
  ))
  const { onNotice } = renderPage(fetchMock, vi.fn(), {
    initialTaskId: task.id,
    workspace: twoTaskWorkspace,
  })
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Review entry refused.', 'error'))
  expect(screen.getByRole('alert')).toHaveTextContent('Review entry refused.')

  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discard }))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: REVIEW_COPY.retryUnchanged })).toBeNull()
  expect(screen.getByRole('button', { name: REVIEW_COPY.save })).toBeDisabled()

  await userEvent.type(screen.getByLabelText(REVIEW_COPY.changed), 'Second attempt')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Review entry refused.'))
  await userEvent.selectOptions(screen.getByLabelText('Task'), otherTask.id)
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.discardDraft }))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByLabelText('Task')).toHaveValue(otherTask.id)
  expect(screen.getByLabelText(REVIEW_COPY.changed)).toHaveValue('')
  expect(screen.getByRole('button', { name: REVIEW_COPY.save })).toBeDisabled()
})

test('saving progress invalidates checkpoint-audit even when Task revision is unchanged', async () => {
  const uid = '22222222-2222-4222-8222-222222222222'
  const boundWorkspace = { ...workspace, workspace: { ...workspace.workspace, id: uid } }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url.startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: uid, entries: [] } })
    }
    if (url === '/api/v1/review/entries' && init?.method === 'POST') return acceptedEntryPost(init)
    const saved = emptySavedReportsList(url)
    if (saved) return saved
    throw new Error(`Unexpected request: ${url}`)
  })
  const { client } = renderPage(fetchMock, vi.fn(), {
    initialTaskId: task.id,
    workspace: boundWorkspace,
  })
  const auditKey = ['checkpoint-audit', uid]
  await screen.findByText('Not yet')
  await waitFor(() => expect(client.getQueryState(auditKey)?.dataUpdatedAt ?? 0).toBeGreaterThan(0))
  await act(async () => {
    client.setQueryData(auditKey, { workspace_uid: uid, entries: [] })
  })
  const before = client.getQueryState(auditKey)?.dataUpdatedAt ?? 0
  const checkpointCalls = () => (
    fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/api/v1/review/checkpoints')).length
  )
  const beforeCalls = checkpointCalls()
  expect(task.revision).toBe(2)
  await userEvent.type(await screen.findByLabelText(REVIEW_COPY.changed), 'Closed the release gate')
  await userEvent.click(screen.getByRole('button', { name: REVIEW_COPY.save }))
  await waitFor(() => expect(client.getQueryState(auditKey)?.dataUpdatedAt ?? 0).toBeGreaterThan(before))
  expect(checkpointCalls()).toBeGreaterThan(beforeCalls)
  expect(task.revision).toBe(2)
})
