import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePage } from './WorkspacePage'
import { task, workspace as populatedWorkspace } from '../../test/fixtures'
import { readSavedFilters, writeSavedFilters } from './savedFilters'
import type { WorkspaceProjection } from '../../domain/types'

const state = {
  surface: 'workspace' as const,
  view: 'graph' as const,
  search: '',
  status: 'all' as const,
  priority: 'all' as const,
  readiness: 'all' as const,
  timing: 'all' as const,
  objectiveId: 'all',
  taskId: null,
  captureId: null,
  doneVisibility: 'default' as const,
}

beforeEach(() => window.localStorage.clear())

describe('WorkspacePage first-run state', () => {
  it('offers an outcome-first path and direct task creation in an empty workspace', async () => {
    const onCreateTask = vi.fn()
    const onOpenObjectives = vi.fn()

    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={onCreateTask}
        onOpenObjectives={onOpenObjectives}
        onRefresh={vi.fn()}
        state={state}
        updateUrl={vi.fn()}
        workspace={{
          schema_version: '1.0',
          workspace: { id: '00000000-0000-4000-8000-000000000001', name: 'Work Stack' },
          tasks: [],
          objectives: [],
          notes: [],
          edges: [],
          inbox_count: 0,
        }}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Start with an outcome—or capture the first task.' })).toBeVisible()
    expect(screen.queryByRole('tablist', { name: 'Workspace view' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Define an objective' }))
    await userEvent.click(screen.getByRole('button', { name: 'Create first task' }))

    expect(onOpenObjectives).toHaveBeenCalledTimes(1)
    expect(onCreateTask).toHaveBeenCalledTimes(1)
  })
})

describe('WorkspacePage filter summary', () => {
  it('toggles Objective focus inside Workspace without navigating to Objective Hub', async () => {
    const updateUrl = vi.fn()
    const props = {
      isRefreshing: false,
      onChangeTaskStatus: vi.fn(),
      onCreateTask: vi.fn(),
      onOpenObjectives: vi.fn(),
      onRefresh: vi.fn(),
      updateUrl,
      workspace: populatedWorkspace,
    }
    const { rerender } = render(
      <WorkspacePage {...props} state={{ ...state, view: 'table' }} />,
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Focus objective O-1' }))
    expect(updateUrl).toHaveBeenLastCalledWith({
      objectiveId: 'O-1',
      taskId: null,
      captureId: null,
    })

    rerender(
      <WorkspacePage {...props} state={{ ...state, view: 'table', objectiveId: 'O-1' }} />,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Focus objective O-1' }))
    expect(updateUrl).toHaveBeenLastCalledWith({
      objectiveId: 'all',
      taskId: null,
      captureId: null,
    })
    expect(props.onOpenObjectives).not.toHaveBeenCalled()
  })

  it('reports the filtered Task count against the complete Workspace', () => {
    const blocked = {
      ...task,
      id: 'T-0002',
      uid: '22222222-2222-4222-8222-222222222223',
      title: 'Blocked follow-up',
      status: 'open' as const,
      dependencies: [task.id],
    }

    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, view: 'board', readiness: 'blocked' }}
        updateUrl={vi.fn()}
        workspace={{ ...populatedWorkspace, tasks: [task, blocked] }}
      />,
    )

    // The summary now always comes from the shared projection, and the
    // relationship total is explicitly labelled canonical rather than rendered.
    expect(screen.getByText(/1 of 2 tasks shown/)).toBeVisible()
    expect(screen.getByText(/0 canonical relationships/)).toBeVisible()
    const blockedMetric = within(screen.getByLabelText('Workspace summary')).getByText('Blocked').closest('div')
    expect(blockedMetric).toHaveTextContent('1')
    expect(blockedMetric).toHaveTextContent('2 P0 active tasks')
  })

  it('clears one active filter without resetting the others', async () => {
    const updateUrl = vi.fn()
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, view: 'board', objectiveId: 'O-1', status: 'started', readiness: 'blocked', timing: 'overdue' }}
        updateUrl={updateUrl}
        workspace={populatedWorkspace}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Clear objective filter O-1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Clear readiness filter Blocked work' }))
    await userEvent.click(screen.getByRole('button', { name: 'Clear due timing filter Overdue' }))

    expect(updateUrl).toHaveBeenNthCalledWith(1, { objectiveId: 'all' })
    expect(updateUrl).toHaveBeenNthCalledWith(2, { readiness: 'all' })
    expect(updateUrl).toHaveBeenNthCalledWith(3, { timing: 'all' })
  })

  it('creates a named saved view and completes its rename, update, and remove lifecycle', async () => {
    const updateUrl = vi.fn()
    const existing = {
      id: 'release-lane',
      name: 'Release lane',
      objectiveId: 'all',
      priority: 'all' as const,
      readiness: 'all' as const,
      timing: 'all' as const,
      search: 'release',
      status: 'all' as const,
      view: 'board' as const,
    }
    writeSavedFilters([existing])

    const props = {
      isRefreshing: false,
      onChangeTaskStatus: vi.fn(),
      onCreateTask: vi.fn(),
      onOpenObjectives: vi.fn(),
      onRefresh: vi.fn(),
      updateUrl,
      workspace: populatedWorkspace,
    }
    const { rerender } = render(<WorkspacePage {...props} state={{ ...state, view: 'board' }} />)

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Saved filters' }), 'release-lane')
    expect(updateUrl).toHaveBeenCalledWith(expect.objectContaining({ search: 'release', view: 'board', taskId: null }))

    await userEvent.click(screen.getByRole('button', { name: 'Saved view actions' }))
    await userEvent.click(screen.getByRole('button', { name: 'Rename saved view' }))
    const nameInput = screen.getByRole('textbox', { name: 'Saved view name' })
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'Release focus')
    await userEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect(readSavedFilters()[0].name).toBe('Release focus')

    rerender(<WorkspacePage {...props} state={{ ...state, view: 'table', priority: 'P0', timing: 'soon' }} />)
    await userEvent.click(screen.getByRole('button', { name: 'Update saved view' }))
    expect(readSavedFilters()[0]).toMatchObject({
      id: 'release-lane',
      name: 'Release focus',
      priority: 'P0',
      timing: 'soon',
      view: 'table',
    })

    await userEvent.click(screen.getByRole('button', { name: 'Remove saved view' }))
    expect(readSavedFilters()).toEqual([])
  })

  it('asks for a bounded name before saving the current view', async () => {
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, view: 'table', readiness: 'ready' }}
        updateUrl={vi.fn()}
        workspace={populatedWorkspace}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Save view' }))
    const nameInput = screen.getByRole('textbox', { name: 'Saved view name' })
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'Ready now')
    await userEvent.click(screen.getByRole('button', { name: 'Create saved view' }))

    expect(readSavedFilters()).toEqual([
      expect.objectContaining({ name: 'Ready now', readiness: 'ready', view: 'table' }),
    ])
  })

  it('keeps advanced filters behind one accessible disclosure', async () => {
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, view: 'board', priority: 'P0' }}
        updateUrl={vi.fn()}
        workspace={populatedWorkspace}
      />,
    )

    const disclosure = screen.getByRole('button', { name: 'Filter tasks' })
    expect(disclosure).toHaveTextContent('1')
    await userEvent.click(disclosure)
    const priority = screen.getByRole('combobox', { name: 'Filter by priority' })
    expect(priority).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Filter by objective' })).toBeVisible()
    priority.focus()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(disclosure).toHaveFocus())
    expect(screen.queryByRole('combobox', { name: 'Filter by priority' })).not.toBeInTheDocument()
  })
})

