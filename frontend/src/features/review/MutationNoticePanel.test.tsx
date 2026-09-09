import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi, afterEach } from 'vitest'
import { jsonResponse, task, workspace } from '../../test/fixtures'
import type { MutationNotice } from '../../api/mutationNotices'
import { MutationNoticePanel } from './MutationNoticePanel'

/**
 * The reader-facing half of the mutation-notice slice: what a row is allowed to claim,
 * which rows may offer an Undo at all, and what the one guarded Undo does with a
 * conflict, an unknown outcome and a workspace that was replaced under it.
 */

const WORKSPACE_ID = workspace.workspace.id
const WORKSPACE_UID = '5d3b7a4c-ae9f-5071-bc3d-4e5f6a7b8c9d'
const MUTATION_UID = '6e4c8b5d-bf10-5182-8d4e-5f6a7b8c9d0e'
const STATUS_NOTICE = '3b1f5e2a-8c7d-5e4f-9a1b-2c3d4e5f6a7b'
const DELETE_NOTICE = '4c2a6f3b-9d8e-5f60-ab2c-3d4e5f6a7b8c'
const FOREIGN_NOTICE = '7f5d9c6e-c021-5293-9e5f-6a7b8c9d0e1f'
const OTHER_TASK_UID = '88888888-2222-1222-8222-222222222222'
const CURSOR = `eyJlIjoiRS0wMDAwMDEiLCJ2IjoxLCJ3IjoiJHt3fSJ9${'A'.repeat(8)}`
/** The idempotency key a notice carries is request identity. It must never be readable. */
const SECRET_KEY = 'wire.status.0001'
const RESTORED = { ...task, status: 'open' as const, revision: 3 }

function notice(overrides: Partial<MutationNotice> = {}): MutationNotice {
  return {
    format: 'workstack.mutation-notice',
    schema_version: 1,
    notice_id: STATUS_NOTICE,
    workspace_uid: WORKSPACE_UID,
    mutation_uid: MUTATION_UID,
    entity_kind: 'task',
    entity_uid: task.uid,
    operation: 'task.status',
    before_revision: 1,
    after_revision: 2,
    source: 'cli',
    actor: 'local.user',
    idempotency_key: SECRET_KEY,
    commit_state: 'committed',
    summary: 'Task status open to started',
    undoable: true,
    status_before: 'open',
    status_after: 'started',
    ...overrides,
  }
}

/** A recorded change that the contract declares non-undoable, on a Task nobody can see. */
function deleteNotice(): MutationNotice {
  return notice({
    notice_id: DELETE_NOTICE,
    mutation_uid: FOREIGN_NOTICE,
    entity_uid: OTHER_TASK_UID,
    operation: 'task.permanent_delete',
    source: 'gui',
    summary: 'Permanent deletion is not undoable',
    undoable: false,
    status_before: null,
    status_after: null,
  })
}

/** A change whose outcome the owner could not verify: no after-revision, no status pair. */
function unverifiedNotice(): MutationNotice {
  return notice({
    notice_id: FOREIGN_NOTICE,
    mutation_uid: DELETE_NOTICE,
    entity_kind: 'capture',
    operation: 'capture.ingest',
    entity_uid: OTHER_TASK_UID,
    source: 'agent',
    after_revision: null,
    commit_state: 'commit_unknown',
    summary: 'Mutation outcome is unverifiable',
    undoable: false,
    status_before: null,
    status_after: null,
  })
}

function page(items: MutationNotice[], nextCursor: string | null = null) {
  return { data: { items, next_cursor: nextCursor } }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderPanel(onOpenTask = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MutationNoticePanel onOpenTask={onOpenTask} tasks={workspace.tasks} workspaceId={WORKSPACE_ID} />
    </QueryClientProvider>,
  )
  return { onOpenTask, queryClient, view }
}

function undoPosts(mock: ReturnType<typeof stubFetch>) {
  return mock.mock.calls.filter(([input, init]) => (
    String(input).includes('/undo') && (init as RequestInit | undefined)?.method === 'POST'
  ))
}

function rowFor(name: string | RegExp) {
  const row = screen.getByText(name).closest('li')
  if (!row) throw new Error('the notice row was not rendered')
  return within(row)
}

test('a row names who changed what, and only an admitted row offers an undo', async () => {
  const onOpenTask = vi.fn()
  const mock = stubFetch(() => jsonResponse(page([notice(), deleteNotice(), unverifiedNotice()], CURSOR)))
  renderPanel(onOpenTask)

  await screen.findByText('Task status open to started')

  // The one page the panel asked for: the published query, at the bounded default.
  expect(String(mock.mock.calls[0][0])).toBe('/api/v1/mutation-notices?limit=20')

  const reversible = rowFor('Task status open to started')
  // Actor, source and the revision pair, in the reader's own words.
  expect(reversible.getByText(/CLI · local\.user · Revision 1 → 2/)).toBeTruthy()
  expect(reversible.getByText('Open')).toBeTruthy()
  expect(reversible.getByText('Started')).toBeTruthy()
  expect(reversible.getByRole('button', { name: /Undo/ })).toBeTruthy()

  // The click opens the Task through the existing callback, by its readable display id.
  await userEvent.click(reversible.getByRole('button', { name: /T-0001/ }))
  expect(onOpenTask).toHaveBeenCalledWith('T-0001')

  const readOnly = rowFor('Permanent deletion is not undoable')
  expect(readOnly.queryByRole('button', { name: /Undo/ })).toBeNull()
  expect(readOnly.getByText('Recorded only')).toBeTruthy()
  // A Task this workspace projection does not carry is said to be absent, never invented.
  expect(readOnly.getByText('Task not in this workspace view')).toBeTruthy()
  expect(readOnly.getByText(/App · local\.user/)).toBeTruthy()

  // A change the owner could not verify says so, and prints no revision nobody wrote.
  const unverified = rowFor('Mutation outcome is unverifiable')
  expect(unverified.queryByRole('button', { name: /Undo/ })).toBeNull()
  expect(unverified.getByText(/Agent · local\.user · Revision 1 · outcome unverified/)).toBeTruthy()
  // This one is a Capture, not a Task. The Task UID map is never asked about it, so the
  // row names the kind the owner recorded instead of diagnosing an absent Task.
  expect(unverified.getByText('Capture')).toBeTruthy()
  expect(unverified.queryByText('Task not in this workspace view')).toBeNull()

  // No request identity, and no raw document, reaches the screen.
  const rendered = document.body.textContent ?? ''
  expect(rendered).not.toContain(SECRET_KEY)
  expect(rendered).not.toContain('workstack.mutation-notice')
  expect(rendered).not.toContain(WORKSPACE_UID)
  expect(rendered).not.toContain('{')
})

