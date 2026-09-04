import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { App } from './App'
import { ApiError } from '../api/client'
import type { ReviewProjection, TaskDetail, WorkspaceProjection } from '../domain/types'
import { syncStatusSchema } from '../domain/schemas'
import { decodeCheckpointTransition, decodeWorkspaceChange } from './workspaceChangeEvents'
import { jsonResponse, task, workspace } from '../test/fixtures'

/**
 * The REAL App, the admitted intent hook and the real client helper, with only
 * fetch and EventSource substituted. A mocked api.patchTaskStatusIntent could
 * not establish the route, body, key or receipt handling, so nothing here mocks
 * the client.
 */

const workspaceId = '123e4567-e89b-42d3-a456-426614174000'
const openTask = { ...task, id: 'T-0001', status: 'open', revision: 3 }
const secondTask = { ...openTask, id: 'T-0002', uid: '234e4567-e89b-42d3-a456-426614174001', title: 'Second intent owner' }
const projection = {
  ...workspace,
  tasks: [openTask],
  workspace: { ...workspace.workspace, id: workspaceId },
} as WorkspaceProjection

const committed = {
  event_id: 2, kind: 'agent.checkpoint.committed', workspace_uid: workspaceId,
  task_id: 'T-0001', date: '2026-09-03', checkpoint_id: `CP-${'a'.repeat(64)}`,
  done_count: 1, next_count: 0, blocker_count: 0,
  // Eligibility is NOT restricted to the first checkpoint for a Task.
  first_for_task: false,
  origin: 'agent-cli-v1', replayed: false,
}

const transition = {
  event_id: 3, kind: 'agent.checkpoint.superseded', workspace_uid: workspaceId,
  task_id: 'T-0001', date: '2026-09-03', checkpoint_id: `CP-${'b'.repeat(64)}`,
  ordinal: 0, entry_digest: `sha256:${'d'.repeat(64)}`, state: 'superseded',
  transition_revision: 1, origin: 'agent-cli-v1',
}

const transitionAudit = {
  workspace_uid: workspaceId,
  entries: [{
    locator: { workspace_uid: workspaceId, task_id: openTask.id, date: transition.date, ordinal: 0, entry_digest: transition.entry_digest },
    checkpoint_id: transition.checkpoint_id,
    entry: { done: ['Recorded work'] },
    recorded: { type: 'worklog.recorded', workspace_uid: workspaceId, task_id: openTask.id, checkpoint_id: transition.checkpoint_id, date: transition.date, ordinal: 0, entry_digest: transition.entry_digest, origin: 'agent-cli-v1' },
    state: 'superseded', revision: 1,
    transitions: [{ type: 'worklog.superseded', workspace_uid: workspaceId, task_id: openTask.id, checkpoint_id: transition.checkpoint_id, date: transition.date, ordinal: 0, entry_digest: transition.entry_digest, state: 'superseded', revision: 1, reason: { code: 'incorrect', explanation: 'Corrected source' }, origin: 'agent-cli-v1' }],
  }],
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

interface PatchCall {
  url: string
  method: string
  key: string | null
  credentials?: RequestCredentials
  csrf: string | null
  body: unknown
  rawBody: string
}

function setup(options: {
  taskStatus?: string
  patch?: (call: PatchCall, index: number) => Response | Promise<Response>
  workspaceGate?: () => Promise<Response>
  taskGate?: () => Promise<Response>
  includeSecondTask?: boolean
  syncUnavailable?: boolean
} = {}) {
  const reads = { workspace: 0, task: 0 }
  NoticeSource.instances = []
  window.history.replaceState(null, '', '/?surface=workspace&view=table')
  const patches: PatchCall[] = []
  const posts: string[] = []
  const http: { workspace: WorkspaceProjection; detail: TaskDetail; review: ReviewProjection } = {
    workspace: options.taskStatus
      ? ({ ...projection, tasks: [{ ...openTask, status: options.taskStatus }] } as WorkspaceProjection)
      : options.includeSecondTask ? { ...projection, tasks: [openTask, secondTask] } as WorkspaceProjection : projection,
    detail: {
      task: options.taskStatus ? { ...openTask, status: options.taskStatus } : openTask,
      context: [], activity: [], replies: [],
    } as unknown as TaskDetail,
    review: {
      day: { date: '2026-09-03', start_time: null, entries: [] },
      weekly: { range: { start: '2026-08-28', end: '2026-09-03', days: 7 }, objectives: [], projects: [] },
    } as ReviewProjection,
  }
  vi.stubGlobal('EventSource', NoticeSource)
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const headers = (init?.headers ?? {}) as Record<string, string>
    if (method === 'POST') posts.push(url)
    if (method === 'PATCH') {
      const call: PatchCall = {
        body: init?.body ? JSON.parse(String(init.body)) : null,
        rawBody: String(init?.body),
        credentials: init?.credentials,
        csrf: headers['X-WorkStack-CSRF'] ?? null,
        key: headers['Idempotency-Key'] ?? null,
        method,
        url,
      }
      patches.push(call)
      const responder = options.patch ?? (() => jsonResponse({
        data: { ...http.workspace.tasks[0], status: 'started', revision: 4 },
        meta: { replayed: false },
      }))
      return Promise.resolve(responder(call, patches.length - 1))
    }
    if (url.includes('/api/v1/sync/status') && options.syncUnavailable) return jsonResponse({ error: { code: 'not_found', message: 'Endpoint unavailable' } }, 404)
    if (url.includes('/api/v1/sync/status')) return jsonResponse({ data: {
      state: 'in-sync', workspace_id: workspaceId, candidate_workspace_id: workspaceId,
      generation: 1, manifest_digest: null, changed_files: [], reason: null, rebind_available: false,
    } })
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token' } })
    if (url.includes('/api/v1/workspace')) {
      reads.workspace += 1
      // The initial query and the notice hook's authoritative read pass through;
      // only the conflict reconciliation read is gated.
      if (options.workspaceGate && reads.workspace > 2) return options.workspaceGate()
      return jsonResponse({ data: http.workspace })
    }
    if (url.includes('/api/v1/tasks/T-0001')) {
      reads.task += 1
      if (options.taskGate && reads.task > 1) return options.taskGate()
      return jsonResponse({ data: http.detail })
    }
    if (url.includes('/api/v1/tasks/T-0002')) {
      reads.task += 1
      return jsonResponse({ data: { ...http.detail, task: secondTask } })
    }
    if (url.includes('/api/v1/review/checkpoints')) return jsonResponse({ data: transitionAudit })
    if (url.includes('/api/v1/review')) return jsonResponse({ data: http.review })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>,
  )
  return { ...rendered, client, fetchMock, http, patches, posts, reads }
}

