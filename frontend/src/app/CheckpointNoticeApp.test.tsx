import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { App } from './App'
import { api } from '../api/client'
import type { ReviewProjection, TaskDetail, WorkspaceProjection } from '../domain/types'
import { jsonResponse, task, workspace } from '../test/fixtures'

const workspaceId = '123e4567-e89b-42d3-a456-426614174000'
const projection = { ...workspace, workspace: { ...workspace.workspace, id: workspaceId } }
const committed = {
  event_id: 2, kind: 'agent.checkpoint.committed', workspace_uid: workspaceId,
  task_id: 'T-0001', date: '2026-09-03', checkpoint_id: `CP-${'a'.repeat(64)}`,
  done_count: 1, next_count: 0, blocker_count: 0, first_for_task: true,
  origin: 'agent-cli-v1', replayed: false,
}

class NoticeSource {
  static instances: NoticeSource[] = []
  listeners = new Map<string, EventListener>()
  closed = false
  constructor() { NoticeSource.instances.push(this) }
  addEventListener(name: string, listener: EventListener) { this.listeners.set(name, listener) }
  close() { this.closed = true }
  emit(name: string, payload: unknown, id = '2') {
    this.listeners.get(name)?.({ data: JSON.stringify(payload), lastEventId: id } as MessageEvent)
  }
}

function setup(options: { reviewGate?: Promise<void>; reviewStatus?: number } = {}) {
  NoticeSource.instances = []
  window.history.replaceState(null, '', '/?surface=workspace')
  const reads = { workspace: 0, task: 0, review: 0 }
  const http: { workspace: WorkspaceProjection; detail: TaskDetail; review: ReviewProjection } = {
    workspace: projection,
    detail: { task, context: [], activity: [], replies: [] },
    review: {
      day: { date: '2026-09-03', start_time: null, entries: [] },
      weekly: { range: { start: '2026-08-28', end: '2026-09-03', days: 7 }, objectives: [], projects: [] },
    },
  }
  vi.stubGlobal('EventSource', NoticeSource)
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.method ?? 'GET').toBe('GET')
    const url = String(input)
    if (url.includes('/api/v1/sync/status')) return jsonResponse({ data: {
      state: 'in-sync', workspace_id: workspaceId, candidate_workspace_id: workspaceId,
      generation: 1, manifest_digest: null, changed_files: [], reason: null, rebind_available: false,
    } })
    if (url.includes('/api/v1/workspace')) { reads.workspace++; return jsonResponse({ data: http.workspace }) }
    if (url.includes('/api/v1/tasks/T-0001')) {
      reads.task++
      return jsonResponse({ data: http.detail })
    }
    if (url.includes('/api/v1/review')) {
      reads.review++
      const snapshot = http.review
      const response = () => jsonResponse({ data: snapshot }, options.reviewStatus ?? 200)
      return options.reviewGate && reads.review === 1 ? options.reviewGate.then(response) : response()
    }
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(<StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>)
  return { ...rendered, client, reads, fetchMock, http }
}

test('legacy sync still refreshes and an attributed checkpoint refetches inactive Task and Review before View', async () => {
  const app = setup()
  await screen.findByRole('button', { name: 'SSOT in sync' })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const sources = NoticeSource.instances.filter((source) => !source.closed)
  expect(sources).toHaveLength(1)
  const source = sources[0]
  const before = app.reads.workspace
  act(() => source.emit('sync', { generation: 2, state: 'in-sync' }, '1'))
  await waitFor(() => expect(app.reads.workspace).toBeGreaterThan(before))
  expect(app.reads.task).toBe(0)
  expect(app.reads.review).toBe(0)
  act(() => source.emit('workstack.change.v1', committed))
  const view = await screen.findByRole('button', { name: 'View' })
  expect(view.closest('[role="status"]')).toHaveTextContent('Agent CLI')
  expect(app.reads.task).toBe(1)
  expect(app.reads.review).toBe(1)
  await userEvent.click(view)
  expect(window.location.search).toContain('task=T-0001')
  expect(view.closest('[role="status"]')).toHaveTextContent('2026-09-03')
  expect(app.fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  app.unmount()
  expect(NoticeSource.instances.every((item) => item.closed)).toBe(true)
})

test('StrictMode preserves one live source and dedupes pending and later CP delivery', async () => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const app = setup({ reviewGate: gate })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const live = NoticeSource.instances.filter((source) => !source.closed)
  expect(live).toHaveLength(1)
  act(() => {
    live[0].emit('workstack.change.v1', committed)
    live[0].emit('workstack.change.v1', { ...committed, event_id: 3 }, '3')
  })
  await waitFor(() => expect(app.reads.review).toBe(1))
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  await act(async () => release())
  await screen.findByRole('button', { name: 'View' })
  await userEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }))
  act(() => live[0].emit('workstack.change.v1', { ...committed, event_id: 4 }, '4'))
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  expect(app.reads.review).toBe(1)
  act(() => live[0].emit('workstack.change.v1', { ...committed, event_id: 5, checkpoint_id: `CP-${'b'.repeat(64)}` }, '5'))
  await screen.findByRole('button', { name: 'View' })
  expect(app.reads.review).toBe(2)
})

