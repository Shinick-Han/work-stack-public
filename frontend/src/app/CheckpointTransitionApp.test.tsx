import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { App } from './App'
import type { CheckpointAudit, CheckpointTransitionEventRecord, ReviewProjection, TaskDetail, WorkspaceProjection } from '../domain/types'
import { addCalendarDays, localIsoDate } from '../features/focus/focusModel'
import { task, workspace } from '../test/fixtures'

/**
 * Integration coverage for the D5 transition slice through the real App,
 * DailyReviewPage, CheckpointHistory, notices hook and API client. Only the
 * HTTP interface and the event stream are substituted.
 */

const workspaceId = '123e4567-e89b-42d3-a456-426614174000'
const CP = `CP-${'a'.repeat(64)}`
const DIGEST = `sha256:${'b'.repeat(64)}`
// The App resolves the review day from the browser's local calendar date
// (useLocalToday -> localIsoDate), so the fixture day must be that same date.
// A literal here silently stopped matching after local midnight.
const DATE = localIsoDate()
const WEEK_START = addCalendarDays(DATE, -6)
const projection = { ...workspace, workspace: { ...workspace.workspace, id: workspaceId } }
const syncStatus = {
  state: 'in-sync', workspace_id: workspaceId, candidate_workspace_id: workspaceId,
  generation: 1, manifest_digest: null, changed_files: [], reason: null, rebind_available: false,
}

function transitionRecord(revision: number): CheckpointTransitionEventRecord {
  return {
    type: revision % 2 === 1 ? 'worklog.superseded' : 'worklog.restored',
    workspace_uid: workspaceId,
    task_id: 'T-0001',
    checkpoint_id: CP,
    date: DATE,
    ordinal: 0,
    entry_digest: DIGEST,
    state: revision % 2 === 1 ? 'superseded' : 'active',
    revision,
    reason: { code: revision % 2 === 1 ? 'incorrect' : 'restore', explanation: 'spaced' },
    origin: null,
  }
}

function auditAt(revision: number, overrides: Partial<CheckpointAudit['entries'][number]> = {}): CheckpointAudit {
  return {
    workspace_uid: workspaceId,
    entries: [
      {
        locator: {
          workspace_uid: workspaceId,
          task_id: 'T-0001',
          date: DATE,
          ordinal: 0,
          entry_digest: DIGEST,
        },
        checkpoint_id: CP,
        entry: { done: ['shipped'] },
        recorded: {
          type: 'worklog.recorded',
          workspace_uid: workspaceId,
          task_id: 'T-0001',
          checkpoint_id: CP,
          date: DATE,
          ordinal: 0,
          entry_digest: DIGEST,
          origin: 'agent-cli-v1',
        },
        state: revision % 2 === 1 ? 'superseded' : 'active',
        revision,
        transitions: Array.from({ length: revision }, (_, index) => transitionRecord(index + 1)),
        ...overrides,
      },
    ],
  }
}

function transitionNotice(revision: number, eventId = revision) {
  return {
    event_id: eventId,
    kind: revision % 2 === 1 ? 'agent.checkpoint.superseded' : 'agent.checkpoint.restored',
    workspace_uid: workspaceId,
    task_id: 'T-0001',
    date: DATE,
    checkpoint_id: CP,
    ordinal: 0,
    entry_digest: DIGEST,
    state: revision % 2 === 1 ? 'superseded' : 'active',
    transition_revision: revision,
    origin: 'agent-cli-v1',
  }
}

class Stream {
  static instances: Stream[] = []
  listeners = new Map<string, EventListener>()
  closed = false
  constructor() { Stream.instances.push(this) }
  addEventListener(name: string, listener: EventListener) { this.listeners.set(name, listener) }
  removeEventListener() { /* not used by the subject */ }
  close() { this.closed = true }
  emit(name: string, payload: unknown, id: string) {
    this.listeners.get(name)?.({ data: JSON.stringify(payload), lastEventId: id } as MessageEvent)
  }
}

