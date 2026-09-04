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


const numberedDestinations: readonly [string, number, Record<string, string>][] = [
  ['Open Graph view', 1, { surface: 'workspace', view: 'graph' }],
  ['Open Board view', 2, { surface: 'workspace', view: 'board' }],
  ['Open Treemap view', 3, { surface: 'workspace', view: 'treemap' }],
  ['Open Table view', 4, { surface: 'workspace', view: 'table' }],
  ['Open Focus', 5, { surface: 'focus' }],
  ['Open Context Inbox', 6, { surface: 'inbox' }],
  ['Open Daily Review', 7, { surface: 'review' }],
  ['Open Objective Hub', 8, { surface: 'objectives' }],
]

test.each(numberedDestinations)(
  'palette caption for %s advertises shortcut %i and navigates there when clicked',
  async (label, shortcut, target) => {
    vi.spyOn(api, 'search').mockResolvedValue({ query: '', items: [] })
    const onNavigate = vi.fn()
    renderPalette(
      <CommandPalette
        onClose={vi.fn()}
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

    const option = screen.getByRole('option', { name: new RegExp(label, 'i') })
    // The caption a user reads must name the same number the App key handler
    // uses; a mismatch here is exactly the T-0004 defect.
    expect(option).toHaveTextContent(new RegExp(`shortcut ${shortcut}\\b`))
    await userEvent.click(option)
    expect(onNavigate).toHaveBeenCalledWith(target)
  },
)

test('lists the eight numbered destinations in shortcut order', () => {
  vi.spyOn(api, 'search').mockResolvedValue({ query: '', items: [] })
  renderPalette(<CommandPalette onClose={vi.fn()} onImportCapture={vi.fn()} onNavigate={vi.fn()} onNewTask={vi.fn()} onOpenCapture={vi.fn()} onOpenObjective={vi.fn()} onOpenTask={vi.fn()} open tasks={[task]} />)

  const captions = screen.getAllByRole('option')
    .map((option) => option.textContent ?? '')
    .filter((text) => /shortcut \d/.test(text))
    .map((text) => Number(/shortcut (\d)/.exec(text)?.[1]))

  expect(captions).toEqual([1, 2, 3, 4, 5, 6, 7, 8])
})