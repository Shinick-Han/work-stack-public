import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { api } from '../../api/client'
import { task } from '../../test/fixtures'
import { CommandPalette } from './CommandPalette'

function renderPalette(element: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>)
}

test('searches tasks and opens the selected task', async () => {
  vi.spyOn(api, 'search').mockResolvedValue({ query: 'customer', items: [] })
  const onOpenTask = vi.fn()
  const onClose = vi.fn()

  renderPalette(
    <CommandPalette
      onClose={onClose}
      onImportCapture={vi.fn()}
      onNavigate={vi.fn()}
      onNewTask={vi.fn()}
      onOpenCapture={vi.fn()}
      onOpenObjective={vi.fn()}
      onOpenTask={onOpenTask}
      open
      tasks={[task, { ...task, id: 'T-0002', title: 'Prepare customer review' }]}
    />,
  )

  const dialog = screen.getByRole('dialog', { name: 'Search or jump' })
  await userEvent.type(within(dialog).getByRole('searchbox'), 'customer')
  await userEvent.click(within(dialog).getByRole('option', { name: /T-0002.*Prepare customer review/i }))

  expect(onOpenTask).toHaveBeenCalledWith('T-0002')
  expect(onClose).toHaveBeenCalledOnce()
})

test('supports keyboard navigation for product actions', async () => {
  vi.spyOn(api, 'search').mockResolvedValue({ query: 'treemap', items: [] })
  const onNavigate = vi.fn()
  const onClose = vi.fn()

  renderPalette(
    <CommandPalette
      onClose={onClose}
      onImportCapture={vi.fn()}
      onNavigate={onNavigate}
      onNewTask={vi.fn()}
      onOpenCapture={vi.fn()}
      onOpenObjective={vi.fn()}
      onOpenTask={vi.fn()}
      open
      tasks={[task]}
    />,
  )

  const search = screen.getByRole('searchbox')
  await userEvent.type(search, 'treemap')
  await userEvent.keyboard('{Enter}')

  expect(onNavigate).toHaveBeenCalledWith({ surface: 'workspace', view: 'treemap' })
  expect(onClose).toHaveBeenCalledOnce()
})

test('opens a unified Objective search result', async () => {
  vi.spyOn(api, 'search').mockResolvedValue({
    query: 'release',
    items: [{
      kind: 'objective', id: 'O-1', title: 'Release confidence', subtitle: '2026-Q3 · active',
      target_kind: 'objective', target_id: 'O-1',
    }],
  })
  const onOpenObjective = vi.fn()
  renderPalette(<CommandPalette onClose={vi.fn()} onImportCapture={vi.fn()} onNavigate={vi.fn()} onNewTask={vi.fn()} onOpenCapture={vi.fn()} onOpenObjective={onOpenObjective} onOpenTask={vi.fn()} open tasks={[task]} />)

  await userEvent.type(screen.getByRole('searchbox'), 'release')
  await userEvent.click(await screen.findByRole('option', { name: /O-1.*Release confidence/i }))

  expect(api.search).toHaveBeenCalledWith('release')
  expect(onOpenObjective).toHaveBeenCalledWith('O-1')
})

test('presents Graph note search results as Context cards while preserving metadata', async () => {
  vi.spyOn(api, 'search').mockResolvedValue({
    query: 'decision',
    items: [{
      kind: 'note', id: 'N-0001', title: 'Decision context', subtitle: 'Graph note · 2 links',
      target_kind: 'workspace', target_id: null,
    }],
  })
  renderPalette(<CommandPalette onClose={vi.fn()} onImportCapture={vi.fn()} onNavigate={vi.fn()} onNewTask={vi.fn()} onOpenCapture={vi.fn()} onOpenObjective={vi.fn()} onOpenTask={vi.fn()} open tasks={[]} />)

  await userEvent.type(screen.getByRole('searchbox'), 'decision')

  expect(await screen.findByRole('option', { name: /N-0001.*Decision context/i })).toHaveTextContent('note · Context card · 2 links')
  expect(screen.queryByText(/Graph note/)).not.toBeInTheDocument()
})

test('bounds the initial task options for a 10,000-Task workspace', () => {
  const tasks = Array.from({ length: 10_000 }, (_, index) => ({
    ...task,
    id: `T-${String(index + 1).padStart(5, '0')}`,
    title: `Scale task ${index + 1}`,
  }))
  renderPalette(<CommandPalette onClose={vi.fn()} onImportCapture={vi.fn()} onNavigate={vi.fn()} onNewTask={vi.fn()} onOpenCapture={vi.fn()} onOpenObjective={vi.fn()} onOpenTask={vi.fn()} open tasks={tasks} />)

  expect(screen.getAllByRole('option').length).toBeLessThanOrEqual(30)
  expect(screen.getByRole('option', { name: /T-00001.*Scale task 1/i })).toBeVisible()
  expect(screen.queryByRole('option', { name: /T-10000.*Scale task 10000/i })).not.toBeInTheDocument()
})