async function raiseCommittedNotice(app: ReturnType<typeof setup>, event: typeof committed | typeof transition = committed) {
  await screen.findByRole('button', { name: 'SSOT in sync' })
  await waitFor(() => expect(app.client.getQueryData(['workspace'])).toBeTruthy())
  const source = NoticeSource.instances.at(-1)!
  act(() => source.emit('workstack.change.v1', event, String(event.event_id)))
  return source
}

test('SI01 a validated committed notice for an OPEN Task offers View and Mark In Progress, and nothing writes on arrival', async () => {
  const app = setup()
  await raiseCommittedNotice(app)

  expect(await screen.findByRole('button', { name: 'Mark In Progress' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'View' })).toBeInTheDocument()
  // Arrival, render and StrictMode replay write nothing.
  expect(app.patches).toHaveLength(0)
  expect(app.posts).toHaveLength(0)
})

test('SI01 a started Task and a transition notice cannot offer the forward action', async () => {
  const started = setup({ taskStatus: 'started' })
  await raiseCommittedNotice(started)
  await screen.findByRole('button', { name: 'View' })
  expect(screen.queryByRole('button', { name: 'Mark In Progress' })).not.toBeInTheDocument()
  expect(started.patches).toHaveLength(0)
  started.unmount()

  const other = setup()
  expect(decodeCheckpointTransition(JSON.stringify(transition), '3')).toEqual(transition)
  await raiseCommittedNotice(other, transition)
  await screen.findByText(`Agent CLI superseded checkpoint ${transition.checkpoint_id} for T-0001 on 2026-09-03.`)
  expect(screen.getByRole('button', { name: 'View' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Mark In Progress' })).not.toBeInTheDocument()
  expect(other.patches).toHaveLength(0)
})

test('SI02 the click sends exactly one keyed PATCH on the real route with the frozen body', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  await waitFor(() => expect(app.patches).toHaveLength(1))
  const call = app.patches[0]
  expect(call.url).toContain('/api/v1/tasks/T-0001')
  expect(call.method).toBe('PATCH')
  expect(call.body).toEqual({ status: 'started', revision: 3 })
  expect(call.key).toBeTruthy()
  expect(call.credentials).toBe('same-origin')
  expect(call.csrf).toBeTruthy()
  // The keyed branch never becomes a POST.
  expect(app.posts).toHaveLength(0)
})

test('SI03 a network loss stays ambiguous and explicit Retry re-sends the identical frozen body and key', async () => {
  const app = setup({
    patch: (_call, index) => index === 0
      ? Promise.reject(new TypeError('Failed to fetch')) as unknown as Promise<Response>
      : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  const retry = await screen.findByRole('button', { name: 'Retry mark in progress' })
  expect(app.patches).toHaveLength(1)

  await userEvent.click(retry)
  await waitFor(() => expect(app.patches).toHaveLength(2))
  // Same frozen body AND the same key: no new key was generated for the retry.
  expect(app.patches[1].body).toEqual(app.patches[0].body)
  expect(app.patches[1].key).toBe(app.patches[0].key)
})

test('SI04 a malformed successful receipt stays ambiguous instead of settling', async () => {
  const app = setup({
    patch: () => jsonResponse({
      // Right envelope, wrong Task: the revision does not answer the request.
      data: { ...openTask, status: 'started', revision: 99 },
      meta: { replayed: false },
    }),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  expect(await screen.findByRole('button', { name: 'Retry mark in progress' })).toBeInTheDocument()
  expect(app.patches).toHaveLength(1)
})

test('SI06 a changing receipt offers Undo, which sends one PATCH with a distinct key at the receipt revision', async () => {
  const app = setup({ patch: (_call, index) => jsonResponse({
    data: { ...openTask, status: index === 0 ? 'started' : 'open', revision: index === 0 ? 4 : 5 },
    meta: { replayed: false },
  }) })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  const undo = await screen.findByRole('button', { name: 'Undo mark in progress' })
  await userEvent.click(undo)

  await waitFor(() => expect(app.patches).toHaveLength(2))
  const [forward, inverse] = app.patches
  expect(inverse.body).toEqual({ status: 'open', revision: 4 })
  expect(inverse.key).not.toBe(forward.key)
  expect(inverse.url).toContain('/api/v1/tasks/T-0001')
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Retry mark in progress' })).not.toBeInTheDocument()
})

test('SI05 an unchanged receipt settles without offering any inverse action', async () => {
  const app = setup({
    patch: () => jsonResponse({
      data: { ...openTask, status: 'started', revision: 3 },
      meta: { replayed: false },
    }),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  await waitFor(() => expect(app.patches).toHaveLength(1))
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Marking in progress…' })).not.toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Retry mark in progress' })).not.toBeInTheDocument()
})

test('SI07 a determinate 409 reconciles for display and sends no second write', async () => {
  const app = setup({
    patch: () => jsonResponse({ error: { code: 'conflict', message: 'Revision conflict.' } }, 409),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  await waitFor(() => expect(app.patches).toHaveLength(1))
  // No automatic second write, and no Undo from a conflict.
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Marking in progress…' })).not.toBeInTheDocument())
  expect(app.patches).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
})


/** The live authoritative workspace snapshot, as an arriving cache update. */
function installWorkspace(app: ReturnType<typeof setup>, patch: Record<string, unknown>) {
  app.client.setQueryData(['workspace'], {
    ...projection,
    tasks: [{ ...openTask, ...patch }],
  })
}

test('GI-F1 a same-batch done, dropped or started Task blocks the displayed Mark button', async () => {
  for (const status of ['done', 'dropped', 'started']) {
    const app = setup()
    await raiseCommittedNotice(app)
    const button = await screen.findByRole('button', { name: 'Mark In Progress' })
    // The authoritative Task changes in the SAME batch as the click.
    await act(async () => {
      installWorkspace(app, { status })
      button.click()
      await Promise.resolve()
    })
    expect(app.patches, `status ${status}`).toHaveLength(0)
    app.unmount()
  }
})

test('GI-F1 a compatible same-UID revision advance stays eligible and freezes the LIVE revision', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const button = await screen.findByRole('button', { name: 'Mark In Progress' })
  await act(async () => {
    installWorkspace(app, { revision: 9 })
    button.click()
    await Promise.resolve()
  })

  await waitFor(() => expect(app.patches).toHaveLength(1))
  expect(app.patches[0].body).toEqual({ status: 'started', revision: 9 })
})

test('GI-F1 a cached detail whose identity disagrees blocks the write', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const button = await screen.findByRole('button', { name: 'Mark In Progress' })
  await act(async () => {
    app.client.setQueryData(['task', 'T-0001'], {
      task: { ...openTask, uid: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' },
      context: [], activity: [], replies: [],
    })
    button.click()
    await allowCompletions()
  })

  expect(app.patches).toHaveLength(0)
})

test('GI-F1 an old receipt cannot open a NEW Undo after the live Task already moved on', async () => {
  let release!: (value: Response | PromiseLike<Response>) => void
  const app = setup({ patch: () => new Promise<Response>((resolve) => { release = resolve }) })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  await waitFor(() => expect(app.patches).toHaveLength(1))

  // The authoritative Task reaches done/12 before the old started/8 answer lands.
  await act(async () => {
    installWorkspace(app, { status: 'done', revision: 12 })
    release(jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: true } }))
    await Promise.resolve()
  })

  expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
})

test('GI-F2 a determinate 409 reads BOTH authoritative snapshots and installs the validated pair once', async () => {
  const app = setup({
    patch: () => jsonResponse({ error: { code: 'conflict', message: 'Revision conflict.' } }, 409),
  })
  await raiseCommittedNotice(app)
  const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
  const workspaceReadsBefore = app.reads.workspace
  const taskReadsBefore = app.reads.task
  // A genuinely different authoritative payload, so an install is observable.
  app.http.workspace = {
    ...projection,
    tasks: [{ ...openTask, revision: 5, title: 'authoritative marker' }],
  } as WorkspaceProjection
  app.http.detail = {
    task: { ...openTask, revision: 5, title: 'authoritative marker' },
    context: [], activity: [], replies: [],
  } as unknown as TaskDetail
  const beforeInstall = snapshotCounters(app)

  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))

  await waitFor(() => expect(app.reads.task).toBeGreaterThan(taskReadsBefore))
  expect(app.reads.workspace).toBeGreaterThan(workspaceReadsBefore)
  await waitFor(() => expect(
    (app.client.getQueryData(['workspace']) as WorkspaceProjection).tasks[0].title,
  ).toBe('authoritative marker'))
  expect((app.client.getQueryData(['task', 'T-0001']) as TaskDetail).task.revision).toBe(5)
  expect(snapshotCounters(app)).toEqual(beforeInstall.map((count) => count! + 1))
  // Display only: no second mutation.
  expect(app.patches).toHaveLength(1)
})

test('GI-F2 a newer arrival during the conflict fetch survives, with no rollback and no extra install', async () => {
  let releaseWorkspace!: (value: Response | PromiseLike<Response>) => void
  const app = setup({
    patch: () => jsonResponse({ error: { code: 'conflict', message: 'Revision conflict.' } }, 409),
    workspaceGate: () => new Promise<Response>((resolve) => { releaseWorkspace = resolve }),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  await waitFor(() => expect(releaseWorkspace).toBeTruthy())

  // A newer authoritative snapshot arrives while the conflict read is in flight.
  app.client.setQueryData(['workspace'], {
    ...projection,
    tasks: [{ ...openTask, revision: 11, title: 'new cache survives' }],
  })
  const updatesAfterArrival = app.client.getQueryCache()
    .find({ exact: true, queryKey: ['workspace'] })!.state.dataUpdateCount

  await act(async () => {
    releaseWorkspace(jsonResponse({ data: { ...projection, tasks: [{ ...openTask, revision: 7, title: 'stale conflict response' }] } }))
    await Promise.resolve()
    await Promise.resolve()
  })

  const live = app.client.getQueryData(['workspace']) as WorkspaceProjection
  expect(live.tasks[0].revision).toBe(11)
  expect(live.tasks[0].title).toBe('new cache survives')
  expect(app.client.getQueryCache().find({ exact: true, queryKey: ['workspace'] })!.state.dataUpdateCount)
    .toBe(updatesAfterArrival)
})

test('GI-F3 a same-batch Task UID A to B to A permanently disposes the captured capability', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const button = await screen.findByRole('button', { name: 'Mark In Progress' })

  // No React render between the two updates.
  await act(async () => {
    installWorkspace(app, { uid: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' })
    installWorkspace(app, {})
    button.click()
    await Promise.resolve()
  })

  expect(app.patches).toHaveLength(0)
})

test('GI-F4 an informational second checkpoint for the same Task cannot take over an unresolved intent', async () => {
  const app = setup({
    patch: (_call, index) => index === 0
      ? Promise.reject(new TypeError('Failed to fetch')) as unknown as Promise<Response>
      : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }),
  })
  const source = await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  const retry = await screen.findByRole('button', { name: 'Retry mark in progress' })

  // A second, purely informational checkpoint for the SAME Task and owner.
  const replacement = { ...committed, checkpoint_id: `CP-${'c'.repeat(64)}`, event_id: 4, done_count: 4 }
  expect(decodeWorkspaceChange(JSON.stringify(replacement), '4')).toEqual(replacement)
  act(() => source.emit('workstack.change.v1', replacement, '4'))
  await screen.findByText('Agent CLI recorded a checkpoint for T-0001 on 2026-09-03: 4 done, 0 next, 0 blockers.')

  expect(screen.getByRole('button', { name: 'Retry mark in progress' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Mark In Progress' })).not.toBeInTheDocument()

  await userEvent.click(retry)
  await waitFor(() => expect(app.patches).toHaveLength(2))
  // The original immutable intent, not a new operation.
  expect(app.patches[1].key).toBe(app.patches[0].key)
  expect(app.patches[1].body).toEqual(app.patches[0].body)
  expect(app.patches[1].rawBody).toBe(app.patches[0].rawBody)
})

test('GI-F4 the ordinary presentation timeout cannot reset an unresolved intent into a new key', async () => {
  const app = setup({
    patch: (_call, index) => index === 0
      ? Promise.reject(new TypeError('Failed to fetch')) as unknown as Promise<Response>
      : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }),
  })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  await screen.findByRole('button', { name: 'Retry mark in progress' })

  // Past the ordinary 3200 ms dismissal.
  await new Promise((resolve) => setTimeout(resolve, 3300))

  const retry = screen.getByRole('button', { name: 'Retry mark in progress' })
  await userEvent.click(retry)
  await waitFor(() => expect(app.patches).toHaveLength(2))
  expect(app.patches[1].key).toBe(app.patches[0].key)
})

function controlledResponse() {
  let resolve!: (response: Response | PromiseLike<Response>) => void
  let reject!: (error: Error) => void
  const response = new Promise<Response>((accept, refuse) => { resolve = accept; reject = refuse })
  return { response, resolve, reject }
}

const allowCompletions = () => new Promise<void>((resolve) => setTimeout(resolve, 20))

function snapshotCounters(app: ReturnType<typeof setup>) {
  return [['workspace'], ['task', openTask.id]].map((queryKey) =>
    app.client.getQueryCache().find({ queryKey, exact: true })?.state.dataUpdateCount)
}

async function displaySecondOwner(source: NoticeSource) {
  const event = { ...committed, task_id: secondTask.id, checkpoint_id: `CP-${'e'.repeat(64)}`, event_id: 9, done_count: 9 }
  expect(decodeWorkspaceChange(JSON.stringify(event), '9')).toEqual(event)
  act(() => source.emit('workstack.change.v1', event, '9'))
  await screen.findByText('Agent CLI recorded a checkpoint for T-0002 on 2026-09-03: 9 done, 0 next, 0 blockers.')
}

test('GIA-F1 the captured toast action reads a sync blockade before React can render it', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
  const sync = app.client.getQueryData<Record<string, unknown>>(['sync-status'])!
  await act(async () => {
    app.client.setQueryData(['sync-status'], { ...sync, state: 'invalid', reason: 'Invalid external document' })
    mark.click()
    await allowCompletions()
  })
  expect(app.patches).toHaveLength(0)
  expect(document.querySelector('.app-shell')).toHaveClass('is-write-blocked')
})

test('GIA-F2 an inactive detail already ahead of workspace refuses the whole conflict installation', async () => {
  const app = setup({ patch: () => jsonResponse({ error: { code: 'revision_conflict', message: 'Stale revision' } }, 409) })
  await raiseCommittedNotice(app)
  const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
  act(() => {
    installWorkspace(app, { revision: 7 })
    app.client.setQueryData(['task', openTask.id], { ...app.http.detail, task: { ...openTask, revision: 12, title: 'Already at twelve' } })
  })
  const counters = snapshotCounters(app)
  const detailBefore = app.client.getQueryData<TaskDetail>(['task', openTask.id])
  app.http.workspace = { ...projection, tasks: [{ ...openTask, revision: 9, title: 'Fetched nine' }] } as WorkspaceProjection
  app.http.detail = { ...app.http.detail, task: { ...openTask, revision: 9, title: 'Fetched nine' } } as TaskDetail
  await act(async () => { mark.click(); await allowCompletions() })
  expect(app.patches).toHaveLength(1)
  expect(app.reads.task).toBe(2)
  expect(app.client.getQueryData(['task', openTask.id])).toBe(detailBefore)
  expect(snapshotCounters(app)).toEqual(counters)
})

test.each(['success', 'conflict', 'malformed', 'loss'] as const)(
  'GIA-F3 distinct B owns its pending slot before late A %s', async (outcome) => {
    const first = controlledResponse()
    const second = controlledResponse()
    const app = setup({ includeSecondTask: true, patch: (_call, index) => index === 0 ? first.response : second.response })
    const source = await raiseCommittedNotice(app)
    await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
    expect(app.patches).toHaveLength(1)
    await displaySecondOwner(source)
    const markSecond = await screen.findByRole('button', { name: 'Mark In Progress' })
    await userEvent.click(markSecond)
    expect(app.patches).toHaveLength(2)
    expect(app.patches[1].url).toMatch(/\/tasks\/T-0002$/)
    expect(screen.getByText('Marking in progress…')).toBeInTheDocument()
    const beforeReads = { ...app.reads }
    const beforeCounters = snapshotCounters(app)
    await act(async () => {
      if (outcome === 'loss') first.reject(new TypeError('Lost A response'))
      else if (outcome === 'conflict') first.resolve(jsonResponse({ error: { code: 'revision_conflict', message: 'A changed' } }, 409))
      else first.resolve(jsonResponse(outcome === 'success'
        ? { data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }
        : { data: { id: openTask.id }, meta: { replayed: false } }))
      await allowCompletions()
    })
    expect(screen.getByText('Marking in progress…')).toBeInTheDocument()
    expect(app.reads).toEqual(beforeReads)
    expect(snapshotCounters(app)).toEqual(beforeCounters)
    await act(async () => { markSecond.click(); await allowCompletions() })
    expect(app.patches).toHaveLength(2)
    await act(async () => {
      second.resolve(jsonResponse({ data: { ...secondTask, status: 'started', revision: 4 }, meta: { replayed: false } }))
      await allowCompletions()
    })
    expect(await screen.findByRole('button', { name: 'Undo mark in progress' })).toBeInTheDocument()
  },
)

test('GIA-F3 an unknown A yields fresh eligible B rather than an A Retry beneath B text', async () => {
  const app = setup({ includeSecondTask: true, patch: (_call, index) => index === 0
    ? Promise.reject(new TypeError('A may have committed'))
    : jsonResponse({ data: { ...secondTask, status: 'started', revision: 4 }, meta: { replayed: false } }) })
  const source = await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  await screen.findByRole('button', { name: 'Retry mark in progress' })
  await displaySecondOwner(source)
  expect(screen.queryByRole('button', { name: 'Retry mark in progress' })).not.toBeInTheDocument()
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  expect(app.patches).toHaveLength(2)
  expect(app.patches[1].url).toMatch(/\/tasks\/T-0002$/)
  expect(app.patches[1].key).not.toBe(app.patches[0].key)
})

test('live sync query errors refuse immediately while legacy404 remains available', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
  await act(async () => {
    app.client.getQueryCache().find({ queryKey: ['sync-status'], exact: true })!
      .setState({ error: new ApiError(500, 'unavailable', 'Sync failed'), status: 'error' })
    mark.click()
    await allowCompletions()
  })
  expect(app.patches).toHaveLength(0)
  app.unmount()

  const legacy = setup({ syncUnavailable: true })
  await waitFor(() => expect(legacy.client.getQueryState(['sync-status'])?.status).toBe('error'))
  await waitFor(() => expect(legacy.client.getQueryData(['workspace'])).toBeTruthy())
  act(() => NoticeSource.instances.at(-1)!.emit('workstack.change.v1', committed, '2'))
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  expect(legacy.patches).toHaveLength(1)
})

test('rendered blocked authority and a same-batch wrong detail ID both refuse', async () => {
  const app = setup()
  await raiseCommittedNotice(app)
  const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
  await act(async () => {
    app.client.setQueryData(['task', openTask.id], { ...app.http.detail, task: { ...openTask, id: secondTask.id } })
    mark.click()
    await allowCompletions()
  })
  expect(app.patches).toHaveLength(0)
  act(() => {
    app.client.setQueryData(['task', openTask.id], app.http.detail)
    app.client.setQueryData(['sync-status'], { ...app.client.getQueryData<object>(['sync-status']), state: 'invalid' })
  })
  await waitFor(() => expect(document.querySelector('.app-shell')).toHaveClass('is-write-blocked'))
  await userEvent.click(mark)
  expect(app.patches).toHaveLength(0)
})

test('a sync blockade arriving with a receipt cannot create a new Undo', async () => {
  const pending = controlledResponse()
  const app = setup({ patch: () => pending.response })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  await act(async () => {
    app.client.setQueryData(['sync-status'], { ...app.client.getQueryData<object>(['sync-status']), state: 'invalid' })
    pending.resolve(jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }))
    await allowCompletions()
  })
  expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
  expect(app.patches).toHaveLength(1)
})