test('one guarded undo is one POST, and only a proven answer says the status came back', async () => {
  let posts = 0
  const mock = stubFetch((url, init) => {
    if (String(url).includes('/undo') && init?.method === 'POST') {
      posts += 1
      return jsonResponse({ data: RESTORED, meta: { replayed: false, undone_notice_id: STATUS_NOTICE } })
    }
    return jsonResponse(page([notice()]))
  })
  const { queryClient } = renderPanel()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

  await screen.findByText('Task status open to started')
  const undo = screen.getByRole('button', { name: /Undo/ })
  // Two clicks in ONE batch, before the first request can settle: the lock is claimed
  // synchronously, so the second click has to find it taken and send nothing.
  act(() => {
    fireEvent.click(undo)
    fireEvent.click(undo)
  })

  await screen.findByText('Status restored.')
  expect(posts).toBe(1)
  expect(undoPosts(mock)).toHaveLength(1)
  const sent = JSON.parse(String((undoPosts(mock)[0][1] as RequestInit).body))
  // The revision the row displayed, which is the notice's own recorded after-revision.
  expect(sent).toEqual({ revision: 2 })

  const invalidated = invalidate.mock.calls.map(([options]) => JSON.stringify(options?.queryKey))
  expect(invalidated).toContain(JSON.stringify(['task', 'T-0001']))
  expect(invalidated).toContain(JSON.stringify(['workspace']))
  expect(invalidated).toContain(JSON.stringify(['review']))
  expect(invalidated).toContain(JSON.stringify(['mutation-notices']))
})

test('a conflict, an unknown outcome and a replaced workspace each claim nothing', async () => {
  const answers: Array<(response: Response) => void> = []
  let attempt = 0
  const keys: string[] = []
  const mock = stubFetch((url, init) => {
    if (!String(url).includes('/undo') || init?.method !== 'POST') return jsonResponse(page([notice()]))
    attempt += 1
    keys.push((init.headers as Record<string, string>)['Idempotency-Key'])
    if (attempt === 1) {
      return jsonResponse({ error: { code: 'revision_conflict', message: 'remote prose' } }, 409)
    }
    if (attempt === 2) return Promise.reject(new TypeError('owner unreachable'))
    // The third attempt is held open until the workspace under it has been replaced.
    return new Promise<Response>((resolve) => { answers.push(resolve) })
  })
  const { queryClient } = renderPanel()

  await screen.findByText('Task status open to started')

  // 1. A stale revision: understandable, determinate, and never an undo.
  await userEvent.click(screen.getByRole('button', { name: /^Undo$/ }))
  const conflict = await screen.findByLabelText('Undo outcome')
  expect(conflict.textContent).toContain('changed after this entry was recorded')
  expect(conflict.textContent).toContain('nothing was undone')
  expect(screen.queryByText('Status restored.')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

  // 2. An unreachable owner: unknown, so the panel says exactly that and holds the intent.
  await userEvent.click(screen.getByRole('button', { name: /^Undo$/ }))
  const unknown = await screen.findByLabelText('Undo outcome')
  expect(unknown.textContent).toContain('could not confirm whether this change was undone')
  expect(screen.queryByText('Status restored.')).toBeNull()
  // An unresolved outcome is not dismissable: the same-key retry is the only way out.
  expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()

  // 3. The retry is the SAME intent under the SAME key, not a second one.
  await userEvent.click(screen.getByRole('button', { name: 'Send the same undo again' }))
  await waitFor(() => expect(answers).toHaveLength(1))
  expect(keys[1]).toBe(keys[2])
  expect(keys[0]).not.toBe(keys[1])

  // 4. The workspace is replaced while that third attempt is still in flight, and its
  //    answer lands afterwards. It belongs to a workspace nobody is looking at.
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  await act(async () => {
    queryClient.setQueryData(['workspace'], { ...workspace, workspace: { id: 'replaced', name: 'Other' } })
    answers[0](new Response(
      JSON.stringify({ data: RESTORED, meta: { replayed: false, undone_notice_id: STATUS_NOTICE } }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
  })

  await waitFor(() => expect(screen.getByRole('button', { name: /^Undo$/ })).toBeTruthy())
  expect(screen.queryByText('Status restored.')).toBeNull()
  expect(screen.queryByLabelText('Undo outcome')).toBeNull()
  expect(undoPosts(mock)).toHaveLength(3)
  // A proven 200 minted under workspace A, landing on B. Dropping the feedback is not
  // enough: nothing of B's may be invalidated or refetched by it either, so the owner
  // gate has to run before the invalidation, not after it.
  expect(invalidate).not.toHaveBeenCalled()
})
