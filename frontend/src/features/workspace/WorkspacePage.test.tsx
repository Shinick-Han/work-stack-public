import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePage } from './WorkspacePage'
import { task, workspace as populatedWorkspace } from '../../test/fixtures'
import { readSavedFilters, writeSavedFilters } from './savedFilters'

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

    expect(screen.getByText('1 of 2 tasks · 0 relationships')).toBeVisible()
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
