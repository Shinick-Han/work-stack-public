import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { TaskPrerequisiteContext } from './TaskPrerequisiteContext'
import {
  completedVisibilityScopeKey,
  projectCompletedTaskVisibility,
  REVEAL_DISPLAY_LIMIT,
  type CompletedVisibilityReveal,
} from './completedTaskVisibility'
import type { WorkspaceTask } from './types'

/**
 * The panel is driven by REAL projected data from the admitted pure module, so
 * these assertions are about the shipped contract rather than a hand-built prop.
 */
function task(id: string, patch: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return { id, title: `Task ${id}`, status: 'open', priority: 'P2', ...patch }
}

function project(
  tasks: readonly WorkspaceTask[],
  anchorTaskId: string | null,
  reveal: CompletedVisibilityReveal | null = null,
  filters: Parameters<typeof projectCompletedTaskVisibility>[0]['filters'] = {},
) {
  return projectCompletedTaskVisibility({
    tasks,
    filters,
    view: 'graph',
    prerequisiteAnchorTaskId: anchorTaskId,
    reveal,
  })
}

function scopeKey(filters: Parameters<typeof completedVisibilityScopeKey>[1] = {}) {
  return completedVisibilityScopeKey('graph', filters, 'default')
}

describe('anchor resolution', () => {
  it('invites a selection when there is no anchor', () => {
    const tasks = [task('T-A')]
    render(
      <TaskPrerequisiteContext
        anchorTaskId={null}
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project(tasks, null)}
      />,
    )
    expect(screen.getByText('Select a Task to see its prerequisites.')).toBeVisible()
  })

  it('renders the anchor identity and reports a Task with no prerequisites', () => {
    const tasks = [task('T-A')]
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project(tasks, 'T-A')}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Prerequisites for T-A' })).toBeVisible()
    expect(screen.getByText('This Task has no prerequisites.')).toBeVisible()
  })

  it('shows nothing for an anchor that is not canonical', () => {
    render(
      <TaskPrerequisiteContext
        anchorTaskId="GHOST"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project([task('T-A')], 'GHOST')}
      />,
    )
    expect(screen.getByText('Select a Task to see its prerequisites.')).toBeVisible()
  })
})

describe('classification, labels and counts', () => {
  const tasks = [
    task('T-A', { dependencies: ['T-VIS', 'T-DONE', 'T-OTHER', 'T-GONE'] }),
    task('T-VIS'),
    task('T-DONE', { status: 'done' }),
    task('T-OTHER', { status: 'done', priority: 'P0' }),
  ]

  it('labels the four reasons distinctly and shows an ID only for missing', () => {
    const projection = project(tasks, 'T-A', null, { priority: 'P2' })
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )

    const list = within(screen.getByRole('list', { name: 'Direct prerequisites' }))
    const rows = list.getAllByRole('listitem')
    expect(rows.map((row) => row.getAttribute('data-prereq-reason'))).toEqual([
      'visible',
      'completed',
      'other-filter',
      'missing',
    ])

    expect(within(rows[0]).getByText('Shown')).toBeVisible()
    expect(within(rows[1]).getByText('Completed · hidden')).toBeVisible()
    expect(within(rows[2]).getByText('Hidden by another filter')).toBeVisible()
    expect(within(rows[3]).getByText('Missing from this workspace')).toBeVisible()

    // A missing prerequisite has no canonical Task, so it carries the ID alone.
    expect(within(rows[3]).getByText('T-GONE')).toBeVisible()
    expect(within(rows[3]).queryByText(/^Task T-GONE$/)).toBeNull()
    expect(within(rows[0]).getByText('Task T-VIS')).toBeVisible()
  })

  it('summarizes counts per reason from the projection', () => {
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project(tasks, 'T-A', null, { priority: 'P2' })}
      />,
    )
    const summary = within(screen.getByRole('list', { name: 'Prerequisite summary' }))
    expect(summary.getByText('Shown').nextSibling).toHaveTextContent('1')
    expect(summary.getByText('Completed · hidden').nextSibling).toHaveTextContent('1')
    expect(summary.getByText('Hidden by another filter').nextSibling).toHaveTextContent('1')
    expect(summary.getByText('Missing from this workspace').nextSibling).toHaveTextContent('1')
  })

  it('offers a view action for real Tasks and never for a missing reference', () => {
    const onOpenTask = vi.fn()
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onOpenTask={onOpenTask}
        onReveal={vi.fn()}
        projection={project(tasks, 'T-A', null, { priority: 'P2' })}
      />,
    )
    expect(screen.getByRole('button', { name: 'View T-VIS' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'View T-GONE' })).toBeNull()
  })
})

