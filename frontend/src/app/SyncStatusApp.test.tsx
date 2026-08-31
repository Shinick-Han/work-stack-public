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
        generation: 4,
        manifest_digest: `sha256:${'a'.repeat(64)}`,
        changed_files: state === 'external-change-detected' ? ['tasks.json'] : [],
        reason: state === 'external-change-detected' ? 'External edit detected' : null,
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
  const review = await screen.findByRole('button', { name: 'Review SSOT changes' })
  expect(document.querySelector('main')).toHaveAttribute('inert')

  await userEvent.click(review)
  expect(await screen.findByRole('dialog', { name: 'Review SSOT changes' })).toHaveTextContent('tasks.json')
  expect(reads.getStatusReads()).toBe(1)
  await userEvent.click(screen.getByRole('button', { name: 'Refresh authoritative state' }))
  await waitFor(() => expect(reads.getStatusReads()).toBeGreaterThan(1))
  expect(reads.getWorkspaceReads()).toBeGreaterThan(1)
})

test('invalidates authoritative queries from a content-free SSE sync hint', async () => {
  const reads = renderWithStatus('in-sync')
  expect(await screen.findByRole('button', { name: 'SSOT in sync' })).toBeDisabled()
  await waitFor(() => expect(AppEventSource.instance).not.toBeNull())
  const initialWorkspaceReads = reads.getWorkspaceReads()

  AppEventSource.instance?.emitSync(5)
  await waitFor(() => expect(reads.getWorkspaceReads()).toBeGreaterThan(initialWorkspaceReads))
})