test.each(['forbidden', 'unreadable', 'wrapped', 'missing-meta', 'nested', 'network'] as const)(
  'SI03/04 %s possible-send ambiguity publishes nothing and retries the exact body/key once', async (mode) => {
    const app = setup({ patch: (_call, index) => {
      if (index > 0) return jsonResponse({ data: { ...openTask, status: 'started', revision: 4, legacy: { preserved: true } }, meta: { replayed: true, future: 'allowed' } })
      if (mode === 'network') return Promise.reject(new TypeError('Response lost'))
      if (mode === 'forbidden') return jsonResponse({ error: { code: 'forbidden', message: 'Denied' } }, 403)
      if (mode === 'unreadable') return new Response('not json', { status: 200 })
      if (mode === 'wrapped') return jsonResponse({ data: { task: { ...openTask, status: 'started', revision: 4 } }, meta: { replayed: false } })
      if (mode === 'missing-meta') return jsonResponse({ data: { ...openTask, status: 'started', revision: 4 } })
      return jsonResponse({ data: { ...openTask, status: 'started', revision: 4, subtasks: [{ id: 'S1', title: 'Bad enum', status: 'invalid' }] }, meta: { replayed: false } })
    } })
    await raiseCommittedNotice(app)
    const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
    const reads = { ...app.reads }
    const counters = snapshotCounters(app)
    await userEvent.click(mark)
    const retry = await screen.findByRole('button', { name: 'Retry mark in progress' })
    expect(app.patches).toHaveLength(1)
    expect(app.reads).toEqual(reads)
    expect(snapshotCounters(app)).toEqual(counters)
    await userEvent.click(retry)
    await screen.findByRole('button', { name: 'Undo mark in progress' })
    expect(app.patches).toHaveLength(2)
    expect(app.patches[1].rawBody).toBe(app.patches[0].rawBody)
    expect(app.patches[1].key).toBe(app.patches[0].key)
    expect(app.posts).toEqual([])
  },
)

