import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
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
