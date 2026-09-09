import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi, afterEach } from 'vitest'
import { microsoftProviderGates } from '../../config/providerGates'
import type { ApprovedReplyInput, ContextItem, ReplyCommand, ReplyReceipt, TaskDetail } from '../../domain/types'
import { capture, jsonResponse, task, workspace } from '../../test/fixtures'
import { TaskContextSubview } from './TaskContextSubview'
import type { TaskDrawerSelection } from './taskDrawerSelectors'
import type { TaskResumeFactsResult } from './useTaskResumeFacts'

/**
 * R33: the production wiring of the Task-context surface.
 *
 * The hook accepts a bare Task id as a compatibility default, so the thing worth an
 * acceptance test here is that PRODUCTION does not use it: this subview is where the
 * workspace UID and the Task UID exist, and an offer scoped only by the display id would
 * be inherited by the same `T-0001` in another workspace, or by a replacement Task —
 * along with the Undo idempotency key that names a receipt belonging to neither.
 */

const RECEIPT = '9f8e7d6c-5b4a-4392-8271-0a1b2c3d4e5f'
const WORKSPACE_UID = workspace.workspace.id
const OTHER_WORKSPACE_UID = '33333333-3333-3333-3333-333333333333'
const REPLACEMENT_UID = '44444444-4444-4444-8444-444444444444'

const linkedCapture = { ...capture, status: 'linked' as const, linked_task_ids: [task.id], revision: 4 }
const unlinked = { ...capture, status: 'inbox' as const, linked_task_ids: [], revision: 5 }

const contextItem = {
  ...linkedCapture,
  ref: { kind: 'capture', id: linkedCapture.id },
  date_precision: 'instant',
  connections: [{ target: { kind: 'task', id: task.id }, reasons: ['capture-link'] }],
} as ContextItem

const facts: TaskResumeFactsResult = {
  status: 'empty',
  workspaceUid: WORKSPACE_UID,
  taskId: task.id,
  provenance: null,
  done: [],
  next: [],
  blockers: [],
  extras: [],
  unreadableReason: null,
  errorMessage: null,
  activeRecordCount: 0,
  supersededRecordCount: 0,
  version: `v1:empty:${WORKSPACE_UID}:${task.id}`,
  retry: vi.fn(),
}

/** No reply sources: the reply section renders nothing and reaches nothing. */
const selection: TaskDrawerSelection = {
  availableDependencyTasks: [],
  availableParentTasks: [],
  childTasks: [],
  dependencyTasks: [],
  dependentTasks: [],
  parentTask: null,
  replySources: [],
  replyUnavailableSources: [],
  taskObjectives: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubFetch() {
  const mock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/unlink')) {
      return jsonResponse({ data: unlinked, meta: { duplicate: false, undo_receipt_id: RECEIPT } })
    }
    return jsonResponse({ data: { task, context: [], activity: [], replies: [] } })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

/** Never reached: with no reply sources the reply section renders nothing at all. */
const unusedReply = (_input: ApprovedReplyInput): Promise<ReplyCommand> => {
  throw new Error('the reply composer is not part of this surface')
}
const unusedReceipt = (_replyId: string, _receipt: ReplyReceipt): Promise<ReplyCommand> => {
  throw new Error('the reply composer is not part of this surface')
}

function mountSubview(initialContext: TaskDetail['context'] = [contextItem]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (workspaceUid: string, uid: string, context: TaskDetail['context']) => (
    <QueryClientProvider client={client}>
      <TaskContextSubview
        context={context}
        facts={facts}
        onBack={vi.fn()}
        onCreate={unusedReply}
        onImportReceipt={unusedReceipt}
        onToggle={vi.fn()}
        open={false}
        providerGates={microsoftProviderGates}
        replies={[] as ReplyCommand[]}
        selection={selection}
        task={{ ...task, uid }}
        taskId={task.id}
        workspaceUid={workspaceUid}
      />
    </QueryClientProvider>
  )
  const view = render(tree(WORKSPACE_UID, task.uid, initialContext))
  return {
    ...view,
    rerender: (workspaceUid: string, uid = task.uid, context = initialContext) => (
      view.rerender(tree(workspaceUid, uid, context))
    ),
  }
}

async function removeTheLink() {
  await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))
  await screen.findByRole('button', { name: 'Undo link removal' })
}

test('a confirmed removal offers Undo on the real subview composition', async () => {
  const mock = stubFetch()

  mountSubview()
  await removeTheLink()

  expect(screen.getByRole('group', { name: 'Removed task link' })).toBeInTheDocument()
  expect(mock.mock.calls.filter(([input]) => String(input).endsWith('/unlink'))).toHaveLength(1)
  // Offering is not sending.
  expect(mock.mock.calls.filter(([input]) => String(input).endsWith('/undo-unlink'))).toHaveLength(0)
})

test('the same Task id in another workspace inherits no offer', async () => {
  stubFetch()
  const view = mountSubview()
  await removeTheLink()

  view.rerender(OTHER_WORKSPACE_UID)

  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
})

test('a replacement Task reusing the display id inherits no offer', async () => {
  stubFetch()
  const view = mountSubview()
  await removeTheLink()

  view.rerender(WORKSPACE_UID, REPLACEMENT_UID)

  expect(screen.queryByRole('group', { name: 'Removed task link' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
})

test('the emptied timeline still carries the Undo the reader was offered', async () => {
  stubFetch()
  const view = mountSubview()
  await removeTheLink()

  // The refreshed Task carries no context any more, so the last card leaves. The offer
  // belongs to the view, not to the card, so it stays exactly where it was.
  view.rerender(WORKSPACE_UID, task.uid, [])

  expect(view.container.querySelector('article')).toBeNull()
  expect(screen.getByRole('button', { name: 'Undo link removal' })).toBeEnabled()
  expect(screen.getByRole('group', { name: 'Removed task link' })).toBeInTheDocument()
})

test('mounting the subview reaches no mutation route', async () => {
  const mock = stubFetch()

  mountSubview()
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })

  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled())
  const mutations = mock.mock.calls
    .map(([input]) => String(input))
    .filter((url) => url.endsWith('/unlink') || url.endsWith('/undo-unlink'))
  expect(mutations).toEqual([])
})
