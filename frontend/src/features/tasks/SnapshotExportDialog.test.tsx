import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { SnapshotExportDialog } from './SnapshotExportDialog'

const digest = `sha256:${'a'.repeat(64)}`
const snapshot = {
  detail: 'Review these exact details before execution.',
  due_date: null,
  format: 'workstack.planning-task-snapshot.v1' as const,
  legacy_task_id: 'T-0001',
  origin_ref: 'workstack://0f50a123-3da8-4c82-8f16-8ee1a57260c4/planning-tasks/2e82845c-bccb-5aa6-9b6d-8ec65170c00a',
  planning_priority: 'P1' as const,
  planning_status: 'open' as const,
  planning_task_uid: '2e82845c-bccb-5aa6-9b6d-8ec65170c00a',
  revision: 3,
  title: 'Prepare the provider-neutral execution adapter',
  workspace_uid: '0f50a123-3da8-4c82-8f16-8ee1a57260c4',
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderDialog(onClose = vi.fn(), onNotice = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SnapshotExportDialog
        onClose={onClose}
        onNotice={onNotice}
        open
        taskId="T-0001"
      />
    </QueryClientProvider>,
  )
  return { onClose, onNotice }
}

test('shows exact disclosed text, snapshot boundaries, and cancels without export', async () => {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(jsonResponse({
    data: {
      digest,
      filename: `${snapshot.planning_task_uid}.workstack-task.json`,
      omissions: ['objectives', 'dependencies', 'subtasks', 'notes', 'tags'],
      snapshot,
    },
  })))
  vi.stubGlobal('fetch', fetchMock)
  const { onClose } = renderDialog()
  const dialog = await screen.findByRole('dialog', { name: 'Export to Conduit' })

  expect(await within(dialog).findByText(snapshot.title)).toBeInTheDocument()
  expect(within(dialog).getByText(snapshot.detail)).toBeInTheDocument()
  expect(within(dialog).getByText(/snapshot, not a live link/i)).toBeInTheDocument()
  expect(within(dialog).getByText(/Conduit receives a copy/i)).toBeInTheDocument()
  expect(within(dialog).getByText(/does not update Work Stack/i)).toBeInTheDocument()
  expect(within(dialog).getByText(/execution must be confirmed in Conduit/i)).toBeInTheDocument()
  expect(within(dialog).getByText(/objectives, dependencies, subtasks, notes, and tags are omitted/i)).toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: 'Save snapshot file' })).toBeDisabled()

  await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined()
})

test('requires disclosure confirmation and downloads one exact reviewed revision', async () => {
  const exactBytes = new TextEncoder().encode(`${JSON.stringify(snapshot)}\n`)
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') {
      return Promise.resolve(jsonResponse({ data: { csrf_token: 'csrf-token-1234' } }))
    }
    if (url.endsWith('/snapshot/export')) {
      return Promise.resolve(new Response(exactBytes, {
        status: 200,
        headers: {
          'Content-Disposition': `attachment; filename="${snapshot.planning_task_uid}.workstack-task.json"`,
          'Content-Type': 'application/json; charset=utf-8',
          'X-WorkStack-Snapshot-Digest': digest,
        },
      }))
    }
    return Promise.resolve(jsonResponse({
      data: {
        digest,
        filename: `${snapshot.planning_task_uid}.workstack-task.json`,
        omissions: ['objectives', 'dependencies', 'subtasks', 'notes', 'tags'],
        snapshot,
      },
    }))
  })
  vi.stubGlobal('fetch', fetchMock)
  const createObjectURL = vi.fn(() => 'blob:workstack-snapshot')
  const revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  const { onClose, onNotice } = renderDialog()
  const dialog = await screen.findByRole('dialog', { name: 'Export to Conduit' })
  const confirmation = await within(dialog).findByRole('checkbox', { name: /I reviewed the exact title and detail/i })
  const save = within(dialog).getByRole('button', { name: 'Save snapshot file' })

  await userEvent.click(confirmation)
  expect(save).toBeEnabled()
  await userEvent.click(save)

  await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
  const exportCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/snapshot/export'))
  expect(exportCall?.[1]?.method).toBe('POST')
  expect(JSON.parse(String(exportCall?.[1]?.body))).toEqual({
    disclosure_confirmed: true,
    expected_digest: digest,
    expected_revision: snapshot.revision,
  })
  expect(createObjectURL).toHaveBeenCalledTimes(1)
  expect(revokeObjectURL).toHaveBeenCalledTimes(1)
  expect(onNotice).toHaveBeenCalledWith('Snapshot download started. Work Stack remains unchanged.', 'success')
  expect(onClose).toHaveBeenCalledTimes(1)
})
