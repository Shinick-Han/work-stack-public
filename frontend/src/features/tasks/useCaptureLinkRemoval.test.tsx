import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, expect, test, vi } from 'vitest'
import { TaskContextTimeline } from '../../components/TaskContextTimeline'
import { microsoftProviderGates } from '../../config/providerGates'
import type { ContextItem } from '../../domain/types'
import { capture, jsonResponse, task, workspace } from '../../test/fixtures'
import { useCaptureLinkRemoval } from './useCaptureLinkRemoval'

/**
 * R22 acceptance for the Task-context surface, on the REAL composition: the feature hook
 * driving the real shared timeline, over a network mock of the published contract.
 *
 * The backend lands on its own branch, so the oracle here is the contract's own wire.
 */

const OTHER_TASK = 'T-0002'

const linkedCapture = {
  ...capture,
  status: 'linked' as const,
  linked_task_ids: [task.id, OTHER_TASK],
  revision: 4,
}

function contextItem(patch: Record<string, unknown> = {}): ContextItem {
  return {
    ...linkedCapture,
    ref: { kind: 'capture', id: linkedCapture.id },
    date_precision: 'instant',
    connections: [
      { target: { kind: 'task', id: task.id }, reasons: ['capture-link'] },
      { target: { kind: 'task', id: OTHER_TASK }, reasons: ['capture-link'] },
    ],
    ...patch,
  } as ContextItem
}

