import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { TaskActionsDialog } from './TaskActionsDialog'

const workspaceUid = workspace.workspace.id

const deletionPreviewPayload = {
  backup: {
    created: false,
    location: '/runtime/task-deletion-backups/pending',
    retention: 'retain-until-operator-purge',
  },
  modified_references: { notes: ['N-1'], tasks: ['T-0002'] },
  preview_token: 'preview-token-1',
  removed_task_owned_records: {
    activity_events: 1,
    idempotency_keys: 0,
    notes: 2,
    planning_events: 1,
    replies: 0,
    work_sessions: 0,
    worklog_entries: 1,
  },
  store_digest: 'digest-1',
  task: { id: task.id, revision: task.revision, title: task.title, uid: task.uid },
  unlinked_captures: { actions: 1, captures: ['C-0001'] },
}

const deletionReceiptPayload = {
  backup: { digest: 'sha256:abc', location: '/runtime/task-deletion-backups/file.zip' },
  deleted: true as const,
  display_id_high_water: 12,
  generation: 4,
  revision: task.revision,
  task_id: task.id,
  task_uid: task.uid,
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderDialog(props: {
  onClose?: ReturnType<typeof vi.fn>
  onNotice?: ReturnType<typeof vi.fn>
  onPermanentlyDeleted?: ReturnType<typeof vi.fn>
  onSaved?: ReturnType<typeof vi.fn>
  task?: typeof task
} = {}) {
  const onClose = props.onClose ?? vi.fn()
  const onNotice = props.onNotice ?? vi.fn()
  const onPermanentlyDeleted = props.onPermanentlyDeleted ?? vi.fn()
  const onSaved = props.onSaved ?? vi.fn()
  render(
    <TaskActionsDialog
      onClose={onClose}
      onNotice={onNotice}
      onPermanentlyDeleted={onPermanentlyDeleted}
      onSaved={onSaved}
      open
      task={props.task ?? task}
      workspaceUid={workspaceUid}
    />,
  )
  return { onClose, onNotice, onPermanentlyDeleted, onSaved }
}

test('adds a revision-guarded Task note and reports the updated Task', async () => {
  const updated = { ...task, revision: task.revision + 1, notes: [{ date: '2026-08-30', text: 'Keep the rollback owner visible.' }] }
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    if (String(input) === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    return Promise.resolve(jsonResponse({ data: updated }))
  })
  vi.stubGlobal('fetch', fetchMock)
  const onClose = vi.fn()
  const onSaved = vi.fn()
  const onNotice = vi.fn()

  renderDialog({ onClose, onNotice, onSaved })
  const dialog = screen.getByRole('dialog', { name: 'Task actions' })
  await userEvent.type(within(dialog).getByLabelText('New log entry'), 'Keep the rollback owner visible.')
  await userEvent.click(within(dialog).getByRole('button', { name: 'Add log entry' }))

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(updated))
  const mutation = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/notes'))
  expect(mutation?.[1]?.method).toBe('POST')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
    text: 'Keep the rollback owner visible.',
    revision: task.revision,
  })
  expect(onNotice).toHaveBeenCalledWith('Task log entry added')
  expect(onClose).toHaveBeenCalledTimes(1)
})

test('reuses one Task-note intent key after response loss', async () => {
  const updated = { ...task, revision: task.revision + 1, notes: [{ date: '2026-08-30', text: 'One intent' }] }
  let noteAttempts = 0
  const noteKeys: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (String(input).endsWith('/notes')) {
      noteAttempts += 1
      noteKeys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      if (noteAttempts <= 2) return Promise.reject(new TypeError('response lost'))
      return Promise.resolve(jsonResponse({ data: updated, meta: { replayed: true } }))
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onSaved = vi.fn()

  renderDialog({ onSaved })
  await userEvent.type(screen.getByLabelText('New log entry'), 'One intent')
  await userEvent.click(screen.getByRole('button', { name: 'Add log entry' }))
  await screen.findByRole('alert')
  await userEvent.click(screen.getByRole('button', { name: 'Add log entry' }))

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(updated))
  const calls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/notes'))
  expect(calls).toHaveLength(3)
  expect(new Set(noteKeys).size).toBe(1)
})

test('adds and updates subtasks through the Task revision boundary', async () => {
  const existing = { id: 'S-1', title: 'Draft checklist', priority: 'P1' as const, status: 'open' as const }
  const withExisting = { ...task, subtasks: [existing] }
  const updated = { ...withExisting, revision: task.revision + 1, subtasks: [{ ...existing, status: 'done' as const }] }
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    if (String(input) === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    return Promise.resolve(jsonResponse({ data: updated }))
  })
  vi.stubGlobal('fetch', fetchMock)
  const onSaved = vi.fn()

  renderDialog({ onSaved, task: withExisting })
  await userEvent.selectOptions(screen.getByLabelText('Change S-1 status'), 'done')

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(updated))
  const mutation = fetchMock.mock.calls.find(([input]) => String(input).includes('/subtasks/S-1'))
  expect(mutation?.[1]?.method).toBe('PATCH')
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({ status: 'done', revision: task.revision })
})