test.each(['same-revision-marker', 'detail-newer', 'detail-recreated', 'workspace-recreated', 'wrong-detail', 'wrong-workspace', 'pair-revision', 'unmount'] as const)(
  'SI08 the whole conflict pair stays uninstalled after %s', async (change) => {
    const pendingWorkspace = controlledResponse()
    const app = setup({
      patch: () => jsonResponse({ error: { code: 'revision_conflict', message: 'Conflict' } }, 409),
      workspaceGate: () => pendingWorkspace.response,
    })
    await raiseCommittedNotice(app)
    app.http.detail = { ...app.http.detail, task: { ...openTask, revision: 5, title: 'Fresh response' } } as TaskDetail
    if (change === 'wrong-detail') app.http.detail.task.uid = secondTask.uid
    if (change === 'pair-revision') app.http.detail.task.revision = 6
    await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
    await waitFor(() => expect(app.reads.task).toBe(2))
    act(() => {
      if (change === 'same-revision-marker') installWorkspace(app, { title: 'New cache marker' })
      if (change === 'detail-newer') app.client.setQueryData(['task', openTask.id], { ...app.http.detail, task: { ...openTask, revision: 12 } })
      if (change === 'detail-recreated' || change === 'workspace-recreated') {
        const queryKey = change === 'detail-recreated' ? ['task', openTask.id] : ['workspace']
        const data = app.client.getQueryData(queryKey)
        app.client.removeQueries({ queryKey, exact: true })
        app.client.setQueryData(queryKey, data)
      }
    })
    const counters = snapshotCounters(app)
    if (change === 'unmount') app.unmount()
    await act(async () => {
      pendingWorkspace.resolve(jsonResponse({ data: {
        ...projection,
        workspace: { ...projection.workspace, id: change === 'wrong-workspace' ? secondTask.uid : workspaceId },
        tasks: [{ ...openTask, revision: 5, title: 'Fresh response' }],
      } }))
      await allowCompletions()
    })
    expect(snapshotCounters(app)).toEqual(counters)
    expect(app.patches).toHaveLength(1)
  },
)

