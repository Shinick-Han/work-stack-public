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

const refreshes = { task: 0 }

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

/**
 * R33 acceptance for the Undo of one confirmed removal, on the REAL composition: the
 * feature hook driving the real shared timeline, over a network mock of the owner's
 * frozen wire. The owner lands on its own branch; nothing here integrates with it.
 */

const RECEIPT = '9f8e7d6c-5b4a-4392-8271-0a1b2c3d4e5f'
/** Post-unlink: one revision past the card, link gone. This is what may be undone. */
const UNLINKED = { ...capture, status: 'inbox' as const, linked_task_ids: [OTHER_TASK], revision: 5 }
const RESTORED = { ...capture, status: 'linked' as const, linked_task_ids: [task.id, OTHER_TASK], revision: 6 }

const SCOPE = { workspaceUid: 'W-1', taskUid: 'U-1', taskId: task.id }
const OTHER_WORKSPACE = { ...SCOPE, workspaceUid: 'W-2' }
const REPLACEMENT_TASK = { ...SCOPE, taskUid: 'U-2' }

const REMOVED_NOTICE = 'The task link was removed.'
const RESTORED_NOTICE = 'The task link was restored.'
const UNDO_UNKNOWN = 'Work Stack could not confirm whether the task link was restored.'

type Route = (url: string, init?: RequestInit) => Promise<Response>

/** Routes the two mutations apart: `/undo-unlink` also contains `/unlink`. */
function stubRoutes({ undo, unlink }: { undo?: Route; unlink?: Route }) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.endsWith('/undo-unlink')) {
      return undo ? undo(url, init) : Promise.reject(new Error('unexpected undo request'))
    }
    if (url.endsWith('/unlink')) {
      return unlink ? unlink(url, init) : Promise.reject(new Error('unexpected unlink request'))
    }
    return jsonResponse({ data: { task, context: [], activity: [], replies: [] } })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function callsTo(mock: ReturnType<typeof stubRoutes>, suffix: string) {
  return mock.mock.calls
    .filter(([input]) => String(input).endsWith(suffix))
    .map(([input, init]) => {
      const request = init as RequestInit
      return {
        url: String(input),
        headers: request.headers as Record<string, string>,
        body: JSON.parse(String(request.body)) as Record<string, unknown>,
      }
    })
}

const undoCalls = (mock: ReturnType<typeof stubRoutes>) => callsTo(mock, '/undo-unlink')
const removeCalls = (mock: ReturnType<typeof stubRoutes>) => callsTo(mock, '/unlink')

const confirmedRemoval = (meta: Record<string, unknown> = { duplicate: false, undo_receipt_id: RECEIPT }) =>
  () => jsonResponse({ data: UNLINKED, meta })

function ScopedHarness({ context, scope }: { context: ContextItem[]; scope: typeof SCOPE }) {
  const removal = useCaptureLinkRemoval(scope)
  useQuery({
    queryKey: ['task', scope.taskId],
    queryFn: async () => {
      refreshes.task += 1
      return scope.taskId
    },
  })
  return <TaskContextTimeline context={context} providerGates={microsoftProviderGates} removal={removal} />
}

function mountScoped(context: ContextItem[] = [contextItem(), conversionOnly], scope = SCOPE) {
  refreshes.task = 0
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidated = vi.spyOn(client, 'invalidateQueries')
  const tree = (next: typeof SCOPE, items: ContextItem[]) => (
    <StrictMode>
      <QueryClientProvider client={client}>
        <ScopedHarness context={items} scope={next} />
      </QueryClientProvider>
    </StrictMode>
  )
  const view = render(tree(scope, context))
  return {
    ...view,
    invalidated,
    rerender: (next: typeof SCOPE, items: ContextItem[] = context) => view.rerender(tree(next, items)),
  }
}

/** One confirmed removal that carries a usable receipt, leaving the offer on screen. */
async function removeWithOffer(mock: ReturnType<typeof stubRoutes>) {
  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  await screen.findByRole('button', { name: 'Undo link removal' })
  expect(removeCalls(mock)).toHaveLength(1)
}