test('verifies a committed subtask status after its response is lost', async () => {
  const existing = { id: 'S-1', title: 'Draft checklist', priority: 'P1' as const, status: 'open' as const }
  const withExisting = { ...task, subtasks: [existing] }
  const committed = { ...withExisting, revision: task.revision + 1, subtasks: [{ ...existing, status: 'done' as const }] }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (init?.method === 'PATCH') return Promise.reject(new TypeError('response lost after commit'))
    if (url.endsWith(`/api/v1/tasks/${task.id}`)) {
      return Promise.resolve(jsonResponse({ data: { task: committed, context: [], activity: [], replies: [] } }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onClose = vi.fn()
  const onNotice = vi.fn()
  const onSaved = vi.fn()

  renderDialog({ onClose, onNotice, onSaved, task: withExisting })
  await userEvent.selectOptions(screen.getByLabelText('Change S-1 status'), 'done')

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committed))
  expect(onNotice).toHaveBeenCalledWith('S-1 moved to Done (verified after reconnect)')
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('requires the exact Task ID before deleting active work while preserving a dropped projection', async () => {
  const deleted = { ...task, revision: task.revision + 1, status: 'dropped' as const }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.endsWith(`/api/v1/tasks/${task.id}`) && init?.method === 'PATCH') {
      return Promise.resolve(jsonResponse({ data: deleted }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onClose = vi.fn()
  const onNotice = vi.fn()
  const onPermanentlyDeleted = vi.fn()
  const onSaved = vi.fn()

  renderDialog({ onClose, onNotice, onPermanentlyDeleted, onSaved })
  const dangerZone = screen.getByRole('region', { name: 'Drop Task' })
  await userEvent.click(within(dangerZone).getByRole('button', { name: 'Drop Task…' }))

  const confirmation = within(dangerZone).getByLabelText(`Type ${task.id} to confirm`)
  const submit = within(dangerZone).getByRole('button', { name: 'Drop Task' })
  expect(submit).toBeDisabled()
  await userEvent.type(confirmation, 'wrong-task')
  expect(submit).toBeDisabled()
  expect(fetchMock).not.toHaveBeenCalled()

  await userEvent.clear(confirmation)
  await userEvent.type(confirmation, task.id)
  expect(submit).toBeEnabled()
  await userEvent.click(submit)

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(deleted))
  const mutation = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({ status: 'dropped', revision: task.revision })
  expect(onNotice).toHaveBeenCalledWith(`${task.id} dropped from active work; immutable history preserved`)
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onPermanentlyDeleted).not.toHaveBeenCalled()
})

test('Wave 0 D0: Drop Task and Delete permanently are distinct actions (plan §8.3)', async () => {
  renderDialog()
  expect(screen.getByRole('button', { name: 'Drop Task…' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Delete permanently/ })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Delete Task' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Delete Task…' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Delete Task' })).not.toBeInTheDocument()
})

test('shows a read-only deletion preview before permanent delete is possible', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.endsWith(`/api/v1/tasks/${task.id}/deletion-preview`)) {
      return Promise.resolve(jsonResponse({ data: deletionPreviewPayload }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onPermanentlyDeleted = vi.fn()
  renderDialog({ onPermanentlyDeleted })

  const permanent = screen.getByRole('region', { name: 'Delete permanently' })
  await userEvent.click(within(permanent).getByRole('button', { name: 'Delete permanently…' }))

  expect(await screen.findByText(`${task.id} · ${task.title}`)).toBeInTheDocument()
  expect(screen.getByText(String(task.revision))).toBeInTheDocument()
  expect(screen.getByText(task.uid)).toBeInTheDocument()
  expect(screen.getByText(/1 activity events, 0 idempotency keys, 2 notes/)).toBeInTheDocument()
  expect(screen.getByText('1 notes, 1 tasks')).toBeInTheDocument()
  expect(screen.getByText('1 actions, 1 captures')).toBeInTheDocument()
  expect(screen.getByText('retain-until-operator-purge · /runtime/task-deletion-backups/pending')).toBeInTheDocument()
  expect(within(permanent).getByRole('button', { name: 'Delete permanently' })).toBeDisabled()
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
  expect(onPermanentlyDeleted).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: /undo/i })).not.toBeInTheDocument()
})

test('permanent DELETE stays gated on the exact Task ID and one frozen idempotency key', async () => {
  const keys: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.endsWith(`/api/v1/tasks/${task.id}/deletion-preview`)) {
      keys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      return Promise.resolve(jsonResponse({ data: deletionPreviewPayload }))
    }
    if (url.endsWith(`/api/v1/tasks/${task.id}`) && init?.method === 'DELETE') {
      keys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      return Promise.resolve(jsonResponse({ data: deletionReceiptPayload }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onPermanentlyDeleted = vi.fn()
  const onSaved = vi.fn()
  renderDialog({ onPermanentlyDeleted, onSaved })

  const permanent = screen.getByRole('region', { name: 'Delete permanently' })
  await userEvent.click(within(permanent).getByRole('button', { name: 'Delete permanently…' }))
  const submit = await within(permanent).findByRole('button', { name: 'Delete permanently' })
  const confirmation = within(permanent).getByLabelText(`Type ${task.id} to confirm permanent deletion`)
  expect(submit).toBeDisabled()
  await userEvent.type(confirmation, 'wrong-task')
  expect(submit).toBeDisabled()
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)

  await userEvent.clear(confirmation)
  await userEvent.type(confirmation, task.id)
  expect(submit).toBeEnabled()
  await userEvent.click(submit)

  await waitFor(() => expect(onPermanentlyDeleted).toHaveBeenCalledWith(deletionReceiptPayload))
  expect(onSaved).not.toHaveBeenCalled()
  expect(new Set(keys).size).toBe(1)
  const previewBody = JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).includes('/deletion-preview'))?.[1]?.body))
  expect(previewBody.client_request_id).toBe(keys[0])
  expect(previewBody.workspace_uid).toBe(workspaceUid)
  expect(screen.queryByRole('button', { name: /undo/i })).not.toBeInTheDocument()
})

