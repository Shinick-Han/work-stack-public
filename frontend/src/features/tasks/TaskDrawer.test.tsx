import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { vi } from 'vitest'
import { api } from '../../api/client'
import type { Task, TaskDetail, WorkspaceProjection } from '../../domain/types'
import { TaskDrawer } from './TaskDrawer'
import { capture, jsonResponse, task, workspace } from '../../test/fixtures'
import { verifiedMicrosoftProviderGates } from '../../test/providerGates'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function detail(taskValue: Task): TaskDetail {
  return { task: taskValue, context: [], activity: [], replies: [] }
}

function createClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function WorkspaceObserver({ initial }: { initial: WorkspaceProjection }) {
  useQuery({
    queryKey: ['workspace'],
    queryFn: api.getWorkspace,
    initialData: initial,
    staleTime: Infinity,
  })
  return null
}

function renderSavingDrawer(client: QueryClient, initialWorkspace: WorkspaceProjection = workspace) {
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceObserver initial={initialWorkspace} />
      <TaskDrawer
        onClose={vi.fn()}
        onNotice={vi.fn()}
        taskId={task.id}
        workspace={initialWorkspace}
      />
    </QueryClientProvider>,
  )
}

function renderStrictSavingDrawer(client: QueryClient, initialWorkspace: WorkspaceProjection = workspace) {
  return render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <WorkspaceObserver initial={initialWorkspace} />
        <TaskDrawer
          onClose={vi.fn()}
          onNotice={vi.fn()}
          taskId={task.id}
          workspace={initialWorkspace}
        />
      </QueryClientProvider>
    </StrictMode>,
  )
}

test('shares overview, sanitized context, and activity in one drawer', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
    data: {
      task,
      context: [{
        id: 'C-0001',
        kind: 'capture',
        created_at: '2026-08-29T08:00:00Z',
        source: { provider: 'microsoft-outlook', display_title: 'Release review feedback', web_url: 'https://outlook.office.com/' },
        normalized: { summary: 'Rollback verification needs an owner.', context: 'Sanitized context.', action_items: [], tags: [] },
      }],
      activity: [{
        id: 'A-1',
        action: 'status_changed',
        created_at: '2026-08-29T09:00:00Z',
        details: { body: 'Hidden reply detail', target: { object_ref: 'Hidden target detail' } },
      }],
    },
  })))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onOpenObjective = vi.fn()
  render(<QueryClientProvider client={client}><TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} onOpenObjective={onOpenObjective} taskId="T-0001" workspace={workspace} /></QueryClientProvider>)

  expect(await screen.findByDisplayValue('Define release quality gate')).toBeInTheDocument()
  expect(screen.getByRole('group', { name: 'Task identity' })).toHaveTextContent(task.uid)
  expect(screen.getByRole('group', { name: 'Task identity' })).toHaveTextContent(`Revision ${task.revision}`)
  await userEvent.click(screen.getByRole('button', { name: 'Open objective O-1' }))
  expect(onOpenObjective).toHaveBeenCalledWith('O-1')
  expect(screen.getByRole('button', { name: 'Export to Conduit' })).toBeEnabled()
  await userEvent.click(screen.getByRole('button', { name: 'More task actions' }))
  expect(screen.getByRole('dialog', { name: 'Task actions' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
  expect(screen.getByLabelText('Priority')).toHaveValue(task.priority)
  await userEvent.click(screen.getByRole('tab', { name: /context/i }))
  expect(screen.getByText('Release review feedback')).toBeInTheDocument()
  expect(screen.getByText('Sanitized context.')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /open source/i })).toHaveAttribute('rel', 'noopener noreferrer')
  await userEvent.click(screen.getByRole('tab', { name: /activity/i }))
  expect(screen.getByText('status_changed')).toBeInTheDocument()
  expect(screen.queryByText('Hidden reply detail')).not.toBeInTheDocument()
  expect(screen.queryByText('Hidden target detail')).not.toBeInTheDocument()
})