test.each(['success', 'conflict', 'malformed', 'loss'] as const)(
  'SI11 unmount silences an in-flight %s completion', async (mode) => {
    const pending = controlledResponse()
    const app = setup({ patch: () => pending.response })
    await raiseCommittedNotice(app)
    await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
    const reads = { ...app.reads }
    const counters = snapshotCounters(app)
    app.unmount()
    await act(async () => {
      if (mode === 'loss') pending.reject(new TypeError('Response gone'))
      else pending.resolve(jsonResponse(mode === 'success'
        ? { data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }
        : mode === 'conflict' ? { error: { code: 'revision_conflict', message: 'Conflict' } } : { data: {} }, mode === 'conflict' ? 409 : 200))
      await allowCompletions()
    })
    expect(app.reads).toEqual(reads)
    expect(snapshotCounters(app)).toEqual(counters)
    expect(app.patches).toHaveLength(1)
  },
)

test.each(['retry', 'undo'] as const)('SI10 same-batch Task identity replacement invalidates the captured %s', async (action) => {
  const app = setup({ patch: () => action === 'retry'
    ? Promise.reject(new TypeError('Unknown'))
    : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }) })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  const captured = await screen.findByRole('button', { name: action === 'retry' ? 'Retry mark in progress' : 'Undo mark in progress' })
  await act(async () => {
    installWorkspace(app, { uid: secondTask.uid })
    installWorkspace(app, {})
    captured.click()
    await allowCompletions()
  })
  expect(app.patches).toHaveLength(1)
})

