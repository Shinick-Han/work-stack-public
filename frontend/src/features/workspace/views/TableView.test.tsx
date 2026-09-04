import { fireEvent, render, screen, within } from '@testing-library/react'
import type { WorkspaceTask } from './types'
import { projectKeyResults } from './keyResultModel'
import userEvent from '@testing-library/user-event'
import { beforeEach, vi } from 'vitest'
import { task } from '../../../test/fixtures'
import { TableView } from './TableView'
import { readTablePreferences } from './tablePreferences'

beforeEach(() => window.localStorage.clear())

test('sorts, selects, and changes status through the shared mutation callback', async () => {
  const onSelectTask = vi.fn()
  const onChangeTaskStatus = vi.fn().mockResolvedValue(undefined)
  const later = { ...task, id: 'T-0010', title: 'Zeta task', priority: 'P2' as const }
  render(<TableView tasks={[later, task]} onSelectTask={onSelectTask} onChangeTaskStatus={onChangeTaskStatus} selectedTaskId={null} />)

  let rows = screen.getAllByRole('row').slice(1)
  expect(within(rows[0]).getByText(task.id)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Sort by Task' }))
  rows = screen.getAllByRole('row').slice(1)
  expect(within(rows[0]).getByText(task.title)).toBeVisible()

  await userEvent.click(within(rows[0]).getByText(task.title))
  expect(onSelectTask).toHaveBeenCalledWith(task.id)
  await userEvent.selectOptions(screen.getByRole('combobox', { name: `Status for ${task.id}` }), 'done')
  expect(onChangeTaskStatus).toHaveBeenCalledWith(task.id, 'done')
})

test('shows dependency readiness from the full workspace and navigates to a blocker', async () => {
  const onSelectTask = vi.fn()
  const prerequisite = { ...task, id: 'T-0200', title: 'Review evidence', status: 'started' as const }
  const blocked = { ...task, id: 'T-0201', title: 'Publish result', dependencies: [prerequisite.id] }

  render(
    <TableView
      tasks={[blocked]}
      referenceTasks={[blocked, prerequisite]}
      onSelectTask={onSelectTask}
      onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      selectedTaskId={null}
    />,
  )

  const blocker = screen.getByRole('button', { name: 'Blocked by T-0200: Review evidence' })
  expect(blocker).toBeVisible()
  await userEvent.click(blocker)
  expect(onSelectTask).toHaveBeenCalledOnce()
  expect(onSelectTask).toHaveBeenCalledWith(prerequisite.id)
})

test('does not report a completed dependency hidden by the active filters as blocked', () => {
  const prerequisite = { ...task, id: 'T-0200', status: 'done' as const }
  const ready = { ...task, id: 'T-0201', dependencies: [prerequisite.id] }

  render(
    <TableView
      tasks={[ready]}
      referenceTasks={[ready, prerequisite]}
      onSelectTask={vi.fn()}
      onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      selectedTaskId={null}
    />,
  )

  expect(screen.getByText('Ready')).toBeVisible()
  expect(screen.queryByText(/Blocked by/)).not.toBeInTheDocument()
})

test('shows subtask completion as a compact planning projection', () => {
  const withSteps = {
    ...task,
    subtasks: [
      { id: 'S-1', title: 'Draft', status: 'done' as const },
      { id: 'S-2', title: 'Review', status: 'open' as const },
      { id: 'S-3', title: 'Publish', status: 'started' as const },
    ],
  }

  render(
    <TableView
      tasks={[withSteps]}
      onSelectTask={vi.fn()}
      onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      selectedTaskId={null}
    />,
  )

  expect(screen.getByLabelText(`Steps for ${task.id}: 1 of 3 done`)).toHaveTextContent('1/3')
})

test('focuses an aligned Objective without selecting the Task row', async () => {
  const onSelectObjective = vi.fn()
  const onSelectTask = vi.fn()

  render(
    <TableView
      tasks={[task]}
      onSelectTask={onSelectTask}
      onSelectObjective={onSelectObjective}
      onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      selectedTaskId={null}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Focus objective O-1' }))
  expect(onSelectObjective).toHaveBeenCalledWith('O-1')
  expect(onSelectTask).not.toHaveBeenCalled()

  const objectiveButton = screen.getByRole('button', { name: 'Focus objective O-1' })
  fireEvent.keyDown(objectiveButton, { key: 'Enter' })
  expect(onSelectTask).not.toHaveBeenCalled()
})

test('shows an active due-today date as a local-calendar urgency label', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(2026, 7, 30, 12, 0, 0))
  try {
    render(
      <TableView
        tasks={[{ ...task, due: '2026-08-30', status: 'started' }]}
        onSelectTask={vi.fn()}
        onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
        selectedTaskId={null}
      />,
    )

    expect(screen.getByLabelText(`Due for ${task.id}: Due today`)).toBeVisible()
  } finally {
    vi.useRealTimers()
  }
})