test('opens parent, dependency, child, and dependent Tasks from the relationship summary', async () => {
  const parent: Task = { ...task, id: 'T-0002', uid: '22222222-2222-4222-8222-222222222222', title: 'Parent outcome', objective_ids: [] }
  const dependency: Task = { ...task, id: 'T-0003', uid: '33333333-3333-4333-8333-333333333333', title: 'Required outcome', objective_ids: [] }
  const child: Task = { ...task, id: 'T-0004', uid: '44444444-4444-4444-8444-444444444444', title: 'Child outcome', objective_ids: [], parent_id: task.id }
  const dependent: Task = { ...task, id: 'T-0005', uid: '55555555-5555-4555-8555-555555555555', title: 'Dependent outcome', objective_ids: [], dependencies: [task.id] }
  const relatedTask: Task = { ...task, parent_id: parent.id, dependencies: [dependency.id] }
  const relatedWorkspace: WorkspaceProjection = { ...workspace, tasks: [relatedTask, parent, dependency, child, dependent] }
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ data: detail(relatedTask) })))
  const onOpenTask = vi.fn()
  const client = createClient()
  render(<QueryClientProvider client={client}><TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} onOpenTask={onOpenTask} taskId={relatedTask.id} workspace={relatedWorkspace} /></QueryClientProvider>)

  await screen.findByDisplayValue(relatedTask.title)
  await userEvent.click(screen.getByRole('button', { name: 'Open parent T-0002' }))
  await userEvent.click(screen.getByRole('button', { name: 'Open dependency T-0003' }))
  await userEvent.click(screen.getByRole('button', { name: 'Open child T-0004' }))
  await userEvent.click(screen.getByRole('button', { name: 'Open dependent T-0005' }))

  const parentSelect = screen.getByLabelText('Parent') as HTMLSelectElement
  const dependencySelect = screen.getByLabelText('Add dependency') as HTMLSelectElement
  expect(Array.from(parentSelect.options).some((option) => option.value === child.id)).toBe(false)
  expect(Array.from(dependencySelect.options).some((option) => option.value === dependent.id)).toBe(false)
  expect(Array.from(dependencySelect.options).some((option) => option.value === parent.id)).toBe(true)

  expect(onOpenTask).toHaveBeenNthCalledWith(1, parent.id)
  expect(onOpenTask).toHaveBeenNthCalledWith(2, dependency.id)
  expect(onOpenTask).toHaveBeenNthCalledWith(3, child.id)
  expect(onOpenTask).toHaveBeenNthCalledWith(4, dependent.id)
})

test('renders append-only planning status history with revisions and provenance', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
    data: {
      task,
      context: [],
      replies: [],
      activity: [{
        id: 'E-000002',
        type: 'task.planning_status',
        task_id: task.id,
        task_uid: task.uid,
        kind: 'transition',
        prior_revision: 1,
        new_revision: 2,
        prior_status: 'open',
        status: 'started',
        created_at: '2026-08-29T09:00:00Z',
        actor: 'user',
        provenance: 'api.v1',
      }],
    },
  })))
  const client = createClient()
  renderSavingDrawer(client)

  await screen.findByDisplayValue(task.title)
  await userEvent.click(screen.getByRole('tab', { name: /activity/i }))

  expect(screen.getByText('Open → In progress')).toBeInTheDocument()
  expect(screen.getByText('Revision 1 → 2')).toBeInTheDocument()
  expect(screen.getByText('By user · api.v1')).toBeInTheDocument()
})

test('offers reply preparation only for the linked Microsoft capture target', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
    data: { task, context: [capture], activity: [], replies: [] },
  })))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} providerGates={verifiedMicrosoftProviderGates} taskId="T-0001" workspace={workspace} /></QueryClientProvider>)

  await screen.findByDisplayValue('Define release quality gate')
  await userEvent.click(screen.getByRole('tab', { name: /context/i }))
  await userEvent.click(screen.getByRole('button', { name: 'Prepare Outlook/Teams reply' }))

  expect(screen.getByLabelText('Linked Microsoft source')).toHaveValue(capture.id)
  expect(screen.getAllByText(capture.source.display_title).length).toBeGreaterThanOrEqual(2)
  const exactTarget = screen.getByLabelText('Exact reply target')
  expect(exactTarget).toHaveTextContent(capture.source.resource_type)
  expect(exactTarget).toHaveTextContent(capture.source.connection_ref)
  expect(exactTarget).toHaveTextContent(capture.source.container_ref)
  expect(exactTarget).toHaveTextContent(capture.source.object_ref)
  expect(exactTarget).toHaveTextContent(capture.source.version_ref)
  expect(screen.queryByLabelText(/recipient/i)).not.toBeInTheDocument()
})

