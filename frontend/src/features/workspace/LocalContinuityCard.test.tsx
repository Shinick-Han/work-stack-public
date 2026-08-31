import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { LocalContinuityCard } from './LocalContinuityCard'

function response(value: unknown, status = 200, headers?: Record<string, string>) {
  return Promise.resolve(new Response(
    value instanceof Blob ? value : JSON.stringify(value),
    { status, headers: { 'Content-Type': 'application/json', ...headers } },
  ))
}

test('requires disclosure and starts a header-verified full backup download', async () => {
  const workspaceId = '0c273163-439f-5c62-8d86-01b009d71805'
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/storage') return response({ data: {
      workspace_id: workspaceId,
      store_schema_version: 3,
      product_version: '1.0.0',
      file_count: 9,
      total_bytes: 4096,
      backup_format: 'workstack-backup-v1',
      restore_requires_shutdown: true,
    } })
    if (url === '/api/v1/session') return response({ data: { csrf_token: 'csrf-token-1234' } })
    if (url === '/api/v1/maintenance/backup' && init?.method === 'POST') return response(
      new Blob(['verified backup']),
      200,
      {
        'Content-Type': 'application/zip',
        'Content-Disposition': 'attachment; filename="workstack-backup-20260830T130000Z-0c273163.zip"',
        'X-WorkStack-Backup-Digest': `sha256:${'a'.repeat(64)}`,
        'X-WorkStack-Workspace-Id': workspaceId,
      },
    )
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:backup'), revokeObjectURL: vi.fn() })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  const notice = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><LocalContinuityCard onNotice={notice} /></QueryClientProvider>)

  expect(await screen.findByText('Ready · schema 3')).toBeVisible()
  const download = screen.getByRole('button', { name: 'Download verified backup' })
  expect(download).toBeDisabled()
  await userEvent.click(screen.getByRole('checkbox'))
  await userEvent.click(download)

  await waitFor(() => expect(click).toHaveBeenCalledOnce())
  expect(notice).toHaveBeenCalledWith('Verified local backup download started.', 'success')
  expect(screen.getByText('workstack-backup-20260830T130000Z-0c273163.zip')).toBeVisible()
  expect(JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).endsWith('/maintenance/backup'))?.[1]?.body))).toEqual({ confirmed: true })
})

test('shows release identity and copies an allowlisted support summary without workspace identity', async () => {
  const workspaceId = '0c273163-439f-5c62-8d86-01b009d71805'
  vi.stubGlobal('fetch', vi.fn(() => response({ data: {
    workspace_id: workspaceId,
    store_schema_version: 3,
    product_version: '1.0.0',
    file_count: 9,
    total_bytes: 4096,
    backup_format: 'workstack-backup-v1',
    restore_requires_shutdown: true,
  } })))
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const notice = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(<QueryClientProvider client={client}><LocalContinuityCard onNotice={notice} /></QueryClientProvider>)

  expect(await screen.findByText('Work Stack 1.0.0')).toBeVisible()
  expect(screen.getByText('Manual, verified updates')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Copy safe support summary' }))

  expect(writeText).toHaveBeenCalledOnce()
  const summary = String(writeText.mock.calls[0][0])
  expect(summary).toContain('Product version: 1.0.0')
  expect(summary).toContain('Store schema: 3')
  expect(summary).toContain('Data files: 9')
  expect(summary).not.toContain(workspaceId)
  expect(summary).not.toMatch(/task|objective|capture|recipient|path:/i)
  expect(notice).toHaveBeenCalledWith('Safe support summary copied.', 'success')
})