test('SI07 an already-owned Undo keeps its captured revision after later compatible edits', async () => {
  const app = setup({ patch: (_call, index) => index === 0
    ? jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } })
    : jsonResponse({ error: { code: 'revision_conflict', message: 'Intervening edit' } }, 409) })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  const undo = await screen.findByRole('button', { name: 'Undo mark in progress' })
  act(() => installWorkspace(app, { revision: 12, status: 'done' }))
  await userEvent.click(undo)
  expect(app.patches[1].body).toEqual({ status: 'open', revision: 4 })
  expect(app.patches[1].key).not.toBe(app.patches[0].key)
  expect(app.patches).toHaveLength(2)
})

test.each((['captured-mark', 'pending', 'retry', 'undo'] as const).flatMap((phase) =>
  (['rendered', 'same-batch'] as const).map((timing) => ({ phase, timing })),
))(
  'SI10 a $timing workspace away-and-back cannot revive the old $phase owner', async ({ phase, timing }) => {
    const pending = controlledResponse()
    const app = setup({ patch: () => phase === 'pending' ? pending.response
      : phase === 'retry' ? Promise.reject(new TypeError('Unknown before workspace switch'))
      : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: false } }) })
    await raiseCommittedNotice(app)
    let action = await screen.findByRole('button', { name: 'Mark In Progress' })
    if (phase !== 'captured-mark') {
      await userEvent.click(action)
      if (phase !== 'pending') action = await screen.findByRole('button', { name: phase === 'retry' ? 'Retry mark in progress' : 'Undo mark in progress' })
    }
    const original = app.client.getQueryData<WorkspaceProjection>(['workspace'])!
    const before = app.patches.length
    const reads = { ...app.reads }
    const away = { ...original, workspace: { ...original.workspace, id: secondTask.uid } }
    if (timing === 'rendered') {
      // Separate acts permit real committed renders; no rerender helper is used.
      await act(async () => { app.client.setQueryData(['workspace'], away); await allowCompletions() })
      await act(async () => { app.client.setQueryData(['workspace'], original); await allowCompletions() })
    }
    await act(async () => {
      if (timing === 'same-batch') {
        app.client.setQueryData(['workspace'], away)
        app.client.setQueryData(['workspace'], original)
      }
      if (phase === 'pending') pending.resolve(jsonResponse({ error: { code: 'revision_conflict', message: 'Old conflict' } }, 409))
      else action.click()
      await allowCompletions()
    })
    expect(app.patches).toHaveLength(before)
    expect(app.reads).toEqual(reads)
    expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
  },
)

