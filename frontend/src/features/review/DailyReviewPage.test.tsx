import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { DailyReviewPage } from './DailyReviewPage'

function jsonResponse(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}

const emptyReview = {
  day: { date: '2026-08-30', start_time: null, entries: [] },
  weekly: {
    range: { start: '2026-08-24', end: '2026-08-30', days: 7 },
    objectives: [],
    projects: [],
  },
}

function renderPage(fetchMock: ReturnType<typeof vi.fn>, onNotice = vi.fn()) {
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DailyReviewPage onNotice={onNotice} onOpenTask={vi.fn()} today="2026-08-30" workspace={workspace} />
    </QueryClientProvider>,
  )
  return { onNotice }
}

test('review calendar changes the selected date query and rejects impossible or future drafts', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    if (String(input).startsWith('/api/v1/review?')) return jsonResponse({ data: emptyReview })
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
    if (url === '/api/v1/review/entries' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      const entry = { task_id: task.id, task: task.title, done: body.done, next: body.next, blockers: body.blockers }
      review = { ...review, day: { ...review.day, entries: [entry] } }
      return jsonResponse({ data: { date: body.date, ...entry }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  const { onNotice } = renderPage(fetchMock)

  const doneInput = await screen.findByLabelText(/Done/)
  await userEvent.type(doneInput, 'Closed the release gate')
  await userEvent.type(screen.getByLabelText(/Next/), 'Open Daily Review')
  await userEvent.type(screen.getByLabelText(/Blockers/), 'Waiting on dogfood')
  await userEvent.click(screen.getByRole('button', { name: 'Add review entry' }))

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
  expect(await screen.findByText('Closed the release gate')).toBeVisible()
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
