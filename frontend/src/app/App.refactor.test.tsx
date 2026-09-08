import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { test, vi } from 'vitest'
import { App } from './App'
import { AppNotice } from './appShellPresentation'
import { api, ApiError } from '../api/client'
import { jsonResponse, workspace } from '../test/fixtures'
import { deriveSyncPresentation } from './useAppShellSync'

test('keeps the public App export callable with the established optional shell props', async () => {
  vi.spyOn(api, 'getSyncStatus').mockRejectedValue(new ApiError(404, 'not_found', 'Optional legacy endpoint'))
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  await screen.findByRole('heading', { name: /keep execution connected/i }, { timeout: 5_000 })
  expect(document.querySelector('.app-shell')).toBeTruthy()
  expect(document.querySelector('.app-shell')).not.toHaveClass('is-write-blocked')
})

test('extracted sync presentation still treats a lone 404 as optional and other errors as fail-closed', () => {
  const missing = new ApiError(404, 'not_found', 'Optional legacy endpoint')
  expect(deriveSyncPresentation(missing, undefined, false)).toEqual({
    effectiveError: null,
    endpointDisconnected: false,
    writeBlocked: false,
  })
  const down = new ApiError(503, 'unavailable', 'sync unavailable')
  const blocked = deriveSyncPresentation(down, undefined, false)
  expect(blocked.effectiveError).toBe(down)
  expect(blocked.endpointDisconnected).toBe(true)
  expect(blocked.writeBlocked).toBe(true)
})

test('extracted notice presentation still offers View beside the keyed Mark action', () => {
  render(
    <AppNotice
      intent={{
        canMarkInProgress: true,
        markInProgress: () => undefined,
        pending: false,
        retry: () => undefined,
        retryable: false,
        undo: () => undefined,
        undoAvailable: false,
      }}
      notice={{
        action: { label: 'View', run: () => undefined },
        id: 1,
        message: 'Agent CLI recorded a checkpoint for T-0001',
        tone: 'success',
      }}
      onDismiss={() => undefined}
    />,
  )
  expect(screen.getByRole('status')).toHaveTextContent('Agent CLI recorded a checkpoint for T-0001')
  expect(screen.getByRole('button', { name: 'View' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Mark In Progress' })).toBeInTheDocument()
})