test('a real App workspace change cancels a pending notification before cache or View publication', async () => {
  let release!: () => void
  const app = setup({ reviewGate: new Promise<void>((resolve) => { release = resolve }) })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const live = NoticeSource.instances.filter((source) => !source.closed)[0]
  act(() => live.emit('workstack.change.v1', committed))
  await waitFor(() => expect(app.reads.review).toBe(1))
  const other = { ...projection, workspace: { ...projection.workspace, id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' } }
  await act(async () => app.client.setQueryData(['workspace'], other))
  await act(async () => release())
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  expect(app.client.getQueryData(['workspace'])).toEqual(other)
})

test('a failed authoritative Review fetch and invalid frames produce no success notice or write', async () => {
  const app = setup({ reviewStatus: 503 })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const live = NoticeSource.instances.filter((source) => !source.closed)[0]
  await act(async () => {
    live.emit('workstack.change.v1', { ...committed, title: 'CANARY' })
    live.emit('workstack.change.v1', { ...committed, workspace_uid: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' })
  })
  expect(app.reads.review).toBe(0)
  await act(async () => live.emit('workstack.change.v1', committed))
  expect(app.reads.review).toBe(1)
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  expect(screen.queryByText(/CANARY/)).not.toBeInTheDocument()
  expect(app.fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})

test('an unmounted App cannot notify its successor through pending reads or a queued old source', async () => {
  let release!: () => void
  const old = setup({ reviewGate: new Promise<void>((resolve) => { release = resolve }) })
  await waitFor(() => expect(old.client.getQueryData(['workspace'])).toEqual(projection))
  const oldSource = NoticeSource.instances.filter((source) => !source.closed)[0]
  act(() => oldSource.emit('workstack.change.v1', committed))
  await waitFor(() => expect(old.reads.review).toBe(1))
  old.unmount()
  expect(oldSource.closed).toBe(true)
  const next = setup()
  await waitFor(() => expect(next.client.getQueryData(['workspace'])).toEqual(projection))
  await act(async () => {
    release()
    oldSource.emit('workstack.change.v1', committed)
  })
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  expect(next.reads.review).toBe(0)
  const source = NoticeSource.instances.filter((item) => !item.closed)[0]
  act(() => source.emit('workstack.change.v1', committed))
  await screen.findByRole('button', { name: 'View' })
  expect(next.reads.review).toBe(1)
})

async function pendingNotice() {
  let release!: () => void
  const app = setup({ reviewGate: new Promise<void>((resolve) => { release = resolve }) })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const source = NoticeSource.instances.filter((item) => !item.closed)[0]
  act(() => source.emit('workstack.change.v1', committed))
  await waitFor(() => expect(app.reads.review).toBe(1))
  expect(app.reads.task).toBe(1)
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  return { ...app, source, release }
}

test('matching delayed authoritative reads still produce a healthy View action', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  expect(app.client.getQueryData(['task', task.id])).toEqual(app.http.detail)
  expect(app.client.getQueryData(['review', committed.date, 7])).toEqual(app.http.review)
  await userEvent.click(view)
  expect(window.location.search).toContain('task=T-0001')
})

test.each(['revision', 'replacement', 'deletion'] as const)('late checkpoint reads cannot overwrite a newer authoritative workspace %s', async (change) => {
  const app = await pendingNotice()
  const changed = change === 'replacement'
    ? { ...task, uid: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' }
    : { ...task, revision: task.revision + 1 }
  const newer = { ...projection, tasks: change === 'deletion' ? [] : projection.tasks.map((item) => item.id === task.id ? changed : item) }
  app.http.workspace = newer
  await act(async () => { await app.client.fetchQuery({ queryKey: ['workspace'], queryFn: api.getWorkspace }) })
  expect(app.client.getQueryData(['workspace'])).toEqual(newer)
  const taskBefore = app.client.getQueryData(['task', task.id])
  const reviewBefore = app.client.getQueryData(['review', committed.date, 7])
  await act(async () => app.release())
  expect.soft(app.client.getQueryData(['workspace'])).toEqual(newer)
  expect.soft(app.client.getQueryData(['task', task.id])).toEqual(taskBefore)
  expect.soft(app.client.getQueryData(['review', committed.date, 7])).toEqual(reviewBefore)
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
})

test('View refuses a replacement Task UID under the same legacy id', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  const replacement = { ...task, uid: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' }
  app.http.workspace = { ...projection, tasks: projection.tasks.map((item) => item.id === task.id ? replacement : item) }
  await act(async () => { await app.client.fetchQuery({ queryKey: ['workspace'], queryFn: api.getWorkspace }) })
  await userEvent.click(view)
  expect(window.location.search).not.toContain('task=')
})

test('batched workspace A to B to A invalidates old pending work before React paints B', async () => {
  const app = await pendingNotice()
  const other = { ...projection, workspace: { ...projection.workspace, id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' } }
  act(() => {
    app.client.setQueryData(['workspace'], other)
    app.client.setQueryData(['workspace'], projection)
  })
  expect(app.client.getQueryData(['workspace'])).toEqual(projection)
  await act(async () => app.release())
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
  expect(app.client.getQueryData(['task', task.id])).toBeUndefined()
  act(() => app.source.emit('workstack.change.v1', committed))
  await screen.findByRole('button', { name: 'View' })
  expect(app.reads.review).toBe(2)
})

test.each(['task', 'review'] as const)('late checkpoint reads preserve a newer authoritative %s cache and write no stale bundle', async (which) => {
  const app = await pendingNotice()
  const key = which === 'task' ? ['task', task.id] : ['review', committed.date, 7]
  if (which === 'task') {
    app.http.detail = { ...app.http.detail, task: { ...task, revision: task.revision + 1 } }
    await act(async () => { await app.client.fetchQuery({ queryKey: key, queryFn: () => api.getTask(task.id) }) })
  } else {
    app.http.review = { ...app.http.review, day: { ...app.http.review.day, start_time: '09:30' } }
    await act(async () => { await app.client.fetchQuery({ queryKey: key, queryFn: () => api.getReview(committed.date, 7) }) })
  }
  const newer = which === 'task' ? app.http.detail : app.http.review
  expect(app.client.getQueryData(key)).toEqual(newer)
  const cached = [
    app.client.getQueryState(['workspace'])?.dataUpdateCount,
    app.client.getQueryData(['task', task.id]),
    app.client.getQueryData(['review', committed.date, 7]),
  ]
  await act(async () => app.release())
  expect(app.client.getQueryData(key)).toEqual(newer)
  expect([
    app.client.getQueryState(['workspace'])?.dataUpdateCount,
    app.client.getQueryData(['task', task.id]),
    app.client.getQueryData(['review', committed.date, 7]),
  ]).toEqual(cached)
  expect(screen.queryByRole('button', { name: 'View' })).not.toBeInTheDocument()
})

test('View retains Task identity while allowing a newer revision of that same Task', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  app.http.workspace = { ...projection, tasks: projection.tasks.map((item) => item.id === task.id ? { ...item, revision: item.revision + 1 } : item) }
  await act(async () => { await app.client.fetchQuery({ queryKey: ['workspace'], queryFn: api.getWorkspace }) })
  await userEvent.click(view)
  expect(window.location.search).toContain('task=T-0001')
})

test('a published View cannot revive after a batched workspace away and back', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  act(() => {
    app.client.setQueryData(['workspace'], { ...projection, workspace: { ...projection.workspace, id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' } })
    app.client.setQueryData(['workspace'], projection)
  })
  await userEvent.click(view)
  expect(window.location.search).not.toContain('task=')
})

test('View refuses a replacement UID learned through an authoritative Task detail refresh', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  app.http.detail = { ...app.http.detail, task: { ...task, uid: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' } }
  await act(async () => { await app.client.fetchQuery({ queryKey: ['task', task.id], queryFn: () => api.getTask(task.id) }) })
  expect(app.client.getQueryData(['task', task.id])).toEqual(app.http.detail)
  await userEvent.click(view)
  expect(window.location.search).not.toContain('task=')
})

test('View accepts a refreshed authoritative detail for the same Task UID', async () => {
  const app = await pendingNotice()
  await act(async () => app.release())
  const view = await screen.findByRole('button', { name: 'View' })
  app.http.detail = { ...app.http.detail, task: { ...task, revision: task.revision + 1 } }
  await act(async () => { await app.client.fetchQuery({ queryKey: ['task', task.id], queryFn: () => api.getTask(task.id) }) })
  expect(app.client.getQueryData(['task', task.id])).toEqual(app.http.detail)
  await userEvent.click(view)
  expect(window.location.search).toContain('task=T-0001')
})


test('the committed notice still writes nothing on arrival or View now that an action can coexist', async () => {
  const app = setup()
  await screen.findByRole('button', { name: 'SSOT in sync' })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toEqual(projection))
  const writes = app.fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method && (init as RequestInit).method !== 'GET')
  expect(writes).toHaveLength(0)

  NoticeSource.instances.at(-1)!.emit('workstack.change.v1', committed)
  const view = await screen.findByRole('button', { name: 'View' })
  await userEvent.click(view)

  const afterView = app.fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method && (init as RequestInit).method !== 'GET')
  expect(afterView).toHaveLength(0)
})