/** A second card connected by conversion only: an origin, and never an action. */
const conversionOnly = contextItem({
  ...capture,
  id: 'C-0002',
  source: { ...capture.source, display_title: 'Converted source' },
  ref: { kind: 'capture', id: 'C-0002' },
  status: 'converted',
  linked_task_ids: [],
  converted_task_ids: [task.id],
  revision: 2,
  connections: [{ target: { kind: 'task', id: task.id }, reasons: ['capture-conversion'] }],
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

/**
 * A settled promise chain plus one macrotask turn: enough for a completion that renders
 * nothing to have finished writing whatever it was going to write. Used only where the
 * point of the test is that an *invisible* stale completion touched no shared state.
 */
async function flushPending() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/unlink')) return handler(url, init)
    return jsonResponse({ data: { task, context: [], activity: [], replies: [] } })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function unlinkCalls(mock: ReturnType<typeof stubFetch>) {
  return mock.mock.calls
    .filter(([input]) => String(input).includes('/unlink'))
    .map(([input, init]) => {
      const request = init as RequestInit
      return {
        url: String(input),
        headers: request.headers as Record<string, string>,
        body: JSON.parse(String(request.body)) as Record<string, unknown>,
      }
    })
}

const refreshes = { task: 0, workspace: 0 }

/**
 * The real subview composition: the feature hook owns the request and hands the shared
 * timeline nothing but presentation state. The two `useQuery` rows below are the
 * existing planning queries whose invalidation the contract asks for.
 */
function Harness({ context, taskId }: { context: ContextItem[]; taskId: string }) {
  const removal = useCaptureLinkRemoval(taskId)
  useQuery({
    queryKey: ['task', taskId],
    queryFn: async () => {
      refreshes.task += 1
      return taskId
    },
  })
  useQuery({
    queryKey: ['workspace'],
    queryFn: async () => {
      refreshes.workspace += 1
      return 'workspace'
    },
  })
  return <TaskContextTimeline context={context} providerGates={microsoftProviderGates} removal={removal} />
}

function mount(context: ContextItem[] = [contextItem(), conversionOnly], taskId = task.id) {
  refreshes.task = 0
  refreshes.workspace = 0
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <Harness context={context} taskId={taskId} />
      </QueryClientProvider>
    </StrictMode>,
  )
  const rerender = (nextTaskId: string) => view.rerender(
    <StrictMode>
      <QueryClientProvider client={client}>
        <Harness context={context} taskId={nextTaskId} />
      </QueryClientProvider>
    </StrictMode>,
  )
  return { ...view, rerender }
}

function cardFor(title: string) {
  return screen.getByRole('heading', { name: title }).closest('article') as HTMLElement
}

test('offers the action only on this Task explicit Capture link', async () => {
  stubFetch(() => jsonResponse({ data: linkedCapture, meta: { duplicate: false } }))

  mount()

  expect(within(cardFor('Release review feedback')).getByRole('button', { name: 'Remove task link' })).toBeEnabled()
  expect(within(cardFor('Converted source')).queryByRole('button')).toBeNull()
  expect(screen.getAllByRole('button', { name: 'Remove task link' })).toHaveLength(1)
})

test('the shared timeline stays read-only for a surface that passes no callback', () => {
  render(
    <TaskContextTimeline
      context={[contextItem(), conversionOnly]}
      providerGates={microsoftProviderGates}
    />,
  )

  expect(screen.queryByRole('button')).toBeNull()
})

test('one click sends one POST with the displayed revision and refreshes on success', async () => {
  const mock = stubFetch(() => jsonResponse({ data: linkedCapture, meta: { duplicate: false } }))
  mount()
  const before = { ...refreshes }

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  const calls = unlinkCalls(mock)
  expect(calls).toHaveLength(1)
  expect(calls[0].url).toBe('/api/v1/captures/C-0001/unlink')
  expect(calls[0].body).toEqual({ task_id: task.id, revision: 4 })
  expect(calls[0].headers['Idempotency-Key']).toMatch(/^workstack:/)
  // No optimistic removal: the card leaves only when the refreshed Task says so.
  expect(cardFor('Release review feedback')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).toBeNull()
  // The existing planning queries are what refreshes the Task, its context and the counts.
  await waitFor(() => {
    expect(refreshes.task).toBeGreaterThan(before.task)
    expect(refreshes.workspace).toBeGreaterThan(before.workspace)
  })
})

test('an already absent link at the correct revision is not shown as a failure', async () => {
  const mock = stubFetch(() => jsonResponse({ data: linkedCapture, meta: { duplicate: true } }))
  mount()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  expect(unlinkCalls(mock)).toHaveLength(1)
  expect(screen.queryByRole('alert')).toBeNull()
})

test('a duplicate click while one request is in flight is still one POST', async () => {
  const gate = deferred<Response>()
  const mock = stubFetch(() => gate.promise)
  mount()
  const button = screen.getByRole('button', { name: 'Remove task link' })

  await userEvent.click(button)
  await userEvent.click(button)
  await userEvent.click(button)

  expect(await screen.findByRole('status')).toHaveTextContent('Removing the task link…')
  expect(button).toBeDisabled()
  expect(unlinkCalls(mock)).toHaveLength(1)
  gate.resolve(new Response(JSON.stringify({ data: linkedCapture, meta: { duplicate: false } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  expect(unlinkCalls(mock)).toHaveLength(1)
})

test('a revision conflict is a closed sentence, no removal and no retry control', async () => {
  const mock = stubFetch(() => jsonResponse(
    { error: { code: 'revision_conflict', message: 'capture revision does not match' } },
    409,
  ))
  mount()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(
    'This capture changed since it was shown, so nothing was removed. Refresh the Task and try again.',
  )
  expect(alert).not.toHaveTextContent('capture revision does not match')
  // A determinate refusal offers no control: the same stale revision cannot succeed.
  expect(screen.queryByRole('button', { name: 'Remove task link' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  expect(cardFor('Release review feedback')).toBeInTheDocument()
  expect(unlinkCalls(mock)).toHaveLength(1)
})

test('an unknown outcome retries the same intent under the same key and body', async () => {
  let attempt = 0
  const mock = stubFetch(() => {
    attempt += 1
    if (attempt <= 2) return Promise.reject(new TypeError('Failed to fetch'))
    return jsonResponse({ data: linkedCapture, meta: { duplicate: true } })
  })
  mount()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Work Stack could not confirm whether the task link was removed.')
  // Nothing was removed and nothing is offered as an Undo.
  expect(cardFor('Release review feedback')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /undo/i })).toBeNull()

  const retry = screen.getByRole('button', { name: 'Try again' })
  await userEvent.click(retry)

  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  const calls = unlinkCalls(mock)
  // The shared transport's own network retry replayed the identical request, and the
  // explicit retry reused that same intent rather than minting a new one.
  expect(calls).toHaveLength(3)
  expect(new Set(calls.map((call) => call.headers['Idempotency-Key'])).size).toBe(1)
  expect(calls.every((call) => call.body.task_id === task.id && call.body.revision === 4)).toBe(true)
  expect(screen.queryByRole('alert')).toBeNull()
})

test('a determinate refusal drops the stored intent so a later attempt is a new one', async () => {
  let attempt = 0
  const mock = stubFetch(() => {
    attempt += 1
    // The shared transport replays a failed fetch once, so the first intent is two calls.
    if (attempt <= 2) return Promise.reject(new TypeError('Failed to fetch'))
    if (attempt === 3) return jsonResponse({ error: { code: 'not_found', message: 'gone' } }, 404)
    return jsonResponse({ data: linkedCapture, meta: { duplicate: false } })
  })
  const { rerender } = mount()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This capture or Task is no longer available, so nothing was removed.',
  )

  // A new view is the only way back to the action after a determinate refusal.
  rerender(OTHER_TASK)
  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  await waitFor(() => expect(unlinkCalls(mock)).toHaveLength(4))
  const keys = unlinkCalls(mock).map((call) => call.headers['Idempotency-Key'])
  expect(keys[0]).toBe(keys[1])
  expect(keys[1]).toBe(keys[2])
  expect(keys[3]).not.toBe(keys[2])
  expect(unlinkCalls(mock)[3].body).toEqual({ task_id: OTHER_TASK, revision: 4 })
})

test('a late answer cannot display in the successor view, ABA included', async () => {
  const gate = deferred<Response>()
  const mock = stubFetch(() => gate.promise)
  const { rerender } = mount()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  expect(await screen.findByRole('status')).toBeInTheDocument()

  rerender(OTHER_TASK)
  // The successor view is idle in the same commit: no pending row, no message.
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()

  rerender(task.id)
  gate.reject(new TypeError('Failed to fetch'))

  await waitFor(() => expect(unlinkCalls(mock).length).toBeGreaterThan(0))
  // The answer was minted under the first view of this Task; the second cannot show it.
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
})

/**
 * The overlapping-completion pair.
 *
 * Both start the same way: attempt A1 is left in flight, the reader goes A→B→A, and a
 * second attempt A2 against the same displayed Capture and revision ends ambiguously, so
 * `Try again` now stands for key K2. Then A1 — belonging to a view that no longer exists
 * — finally answers. The oracle is not what renders; it is the idempotency key on the
 * next actual unlink request, because that is what decides whether the server sees one
 * intent retried or a second intent applied on top of an unknown outcome.
 */

/** Drives A1-in-flight → A→B→A → A2 ambiguous. Leaves A1's gate for the caller. */
async function overlapUntilRetryOffered(
  mock: ReturnType<typeof stubFetch>,
  rerender: (taskId: string) => void,
) {
  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  expect(await screen.findByRole('status')).toBeInTheDocument()

  rerender(OTHER_TASK)
  rerender(task.id)

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  const retry = await screen.findByRole('button', { name: 'Try again' })
  const keys = unlinkCalls(mock).map((call) => call.headers['Idempotency-Key'])
  // Two distinct intents exist: K1 belongs to the abandoned view, K2 to the live one.
  expect(keys).toHaveLength(2)
  expect(keys[1]).not.toBe(keys[0])
  return { keys, retry }
}

test('a stale success cannot spend the intent the live view is retrying', async () => {
  const gate = deferred<Response>()
  let attempt = 0
  const mock = stubFetch(() => {
    attempt += 1
    // 1: A1, held open across the view changes. 2: A2, a 5xx, whose write state this
    // client cannot know, so it is ambiguous and K2 is retained. 3: the `Try again`.
    if (attempt === 1) return gate.promise
    if (attempt === 2) return jsonResponse({ error: { code: 'unavailable', message: 'x' } }, 503)
    return jsonResponse({ data: linkedCapture, meta: { duplicate: true } })
  })
  const { rerender } = mount()
  const { keys } = await overlapUntilRetryOffered(mock, rerender)

  const before = { ...refreshes }
  gate.resolve(new Response(JSON.stringify({ data: linkedCapture, meta: { duplicate: false } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  // The invalidation is the observable proof that A1's success branch has run: it is
  // sequenced after the branch's only write to the retained intent.
  await waitFor(() => expect(refreshes.task).toBeGreaterThan(before.task))
  await flushPending()

  // A settled answer from a dead view neither renders nor cancels the live ambiguity.
  expect(screen.getByRole('alert')).toHaveTextContent(
    'Work Stack could not confirm whether the task link was removed.',
  )
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

  await waitFor(() => expect(unlinkCalls(mock)).toHaveLength(3))
  const sent = unlinkCalls(mock)[2]
  // The counterexample: without the identity guard the stale success clears the stored
  // intent, so this request carries a freshly minted K3 and the server sees a second
  // removal intent for an outcome nobody knows.
  expect(sent.headers['Idempotency-Key']).toBe(keys[1])
  expect(sent.headers['Idempotency-Key']).not.toBe(keys[0])
  expect(sent.body).toEqual({ task_id: task.id, revision: 4 })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  expect(screen.queryByRole('alert')).toBeNull()
})

test('a stale ambiguity cannot reinstate its own key over the live one', async () => {
  const gate = deferred<Response>()
  let attempt = 0
  const mock = stubFetch(() => {
    attempt += 1
    // 1: A1, held open. 2: A2 ambiguous, retaining K2. 3: A1's own transport replay once
    // its gate fails, which is what makes A1's ambiguous landing observable. 4: `Try again`.
    if (attempt === 1) return gate.promise
    if (attempt === 2) return jsonResponse({ error: { code: 'unavailable', message: 'x' } }, 503)
    if (attempt === 3) return Promise.reject(new TypeError('Failed to fetch'))
    return jsonResponse({ data: linkedCapture, meta: { duplicate: true } })
  })
  const { rerender } = mount()
  const { keys } = await overlapUntilRetryOffered(mock, rerender)

  gate.reject(new TypeError('Failed to fetch'))
  // The shared transport replays the identical request under K1; the third call landing
  // is the signal that A1 has reached its own ambiguous completion.
  await waitFor(() => expect(unlinkCalls(mock)).toHaveLength(3))
  expect(unlinkCalls(mock)[2].headers['Idempotency-Key']).toBe(keys[0])
  await flushPending()

  expect(screen.getByRole('alert')).toHaveTextContent(
    'Work Stack could not confirm whether the task link was removed.',
  )
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

  await waitFor(() => expect(unlinkCalls(mock)).toHaveLength(4))
  const sent = unlinkCalls(mock)[3]
  // The counterexample: without the identity guard A1's late ambiguity overwrites the
  // stored intent with K1, so `Try again` resends the abandoned view's key instead of
  // the one the reader was actually shown a retry for.
  expect(sent.headers['Idempotency-Key']).toBe(keys[1])
  expect(sent.headers['Idempotency-Key']).not.toBe(keys[0])
  expect(sent.body).toEqual({ task_id: task.id, revision: 4 })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  expect(screen.queryByRole('alert')).toBeNull()
})