describe('WorkspacePage outcome navigator', () => {
  const outcomeWorkspace = {
    ...populatedWorkspace,
    objectives: [
      {
        id: 'O-1',
        objective: 'Release quality customers trust',
        status: 'active' as const,
        revision: 0,
        key_results: [{ id: 'KR-1', text: 'O-1 KR-1', progress: 20, status: 'active' }],
      },
      {
        id: 'O-2',
        objective: 'Zero-linked outcome',
        status: 'active' as const,
        revision: 0,
        key_results: [{ id: 'KR-1', text: 'O-2 KR-1', status: 'active' }],
      },
    ],
    tasks: [{
      ...task,
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
    }],
  }

  it('renders OutcomeNavigator, reaches a zero-linked KR, and updates the URL filter without Task activation', async () => {
    const updateUrl = vi.fn()
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, view: 'table' }}
        updateUrl={updateUrl}
        workspace={outcomeWorkspace}
      />,
    )

    expect(screen.getAllByRole('navigation', { name: 'Outcome navigator' })).toHaveLength(1)
    const trigger = screen.getByRole('button', { name: 'Outcome navigator' })
    expect(trigger).toHaveAccessibleDescription('All outcomes')
    await userEvent.click(trigger)
    const zeroLinked = screen.getByRole('button', { name: 'Select outcome O-2 KR-1' })
    expect(within(zeroLinked).getByText('0 visible of 0 linked')).toBeVisible()
    await userEvent.click(zeroLinked)
    expect(updateUrl).toHaveBeenCalledExactlyOnceWith({
      outcomeFilter: { kind: 'pair', objectiveId: 'O-2', keyResultId: 'KR-1' },
    })
  })

  it('keeps the active KR summary on a wide layout and preserves URL coordinates across Escape', async () => {
    const updateUrl = vi.fn()
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{
          ...state,
          view: 'table',
          taskId: 'T-0001',
          status: 'started',
          outcomeFilter: { kind: 'pair', objectiveId: 'O-1', keyResultId: 'KR-1' },
        }}
        updateUrl={updateUrl}
        workspace={outcomeWorkspace}
      />,
    )

    const trigger = screen.getByRole('button', { name: 'Outcome navigator' })
    expect(trigger).toHaveAccessibleDescription('O-1 · KR-1')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('tab', { name: 'Table' })).toHaveAttribute('aria-selected', 'true')

    await userEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Select outcome O-1 KR-1' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(trigger).toHaveFocus()
    expect(updateUrl).not.toHaveBeenCalled()
    expect(screen.getByRole('tab', { name: 'Table' })).toHaveAttribute('aria-selected', 'true')
  })

  it('uses the same compact empty-branch disclosure without a second live navigator', async () => {
    render(
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={state}
        updateUrl={vi.fn()}
        workspace={{
          schema_version: '1.0',
          workspace: { id: '00000000-0000-4000-8000-000000000001', name: 'Work Stack' },
          tasks: [],
          objectives: [],
          notes: [],
          edges: [],
          inbox_count: 0,
        }}
      />,
    )

    expect(screen.getAllByRole('navigation', { name: 'Outcome navigator' })).toHaveLength(1)
    const trigger = screen.getByRole('button', { name: 'Outcome navigator' })
    expect(trigger).toHaveAccessibleDescription('No outcomes')
    expect(screen.queryByRole('button', { name: /Select outcome/ })).not.toBeInTheDocument()
    await userEvent.click(trigger)
    expect(screen.getByRole('status')).toHaveTextContent('No outcomes are defined yet')
    expect(screen.getByRole('heading', { name: 'Start with an outcome—or capture the first task.' })).toBeVisible()
  })

  it('keeps the navigator reachable on a narrow layout through its disclosure trigger', async () => {
    const original = window.matchMedia
    window.matchMedia = ((query: string) => ({
      matches: query.includes('720'),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as typeof window.matchMedia
    try {
      const updateUrl = vi.fn()
      render(
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'table' }}
          updateUrl={updateUrl}
          workspace={outcomeWorkspace}
        />,
      )
      const trigger = screen.getByRole('button', { name: 'Outcome navigator' })
      await userEvent.click(trigger)
      await userEvent.click(screen.getByRole('button', { name: 'Select outcome O-2 KR-1' }))
      expect(updateUrl).toHaveBeenCalledExactlyOnceWith({
        outcomeFilter: { kind: 'pair', objectiveId: 'O-2', keyResultId: 'KR-1' },
      })
    } finally {
      window.matchMedia = original
    }
  })
})

