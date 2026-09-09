import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'
import { api, ApiError } from '../api/client'
import { KNOWLEDGE_CAPTURE_IMPORT_PATH } from '../api/knowledgeCapture'
import { knowledgeImportEnvelope, REQUEST_ID } from '../features/inbox/knowledgeCaptureFixture'
import { jsonResponse, workspace } from '../test/fixtures'

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.dataset.theme = 'dark'
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.spyOn(api, 'getSyncStatus').mockRejectedValue(new ApiError(404, 'not_found', 'Optional legacy endpoint'))
})

const successEnvelope = {
  data: {
    request_id: REQUEST_ID,
    capture_ids: ['C-0008'],
    completion_digest: `sha256:${'d'.repeat(64)}`,
    completed_at: '2026-09-08T11:00:00Z',
  },
  meta: { replayed: false, imported_count: 1 },
}

function reviewPayload() {
  return {
    data: {
      day: { date: '2026-08-30', start_time: null, entries: [] },
      weekly: { range: { start: '2026-08-24', end: '2026-08-30', days: 7 }, objectives: [], projects: [] },
    },
  }
}

function renderApp() {
  window.history.replaceState(null, '', '/')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    ...render(<QueryClientProvider client={client}><App /></QueryClientProvider>),
  }
}

function stubAppFetch(importPayload: unknown) {
  const counts = { captures: 0, workspace: 0, importPosts: 0 }
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.includes('/api/v1/workspace')) {
      counts.workspace += 1
      return jsonResponse({ data: workspace })
    }
    if (url.includes('/api/v1/review')) return jsonResponse(reviewPayload())
    if (url.startsWith('/api/v1/captures') && init?.method !== 'POST') {
      counts.captures += 1
      return jsonResponse({ data: { captures: [] } })
    }
    if (url === KNOWLEDGE_CAPTURE_IMPORT_PATH && init?.method === 'POST') {
      counts.importPosts += 1
      return jsonResponse(importPayload)
    }
    if (url.includes('/api/v1/objectives/O-1')) {
      return jsonResponse({ data: { objective: workspace.objectives[0], tasks: workspace.tasks, activity: [] } })
    }
    if (url.includes('/api/v1/tasks/T-0001')) {
      return jsonResponse({ data: { task: workspace.tasks[0], context: [], activity: [], replies: [] } })
    }
    return jsonResponse({ error: { code: 'not_found', message: 'unhandled test path' } }, 404)
  })
  vi.stubGlobal('fetch', mock)
  return { counts, mock }
}

async function openKnowledgeImport() {
  await screen.findByRole('heading', { name: /keep execution connected/i }, { timeout: 5_000 })
  await userEvent.click(screen.getByRole('button', { name: /context inbox/i }))
  await screen.findByRole('heading', { name: /turn signal into useful work/i })
  await userEvent.click(screen.getByRole('button', { name: 'Import packet' }))
  const dialog = await screen.findByRole('dialog', { name: 'Import context' })
  await userEvent.click(screen.getByRole('tab', { name: 'Knowledge result' }))
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(knowledgeImportEnvelope()) },
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Import into Inbox' })).toBeEnabled())
  return dialog
}

test('accepted knowledge import invalidates captures and workspace and closes; malformed success stays open', async () => {
  const success = stubAppFetch(successEnvelope)
  const { client, unmount } = renderApp()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await openKnowledgeImport()
  const capturesBefore = success.counts.captures
  const workspaceBefore = success.counts.workspace
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(success.counts.importPosts).toBe(1))
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['captures'] }))
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['workspace'] }))
  })
  await waitFor(() => {
    expect(screen.queryByRole('dialog', { name: 'Import context' })).not.toBeInTheDocument()
  })
  expect(screen.queryByText(/Imported C-/i)).not.toBeInTheDocument()
  await waitFor(() => {
    expect(success.counts.captures).toBeGreaterThan(capturesBefore)
    expect(success.counts.workspace).toBeGreaterThan(workspaceBefore)
  })
  unmount()
  invalidate.mockRestore()

  const mismatched = stubAppFetch({
    ...successEnvelope,
    data: { ...successEnvelope.data, request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' },
  })
  const second = renderApp()
  const secondInvalidate = vi.spyOn(second.client, 'invalidateQueries')
  const dialog = await openKnowledgeImport()
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(mismatched.counts.importPosts).toBe(1))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  expect(dialog).toHaveAttribute('open')
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeVisible()
  expect(secondInvalidate).not.toHaveBeenCalled()
  expect(screen.queryByText(/Imported /i)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry same import' })).toBeEnabled()
}, 20_000)