test('labels linked Microsoft replies unavailable until Gate 0 passes', async () => {
  vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
    data: { task, context: [capture], activity: [], replies: [] },
  })))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} taskId="T-0001" workspace={workspace} /></QueryClientProvider>)

  await screen.findByDisplayValue('Define release quality gate')
  await userEvent.click(screen.getByRole('tab', { name: /context/i }))

  expect(screen.queryByRole('button', { name: 'Prepare Outlook/Teams reply' })).not.toBeInTheDocument()
  expect(screen.getAllByText('Reply unavailable · Gate 0 pending').length).toBeGreaterThanOrEqual(1)
})

test('serializes queued edits, chains the returned revision, and converges both caches before Saved', async () => {
  const firstPatch = deferred<Response>()
  const secondPatch = deferred<Response>()
  const patchBodies: Array<Record<string, unknown>> = []
  let detailReads = 0
  let workspaceReads = 0
  let serverTask = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      return patchBodies.length === 1 ? firstPatch.promise : secondPatch.promise
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  await screen.findByDisplayValue(task.title)
  const status = screen.getByLabelText('Status') as HTMLSelectElement
  const priority = screen.getByLabelText('Priority') as HTMLSelectElement

  act(() => {
    status.value = 'done'
    status.dispatchEvent(new Event('change', { bubbles: true }))
    priority.value = 'P1'
    priority.dispatchEvent(new Event('change', { bubbles: true }))
  })

  await waitFor(() => expect(patchBodies).toHaveLength(1))
  expect(patchBodies[0]).toEqual({ status: 'done', revision: task.revision })
  expect(screen.getByText('Saving…')).toBeInTheDocument()
  expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  expect(priority).toHaveValue('P1')
  expect(screen.getByLabelText('Tags')).toBeDisabled()
  expect(screen.getByLabelText('Add dependency')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Close task drawer' })).toBeDisabled()

  const firstUpdated: Task = { ...task, status: 'done', revision: task.revision + 1 }
  await act(async () => firstPatch.resolve(response({ data: firstUpdated })))

  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(patchBodies[1]).toEqual({ priority: 'P1', revision: firstUpdated.revision })
  expect(screen.getByText('Saving…')).toBeInTheDocument()
  expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  expect(status).toHaveValue('done')
  expect(priority).toHaveValue('P1')

  serverTask = { ...firstUpdated, priority: 'P1', revision: firstUpdated.revision + 1 }
  await act(async () => secondPatch.resolve(response({ data: serverTask })))

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(detailReads).toBe(2)
  expect(workspaceReads).toBe(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    status: 'done',
    priority: 'P1',
    revision: serverTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    status: 'done',
    priority: 'P1',
    revision: serverTask.revision,
  })
})

test('queues a same-field return to the original value behind the in-flight change', async () => {
  const firstPatch = deferred<Response>()
  const secondPatch = deferred<Response>()
  const patchBodies: Array<Record<string, unknown>> = []
  let detailReads = 0
  let workspaceReads = 0
  let serverTask = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      return patchBodies.length === 1 ? firstPatch.promise : secondPatch.promise
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  await screen.findByDisplayValue(task.title)
  const status = screen.getByLabelText('Status') as HTMLSelectElement
  act(() => {
    status.value = 'done'
    status.dispatchEvent(new Event('change', { bubbles: true }))
    status.value = task.status
    status.dispatchEvent(new Event('change', { bubbles: true }))
  })

  await waitFor(() => expect(patchBodies).toHaveLength(1))
  expect(patchBodies[0]).toEqual({ status: 'done', revision: task.revision })
  expect(status).toHaveValue(task.status)
  expect(screen.getByText('Saving…')).toBeInTheDocument()

  const firstUpdated: Task = { ...task, status: 'done', revision: task.revision + 1 }
  await act(async () => firstPatch.resolve(response({ data: firstUpdated })))

  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(patchBodies[1]).toEqual({ status: task.status, revision: firstUpdated.revision })
  expect(status).toHaveValue(task.status)
  expect(screen.getByText('Saving…')).toBeInTheDocument()

  serverTask = { ...firstUpdated, status: task.status, revision: firstUpdated.revision + 1 }
  await act(async () => secondPatch.resolve(response({ data: serverTask })))

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(detailReads).toBe(2)
  expect(workspaceReads).toBe(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    status: task.status,
    revision: serverTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    status: task.status,
    revision: serverTask.revision,
  })
})

