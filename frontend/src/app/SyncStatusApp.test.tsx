import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { App } from './App'
import { jsonResponse, workspace } from '../test/fixtures'

class AppEventSource {
  static instance: AppEventSource | null = null
  listeners = new Map<string, EventListener>()
  close() {}

  constructor() {
    AppEventSource.instance = this
  }

  addEventListener(name: string, listener: EventListener) {
    this.listeners.set(name, listener)
  }

  emitSync(generation: number) {
    this.listeners.get('sync')?.({ data: JSON.stringify({ generation, state: 'in-sync' }) } as MessageEvent)
  }
}

function renderWithStatus(state: 'in-sync' | 'external-change-detected') {
  let workspaceReads = 0
  let statusReads = 0
  vi.stubGlobal('EventSource', AppEventSource)
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/sync/status')) {
      statusReads += 1
      return jsonResponse({ data: {
        state,
        workspace_id: 'workspace-test',
        candidate_workspace_id: 'workspace-test',
        generation: 4,
        manifest_digest: `sha256:${'a'.repeat(64)}`,
        changed_files: state === 'external-change-detected' ? ['tasks.json'] : [],
        reason: state === 'external-change-detected' ? 'External edit detected' : null,
        rebind_available: false,
      } })
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: workspace })
    }
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  return { getStatusReads: () => statusReads, getWorkspaceReads: () => workspaceReads }
}

test('blocks writes and opens a read-only review when the SSOT changed externally', async () => {
  const reads = renderWithStatus('external-change-detected')
  const review = await screen.findByRole('button', { name: 'Sync / review SSOT changes' })
  expect(document.querySelector('main')).not.toHaveAttribute('inert')
  expect(document.querySelector('.app-main__write-surface')).toHaveAttribute('inert')

  await userEvent.click(review)
  expect(await screen.findByRole('dialog', { name: 'Review SSOT update' })).toHaveTextContent('tasks.json')
  expect(reads.getStatusReads()).toBe(1)
  await userEvent.click(screen.getByRole('button', { name: 'Refresh comparison' }))
  await waitFor(() => expect(reads.getStatusReads()).toBeGreaterThan(1))
  expect(reads.getWorkspaceReads()).toBeGreaterThan(1)
})

test('does not offer a retry that cannot resolve a workspace identity mismatch', async () => {
  let workspaceReads = 0
  vi.stubGlobal('EventSource', AppEventSource)
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/sync/status')) return jsonResponse({ data: {
      state: 'external-change-detected',
      workspace_id: 'workspace-test',
      candidate_workspace_id: 'workspace-test',
      generation: 4,
      manifest_digest: `sha256:${'a'.repeat(64)}`,
      changed_files: ['tasks.json'],
      reason: 'External edit detected',
      rebind_available: true,
    } })
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({
        error: { code: 'workspace_unavailable', message: 'authoritative store changed outside Work Stack' },
      }, 409)
    }
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>)

  expect(await screen.findByText('authoritative store changed outside Work Stack')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  expect(document.querySelector('main')).not.toHaveAttribute('inert')
  expect(workspaceReads).toBe(1)
})

test('invalidates authoritative queries from a content-free SSE sync hint', async () => {
  const reads = renderWithStatus('in-sync')
  expect(await screen.findByRole('button', { name: 'SSOT in sync' })).toBeDisabled()
  await waitFor(() => expect(AppEventSource.instance).not.toBeNull())
  const initialWorkspaceReads = reads.getWorkspaceReads()

  AppEventSource.instance?.emitSync(5)
  await waitFor(() => expect(reads.getWorkspaceReads()).toBeGreaterThan(initialWorkspaceReads))
})

test('reuses one adoption operation key across an explicit unchanged retry', async () => {
  const digest = `sha256:${'b'.repeat(64)}`
  let adoptionAttempts = 0
  vi.stubGlobal('EventSource', AppEventSource)
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/sync/adopt')) {
      adoptionAttempts += 1
      if (adoptionAttempts === 1) return jsonResponse({ error: { code: 'temporarily_unavailable', message: 'retry safely' } }, 503)
      return jsonResponse({ data: {
        state: 'in-sync', workspace_id: 'workspace-test', generation: 5,
        candidate_workspace_id: 'workspace-test',
        manifest_digest: digest, changed_files: [], reason: null, rebind_available: false,
      } })
    }
    if (url.includes('/api/v1/sync/status')) return jsonResponse({ data: {
      state: 'external-change-detected', workspace_id: 'workspace-test', generation: 4,
      candidate_workspace_id: 'workspace-test',
      manifest_digest: digest, changed_files: ['tasks.json'], reason: null, rebind_available: false,
    } })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>)

  await userEvent.click(await screen.findByRole('button', { name: 'Sync / review SSOT changes' }))
  await userEvent.click(screen.getByRole('button', { name: 'Accept reviewed candidate' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('retry safely')
  await userEvent.click(screen.getByRole('button', { name: 'Accept reviewed candidate' }))
  await waitFor(() => expect(adoptionAttempts).toBe(2))

  const adoptionCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/sync/adopt'))
  const keys = adoptionCalls.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])
  expect(keys[0]).toMatch(/^workstack:/)
  expect(keys[1]).toBe(keys[0])
})
