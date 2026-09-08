import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { jsonResponse, task, workspace } from '../../test/fixtures'
import { TaskDrawer } from './TaskDrawer'

test('knowledge context receives the saved Task while title edits are unconfirmed', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ data: { workspace_uid: '123e4567-e89b-42d3-a456-426614174000', entries: [] } })
    }
    return options?.method === 'PATCH'
      ? new Promise<Response>(() => {})
      : jsonResponse({ data: { task, context: [], activity: [], replies: [] } })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} taskId={task.id} workspace={workspace} />
    </QueryClientProvider>,
  )
  await screen.findByRole('tab', { name: 'Resume' })
  await userEvent.click(screen.getByRole('tab', { name: 'Details' }))
  const title = await screen.findByRole('textbox', { name: 'Task title' })
  await userEvent.clear(title)
  await userEvent.type(title, 'Unconfirmed draft title')
  await userEvent.click(screen.getByRole('tab', { name: 'Resume' }))
  await userEvent.click(screen.getByRole('button', { name: /View all context/i }))
  expect(await screen.findByLabelText(`Prepare a resume brief for ${task.title}`)).toBeInTheDocument()
  expect(screen.queryByLabelText('Prepare a resume brief for Unconfirmed draft title')).not.toBeInTheDocument()
})
