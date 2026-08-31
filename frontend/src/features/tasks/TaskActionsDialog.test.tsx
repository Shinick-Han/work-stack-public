import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { task } from '../../test/fixtures'
import { TaskActionsDialog } from './TaskActionsDialog'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
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

  render(<TaskActionsDialog onClose={onClose} onNotice={onNotice} onSaved={onSaved} open task={task} />)
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

  render(<TaskActionsDialog onClose={vi.fn()} onNotice={vi.fn()} onSaved={onSaved} open task={task} />)
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

  render(<TaskActionsDialog onClose={vi.fn()} onNotice={vi.fn()} onSaved={onSaved} open task={withExisting} />)
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

  render(<TaskActionsDialog onClose={onClose} onNotice={onNotice} onSaved={onSaved} open task={withExisting} />)
  await userEvent.selectOptions(screen.getByLabelText('Change S-1 status'), 'done')

  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committed))
  expect(onNotice).toHaveBeenCalledWith('S-1 moved to Done (verified after reconnect)')
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