const undoKeys = (mock: ReturnType<typeof stubRoutes>) => (
  undoCalls(mock).map((call) => call.headers['Idempotency-Key'])
)

test('a confirmed removal with a canonical receipt offers Undo outside every card', async () => {
  const mock = stubRoutes({ unlink: confirmedRemoval() })
  mountScoped()

  await removeWithOffer(mock)

  const banner = screen.getByRole('group', { name: 'Removed task link' })
  expect(within(banner).getByRole('status')).toHaveTextContent(REMOVED_NOTICE)
  expect(within(banner).getByRole('button', { name: 'Dismiss' })).toBeEnabled()
  // Outside the card is the whole point: the control is not inside any context entry.
  expect(banner.closest('article')).toBeNull()
  // Nothing was sent by merely offering it.
  expect(undoCalls(mock)).toHaveLength(0)
})

test('the Undo control survives removing the last card, when the timeline is empty', async () => {
  const mock = stubRoutes({ unlink: confirmedRemoval() })
  const { rerender } = mountScoped([contextItem()])

  await removeWithOffer(mock)
  // The refreshed Task no longer carries the link, so the card — and the whole list — go.
  rerender(SCOPE, [])

  expect(screen.getByText('No context yet')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Undo link removal' })).toBeEnabled()
  expect(screen.getByRole('status')).toHaveTextContent(REMOVED_NOTICE)
})

test('no receipt, a duplicate or a malformed hint cannot offer an Undo', async () => {
  const metas: Record<string, unknown>[] = [
    { duplicate: false },
    { duplicate: true, undo_receipt_id: RECEIPT },
    { duplicate: false, undo_receipt_id: 'not-a-uuid' },
    { duplicate: false, undo_receipt_id: RECEIPT.toUpperCase() },
    { duplicate: false, undo_receipt_id: null },
  ]
  for (const meta of metas) {
    const mock = stubRoutes({ unlink: confirmedRemoval(meta) })
    const view = mountScoped()

    await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
    // Still a settled success: no failure, no Undo, no dead-end control.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
    expect(removeCalls(mock)).toHaveLength(1)
    view.unmount()
    vi.unstubAllGlobals()
  }
})

test('a response about another Capture or another revision cannot offer an Undo', async () => {
  const mismatches = [
    { ...UNLINKED, id: 'C-0009' },
    { ...UNLINKED, revision: 4 },
    { ...UNLINKED, revision: 9 },
  ]
  for (const data of mismatches) {
    const mock = stubRoutes({
      unlink: () => jsonResponse({ data, meta: { duplicate: false, undo_receipt_id: RECEIPT } }),
    })
    const view = mountScoped()

    await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
    expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
    expect(removeCalls(mock)).toHaveLength(1)
    view.unmount()
    vi.unstubAllGlobals()
  }
})

test('Undo posts the exact body under a NEW key and announces the restored link', async () => {
  const mock = stubRoutes({
    unlink: confirmedRemoval(),
    undo: () => jsonResponse({ data: RESTORED, meta: { duplicate: false } }),
  })
  const { invalidated } = mountScoped()
  await removeWithOffer(mock)
  invalidated.mockClear()

  await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

  expect(await screen.findByText(RESTORED_NOTICE)).toBeInTheDocument()
  const calls = undoCalls(mock)
  expect(calls).toHaveLength(1)
  expect(calls[0].url).toBe('/api/v1/captures/C-0001/undo-unlink')
  // The POST-UNLINK revision, and only the two published fields.
  expect(calls[0].body).toEqual({ receipt_id: RECEIPT, revision: 5 })
  // A separate intent: the Undo key is not the unlink key it inverts.
  expect(calls[0].headers['Idempotency-Key']).toMatch(/^workstack:/)
  expect(calls[0].headers['Idempotency-Key']).not.toBe(removeCalls(mock)[0].headers['Idempotency-Key'])
  // The refreshed data restores the card; nothing is edited locally.
  expect(invalidated).toHaveBeenCalledWith({ queryKey: ['task', task.id] })
  expect(invalidated).toHaveBeenCalledWith({ queryKey: ['workspace'] })
  expect(screen.queryByRole('button', { name: 'Undo link removal' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Dismiss' })).toBeEnabled()
})

test('rapid double clicks on Undo are still one POST, and every card is locked', async () => {
  const gate = deferred<Response>()
  const mock = stubRoutes({ unlink: confirmedRemoval(), undo: () => gate.promise })
  mountScoped()
  await removeWithOffer(mock)

  const undoButton = screen.getByRole('button', { name: 'Undo link removal' })
  await userEvent.click(undoButton)
  await userEvent.click(undoButton)
  await userEvent.click(undoButton)

  expect(screen.getByRole('status')).toHaveTextContent('Restoring the task link…')
  expect(undoCalls(mock)).toHaveLength(1)
  // Cross-action lock: no removal may start on top of an Undo in flight.
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeDisabled()
  // Nothing settled, so there is nothing to dismiss yet.
  expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
  gate.resolve(new Response(JSON.stringify({ data: RESTORED, meta: { duplicate: false } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  await waitFor(() => expect(screen.getByText(RESTORED_NOTICE)).toBeInTheDocument())
  expect(undoCalls(mock)).toHaveLength(1)
  expect(removeCalls(mock)).toHaveLength(1)
})

test('an unknown Undo retries the same body under the same key, and never on its own', async () => {
  let attempt = 0
  const mock = stubRoutes({
    unlink: confirmedRemoval(),
    // The first click's reply is lost. One click is one POST, so the retry is the
    // reader's, and it is the one that settles the intent.
    undo: () => {
      attempt += 1
      if (attempt === 1) return Promise.reject(new TypeError('Failed to fetch'))
      return jsonResponse({ data: RESTORED, meta: { duplicate: false } })
    },
  })
  mountScoped()
  await removeWithOffer(mock)

  await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(UNDO_UNKNOWN)
  expect(screen.queryByText(RESTORED_NOTICE)).toBeNull()
  // Unknown is not settled: no removal may start until it is retried or dismissed.
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeDisabled()
  await flushPending()
  // Nothing retried itself while the reader was reading: exactly the one click's POST.
  expect(undoCalls(mock)).toHaveLength(1)

  await userEvent.click(screen.getByRole('button', { name: 'Try Undo again' }))

  expect(await screen.findByText(RESTORED_NOTICE)).toBeInTheDocument()
  const calls = undoCalls(mock)
  expect(calls).toHaveLength(2)
  expect(new Set(undoKeys(mock)).size).toBe(1)
  expect(calls.every((call) => call.body.receipt_id === RECEIPT && call.body.revision === 5)).toBe(true)
  expect(screen.queryByRole('alert')).toBeNull()
})

test('a lost reply and a stale CSRF nonce each send exactly one POST per click', async () => {
  const families: { label: string; undo: () => Promise<Response> }[] = [
    { label: 'network loss', undo: () => Promise.reject(new TypeError('Failed to fetch')) },
    {
      label: 'a 403 the transport would otherwise resend under a refreshed token',
      undo: () => jsonResponse({ error: { code: 'invalid_request', message: 'stale nonce' } }, 403),
    },
  ]
  for (const { label, undo } of families) {
    const mock = stubRoutes({ unlink: confirmedRemoval(), undo })
    const view = mountScoped()
    await removeWithOffer(mock)

    await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

    await screen.findByRole('alert')
    await flushPending()
    // The whole point of the correction: one explicit intent is one mutation, whatever
    // the shared transport would have replayed on its own.
    expect(undoCalls(mock), label).toHaveLength(1)
    expect(screen.queryByText(RESTORED_NOTICE), label).toBeNull()
    view.unmount()
    vi.unstubAllGlobals()
  }
})

test('a 200 about another capture, revision or task is unconfirmed, not a restoration', async () => {
  const unrecognised = [
    { label: 'another capture', data: { ...RESTORED, id: 'C-0009' } },
    { label: 'the revision that was sent', data: { ...RESTORED, revision: 5 } },
    { label: 'a revision beyond the expected one', data: { ...RESTORED, revision: 9 } },
    { label: 'the intended task still unlinked', data: { ...RESTORED, linked_task_ids: [OTHER_TASK] } },
  ]
  for (const { label, data } of unrecognised) {
    const mock = stubRoutes({
      unlink: confirmedRemoval(),
      undo: () => jsonResponse({ data, meta: { duplicate: false } }),
    })
    const { invalidated, unmount } = mountScoped()
    await removeWithOffer(mock)
    invalidated.mockClear()

    await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

    // A schema-valid 200 that does not show THIS restore lands on the unknown copy.
    expect(await screen.findByRole('alert'), label).toHaveTextContent(UNDO_UNKNOWN)
    expect(screen.queryByText(RESTORED_NOTICE), label).toBeNull()
    // No restore was proven, so nothing claims the planning data moved.
    expect(invalidated, label).not.toHaveBeenCalledWith({ queryKey: ['task', task.id] })
    expect(invalidated, label).not.toHaveBeenCalledWith({ queryKey: ['workspace'] })
    // The intent survives: the retry is the same body under the same key.
    expect(undoCalls(mock), label).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name: 'Try Undo again' }))
    await flushPending()
    const calls = undoCalls(mock)
    expect(calls, label).toHaveLength(2)
    expect(new Set(undoKeys(mock)).size, label).toBe(1)
    expect(calls.every((call) => call.body.receipt_id === RECEIPT && call.body.revision === 5), label)
      .toBe(true)
    expect(screen.queryByText(RESTORED_NOTICE), label).toBeNull()
    unmount()
    vi.unstubAllGlobals()
  }
})

test('an unrecognised 200 that later answers correctly settles on the explicit retry', async () => {
  let attempt = 0
  const mock = stubRoutes({
    unlink: confirmedRemoval(),
    undo: () => {
      attempt += 1
      // A mismatched proxy answers first; the same intent then reaches the real owner.
      const data = attempt === 1 ? { ...RESTORED, id: 'C-0009' } : RESTORED
      return jsonResponse({ data, meta: { duplicate: false } })
    },
  })
  const { invalidated } = mountScoped()
  await removeWithOffer(mock)
  invalidated.mockClear()

  await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(UNDO_UNKNOWN)

  await userEvent.click(screen.getByRole('button', { name: 'Try Undo again' }))

  expect(await screen.findByText(RESTORED_NOTICE)).toBeInTheDocument()
  expect(invalidated).toHaveBeenCalledWith({ queryKey: ['task', task.id] })
  expect(invalidated).toHaveBeenCalledWith({ queryKey: ['workspace'] })
  expect(undoCalls(mock)).toHaveLength(2)
  expect(new Set(undoKeys(mock)).size).toBe(1)
})

test('a known Undo refusal offers no retry and claims no restoration', async () => {
  const cases = [
    {
      code: 'capture_unlink_undo_conflict',
      sentence: 'This capture no longer matches the removal that was undone, so the task link was not restored.',
      status: 409,
    },
    {
      code: 'not_found',
      sentence: 'That removal can no longer be undone, so the task link was not restored.',
      status: 404,
    },
  ]
  for (const { code, sentence, status } of cases) {
    const mock = stubRoutes({
      unlink: confirmedRemoval(),
      undo: () => jsonResponse({ error: { code, message: 'backend prose' } }, status),
    })
    const view = mountScoped()
    await removeWithOffer(mock)

    await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(sentence)
    expect(alert).not.toHaveTextContent('backend prose')
    expect(screen.queryByText(RESTORED_NOTICE)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try Undo again' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Undo link removal' })).toBeNull()
    await flushPending()
    expect(undoCalls(mock)).toHaveLength(1)
    // Settled: the reader may dismiss it and act again.
    expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
    view.unmount()
    vi.unstubAllGlobals()
  }
})

test('Dismiss hides the feedback, sends nothing and claims nothing', async () => {
  const mock = stubRoutes({ unlink: confirmedRemoval() })
  mountScoped()
  await removeWithOffer(mock)

  await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.queryByText(RESTORED_NOTICE)).toBeNull()
  await flushPending()
  expect(undoCalls(mock)).toHaveLength(0)
  expect(removeCalls(mock)).toHaveLength(1)
  // Dismissing releases the lock without settling anything remotely.
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
})

test('a later explicit removal replaces the earlier settled offer', async () => {
  const withReceipt = stubRoutes({ unlink: confirmedRemoval() })
  mountScoped()
  await removeWithOffer(withReceipt)

  // The second removal answers without a receipt: the earlier offer must not survive it.
  const legacy = stubRoutes({ unlink: confirmedRemoval({ duplicate: false }) })
  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Undo link removal' })).toBeNull()
  expect(removeCalls(legacy)).toHaveLength(1)
})

test('a workspace, Task UID or Task id change clears the offer and its key', async () => {
  for (const next of [OTHER_WORKSPACE, REPLACEMENT_TASK, { ...SCOPE, taskId: OTHER_TASK }]) {
    const mock = stubRoutes({ unlink: confirmedRemoval() })
    const view = mountScoped()
    await removeWithOffer(mock)

    view.rerender(next)

    // Same display id in another workspace, or a replacement Task, inherits nothing.
    expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Undo link removal' })).toBeNull()
    await flushPending()
    expect(undoCalls(mock)).toHaveLength(0)
    view.unmount()
    vi.unstubAllGlobals()
  }
})

test('a late Undo answer cannot land in a successor view, ABA included', async () => {
  const gate = deferred<Response>()
  const mock = stubRoutes({ unlink: confirmedRemoval(), undo: () => gate.promise })
  const view = mountScoped()
  await removeWithOffer(mock)
  await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Restoring the task link…')

  view.rerender(OTHER_WORKSPACE)
  view.rerender(SCOPE)
  gate.resolve(new Response(JSON.stringify({ data: RESTORED, meta: { duplicate: false } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  await flushPending()

  // The answer was minted under the first view of this scope; the second cannot show it.
  expect(screen.queryByText(RESTORED_NOTICE)).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
})

test('an unmounted view drops a late Undo answer entirely', async () => {
  const gate = deferred<Response>()
  const mock = stubRoutes({ unlink: confirmedRemoval(), undo: () => gate.promise })
  const view = mountScoped()
  await removeWithOffer(mock)
  await userEvent.click(screen.getByRole('button', { name: 'Undo link removal' }))

  view.unmount()
  gate.resolve(new Response(JSON.stringify({ data: RESTORED, meta: { duplicate: false } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  await flushPending()

  expect(screen.queryByText(RESTORED_NOTICE)).toBeNull()
  expect(undoCalls(mock)).toHaveLength(1)
})

test('a removal that settles after the view changed invalidates, but leaks no offer', async () => {
  const gate = deferred<Response>()
  const mock = stubRoutes({ unlink: () => gate.promise })
  const { invalidated, rerender } = mountScoped()

  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  expect(await screen.findByRole('status')).toBeInTheDocument()
  rerender(OTHER_WORKSPACE)
  invalidated.mockClear()
  gate.resolve(new Response(
    JSON.stringify({ data: UNLINKED, meta: { duplicate: false, undo_receipt_id: RECEIPT } }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  ))

  // The write happened, so the ORIGINAL Task and the counts are invalidated regardless.
  await waitFor(() => expect(invalidated).toHaveBeenCalledWith({ queryKey: ['task', task.id] }))
  expect(invalidated).toHaveBeenCalledWith({ queryKey: ['workspace'] })
  await flushPending()
  // The current view neither shows the offer nor holds its key.
  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(undoCalls(mock)).toHaveLength(0)
})

test('the read-only timeline renders no Undo feedback at all', () => {
  render(
    <TaskContextTimeline
      context={[contextItem(), conversionOnly]}
      providerGates={microsoftProviderGates}
    />,
  )

  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.queryByRole('button')).toBeNull()
})

test('mounting and reopening the surface reaches the network for nothing', async () => {
  const mock = stubRoutes({})
  const view = mountScoped()
  view.rerender(SCOPE)
  await flushPending()

  expect(removeCalls(mock)).toHaveLength(0)
  expect(undoCalls(mock)).toHaveLength(0)
  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
})