test('does not invoke permanent deletion after preview failure, stale preview, or commit-unknown', async () => {
  const onPermanentlyDeleted = vi.fn()
  const onSaved = vi.fn()

  const previewFail = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.includes('/deletion-preview')) {
      return Promise.resolve(jsonResponse({ error: { code: 'preview_stale', message: 'stale' } }, 409))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', previewFail)
  renderDialog({ onPermanentlyDeleted, onSaved })
  await userEvent.click(screen.getByRole('button', { name: 'Delete permanently…' }))
  expect(await screen.findByRole('alert')).toBeInTheDocument()
  expect(onPermanentlyDeleted).not.toHaveBeenCalled()
  expect(onSaved).not.toHaveBeenCalled()

  const stale = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.includes('/deletion-preview')) {
      return Promise.resolve(jsonResponse({
        data: { ...deletionPreviewPayload, task: { ...deletionPreviewPayload.task, revision: 99 } },
      }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', stale)
  renderDialog({ onPermanentlyDeleted, onSaved })
  await userEvent.click(screen.getAllByRole('button', { name: 'Delete permanently…' })[1])
  await screen.findAllByRole('alert')
  expect(onPermanentlyDeleted).not.toHaveBeenCalled()

  const unknown = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.includes('/deletion-preview')) return Promise.resolve(jsonResponse({ data: deletionPreviewPayload }))
    if (init?.method === 'DELETE') return Promise.resolve(jsonResponse({ data: { deleted: true } }))
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', unknown)
  renderDialog({ onPermanentlyDeleted, onSaved })
  const regions = screen.getAllByRole('region', { name: 'Delete permanently' })
  const last = regions[regions.length - 1]
  await userEvent.click(within(last).getByRole('button', { name: 'Delete permanently…' }))
  const confirm = await within(last).findByLabelText(`Type ${task.id} to confirm permanent deletion`)
  await userEvent.type(confirm, task.id)
  await userEvent.click(within(last).getByRole('button', { name: 'Delete permanently' }))
  await waitFor(() => expect(onPermanentlyDeleted).not.toHaveBeenCalled())
  expect(onSaved).not.toHaveBeenCalled()
})

test('restarting the preview mints a new idempotency key', async () => {
  const keys: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    if (url.includes('/deletion-preview')) {
      keys.push((init?.headers as Record<string, string>)['Idempotency-Key'])
      return Promise.resolve(jsonResponse({ data: deletionPreviewPayload }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  renderDialog()
  const permanent = screen.getByRole('region', { name: 'Delete permanently' })
  await userEvent.click(within(permanent).getByRole('button', { name: 'Delete permanently…' }))
  await within(permanent).findByRole('button', { name: 'Delete permanently' })
  await userEvent.click(within(permanent).getByRole('button', { name: 'Cancel' }))
  await userEvent.click(within(permanent).getByRole('button', { name: 'Delete permanently…' }))
  await within(permanent).findByRole('button', { name: 'Delete permanently' })
  expect(keys).toHaveLength(2)
  expect(keys[0]).not.toBe(keys[1])
})