describe('temporary reveal', () => {
  const many = Array.from({ length: 12 }, (_, index) => `D-${index + 1}`)
  const tasks = [
    task('T-A', { dependencies: many }),
    ...many.map((id) => task(id, { status: 'done' })),
  ]

  it('requests only completed prerequisites and never exceeds the cap', async () => {
    const user = userEvent.setup()
    const onReveal = vi.fn()
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={onReveal}
        projection={project(tasks, 'T-A')}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Reveal completed prerequisites' }))
    const requested = onReveal.mock.calls[0][0] as string[]
    expect(requested).toHaveLength(REVEAL_DISPLAY_LIMIT)
    expect(requested).toEqual(many.slice(0, REVEAL_DISPLAY_LIMIT))
  })

  it('reports the cap and the true unrevealed remainder', () => {
    const key = scopeKey()
    const projection = project(tasks, 'T-A', {
      anchorTaskId: 'T-A',
      scopeKey: key,
      taskIds: many.slice(0, 10),
    })
    expect(projection.revealRemainingCount).toBe(2)

    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Showing 10 of at most 10 completed prerequisites')
    expect(status).toHaveTextContent('2 more not revealed')
    // The cap is a display bound, not truncation: all 12 stay classified.
    expect(within(screen.getByRole('list', { name: 'Direct prerequisites' })).getAllByRole('listitem'))
      .toHaveLength(12)
  })

  it('marks revealed rows and offers the explicit clear control', async () => {
    const user = userEvent.setup()
    const onClearReveal = vi.fn()
    const projection = project(tasks, 'T-A', {
      anchorTaskId: 'T-A',
      scopeKey: scopeKey(),
      taskIds: ['D-1'],
    })
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={onClearReveal}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )

    const rows = within(screen.getByRole('list', { name: 'Direct prerequisites' })).getAllByRole('listitem')
    expect(rows[0].getAttribute('data-prereq-revealed')).toBe('true')
    expect(within(rows[0]).getByText('Completed · revealed')).toBeVisible()
    expect(rows[1].getAttribute('data-prereq-revealed')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Clear temporary reveal' }))
    expect(onClearReveal).toHaveBeenCalledTimes(1)
  })

  it('never offers reveal for other-filter or missing prerequisites', () => {
    const blocked = [
      task('T-A', { dependencies: ['T-OTHER', 'T-GONE'] }),
      task('T-OTHER', { status: 'done', priority: 'P0' }),
    ]
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project(blocked, 'T-A', null, { priority: 'P2' })}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Reveal completed prerequisites' })).toBeNull()
  })

  it('drops a stale reveal from another scope without any effect pass', () => {
    // A request captured under a different scope key must be inert immediately.
    const projection = project(tasks, 'T-A', {
      anchorTaskId: 'T-A',
      scopeKey: 'stale-scope-key',
      taskIds: many.slice(0, 3),
    })
    expect(projection.reveal).toBeNull()

    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear temporary reveal' })).toBeNull()
  })

  it('drops a reveal whose anchor is no longer the current one', () => {
    const projection = project(tasks, 'T-A', {
      anchorTaskId: 'D-1',
      scopeKey: scopeKey(),
      taskIds: ['D-2'],
    })
    expect(projection.reveal).toBeNull()
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Clear temporary reveal' })).toBeNull()
  })

  it('prunes a reopened target so it cannot resurface when completed again', () => {
    const reopened = [
      task('T-A', { dependencies: ['D-1', 'D-2'] }),
      task('D-1', { status: 'open' }),
      task('D-2', { status: 'done' }),
    ]
    const projection = project(reopened, 'T-A', {
      anchorTaskId: 'T-A',
      scopeKey: scopeKey(),
      taskIds: ['D-1', 'D-2'],
    })
    // D-1 reopened, so only D-2 remains eligible and active.
    expect(projection.reveal?.taskIds).toEqual(['D-2'])

    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )
    const rows = within(screen.getByRole('list', { name: 'Direct prerequisites' })).getAllByRole('listitem')
    expect(rows[0].getAttribute('data-prereq-revealed')).toBeNull()
    expect(rows[1].getAttribute('data-prereq-revealed')).toBe('true')
  })

  it('does not expand transitively or through parents', () => {
    const chain = [
      task('T-A', { dependencies: ['T-B'], parent_id: 'T-P' }),
      task('T-B', { status: 'done', dependencies: ['T-C'] }),
      task('T-C', { status: 'done' }),
      task('T-P', { status: 'done' }),
    ]
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={project(chain, 'T-A')}
      />,
    )
    const rows = within(screen.getByRole('list', { name: 'Direct prerequisites' })).getAllByRole('listitem')
    expect(rows).toHaveLength(1)
    expect(within(rows[0]).getByText('T-B')).toBeVisible()
  })

  it('leaves the canonical input arrays untouched', () => {
    const frozen = [
      task('T-A', { dependencies: Object.freeze(['D-1']) as string[] }),
      task('D-1', { status: 'done' }),
    ]
    Object.freeze(frozen)
    const before = JSON.stringify(frozen)
    const projection = project(frozen, 'T-A', {
      anchorTaskId: 'T-A',
      scopeKey: scopeKey(),
      taskIds: ['D-1'],
    })
    render(
      <TaskPrerequisiteContext
        anchorTaskId="T-A"
        onClearReveal={vi.fn()}
        onReveal={vi.fn()}
        projection={projection}
      />,
    )
    expect(JSON.stringify(frozen)).toBe(before)
    expect(projection.referenceTasks).toBe(frozen)
  })
})