describe('WorkspacePage Board move-to-Done retention', () => {
  interface Deferred {
    promise: Promise<void>
    resolve: () => void
    reject: (reason: Error) => void
  }

  function deferred(): Deferred {
    let resolve!: () => void
    let reject!: (reason: Error) => void
    const promise = new Promise<void>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise
      reject = rejectPromise
    })
    return { promise, resolve, reject }
  }

  const boardTask = {
    ...task,
    status: 'started' as const,
  }

  function pageProps(
    onChangeTaskStatus: (taskId: string, status: 'open' | 'started' | 'done' | 'dropped') => Promise<void>,
    tasks: WorkspaceProjection['tasks'] = [boardTask],
    doneVisibility: 'default' | 'hide' | 'show' = 'default',
  ) {
    return {
      isRefreshing: false,
      onChangeTaskStatus,
      onCreateTask: vi.fn(),
      onOpenObjectives: vi.fn(),
      onRefresh: vi.fn(),
      state: { ...state, view: 'board' as const, doneVisibility },
      updateUrl: vi.fn(),
      workspace: { ...populatedWorkspace, tasks, edges: [] },
    }
  }

  function statusSelect() {
    return screen.getByRole('combobox', { name: `Change ${boardTask.id} status` })
  }

  function boardColumn(name: 'Open' | 'In progress' | 'Done' | 'Dropped') {
    return screen.getByRole('region', { name })
  }

  it('keeps a successful Done move visible under default without fabricating a second card', async () => {
    const request = deferred()
    const onChange = vi.fn(() => request.promise)
    const props = pageProps(onChange)
    const { rerender } = render(<WorkspacePage {...props} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    expect(onChange).toHaveBeenCalledExactlyOnceWith(boardTask.id, 'done')
    expect(statusSelect()).toBeDisabled()
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)

    await act(async () => request.resolve())
    const completed = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 1 }
    rerender(<WorkspacePage {...pageProps(onChange, [completed])} />)

    const select = statusSelect()
    expect(select).toHaveValue('done')
    expect(select).toBeEnabled()
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
    expect(screen.getByText(/1 of 1 tasks shown/)).toBeVisible()
  })

  it('retains an authoritative Done card while the revision-guarded mutation is pending', async () => {
    const request = deferred()
    const onChange = vi.fn(() => request.promise)
    const props = pageProps(onChange)
    const { rerender } = render(<WorkspacePage {...props} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    const completed = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 1 }
    rerender(<WorkspacePage {...pageProps(onChange, [completed])} />)

    expect(statusSelect()).toHaveValue('done')
    expect(statusSelect()).toBeDisabled()

    await act(async () => request.resolve())

    expect(statusSelect()).toHaveValue('done')
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
  })

  it('hides the retained Done card when completed visibility is explicitly hide', async () => {
    const onChange = vi.fn().mockResolvedValue(undefined)
    const completed = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 1 }
    const props = pageProps(onChange)
    const { rerender } = render(<WorkspacePage {...props} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    rerender(<WorkspacePage {...pageProps(onChange, [completed])} />)
    expect(statusSelect()).toHaveValue('done')

    rerender(<WorkspacePage {...pageProps(onChange, [completed], 'hide')} />)
    expect(screen.queryByRole('combobox', { name: `Change ${boardTask.id} status` })).not.toBeInTheDocument()
    expect(screen.getByText(/0 of 1 tasks shown/)).toBeVisible()
  })

  it('rolls back a failed Done move instead of retaining it', async () => {
    const request = deferred()
    const onChange = vi.fn(() => request.promise)
    render(<WorkspacePage {...pageProps(onChange)} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    await act(async () => request.reject(new Error('Revision conflict')))

    expect(statusSelect()).toHaveValue('started')
    expect(statusSelect()).toBeEnabled()
    expect(screen.getByRole('alert')).toHaveTextContent('Revision conflict')
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
  })

  it('reasserts only the completed ID after a stale non-Done refresh before success', async () => {
    const kept = {
      ...boardTask,
      id: 'T-0002',
      uid: '22222222-2222-4222-8222-222222222223',
      title: 'Already retained',
      status: 'started' as const,
    }
    const firstDone = deferred()
    const secondDone = deferred()
    const onChange = vi.fn((taskId: string) => (
      taskId === kept.id ? firstDone.promise : secondDone.promise
    ))
    const { rerender } = render(<WorkspacePage {...pageProps(onChange, [boardTask, kept])} />)

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: `Change ${kept.id} status` }),
      'done',
    )
    await act(async () => firstDone.resolve())
    const keptCompleted = { ...kept, status: 'done' as const, revision: kept.revision + 1 }
    rerender(<WorkspacePage {...pageProps(onChange, [boardTask, keptCompleted])} />)
    expect(screen.getByRole('combobox', { name: `Change ${kept.id} status` })).toHaveValue('done')

    await userEvent.selectOptions(statusSelect(), 'done')
    expect(statusSelect()).toBeDisabled()

    // In-flight workspace/SSE refresh still has the target as non-Done, which
    // prunes the optimistic ID before the mutation response.
    rerender(<WorkspacePage {...pageProps(onChange, [{ ...boardTask }, keptCompleted])} />)

    await act(async () => secondDone.resolve())
    const completed = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 1 }
    rerender(<WorkspacePage {...pageProps(onChange, [completed, keptCompleted])} />)

    expect(statusSelect()).toHaveValue('done')
    expect(statusSelect()).toBeEnabled()
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
    expect(screen.getAllByRole('article', { name: `${kept.id}: ${kept.title}` })).toHaveLength(1)
    expect(screen.getByText(/2 of 2 tasks shown/)).toBeVisible()
  })

  it('uses the refreshed canonical Done Task after SSE without duplicating or keeping a stale revision', async () => {
    const onChange = vi.fn().mockResolvedValue(undefined)
    const props = pageProps(onChange)
    const { rerender } = render(<WorkspacePage {...props} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    const first = { ...boardTask, status: 'done' as const, revision: 3 }
    rerender(<WorkspacePage {...pageProps(onChange, [first])} />)
    expect(statusSelect()).toHaveValue('done')

    const refreshed = { ...first, revision: 4, detail: 'Server-authoritative detail' }
    rerender(<WorkspacePage {...pageProps(onChange, [refreshed])} />)

    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
    expect(statusSelect()).toHaveValue('done')
    expect(screen.getByText('Server-authoritative detail')).toBeVisible()
  })

  it('stops retention when an external undo returns the Task to started', async () => {
    const onChange = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(<WorkspacePage {...pageProps(onChange)} />)

    await userEvent.selectOptions(statusSelect(), 'done')
    const completed = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 1 }
    rerender(<WorkspacePage {...pageProps(onChange, [completed])} />)
    expect(statusSelect()).toHaveValue('done')
    expect(within(boardColumn('Done')).getByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toBeVisible()

    const restored = { ...boardTask, status: 'started' as const, revision: boardTask.revision + 2 }
    rerender(<WorkspacePage {...pageProps(onChange, [restored])} />)

    expect(statusSelect()).toHaveValue('started')
    expect(statusSelect()).toBeEnabled()
    expect(screen.getAllByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toHaveLength(1)
    expect(within(boardColumn('In progress')).getByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).toBeVisible()
    expect(onChange).toHaveBeenCalledExactlyOnceWith(boardTask.id, 'done')

    const doneAgain = { ...boardTask, status: 'done' as const, revision: boardTask.revision + 3 }
    rerender(<WorkspacePage {...pageProps(onChange, [doneAgain])} />)

    expect(onChange).toHaveBeenCalledExactlyOnceWith(boardTask.id, 'done')
    expect(screen.queryByRole('article', { name: `${boardTask.id}: ${boardTask.title}` })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: `Change ${boardTask.id} status` })).not.toBeInTheDocument()
    expect(screen.getByText(/0 of 1 tasks shown/)).toBeVisible()
    expect(screen.getByText('All matching tasks are completed')).toBeVisible()
  })
})