test('SI10 same-batch Task UID away-and-back silences an already-pending conflict', async () => {
  const pending = controlledResponse()
  const app = setup({ patch: () => pending.response })
  await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  const reads = { ...app.reads }
  await act(async () => {
    installWorkspace(app, { uid: secondTask.uid })
    installWorkspace(app, {})
    pending.resolve(jsonResponse({ error: { code: 'revision_conflict', message: 'Old Task' } }, 409))
    await allowCompletions()
  })
  expect(app.patches).toHaveLength(1)
  expect(app.reads).toEqual(reads)
  expect(screen.queryByRole('button', { name: 'Retry mark in progress' })).not.toBeInTheDocument()
})

test('SI11 same-Task information preserves a pending attempt through loss and explicit retry', async () => {
  const pending = controlledResponse()
  const app = setup({ patch: (_call, index) => index === 0 ? pending.response
    : jsonResponse({ data: { ...openTask, status: 'started', revision: 4 }, meta: { replayed: true } }) })
  const source = await raiseCommittedNotice(app)
  await userEvent.click(await screen.findByRole('button', { name: 'Mark In Progress' }))
  const replacement = { ...committed, checkpoint_id: `CP-${'f'.repeat(64)}`, event_id: 8, done_count: 8 }
  act(() => source.emit('workstack.change.v1', replacement, '8'))
  await screen.findByText('Agent CLI recorded a checkpoint for T-0001 on 2026-09-03: 8 done, 0 next, 0 blockers.')
  expect(screen.getByText('Marking in progress…')).toBeInTheDocument()
  await act(async () => { pending.reject(new TypeError('Original response lost')); await allowCompletions() })
  await userEvent.click(await screen.findByRole('button', { name: 'Retry mark in progress' }))
  expect(app.patches).toHaveLength(2)
  expect(app.patches[1].rawBody).toBe(app.patches[0].rawBody)
  expect(app.patches[1].key).toBe(app.patches[0].key)
})

