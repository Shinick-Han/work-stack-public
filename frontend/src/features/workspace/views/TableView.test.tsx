import { fireEvent, render, screen, within } from '@testing-library/react'
import type { WorkspaceTask } from './types'
import { projectKeyResults } from './keyResultModel'
import userEvent from '@testing-library/user-event'
import { beforeEach, vi } from 'vitest'
import { task } from '../../../test/fixtures'
import { TableView } from './TableView'
import { LAYOUT_SAVE_FAILURE_MESSAGE, localViewStorageKey } from './localViewState'
import { TABLE_PREFERENCES_KEY } from './tablePreferences'
import { TABLE_DRAG_THRESHOLD_PX } from './tableOrdering'

class PointerEventPolyfill extends MouseEvent {
  pointerId: number
  pointerType: string
  isPrimary: boolean
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init)
    this.pointerId = init.pointerId ?? 1
    this.pointerType = init.pointerType ?? 'mouse'
    this.isPrimary = init.isPrimary ?? true
  }
}

Object.defineProperty(window, 'PointerEvent', {
  configurable: true,
  writable: true,
  value: PointerEventPolyfill,
})

if (!HTMLElement.prototype.setPointerCapture) {
  HTMLElement.prototype.setPointerCapture = function setPointerCapture() {}
  HTMLElement.prototype.releasePointerCapture = function releasePointerCapture() {}
}

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
  const workspaceId = 'ws-table-prefs'
  const key = localViewStorageKey(workspaceId, 'table')
  const props = {
    workspaceId,
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
  expect(JSON.parse(window.localStorage.getItem(key) as string).data).toEqual({
    density: 'compact',
    descending: true,
    sortField: 'title',
    order: [],
  })
  expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeNull()

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
    expect(screen.getByRole('columnheader', { name: 'Objective' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Key Result' })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Objectives' })).not.toBeInTheDocument()
    expect(screen.getByText('KR-1')).toBeInTheDocument()
    expect(screen.getByText('A outcome')).toBeInTheDocument()
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

  it('keeps scoped duplicate KR-1 identities on one row each', async () => {
    const onSelectOutcome = vi.fn()
    const onSelectTask = vi.fn()
    const scopedProjection = projectKeyResults({
      workspaceId: 'W1',
      tasks: [
        outcomeTask('T-A', {
          objective_ids: ['O-A'],
          key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-1' }],
        }) as never,
        outcomeTask('T-B', {
          objective_ids: ['O-B'],
          key_result_refs: [{ objective_id: 'O-B', key_result_id: 'KR-1' }],
        }) as never,
      ],
      objectives: [
        { id: 'O-A', objective: 'A', revision: 1, key_results: [{ id: 'KR-1', text: 'A outcome' }] } as never,
        { id: 'O-B', objective: 'B', revision: 1, key_results: [{ id: 'KR-1', text: 'B outcome' }] } as never,
      ],
    })
    render(
      <TableView
        tasks={[outcomeTask('T-A'), outcomeTask('T-B')]}
        keyResultProjection={scopedProjection}
        onSelectOutcome={onSelectOutcome}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={onSelectTask}
      />,
    )

    expect(screen.getAllByRole('row')).toHaveLength(3)
    await userEvent.click(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Filter by outcome O-B KR-1' }))
    expect(onSelectOutcome).toHaveBeenNthCalledWith(1, { objectiveId: 'O-A', keyResultId: 'KR-1' })
    expect(onSelectOutcome).toHaveBeenNthCalledWith(2, { objectiveId: 'O-B', keyResultId: 'KR-1' })
    expect(onSelectTask).not.toHaveBeenCalled()
  })

  it('states multi-KR, Objective-only, unaligned and unresolved rows without duplicating Tasks', () => {
    const mixedProjection = projectKeyResults({
      workspaceId: 'W1',
      tasks: [
        outcomeTask('T-MULTI', {
          objective_ids: ['O-A'],
          key_result_refs: [
            { objective_id: 'O-A', key_result_id: 'KR-1' },
            { objective_id: 'O-A', key_result_id: 'KR-2' },
          ],
        }) as never,
        outcomeTask('T-OBJ', { objective_ids: ['O-A'] }) as never,
        outcomeTask('T-NONE') as never,
        outcomeTask('T-MISS', {
          objective_ids: ['O-A'],
          key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-9' }],
        }) as never,
      ],
      objectives: [
        {
          id: 'O-A',
          objective: 'A',
          revision: 1,
          key_results: [
            { id: 'KR-1', text: 'First' },
            { id: 'KR-2', text: 'Second' },
          ],
        } as never,
      ],
    })
    render(
      <TableView
        tasks={[
          outcomeTask('T-MULTI'),
          outcomeTask('T-OBJ', { objective_ids: ['O-A'] }),
          outcomeTask('T-NONE'),
          outcomeTask('T-MISS'),
        ]}
        keyResultProjection={mixedProjection}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.getAllByRole('row')).toHaveLength(5)
    const rowOf = (id: string) => screen.getByText(id).closest('tr') as HTMLElement
    expect(within(rowOf('T-MULTI')).getByText('KR-1')).toBeVisible()
    expect(within(rowOf('T-MULTI')).getByText('First')).toBeVisible()
    expect(within(rowOf('T-MULTI')).getByText('KR-2')).toBeVisible()
    expect(within(rowOf('T-MULTI')).getByText('Second')).toBeVisible()
    expect(within(rowOf('T-OBJ')).getByText('Objective-only')).toBeVisible()
    expect(within(rowOf('T-NONE')).getByText('Unaligned')).toBeVisible()
    expect(within(rowOf('T-NONE')).getByText('Unassigned outcome')).toBeVisible()
    expect(within(rowOf('T-MISS')).getByText('Unresolved outcome')).toBeVisible()
  })

  it('isolates Objective and KR keyboard activation from the Task row', () => {
    const onSelectOutcome = vi.fn()
    const onSelectObjective = vi.fn()
    const onSelectTask = vi.fn()
    render(
      <TableView
        tasks={[outcomeTask('T-1', { objective_ids: ['O-A'] })]}
        keyResultProjection={outcomeProjection}
        onSelectObjective={onSelectObjective}
        onSelectOutcome={onSelectOutcome}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={onSelectTask}
      />,
    )

    fireEvent.keyDown(screen.getByRole('button', { name: 'Focus objective O-A' }), { key: 'Enter' })
    fireEvent.keyDown(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' }), { key: 'Enter' })
    expect(onSelectTask).not.toHaveBeenCalled()
  })

  it('renders Objective and KR as text when their callbacks are absent', () => {
    render(
      <TableView
        tasks={[outcomeTask('T-1')]}
        keyResultProjection={outcomeProjection}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Filter by outcome O-A KR-1' })).not.toBeInTheDocument()
    expect(screen.getByText('KR-1')).toBeVisible()
    expect(screen.getByText('A outcome')).toBeVisible()
  })
})

const WORKSPACE = 'ws-table-drag'
const KEY = localViewStorageKey(WORKSPACE, 'table')

function rowTask(id: string, extra: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    ...task,
    id,
    title: `Task ${id}`,
    ...extra,
  }
}

function envelope(data: unknown, revision = 1, workspaceId = WORKSPACE) {
  return {
    schemaVersion: 1,
    workspaceId,
    view: 'table',
    revision,
    writtenAt: 10,
    writerId: 'peer',
    data,
  }
}

function storedData() {
  return JSON.parse(window.localStorage.getItem(KEY) as string).data
}

function renderDragTable(options: {
  workspaceId?: string
  tasks?: WorkspaceTask[]
  referenceTasks?: WorkspaceTask[]
  onSelectTask?: ReturnType<typeof vi.fn>
  onChangeTaskStatus?: ReturnType<typeof vi.fn>
  onSelectObjective?: ReturnType<typeof vi.fn>
  onSelectOutcome?: ReturnType<typeof vi.fn>
} = {}) {
  const props = {
    workspaceId: options.workspaceId ?? WORKSPACE,
    tasks: options.tasks ?? [rowTask('T-A'), rowTask('T-B'), rowTask('T-C')],
    referenceTasks: options.referenceTasks,
    onSelectTask: options.onSelectTask ?? vi.fn(),
    onChangeTaskStatus: options.onChangeTaskStatus ?? vi.fn().mockResolvedValue(undefined),
    onSelectObjective: options.onSelectObjective ?? vi.fn(),
    onSelectOutcome: options.onSelectOutcome ?? vi.fn(),
    selectedTaskId: null,
  }
  return { ...render(<TableView {...props} />), props }
}

async function enableManual() {
  await userEvent.click(screen.getByRole('button', { name: 'Manual order' }))
}

function bodyRow(id: string) {
  return document.querySelector(`[data-task-id="${id}"]`) as HTMLElement
}

function visibleIds() {
  return screen.getAllByRole('row').slice(1).map((row) => row.getAttribute('data-task-id'))
}

function pointAt(node: Element | null) {
  Object.defineProperty(document, 'elementFromPoint', {
    configurable: true,
    value: () => node,
    writable: true,
  })
}

function dispatchPointer(target: EventTarget, type: 'pointerDown' | 'pointerMove' | 'pointerUp' | 'pointerCancel', clientX: number, clientY: number) {
  const mapped = {
    pointerDown: 'mouseDown',
    pointerMove: 'mouseMove',
    pointerUp: 'mouseUp',
    pointerCancel: 'mouseUp',
  }[type] as 'mouseDown' | 'mouseMove' | 'mouseUp'
  fireEvent[mapped](target as HTMLElement, {
    button: 0,
    buttons: type === 'pointerUp' || type === 'pointerCancel' ? 0 : 1,
    clientX,
    clientY,
  })
}

describe('Table manual order', () => {
  afterEach(() => {
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true,
      value: () => null,
      writable: true,
    })
  })

  it('P1 keeps below-threshold handle movement as focus only and writes once after a valid drop', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const view = renderDragTable()
    await enableManual()
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).revision).toBe(1)
    expect(storedData().order).toEqual([])

    const handle = screen.getByRole('button', { name: 'Reorder T-A' })
    handle.focus()
    dispatchPointer(handle, 'pointerDown', 20, 20)
    dispatchPointer(handle, 'pointerMove', 20, 20 + TABLE_DRAG_THRESHOLD_PX - 1)
    dispatchPointer(handle, 'pointerUp', 20, 20 + TABLE_DRAG_THRESHOLD_PX - 1)
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).revision).toBe(1)
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(handle).toHaveFocus()

    pointAt(bodyRow('T-C'))
    dispatchPointer(handle, 'pointerDown', 20, 20)
    dispatchPointer(handle, 'pointerMove', 20, 20 + TABLE_DRAG_THRESHOLD_PX)
    dispatchPointer(handle, 'pointerUp', 20, 80)
    expect(storedData().order).toEqual(['T-B', 'T-C', 'T-A'])
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).revision).toBe(2)
    expect(visibleIds()).toEqual(['T-B', 'T-C', 'T-A'])
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(view.props.onChangeTaskStatus).not.toHaveBeenCalled()
    expect(view.props.onSelectObjective).not.toHaveBeenCalled()
    expect(view.props.onSelectOutcome).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('P2 restores the snapshot and writes nothing when Escape, no target, or unmount cancels a drag', async () => {
    const { unmount } = renderDragTable()
    await enableManual()
    const handle = screen.getByRole('button', { name: 'Reorder T-A' })
    dispatchPointer(handle, 'pointerDown', 8, 8)
    dispatchPointer(handle, 'pointerMove', 8, 8 + TABLE_DRAG_THRESHOLD_PX)
    expect(screen.getByText(/Picked up T-A/)).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(storedData().order).toEqual([])
    expect(visibleIds()).toEqual(['T-A', 'T-B', 'T-C'])
    expect(screen.getByText('Reorder cancelled')).toBeInTheDocument()

    pointAt(null)
    dispatchPointer(handle, 'pointerDown', 8, 8)
    dispatchPointer(handle, 'pointerMove', 8, 8 + TABLE_DRAG_THRESHOLD_PX)
    dispatchPointer(handle, 'pointerUp', 8, 80)
    expect(storedData().order).toEqual([])
    expect(screen.getByText('Reorder cancelled')).toBeInTheDocument()

    pointAt(bodyRow('T-C'))
    dispatchPointer(handle, 'pointerDown', 8, 8)
    dispatchPointer(handle, 'pointerMove', 8, 8 + TABLE_DRAG_THRESHOLD_PX)
    unmount()
    expect(storedData().order).toEqual([])
  })

  it('K1 reorders from the handle with keyboard, follows focus, and announces positions', async () => {
    const view = renderDragTable()
    await enableManual()
    const handle = screen.getByRole('button', { name: 'Reorder T-A' })
    handle.focus()
    await userEvent.keyboard('{Enter}{ArrowDown}{Enter}')
    expect(screen.getByRole('button', { name: 'Reorder T-A' })).toHaveFocus()
    expect(visibleIds()).toEqual(['T-B', 'T-A', 'T-C'])
    expect(storedData().order).toEqual(['T-B', 'T-A', 'T-C'])
    expect(screen.getByText('Moved T-A to position 2 of 3')).toBeInTheDocument()
    expect(view.props.onSelectTask).not.toHaveBeenCalled()

    screen.getByRole('button', { name: 'Reorder T-A' }).focus()
    await userEvent.keyboard('{Enter}{End}{Enter}')
    expect(visibleIds()).toEqual(['T-B', 'T-C', 'T-A'])
    expect(screen.getByText('Moved T-A to position 3 of 3')).toBeInTheDocument()
  })

  it('TA1 keeps named-sort handles disabled without erasing Manual order, then restores it', async () => {
    renderDragTable()
    await enableManual()
    const handle = screen.getByRole('button', { name: 'Reorder T-A' })
    handle.focus()
    await userEvent.keyboard('{Enter}{End}{Enter}')
    expect(storedData().order).toEqual(['T-B', 'T-C', 'T-A'])

    await userEvent.click(screen.getByRole('button', { name: 'Sort by Task' }))
    expect(screen.getByRole('button', { name: 'Choose Manual order to reorder T-A' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Sort by Task' }).closest('th')).toHaveAttribute('aria-sort', 'ascending')
    expect(storedData()).toMatchObject({ sortField: 'title', order: ['T-B', 'T-C', 'T-A'] })

    await userEvent.click(screen.getByRole('button', { name: 'Manual order' }))
    expect(visibleIds()).toEqual(['T-B', 'T-C', 'T-A'])
    expect(screen.getByRole('button', { name: 'Reorder T-A' })).toBeEnabled()
    expect(screen.getByRole('table', { name: 'Tasks in Manual order' })).toBeInTheDocument()
  })

  it('does not consult Manual order while a named sort is active and still ties on Task ID', async () => {
    renderDragTable({
      tasks: [
        rowTask('T-2', { title: 'Same' }),
        rowTask('T-10', { title: 'Same' }),
        rowTask('T-1', { title: 'Same' }),
      ],
    })
    await enableManual()
    screen.getByRole('button', { name: 'Reorder T-2' }).focus()
    await userEvent.keyboard('{Enter}{End}{Enter}')
    await userEvent.click(screen.getByRole('button', { name: 'Sort by Task' }))
    expect(visibleIds()).toEqual(['T-1', 'T-2', 'T-10'])
  })

  it('F1 reorders only visible slots, keeps hidden relative order, and does not write on filter changes', async () => {
    const all = [rowTask('T-A'), rowTask('T-B'), rowTask('T-C')]
    const view = renderDragTable({ tasks: [all[0], all[2]], referenceTasks: all })
    await enableManual()
    const before = window.localStorage.getItem(KEY)
    view.rerender(
      <TableView
        workspaceId={WORKSPACE}
        tasks={[all[0], all[2]]}
        referenceTasks={all}
        onSelectTask={view.props.onSelectTask}
        onChangeTaskStatus={view.props.onChangeTaskStatus}
        selectedTaskId={null}
      />,
    )
    expect(window.localStorage.getItem(KEY)).toBe(before)

    screen.getByRole('button', { name: 'Reorder T-A' }).focus()
    await userEvent.keyboard('{Enter}{ArrowDown}{Enter}')
    expect(visibleIds()).toEqual(['T-C', 'T-A'])
    expect(storedData().order).toEqual(['T-C', 'T-B', 'T-A'])

    view.rerender(
      <TableView
        workspaceId={WORKSPACE}
        tasks={all}
        referenceTasks={all}
        onSelectTask={view.props.onSelectTask}
        onChangeTaskStatus={view.props.onChangeTaskStatus}
        selectedTaskId={null}
      />,
    )
    expect(visibleIds()).toEqual(['T-C', 'T-B', 'T-A'])
    expect(storedData().order).toEqual(['T-C', 'T-B', 'T-A'])
  })

  it('C1 buffers a cross-tab write during a gesture and rebases the visible delta on commit', async () => {
    const all = [rowTask('T-A'), rowTask('T-B'), rowTask('T-C'), rowTask('T-H')]
    renderDragTable({ tasks: all.slice(0, 3), referenceTasks: all })
    await enableManual()
    const handle = screen.getByRole('button', { name: 'Reorder T-A' })
    handle.focus()
    await userEvent.keyboard('{Enter}')
    const foreign = envelope({
      density: 'comfortable',
      descending: false,
      sortField: 'manual',
      order: ['T-C', 'T-H', 'T-B', 'T-A'],
    }, 4)
    window.localStorage.setItem(KEY, JSON.stringify(foreign))
    fireEvent(window, new StorageEvent('storage', {
      key: KEY,
      newValue: JSON.stringify(foreign),
      storageArea: window.localStorage,
    }))
    expect(visibleIds()).toEqual(['T-A', 'T-B', 'T-C'])
    await userEvent.keyboard('{ArrowDown}{Enter}')
    expect(storedData().order).toEqual(['T-B', 'T-H', 'T-A', 'T-C'])
  })

  it('R1 reset rewrites only the order coordinate and leaves density, sort, and other views alone', async () => {
    const graphKey = localViewStorageKey(WORKSPACE, 'graph')
    window.localStorage.setItem(graphKey, '{"keep":true}')
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const view = renderDragTable()
    await userEvent.click(screen.getByRole('button', { name: 'Compact rows' }))
    await enableManual()
    screen.getByRole('button', { name: 'Reorder T-A' }).focus()
    await userEvent.keyboard('{Enter}{End}{Enter}')
    expect(storedData().order).toEqual(['T-B', 'T-C', 'T-A'])

    await userEvent.click(screen.getByRole('button', { name: 'Reset table order' }))
    expect(storedData()).toEqual({
      density: 'compact',
      descending: false,
      sortField: 'manual',
      order: [],
    })
    expect(visibleIds()).toEqual(['T-A', 'T-B', 'T-C'])
    expect(window.localStorage.getItem(graphKey)).toBe('{"keep":true}')
    expect(screen.getByText('Table order reset')).toBeInTheDocument()
    expect(view.props.onChangeTaskStatus).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('isolates status controls, seeds legacy preferences, and keeps workspaces apart', async () => {
    window.localStorage.setItem(TABLE_PREFERENCES_KEY, JSON.stringify({
      density: 'compact', descending: true, sortField: 'due',
    }))
    const onChangeTaskStatus = vi.fn().mockResolvedValue(undefined)
    const first = renderDragTable({ onChangeTaskStatus })
    expect(screen.getByRole('button', { name: 'Compact rows' })).toHaveAttribute('aria-pressed', 'true')
    expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeTruthy()
    expect(window.localStorage.getItem(KEY)).toBeNull()

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Status for T-A' }), 'done')
    expect(onChangeTaskStatus).toHaveBeenCalledWith('T-A', 'done')
    expect(window.localStorage.getItem(KEY)).toBeNull()

    await enableManual()
    expect(storedData()).toMatchObject({ density: 'compact', descending: true, sortField: 'manual' })
    expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeTruthy()
    first.unmount()

    const other = localViewStorageKey('ws-other', 'table')
    renderDragTable({ workspaceId: 'ws-other', tasks: [rowTask('T-Z')] })
    expect(window.localStorage.getItem(KEY)).toBeTruthy()
    expect(window.localStorage.getItem(other)).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Manual order' }))
    expect(JSON.parse(window.localStorage.getItem(other) as string).workspaceId).toBe('ws-other')
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).workspaceId).toBe(WORKSPACE)
  })

  it('cancels an in-flight gesture when the workspace identity changes, even with the same Task IDs', async () => {
    const tasks = [rowTask('T-A'), rowTask('T-B'), rowTask('T-C')]
    const view = renderDragTable({ workspaceId: 'ws-a', tasks })
    await enableManual()
    screen.getByRole('button', { name: 'Reorder T-A' }).focus()
    await userEvent.keyboard('{Enter}{End}')
    expect(visibleIds()).toEqual(['T-B', 'T-C', 'T-A'])
    const sourceKey = localViewStorageKey('ws-a', 'table')
    expect(JSON.parse(window.localStorage.getItem(sourceKey) as string).data.order).toEqual([])

    view.rerender(
      <TableView
        workspaceId="ws-b"
        tasks={tasks}
        onSelectTask={view.props.onSelectTask}
        onChangeTaskStatus={view.props.onChangeTaskStatus}
        selectedTaskId={null}
      />,
    )
    expect(visibleIds()).toEqual(['T-A', 'T-B', 'T-C'])
    expect(JSON.parse(window.localStorage.getItem(sourceKey) as string).data.order).toEqual([])
    expect(window.localStorage.getItem(localViewStorageKey('ws-b', 'table'))).toBeNull()
    expect(screen.queryByText(/Picked up T-A/)).not.toBeInTheDocument()
  })

  it('keeps the once-per-mount save-failure announcement after a later move or reset', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    renderDragTable()
    await enableManual()
    expect(screen.getByText(LAYOUT_SAVE_FAILURE_MESSAGE)).toBeInTheDocument()
    screen.getByRole('button', { name: 'Reorder T-A' }).focus()
    await userEvent.keyboard('{Enter}{End}{Enter}')
    expect(screen.getByText(LAYOUT_SAVE_FAILURE_MESSAGE)).toBeInTheDocument()
    expect(screen.queryByText(/Moved T-A/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reset table order' }))
    expect(screen.getByText(LAYOUT_SAVE_FAILURE_MESSAGE)).toBeInTheDocument()
    expect(screen.queryByText('Table order reset')).not.toBeInTheDocument()
  })

  it('announces a storage failure without planning writes and continues the interaction', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const view = renderDragTable()
    await enableManual()
    expect(screen.getByText(LAYOUT_SAVE_FAILURE_MESSAGE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Manual order' })).toHaveAttribute('aria-pressed', 'true')
    expect(view.props.onChangeTaskStatus).not.toHaveBeenCalled()
  })
})