test('drains the first and queued PATCH through React StrictMode remount checks', async () => {
  const firstPatch = deferred<Response>()
  const secondPatch = deferred<Response>()
  const patchBodies: Array<Record<string, unknown>> = []
  let serverTask = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      return patchBodies.length === 1 ? firstPatch.promise : secondPatch.promise
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderStrictSavingDrawer(client)

  await screen.findByDisplayValue(task.title)
  const status = screen.getByLabelText('Status') as HTMLSelectElement
  const priority = screen.getByLabelText('Priority') as HTMLSelectElement
  act(() => {
    status.value = 'done'
    status.dispatchEvent(new Event('change', { bubbles: true }))
    priority.value = 'P1'
    priority.dispatchEvent(new Event('change', { bubbles: true }))
  })

  await waitFor(() => expect(patchBodies).toHaveLength(1))
  const firstUpdated: Task = { ...task, status: 'done', revision: task.revision + 1 }
  await act(async () => firstPatch.resolve(response({ data: firstUpdated })))

  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(patchBodies[1]).toEqual({ priority: 'P1', revision: firstUpdated.revision })
  expect(screen.getByText('Saving…')).toBeInTheDocument()

  serverTask = { ...firstUpdated, priority: 'P1', revision: firstUpdated.revision + 1 }
  await act(async () => secondPatch.resolve(response({ data: serverTask })))

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(patchBodies).toEqual([
    { status: 'done', revision: task.revision },
    { priority: 'P1', revision: firstUpdated.revision },
  ])
})

test('automatically rebases one disjoint-field conflict onto the latest revision', async () => {
  const patchBodies: Array<Record<string, unknown>> = []
  const dirtyTitle = 'Rebased local title'
  let serverTask: Task = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      patchBodies.push(body)
      if (patchBodies.length === 1) {
        serverTask = { ...task, priority: 'P3', revision: task.revision + 1 }
        return response({ error: { code: 'revision_conflict', message: 'task revision is stale' } }, 409)
      }
      serverTask = { ...serverTask, title: dirtyTitle, revision: serverTask.revision + 1 }
      return response({ data: serverTask })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) return jsonResponse({ data: detail(serverTask) })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, dirtyTitle)
  await userEvent.tab()

  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(patchBodies).toEqual([
    { title: dirtyTitle, revision: task.revision },
    { title: dirtyTitle, revision: task.revision + 1 },
  ])
  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(title).toHaveValue(dirtyTitle)
  expect(screen.getByLabelText('Priority')).toHaveValue('P3')
  expect(screen.queryByRole('button', { name: 'Retry save' })).not.toBeInTheDocument()
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject(serverTask)
})

test('keeps dirty intent after a conflict and retries only explicitly with the latest revision', async () => {
  const retryPatch = deferred<Response>()
  const patchBodies: Array<Record<string, unknown>> = []
  const dirtyTitle = 'Keep this unsaved release title'
  let detailReads = 0
  let workspaceReads = 0
  let serverTask: Task = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      if (patchBodies.length === 1) {
        serverTask = {
          ...task,
          title: 'Title changed elsewhere',
          priority: 'P3',
          revision: task.revision + 1,
        }
        return Promise.resolve(response({
          error: {
            code: 'revision_conflict',
            message: 'task revision is stale',
            details: { expected: serverTask.revision, received: task.revision },
          },
        }, 409))
      }
      return retryPatch.promise
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, dirtyTitle)
  await userEvent.tab()

  expect(await screen.findByText('Not saved')).toBeInTheDocument()
  await waitFor(() => expect(detailReads).toBe(2))
  expect(patchBodies).toHaveLength(1)
  expect(patchBodies[0]).toEqual({ title: dirtyTitle, revision: task.revision })
  expect(title).toHaveValue(dirtyTitle)
  expect(screen.getByLabelText('Priority')).toHaveValue('P3')
  expect(screen.getByRole('alert')).toHaveTextContent(/changed elsewhere|revision|stale/i)

  await act(async () => Promise.resolve())
  expect(patchBodies).toHaveLength(1)

  await userEvent.click(screen.getByRole('button', { name: 'Retry save' }))
  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(detailReads).toBeGreaterThanOrEqual(3)
  expect(patchBodies[1]).toEqual({ title: dirtyTitle, revision: task.revision + 1 })
  expect(title).toHaveValue(dirtyTitle)
  expect(screen.getByLabelText('Priority')).toHaveValue('P3')
  expect(screen.getByText('Saving…')).toBeInTheDocument()

  serverTask = { ...serverTask, title: dirtyTitle, revision: task.revision + 2 }
  await act(async () => retryPatch.resolve(response({ data: serverTask })))

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(detailReads).toBeGreaterThanOrEqual(4)
  expect(workspaceReads).toBeGreaterThanOrEqual(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    title: dirtyTitle,
    revision: serverTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    title: dirtyTitle,
    revision: serverTask.revision,
  })
})

