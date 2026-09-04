import { StrictMode, type PropsWithChildren } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { api } from '../api/client'
import type { ReviewProjection, TaskDetail, WorkspaceProjection } from '../domain/types'
import { task, workspace } from '../test/fixtures'
import { useCheckpointNotices } from './useCheckpointNotices'
import type { CheckpointCommittedEvent } from './workspaceChangeEvents'

const id = '123e4567-e89b-42d3-a456-426614174000'
const otherId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const event: CheckpointCommittedEvent = {
  event_id: 1, kind: 'agent.checkpoint.committed', workspace_uid: id, task_id: task.id,
  checkpoint_id: `CP-${'a'.repeat(64)}`, date: '2026-09-03', done_count: 1, next_count: 0,
  blocker_count: 0, first_for_task: true, origin: 'agent-cli-v1', replayed: false,
}
const projection: WorkspaceProjection = { ...workspace, workspace: { ...workspace.workspace, id } }
const detail: TaskDetail = { task, context: [], activity: [], replies: [] }
const review: ReviewProjection = {
  day: { date: event.date, start_time: null, entries: [] },
  weekly: { range: { start: '2026-08-28', end: event.date, days: 7 }, projects: [], objectives: [] },
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup() {
  const workspaceRead = vi.spyOn(api, 'getWorkspace').mockResolvedValue(projection)
  const taskRead = vi.spyOn(api, 'getTask').mockResolvedValue(detail)
  const reviewRead = vi.spyOn(api, 'getReview').mockResolvedValue(review)
  const mutate = vi.spyOn(api, 'patchTask')
  const onNotice = vi.fn()
  const onView = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['workspace'], projection)
  client.setQueryData(['task', task.id], detail)
  client.setQueryData(['review', event.date, 7], review)
  const wrapper = ({ children }: PropsWithChildren) => <StrictMode><QueryClientProvider client={client}>{children}</QueryClientProvider></StrictMode>
  const hook = renderHook(({ workspaceId }) => useCheckpointNotices({
    workspace: { ...projection, workspace: { ...projection.workspace, id: workspaceId } }, onNotice, onView,
  }), {
    wrapper, initialProps: { workspaceId: id },
  })
  return { ...hook, client, onNotice, onView, workspaceRead, taskRead, reviewRead, mutate }
}

test('waits for three real reads even when inactive query data is fresh, then View is read-only', async () => {
  const app = setup()
  const pending = deferred<ReviewProjection>()
  app.reviewRead.mockReturnValueOnce(pending.promise)
  act(() => app.result.current(event))
  await waitFor(() => expect(app.reviewRead).toHaveBeenCalledWith(event.date, 7))
  expect(app.workspaceRead).toHaveBeenCalledTimes(1)
  expect(app.taskRead).toHaveBeenCalledWith(task.id)
  expect(app.onNotice).not.toHaveBeenCalled()
  await act(async () => pending.resolve(review))
  expect(app.onNotice).toHaveBeenCalledTimes(1)
  expect(app.onNotice.mock.calls[0][0].message).toContain('Agent CLI')
  app.onNotice.mock.calls[0][0].view()
  expect(app.onView).toHaveBeenCalledWith(event)
  expect(app.mutate).not.toHaveBeenCalled()
})

test('suppresses concurrent and reconnect duplicates by CP, and keeps distinct checkpoints', async () => {
  const app = setup()
  const pending = deferred<ReviewProjection>()
  app.reviewRead.mockReturnValueOnce(pending.promise)
  act(() => {
    app.result.current(event)
    app.result.current({ ...event, event_id: 2 })
  })
  await waitFor(() => expect(app.reviewRead).toHaveBeenCalledTimes(1))
  await act(async () => pending.resolve(review))
  const handler = app.result.current
  app.rerender({ workspaceId: id })
  expect(app.result.current).toBe(handler)
  act(() => app.result.current({ ...event, event_id: 9 }))
  expect(app.onNotice).toHaveBeenCalledTimes(1)
  act(() => app.result.current({ ...event, checkpoint_id: `CP-${'b'.repeat(64)}`, event_id: 10 }))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(2))
  expect(app.workspaceRead).toHaveBeenCalledTimes(2)
})

test('serializes distinct event refetches in received order', async () => {
  const app = setup()
  const pending = deferred<ReviewProjection>()
  app.reviewRead.mockReturnValueOnce(pending.promise)
  act(() => {
    app.result.current(event)
    app.result.current({ ...event, checkpoint_id: `CP-${'b'.repeat(64)}`, event_id: 2, done_count: 2 })
  })
  await waitFor(() => expect(app.reviewRead).toHaveBeenCalledTimes(1))
  expect(app.onNotice).not.toHaveBeenCalled()
  await act(async () => pending.resolve(review))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(2))
  expect(app.onNotice.mock.calls.map(([notice]) => notice.message)).toEqual([
    expect.stringContaining('1 done'), expect.stringContaining('2 done'),
  ])
})

test('ignores foreign events before fetching anything', () => {
  const app = setup()
  act(() => app.result.current({ ...event, workspace_uid: otherId }))
  expect(app.workspaceRead).not.toHaveBeenCalled()
  expect(app.onNotice).not.toHaveBeenCalled()
})