test.each(['blocked', 'sync-error', 'in-sync', 'unavailable404'] as const)(
  'GIT-F1 delayed conflict installation rechecks current %s authority', async (authority) => {
    const workspaceResponse = controlledResponse()
    const detailResponse = controlledResponse()
    const app = setup({
      patch: () => jsonResponse({ error: { code: 'revision_conflict', message: 'Changed on owner' } }, 409),
      workspaceGate: () => workspaceResponse.response,
      taskGate: () => detailResponse.response,
    })
    expect(decodeWorkspaceChange(JSON.stringify(committed), '2')).toEqual(committed)
    await raiseCommittedNotice(app)
    const mark = await screen.findByRole('button', { name: 'Mark In Progress' })
    act(() => {
      installWorkspace(app, { revision: 7, title: 'Captured seven' })
      app.client.setQueryData(['task', openTask.id], {
        ...app.http.detail, task: { ...openTask, revision: 7, title: 'Captured seven' },
      })
    })
    const beforeWorkspace = app.client.getQueryData<WorkspaceProjection>(['workspace'])!
    const beforeDetail = app.client.getQueryData<TaskDetail>(['task', openTask.id])!
    const counters = snapshotCounters(app)
    await act(async () => { mark.click(); await allowCompletions() })
    await waitFor(() => expect(app.reads).toEqual({ workspace: 3, task: 2 }))
    expect(app.patches).toHaveLength(1)
    expect(app.patches[0].body).toEqual({ status: 'started', revision: 7 })
    expect(snapshotCounters(app)).toEqual(counters)
    const syncQuery = app.client.getQueryCache().find({ queryKey: ['sync-status'], exact: true })!
    act(() => {
      if (authority === 'blocked' || authority === 'in-sync') {
        const sync = syncStatusSchema.parse({
          ...syncStatusSchema.parse(syncQuery.state.data),
          state: authority === 'blocked' ? 'invalid' : 'in-sync',
          reason: authority === 'blocked' ? 'Invalid external document' : null,
        })
        app.client.setQueryData(['sync-status'], sync)
      } else {
        // This changes the actual cached error while BOTH display GETs are held.
        syncQuery.setState({
          data: authority === 'unavailable404' ? undefined : syncQuery.state.data,
          error: new ApiError(authority === 'unavailable404' ? 404 : 500, 'unavailable', 'Sync endpoint unavailable'),
          status: 'error',
        })
      }
    })
    const freshTask = { ...openTask, revision: 9, title: 'Authoritative nine after conflict' }
    await act(async () => {
      workspaceResponse.resolve(jsonResponse({ data: { ...projection, tasks: [freshTask] } }))
      await allowCompletions()
    })
    // Returning one member cannot partially install even on the healthy path.
    expect(snapshotCounters(app)).toEqual(counters)
    expect(app.client.getQueryData(['workspace'])).toBe(beforeWorkspace)
    expect(app.client.getQueryData(['task', openTask.id])).toBe(beforeDetail)
    await act(async () => {
      detailResponse.resolve(jsonResponse({ data: { ...app.http.detail, task: freshTask } }))
      await allowCompletions()
    })
    const allowed = authority === 'in-sync' || authority === 'unavailable404'
    if (allowed) {
      expect(snapshotCounters(app)).toEqual(counters.map((value) => value! + 1))
      const installedWorkspace = app.client.getQueryData<WorkspaceProjection>(['workspace'])!
      const installedDetail = app.client.getQueryData<TaskDetail>(['task', openTask.id])!
      expect(installedWorkspace).not.toBe(beforeWorkspace)
      expect(installedDetail).not.toBe(beforeDetail)
      expect(installedWorkspace.tasks[0]).toMatchObject(freshTask)
      expect(installedDetail.task).toMatchObject(freshTask)
    } else {
      expect(snapshotCounters(app)).toEqual(counters)
      expect(app.client.getQueryData(['workspace'])).toBe(beforeWorkspace)
      expect(app.client.getQueryData(['task', openTask.id])).toBe(beforeDetail)
    }
    expect(app.patches).toHaveLength(1)
    expect(app.reads).toEqual({ workspace: 3, task: 2 })
    expect(app.posts).toEqual([])
    expect(screen.queryByRole('button', { name: 'Undo mark in progress' })).not.toBeInTheDocument()
  },
)