test('requires an explicit discard before leaving after a failed save', async () => {
  const onClose = vi.fn()
  const onNavigationLockChange = vi.fn()
  const onOpenObjective = vi.fn()
  let serverTask = task
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      return Promise.reject(new TypeError('Offline before save confirmation'))
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) return jsonResponse({ data: detail(serverTask) })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  render(<QueryClientProvider client={client}><TaskDrawer onClose={onClose} onNavigationLockChange={onNavigationLockChange} onNotice={vi.fn()} onOpenObjective={onOpenObjective} taskId={task.id} workspace={workspace} /></QueryClientProvider>)

  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, 'Local title that was not saved')
  await userEvent.tab()

  expect(await screen.findByText('Not saved')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Close task drawer' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Open objective O-1' })).toBeDisabled()
  expect(title).toHaveValue('Local title that was not saved')
  expect(onNavigationLockChange).toHaveBeenLastCalledWith(true)
  const blockedUnload = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(blockedUnload)
  expect(blockedUnload.defaultPrevented).toBe(true)

  await userEvent.click(screen.getByRole('button', { name: 'Discard unsaved changes' }))
  expect(title).toHaveValue(serverTask.title)
  expect(screen.getByRole('button', { name: 'Close task drawer' })).toBeEnabled()
  expect(onNavigationLockChange).toHaveBeenLastCalledWith(false)
  const allowedUnload = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(allowedUnload)
  expect(allowedUnload.defaultPrevented).toBe(false)
  await userEvent.click(screen.getByRole('button', { name: 'Close task drawer' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onOpenObjective).not.toHaveBeenCalled()
})

test('clears a conflicted title when the user restores the authoritative value without another PATCH', async () => {
  const patchBodies: Array<Record<string, unknown>> = []
  const dirtyTitle = 'Discard this conflicted title'
  const authoritativeTitle = 'Authoritative server title'
  let detailReads = 0
  let workspaceReads = 0
  let serverTask: Task = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      serverTask = { ...task, title: authoritativeTitle, revision: task.revision + 1 }
      return Promise.resolve(response({
        error: {
          code: 'revision_conflict',
          message: 'task revision is stale',
          details: { expected: serverTask.revision, received: task.revision },
        },
      }, 409))
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, dirtyTitle)
  await userEvent.tab()

  expect(await screen.findByText('Not saved')).toBeInTheDocument()
  await waitFor(() => expect(detailReads).toBe(2))
  expect(patchBodies).toEqual([{ title: dirtyTitle, revision: task.revision }])
  expect(title).toHaveValue(dirtyTitle)
  expect(screen.getByRole('button', { name: 'Retry save' })).toBeEnabled()

  await userEvent.clear(title)
  await userEvent.type(title, authoritativeTitle)
  await userEvent.tab()

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Retry save' })).not.toBeInTheDocument()
  expect(patchBodies).toHaveLength(1)
  expect(detailReads).toBe(3)
  expect(workspaceReads).toBe(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    title: authoritativeTitle,
    revision: serverTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    title: authoritativeTitle,
    revision: serverTask.revision,
  })
})

test('finishes an ambiguous failure retry without another PATCH when the server already applied it', async () => {
  const patchBodies: Array<Record<string, unknown>> = []
  const dirtyTitle = 'Applied before the response was lost'
  let detailReads = 0
  let workspaceReads = 0
  let serverTask: Task = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      serverTask = { ...task, title: dirtyTitle, revision: task.revision + 1 }
      return Promise.reject(new TypeError('Network connection lost after apply'))
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return jsonResponse({ data: detail(serverTask) })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, dirtyTitle)
  await userEvent.tab()

  expect(await screen.findByText('Not saved')).toBeInTheDocument()
  await waitFor(() => expect(detailReads).toBe(2))
  expect(patchBodies).toEqual([{ title: dirtyTitle, revision: task.revision }])
  expect(title).toHaveValue(dirtyTitle)

  await userEvent.click(screen.getByRole('button', { name: 'Retry save' }))

  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(patchBodies).toHaveLength(1)
  expect(detailReads).toBe(4)
  expect(workspaceReads).toBe(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    title: dirtyTitle,
    revision: serverTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    title: dirtyTitle,
    revision: serverTask.revision,
  })
})

