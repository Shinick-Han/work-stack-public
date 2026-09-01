import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { expect, test, vi } from 'vitest'
import { jsonResponse } from '../test/fixtures'
import type { SyncStatus } from '../domain/types'
import { useWorkspaceRebind } from './useWorkspaceRebind'

const oldWorkspaceId = '11111111-1111-4111-8111-111111111111'
const nextWorkspaceId = '22222222-2222-4222-8222-222222222222'
const digest = (value: string) => `sha256:${value.repeat(64)}`

test('coordinates a completed server rebind with the desktop connection profile', async () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const postMessage = vi.fn((message: string) => {
    if (message.includes('rebind-start')) listener?.({
      data: { type: 'workstack-ssot-rebind-ready', workspace_id: nextWorkspaceId },
    } as Event & { data: unknown })
  })
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(), postMessage,
  } } })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/sync/rebind-preview')) return jsonResponse({ data: {
      state: 'workspace-identity-mismatch', manifest_workspace_id: oldWorkspaceId,
      candidate_workspace_id: nextWorkspaceId, manifest_digest: digest('a'),
      candidate_digest: digest('b'), changed_files: [],
    } })
    if (url.endsWith('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    if (url.endsWith('/api/v1/sync/rebind-workspace')) return jsonResponse({ data: {
      state: 'in-sync', workspace_id: nextWorkspaceId, generation: 5,
      recovery_receipt_digest: digest('c'), planning_mutated: false,
    } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const status: SyncStatus = {
    state: 'external-change-detected', workspace_id: oldWorkspaceId,
    candidate_workspace_id: nextWorkspaceId, generation: 4,
    manifest_digest: digest('a'), changed_files: [], reason: null, rebind_available: true,
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const { result } = renderHook(
    () => useWorkspaceRebind({ onRecovered: vi.fn(), open: true, status }),
    { wrapper },
  )

  await waitFor(() => expect(result.current.previewQuery.isSuccess).toBe(true))
  act(() => result.current.run())
  await waitFor(() => expect(result.current.mutation.isSuccess).toBe(true))

  expect(postMessage).toHaveBeenNthCalledWith(1,
    `workstack-ssot-host|rebind-start|${nextWorkspaceId}`,
  )
  expect(postMessage).toHaveBeenNthCalledWith(2,
    `workstack-ssot-host|rebind-complete|${nextWorkspaceId}`,
  )
})