interface Plan {
  audits: CheckpointAudit[]
  /** Holds the second audit read open so a newer cache can land meanwhile. */
  auditGate?: Promise<void>
  transition?: (attempt: number, body: unknown, key: string) => Response
  tasksPresent?: boolean
  /** Lets a case return a Task whose identity disagrees with the workspace. */
  detailTask?: typeof task
  /** Distinct authoritative payloads, so a real write is always observable. */
  freshMarkers?: boolean
}

function receipt(revision: number, status = 201, replayed = false) {
  return jsonResponseOf({ data: transitionRecord(revision), meta: { replayed } }, status)
}

function jsonResponseOf(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

function setup(plan: Plan) {
  Stream.instances = []
  window.history.replaceState(null, '', '/?surface=review')
  const state = {
    auditIndex: 0,
    transitionAttempts: 0,
    bodies: [] as string[],
    keys: [] as string[],
    auditReads: 0,
  }
  const review: ReviewProjection = {
    day: { date: DATE, start_time: plan.freshMarkers ? '09:30' : null, entries: [] },
    weekly: { range: { start: WEEK_START, end: DATE, days: 7 }, objectives: [], projects: [] },
  }
  const detail: TaskDetail = { task: plan.detailTask ?? task, context: [], activity: [], replies: [] }
  const marked: WorkspaceProjection = plan.freshMarkers
    ? { ...projection, workspace: { ...projection.workspace, name: 'Fresh authoritative name' } }
    : projection
  const workspaceBody: WorkspaceProjection = plan.tasksPresent === false
    ? { ...marked, tasks: [] }
    : marked

  vi.stubGlobal('EventSource', Stream)
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponseOf({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (url.includes('/api/v1/review/checkpoints') && url.includes('/transitions')) {
      state.transitionAttempts += 1
      state.bodies.push(String(init?.body ?? ''))
      state.keys.push(String(new Headers(init?.headers).get('Idempotency-Key') ?? ''))
      const responder = plan.transition
        ?? ((attempt: number) => receipt(attempt))
      return responder(state.transitionAttempts, init?.body, state.keys.at(-1) ?? '')
    }
    if (url.endsWith('/api/v1/review/checkpoints')) {
      const index = Math.min(state.auditIndex, plan.audits.length - 1)
      state.auditReads += 1
      if (plan.auditGate && state.auditReads === 2) await plan.auditGate
      const audit = plan.audits[index]
      if (state.auditIndex < plan.audits.length - 1) state.auditIndex += 1
      return jsonResponseOf({ data: audit })
    }
    if (url.includes('/api/v1/sync/status')) return jsonResponseOf({ data: syncStatus })
    if (url.includes('/api/v1/review')) return jsonResponseOf({ data: review })
    if (url.includes('/api/v1/workspace')) return jsonResponseOf({ data: workspaceBody })
    if (url.includes('/api/v1/tasks/')) return jsonResponseOf({ data: detail })
    if (url.includes('/api/v1/captures')) return jsonResponseOf({ data: [] })
    return jsonResponseOf({ data: {} })
  })
  vi.stubGlobal('fetch', fetchMock)

  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const view = render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
  return { ...view, state, client }
}

async function confirmSupersede(user: ReturnType<typeof userEvent.setup>, explanation = '  spaced  ') {
  await user.click(await screen.findByRole('button', { name: `Supersede ${CP}` }))
  await user.type(screen.getByLabelText('Explanation'), explanation)
  await user.click(screen.getByRole('button', { name: 'Confirm supersede' }))
}

test('mounts the whole validated audit in Daily Review before day filtering', async () => {
  setup({ audits: [auditAt(0)] })
  const history = await screen.findByRole('region', { name: 'Checkpoint history' })
  expect(within(history).getByText(CP)).toBeVisible()
})

test('explicit repeated Supersede and Restore cycles 0 to 4 each send one POST', async () => {
  const user = userEvent.setup()
  const audits = [auditAt(0), auditAt(1), auditAt(2), auditAt(3), auditAt(4)]
  const { state } = setup({ audits })

  await screen.findByRole('region', { name: 'Checkpoint history' })
  for (let cycle = 1; cycle <= 4; cycle += 1) {
    const superseded = cycle % 2 === 0
    const label = superseded ? `Restore ${CP}` : `Supersede ${CP}`
    await waitFor(async () => { await screen.findByRole('button', { name: label }) })
    await user.click(screen.getByRole('button', { name: label }))
    await user.type(screen.getByLabelText('Explanation'), 'cycle')
    await user.click(screen.getByRole('button', {
      name: superseded ? 'Confirm restore' : 'Confirm supersede',
    }))
    await waitFor(() => expect(state.transitionAttempts).toBe(cycle))
  }
  // Four explicit submits, four POSTs, four distinct keys.
  expect(state.transitionAttempts).toBe(4)
  expect(new Set(state.keys).size).toBe(4)
})

test('sends the verbatim explanation and one key per explicit submit', async () => {
  const user = userEvent.setup()
  const { state } = setup({ audits: [auditAt(0), auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  await confirmSupersede(user)

  await waitFor(() => expect(state.transitionAttempts).toBe(1))
  const body = JSON.parse(state.bodies[0])
  expect(body.reason.explanation).toBe('  spaced  ')
  expect(body.revision).toBe(0)
  expect(state.keys[0]).toMatch(/^workstack:/)
})

test('a 409 refusal is shown and no second POST is sent', async () => {
  const user = userEvent.setup()
  const { state } = setup({
    audits: [auditAt(0), auditAt(1)],
    transition: () => jsonResponseOf({
      error: {
        code: 'checkpoint_transition_conflict',
        message: 'invalid checkpoint transition input',
        details: { transition_code: 'stale_revision' },
      },
    }, 409),
  })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  await confirmSupersede(user)

  await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  expect(state.transitionAttempts).toBe(1)
  // No retry control is offered for a determinate refusal.
  expect(screen.queryByRole('button', { name: 'Retry the same request' })).toBeNull()
})

test('ambiguity offers an explicit retry that reuses the same body and key', async () => {
  const user = userEvent.setup()
  const { state } = setup({
    audits: [auditAt(0)],
    transition: () => jsonResponseOf({ data: { wrong: 'shape' } }, 201),
  })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  await confirmSupersede(user)

  const retry = await screen.findByRole('button', { name: 'Retry the same request' })
  expect(state.transitionAttempts).toBe(1)

  await user.click(retry)
  await waitFor(() => expect(state.transitionAttempts).toBe(2))
  // Same frozen snapshot: identical body and identical key, never a rebase.
  expect(state.bodies[1]).toBe(state.bodies[0])
  expect(state.keys[1]).toBe(state.keys[0])
})

test('a missing current Task keeps its audit row and reasons readable', async () => {
  setup({ audits: [auditAt(1)], tasksPresent: false })
  const history = await screen.findByRole('region', { name: 'Checkpoint history' })
  expect(within(history).getByText(CP)).toBeVisible()
  expect(within(history).getByText(/Wrong day|spaced/)).toBeTruthy()
})

test('an opaque legacy row stays readable with no compensation control', async () => {
  const legacy: CheckpointAudit = {
    workspace_uid: workspaceId,
    entries: [{
      locator: { workspace_uid: workspaceId, task_id: null, date: DATE, ordinal: 0, entry_digest: null },
      checkpoint_id: null,
      entry: { free: 'text' },
      recorded: null,
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  }
  setup({ audits: [legacy] })
  const history = await screen.findByRole('region', { name: 'Checkpoint history' })
  expect(within(history).getByText('Legacy entry')).toBeVisible()
  expect(within(history).queryByRole('button', { name: /Supersede/ })).toBeNull()
})

test('a browser null-origin transition manufactures no attributed notice', async () => {
  const user = userEvent.setup()
  setup({ audits: [auditAt(0), auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  await confirmSupersede(user)

  await waitFor(() => expect(screen.queryByRole('form')).toBeNull())
  // The durable response carries origin null; no attributed banner appears.
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('attributed revisions 1 and 3 are distinct hints and each refreshes first', async () => {
  const { state } = setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(screen.getByText(/Agent CLI superseded/)).toBeVisible())
  const afterFirst = state.auditReads

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(3, 3), '3') })
  // Identity includes the revision, so 3 is a second hint with its own
  // authoritative audit read, not a duplicate of 1.
  await waitFor(() => expect(state.auditReads).toBeGreaterThan(afterFirst))
  expect(screen.getByText(/Agent CLI superseded/)).toBeVisible()
  const afterSecond = state.auditReads

  // Replaying revision 1 is the same hint and is dropped without a new read.
  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await act(async () => { await Promise.resolve() })
  expect(state.auditReads).toBe(afterSecond)
})

test('a late old revision is not announced when the audit is already newer', async () => {
  setup({ audits: [auditAt(4)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await act(async () => { await Promise.resolve() })
  // Audit is at revision 4, so the stale revision-1 hint is dropped.
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('a foreign-workspace notice is ignored by the same owner', async () => {
  setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => {
    stream.emit('workstack.change.v1', {
      ...transitionNotice(1),
      workspace_uid: '00000000-0000-4000-8000-000000000009',
    }, '1')
  })
  await act(async () => { await Promise.resolve() })
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('disposal closes the stream and a later notice changes nothing', async () => {
  const view = setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  view.unmount()
  expect(stream.closed).toBe(true)
  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('StrictMode replay keeps one live stream and one notice', async () => {
  Stream.instances = []
  window.history.replaceState(null, '', '/?surface=review')
  const audits = [auditAt(1)]
  const state = { transitionAttempts: 0 }
  vi.stubGlobal('EventSource', Stream)
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponseOf({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (url.includes('/api/v1/sync/status')) return jsonResponseOf({ data: syncStatus })
    if (url.endsWith('/api/v1/review/checkpoints')) return jsonResponseOf({ data: audits[0] })
    if (url.includes('/api/v1/review')) {
      return jsonResponseOf({ data: {
        day: { date: DATE, start_time: null, entries: [] },
        weekly: { range: { start: WEEK_START, end: DATE, days: 7 }, objectives: [], projects: [] },
      } })
    }
    if (url.includes('/api/v1/workspace')) return jsonResponseOf({ data: projection })
    if (url.includes('/api/v1/tasks/')) {
      return jsonResponseOf({ data: { task, context: [], activity: [], replies: [] } })
    }
    return jsonResponseOf({ data: [] })
  }))

  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(
    <StrictMode>
      <QueryClientProvider client={client}><App /></QueryClientProvider>
    </StrictMode>,
  )
  await screen.findByRole('region', { name: 'Checkpoint history' })

  const live = Stream.instances.filter((instance) => !instance.closed)
  expect(live.length).toBe(1)
  await act(async () => { live[0].emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(screen.getAllByText(/Agent CLI superseded/).length).toBe(1))
  expect(state.transitionAttempts).toBe(0)
})

test('a hint whose audit disagrees on state is not announced', async () => {
  // The audit is at revision 2 and active; the hint claims the same revision
  // ended superseded. Same revision, contradicting state.
  setup({ audits: [auditAt(2)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => {
    stream.emit('workstack.change.v1', {
      ...transitionNotice(2, 2),
      kind: 'agent.checkpoint.superseded',
      state: 'superseded',
    }, '2')
  })
  await act(async () => { await Promise.resolve() })
  expect(screen.queryByText(/Agent CLI/)).toBeNull()
})

test('a hint for a checkpoint missing from the audit is not announced', async () => {
  const audit = auditAt(1)
  audit.entries = []
  setup({ audits: [audit] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await act(async () => { await Promise.resolve() })
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('a failed authoritative read is contained and a later valid hint still works', async () => {
  let auditCalls = 0
  Stream.instances = []
  window.history.replaceState(null, '', '/?surface=review')
  vi.stubGlobal('EventSource', Stream)
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponseOf({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (url.includes('/api/v1/sync/status')) return jsonResponseOf({ data: syncStatus })
    if (url.endsWith('/api/v1/review/checkpoints')) {
      auditCalls += 1
      // Only the first hint's authoritative read fails.
      if (auditCalls === 2) return jsonResponseOf({ error: { code: 'boom' } }, 500)
      return jsonResponseOf({ data: auditAt(3) })
    }
    if (url.includes('/api/v1/review')) {
      return jsonResponseOf({ data: {
        day: { date: DATE, start_time: null, entries: [] },
        weekly: { range: { start: WEEK_START, end: DATE, days: 7 }, objectives: [], projects: [] },
      } })
    }
    if (url.includes('/api/v1/workspace')) return jsonResponseOf({ data: projection })
    if (url.includes('/api/v1/tasks/')) {
      return jsonResponseOf({ data: { task, context: [], activity: [], replies: [] } })
    }
    return jsonResponseOf({ data: [] })
  }))

  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(auditCalls).toBeGreaterThanOrEqual(2))
  // The rejected read is contained: nothing announced, no unhandled rejection.
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()

  // The queue must still be usable for the next valid hint.
  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(3, 3), '3') })
  await waitFor(() => expect(screen.getByText(/Agent CLI superseded/)).toBeVisible())
})

test('a newer cached audit makes the pending hint obsolete, not merely unwritten', async () => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const { state, client } = setup({ audits: [auditAt(1)], auditGate: gate })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(state.auditReads).toBeGreaterThan(1))
  // A newer authoritative audit lands while that read is still open.
  await act(async () => { client.setQueryData(['checkpoint-audit', workspaceId], auditAt(3)) })
  await act(async () => { release() })
  await act(async () => { await Promise.resolve() })
  // Obsolete: the older read cannot announce over the newer cache.
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
  expect(client.getQueryData(['checkpoint-audit', workspaceId])).toEqual(auditAt(3))
})

test('a fetched Task whose UID disagrees neither installs nor announces', async () => {
  const { state, client } = setup({
    audits: [auditAt(1)],
    detailTask: { ...task, uid: '99999999-9999-4999-8999-999999999999' },
  })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(state.auditReads).toBeGreaterThan(1))
  await act(async () => { await Promise.resolve() })

  // Wrong Task identity blocks the message and every side effect together:
  // the disagreeing detail is never installed under this Task identity.
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
  const cachedDetail = client.getQueryData<TaskDetail>(['task', 'T-0001'])
  expect(cachedDetail?.task.uid).not.toBe('99999999-9999-4999-8999-999999999999')
})

test('an already-newer cached Task is not replaced by an older fetched one', async () => {
  const { state, client } = setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  // Already newer than anything the fetch will return, present beforehand.
  const newerWorkspace = {
    ...projection,
    tasks: projection.tasks.map((entry) => (
      entry.id === 'T-0001' ? { ...entry, revision: entry.revision + 5 } : entry
    )),
  }
  client.setQueryData(['workspace'], newerWorkspace)

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(state.auditReads).toBeGreaterThan(1))
  await act(async () => { await Promise.resolve() })

  // The older fetched Task cannot roll the cache back, and an obsolete
  // notice cannot be announced on the strength of it either.
  const cached = client.getQueryData<typeof projection>(['workspace'])
  expect(cached?.tasks.find((entry) => entry.id === 'T-0001')?.revision)
    .toBe(task.revision + 5)
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
})

test('a compatible same-UID revision advance still allows the View', async () => {
  const { client } = setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  const view = await screen.findByRole('button', { name: 'View' })

  // Same Task identity, ordinary later mutation: identity is the UID, not a
  // promise that nothing changed.
  await act(async () => {
    client.setQueryData(['workspace'], {
      ...projection,
      tasks: projection.tasks.map((entry) => (
        entry.id === 'T-0001' ? { ...entry, revision: entry.revision + 1 } : entry
      )),
    })
  })

  window.history.replaceState(null, '', '/?surface=inbox')
  await act(async () => { view.click() })
  expect(window.location.search).toBe('?surface=review')
})

test('a Task UID replaced inside the same workspace invalidates the old View', async () => {
  const { client } = setup({ audits: [auditAt(1)] })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  const view = await screen.findByRole('button', { name: 'View' })

  await act(async () => {
    client.setQueryData(['workspace'], {
      ...projection,
      tasks: projection.tasks.map((entry) => (
        entry.id === 'T-0001'
          ? { ...entry, uid: '77777777-7777-4777-8777-777777777777' }
          : entry
      )),
    })
  })

  window.history.replaceState(null, '', '/?surface=inbox')
  await act(async () => { view.click() })
  expect(window.location.search).toBe('?surface=inbox')
})

/**
 * D5-OI1: an incompatible cached Task identity must be decided in the COMMON
 * pre-install guard, not skipped afterwards while every other write lands.
 */
function updateCounts(client: QueryClient, keys: unknown[][]) {
  return keys.map((key) => (
    client.getQueryCache().find({ queryKey: key, exact: true })?.state.dataUpdateCount ?? 0
  ))
}

async function runCachedIdentity(cachedTaskId: string) {
  const { state, client } = setup({ audits: [auditAt(1)], freshMarkers: true })
  await screen.findByRole('region', { name: 'Checkpoint history' })
  const stream = Stream.instances[0]

  // A pre-existing cached detail under this key, same UID and revision as the
  // genuine fetched Task, but carrying the given Task ID.
  const cached = {
    task: { ...task, id: cachedTaskId },
    context: [],
    activity: [],
    replies: [],
  }
  client.setQueryData(['task', 'T-0001'], cached)

  // The Task key is asserted by identity below: with gcTime 0 an unobserved
  // query can be collected, so its counter is not a dependable witness.
  const keys = [['workspace'], ['checkpoint-audit', workspaceId], ['review', DATE, 7]]
  const before = updateCounts(client, keys)

  await act(async () => { stream.emit('workstack.change.v1', transitionNotice(1), '1') })
  await waitFor(() => expect(state.auditReads).toBeGreaterThan(1))
  await act(async () => { await Promise.resolve() })

  return { client, cached, before, after: () => updateCounts(client, keys) }
}

test('an incompatible cached Task ID blocks every install and the notice', async () => {
  const run = await runCachedIdentity('T-9999')

  // Nothing at all: no workspace, audit, Review or Task write, no message.
  expect(run.after()).toEqual(run.before)
  expect(screen.queryByText(/Agent CLI superseded/)).toBeNull()
  expect(screen.queryByRole('button', { name: 'View' })).toBeNull()
  // The genuine fetched detail is never installed under this key.
  const detail = run.client.getQueryData<TaskDetail>(['task', 'T-0001'])
  expect(detail === undefined || detail === run.cached).toBe(true)
})

test('a matching cached Task ID installs the fresh snapshots and announces', async () => {
  const run = await runCachedIdentity('T-0001')

  const after = run.after()
  // Distinct markers, so React Query structural sharing cannot hide a write.
  expect(after[0]).toBeGreaterThan(run.before[0])
  expect(after[1]).toBeGreaterThan(run.before[1])
  expect(after[2]).toBeGreaterThan(run.before[2])
  expect(screen.getByText(/Agent CLI superseded/)).toBeVisible()
})