test('persists compact density and sort direction as local Table preferences', async () => {
  const later = { ...task, id: 'T-0010', title: 'Zeta task', priority: 'P2' as const }
  const props = {
    tasks: [later, task],
    onSelectTask: vi.fn(),
    onChangeTaskStatus: vi.fn().mockResolvedValue(undefined),
    selectedTaskId: null,
  }
  const { unmount } = render(<TableView {...props} />)

  await userEvent.click(screen.getByRole('button', { name: 'Compact rows' }))
  await userEvent.click(screen.getByRole('button', { name: 'Sort by Task' }))
  await userEvent.click(screen.getByRole('button', { name: 'Sort by Task' }))

  expect(screen.getByRole('table')).toHaveClass('wsv-table--compact')
  expect(readTablePreferences()).toEqual({ density: 'compact', descending: true, sortField: 'title' })

  unmount()
  render(<TableView {...props} />)
  expect(screen.getByRole('button', { name: 'Compact rows' })).toHaveAttribute('aria-pressed', 'true')
  expect(within(screen.getAllByRole('row')[1]).getByText('Zeta task')).toBeVisible()
})


function outcomeTask(id: string, extra: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    id,
    title: 'Outcome task ' + id,
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: [],
    dependencies: [],
    subtasks: [],
    context_count: 0,
    revision: 1,
    ...extra,
  }
}

const outcomeProjection = projectKeyResults({
  workspaceId: "W1",
  tasks: [
    outcomeTask('T-1', { objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-1' }] }) as never,
    outcomeTask('T-2', { objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-9' }] }) as never,
    outcomeTask('T-3') as never,
  ],
  objectives: [
    { id: "O-A", objective: "Objective A", revision: 1, key_results: [{ id: "KR-1", text: "A outcome" }] } as never,
  ],
});

describe('outcome chips', () => {
  it('keeps one row per Task and labels every stored reference', () => {
    render(
      <TableView
        tasks={[outcomeTask('T-1'), outcomeTask('T-2'), outcomeTask('T-3')]}
        keyResultProjection={outcomeProjection}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.getAllByRole('row')).toHaveLength(4)
    expect(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' })).toBeInTheDocument()
    expect(screen.getByText('Unresolved outcome')).toBeInTheDocument()
    expect(screen.getByText('Unassigned outcome')).toBeInTheDocument()
  })

  it('chip activation chooses the pair without selecting the row', async () => {
    const onSelectOutcome = vi.fn()
    const onSelectTask = vi.fn()
    render(
      <TableView
        tasks={[outcomeTask('T-1')]}
        keyResultProjection={outcomeProjection}
        onSelectOutcome={onSelectOutcome}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={onSelectTask}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' }))

    expect(onSelectOutcome).toHaveBeenCalledExactlyOnceWith({ objectiveId: 'O-A', keyResultId: 'KR-1' })
    expect(onSelectTask).not.toHaveBeenCalled()
  })
})