test('a workspace change and return cannot revive an old pending owner', async () => {
  const app = setup()
  const pending = deferred<ReviewProjection>()
  app.reviewRead.mockReturnValueOnce(pending.promise)
  act(() => app.result.current(event))
  await waitFor(() => expect(app.reviewRead).toHaveBeenCalledTimes(1))
  app.rerender({ workspaceId: otherId })
  app.rerender({ workspaceId: id })
  await act(async () => pending.resolve(review))
  expect(app.onNotice).not.toHaveBeenCalled()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))
})

test('unmount cancels pending work and previously exposed View actions', async () => {
  const app = setup()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))
  const view = app.onNotice.mock.calls[0][0].view
  const pending = deferred<ReviewProjection>()
  app.reviewRead.mockReturnValueOnce(pending.promise)
  act(() => app.result.current({ ...event, checkpoint_id: `CP-${'b'.repeat(64)}` }))
  await waitFor(() => expect(app.reviewRead).toHaveBeenCalledTimes(2))
  app.unmount()
  await act(async () => pending.resolve(review))
  view()
  expect(app.onNotice).toHaveBeenCalledTimes(1)
  expect(app.onView).not.toHaveBeenCalled()
})

test.each(['workspace', 'task', 'review'] as const)('failed %s read creates no notice and allows a later duplicate to retry', async (which) => {
  const app = setup()
  const reader = { workspace: app.workspaceRead, task: app.taskRead, review: app.reviewRead }[which]
  reader.mockRejectedValueOnce(new Error('CANARY internal path'))
  await act(async () => app.result.current(event))
  expect(app.onNotice).not.toHaveBeenCalled()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))
  expect(app.onNotice.mock.calls[0][0].message).not.toContain('CANARY')
})

test.each(['workspace', 'missing-task', 'task-id', 'task-uid', 'task-revision', 'review-date'] as const)('refuses contradictory authoritative %s data', async (which) => {
  const app = setup()
  if (which === 'workspace') app.workspaceRead.mockResolvedValue({ ...projection, workspace: { ...projection.workspace, id: otherId } })
  if (which === 'missing-task') app.workspaceRead.mockResolvedValue({ ...projection, tasks: [] })
  if (which === 'task-id') app.taskRead.mockResolvedValue({ ...detail, task: { ...task, id: 'T-0002' } })
  if (which === 'task-uid') app.taskRead.mockResolvedValue({ ...detail, task: { ...task, uid: otherId } })
  if (which === 'task-revision') app.taskRead.mockResolvedValue({ ...detail, task: { ...task, revision: task.revision + 1 } })
  if (which === 'review-date') app.reviewRead.mockResolvedValue({ ...review, day: { ...review.day, date: '2026-09-04' } })
  await act(async () => app.result.current(event))
  expect(app.onNotice).not.toHaveBeenCalled()
  expect(app.client.getQueryData(['workspace'])).toEqual(projection)
})

test('View refuses after its Task disappears or workspace changes', async () => {
  const app = setup()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))
  const view = app.onNotice.mock.calls[0][0].view
  app.client.setQueryData(['workspace'], { ...projection, tasks: [] })
  view()
  expect(app.onView).not.toHaveBeenCalled()
  app.client.setQueryData(['workspace'], projection)
  app.rerender({ workspaceId: otherId })
  view()
  expect(app.onView).not.toHaveBeenCalled()
})

test.each(['workspace', 'task'] as const)('a response older than the existing %s Task revision is not an authoritative replacement', async (which) => {
  const app = setup()
  const newer = { ...task, revision: task.revision + 1 }
  if (which === 'workspace') app.client.setQueryData(['workspace'], { ...projection, tasks: projection.tasks.map((item) => item.id === task.id ? newer : item) })
  else app.client.setQueryData(['task', task.id], { ...detail, task: newer })
  const before = [app.client.getQueryData(['workspace']), app.client.getQueryData(['task', task.id])]
  await act(async () => app.result.current(event))
  expect(app.workspaceRead).toHaveBeenCalledTimes(1)
  expect(app.onNotice).not.toHaveBeenCalled()
  expect([app.client.getQueryData(['workspace']), app.client.getQueryData(['task', task.id])]).toEqual(before)
})


test('carries the verified Task and an opaque owner lifetime to the caller, with synchronous liveness', async () => {
  const app = setup()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))

  const notice = app.onNotice.mock.calls[0][0]
  expect(notice.intent).toMatchObject({
    taskId: task.id,
    taskUid: task.uid,
    workspaceUid: id,
  })
  // The epoch is an opaque lifetime token, not a repeatable workspace or date.
  expect(notice.intent.ownerEpoch).not.toContain(id)
  expect(notice.intent.ownerEpoch).not.toContain(event.date)
  expect(notice.intent.isCurrent()).toBe(true)

  // A Task UID replacement in the live cache is a different Task, synchronously.
  app.client.setQueryData(['workspace'], {
    ...projection,
    tasks: projection.tasks.map((item) => item.id === task.id ? { ...item, uid: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' } : item),
  })
  expect(notice.intent.isCurrent()).toBe(false)
})

test('a second checkpoint for the same Task gets its own distinct owner lifetime', async () => {
  const app = setup()
  act(() => app.result.current(event))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(1))
  act(() => app.result.current({ ...event, checkpoint_id: `CP-${'b'.repeat(64)}`, event_id: 2, first_for_task: false }))
  await waitFor(() => expect(app.onNotice).toHaveBeenCalledTimes(2))

  const first = app.onNotice.mock.calls[0][0].intent.ownerEpoch
  const second = app.onNotice.mock.calls[1][0].intent.ownerEpoch
  expect(second).not.toBe(first)
})