test('does not let a late lower revision rewind newer detail or workspace caches', async () => {
  const latePatch = deferred<Response>()
  const detailRefresh = deferred<Response>()
  let detailReads = 0
  let workspaceReads = 0
  let patchCalls = 0

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      patchCalls += 1
      return latePatch.promise
    }
    if (url.includes(`/api/v1/tasks/${task.id}`)) {
      detailReads += 1
      return detailReads === 1
        ? jsonResponse({ data: detail(task) })
        : detailRefresh.promise
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [task] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = createClient()
  renderSavingDrawer(client)

  await screen.findByDisplayValue(task.title)
  await userEvent.selectOptions(screen.getByLabelText('Priority'), 'P1')

  const newerTask: Task = {
    ...task,
    title: 'Newer cached task',
    priority: 'P3',
    revision: task.revision + 3,
  }
  const newerWorkspace: WorkspaceProjection = { ...workspace, tasks: [newerTask] }
  act(() => {
    client.setQueryData<TaskDetail>(['task', task.id], detail(newerTask))
    client.setQueryData<WorkspaceProjection>(['workspace'], newerWorkspace)
  })

  const lateTask: Task = { ...task, priority: 'P1', revision: task.revision + 1 }
  await act(async () => latePatch.resolve(response({ data: lateTask })))

  await waitFor(() => expect(detailReads).toBe(2))
  expect(patchCalls).toBe(1)
  expect(workspaceReads).toBe(0)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task).toMatchObject({
    title: newerTask.title,
    priority: newerTask.priority,
    revision: newerTask.revision,
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    title: newerTask.title,
    priority: newerTask.priority,
    revision: newerTask.revision,
  })

  await act(async () => detailRefresh.resolve(response({ data: detail(newerTask) })))

  expect(await screen.findByText('Not saved')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry save' })).toBeEnabled()
  expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  expect(patchCalls).toBe(1)
  expect(client.getQueryData<TaskDetail>(['task', task.id])?.task.revision).toBe(newerTask.revision)
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0].revision).toBe(newerTask.revision)
})


const outcomeWorkspace: WorkspaceProjection = {
  ...workspace,
  objectives: [{ ...workspace.objectives[0], key_results: [{ id: 'KR-1', text: 'First outcome' }] }],
}

test('links an outcome through the real drawer and never repeats a confirmed ref PATCH', async () => {
  const aligned: Task = { ...task, objective_ids: ['O-1'] }
  const patches: Array<Record<string, unknown>> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      patches.push(body)
      const refs = body.key_result_refs as Task['key_result_refs']
      return jsonResponse({
        data: {
          ...aligned,
          revision: Number(body.revision) + 1,
          ...(refs ? { key_result_refs: JSON.parse(JSON.stringify(refs)) as Task['key_result_refs'] } : {}),
          ...(body.priority ? { priority: body.priority } : {}),
        },
      })
    }
    return jsonResponse({ data: detail(aligned) })
  }))
  const client = createClient()
  renderSavingDrawer(client, outcomeWorkspace)

  await screen.findByDisplayValue(aligned.title)
  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Add outcome' }),
    screen.getByRole('option', { name: /First outcome/ }),
  )

  await waitFor(() => expect(patches).toHaveLength(1))
  expect(patches[0]).toEqual({
    key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
    revision: aligned.revision,
  })

  await userEvent.selectOptions(screen.getByLabelText('Priority'), 'P1')
  await waitFor(() => expect(patches).toHaveLength(2))
  expect(patches[1]).not.toHaveProperty('key_result_refs')
  expect(patches).toHaveLength(2)
})

test('an unrelated edit on a Task with an omitted field never emits an empty ref list', async () => {
  const patches: Array<Record<string, unknown>> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      patches.push(body)
      return jsonResponse({ data: { ...task, revision: Number(body.revision) + 1, ...body } })
    }
    return jsonResponse({ data: detail(task) })
  }))
  const client = createClient()
  renderSavingDrawer(client, outcomeWorkspace)

  await screen.findByDisplayValue(task.title)
  await userEvent.selectOptions(screen.getByLabelText('Priority'), 'P1')

  await waitFor(() => expect(patches).toHaveLength(1))
  expect(patches[0]).toEqual({ priority: 'P1', revision: task.revision })
})
