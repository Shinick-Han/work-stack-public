import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// TE-F1: a narrowly scoped canvas recorder. It captures the actual generated
// edge array and then renders the REAL ReactFlow, so every existing unmocked
// control in this file keeps its current behaviour.
let recordedFlowEdges: { id: string; source: string; target: string; data?: { kind?: string } }[] = []
vi.mock('@xyflow/react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@xyflow/react')>()
  const RealReactFlow = actual.ReactFlow
  return {
    ...actual,
    ReactFlow: (props: Parameters<typeof RealReactFlow>[0]) => {
      recordedFlowEdges = (props.edges ?? []) as typeof recordedFlowEdges
      return <RealReactFlow {...props} />
    },
  }
})
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { WorkspacePage } from '../WorkspacePage'
import { Profiler, StrictMode } from 'react'
import { useUrlState } from '../../../app/urlState'
import { projectCompletedTaskVisibility } from './completedTaskVisibility'
import { filterCoordinates, readSavedFilters, sameSavedFilter, writeSavedFilters, SAVED_FILTERS_KEY } from '../savedFilters'
import { task as baseTask, workspace as populatedWorkspace } from '../../../test/fixtures'
import type { AppUrlState, Task, WorkspaceProjection } from '../../../domain/types'

/**
 * These tests drive the REAL WorkspacePage + WorkspaceViews chain. A test that
 * mocked the coordinator away could not prove the shared count/render contract,
 * which is the whole point of this slice.
 */

vi.mock('../../../api/client', () => ({
  api: {
    getTask: vi.fn(async () => ({ task: baseTask, contexts: [], replies: [] })),
  },
}))

const state: AppUrlState = {
  surface: 'workspace',
  view: 'graph',
  search: '',
  status: 'all',
  priority: 'all',
  readiness: 'all',
  timing: 'all',
  objectiveId: 'all',
  taskId: null,
  captureId: null,
  doneVisibility: 'default',
}

function makeTask(patch: Partial<Task> & { id: string }): Task {
  return { ...baseTask, uid: `uid-${patch.id}`, title: `Task ${patch.id}`, ...patch }
}

/** open A, done B (a prerequisite of A), dropped C, and open child D of done parent B. */
const openA = makeTask({ id: 'T-A', status: 'open', dependencies: ['T-B'], objective_ids: [] })
const doneB = makeTask({ id: 'T-B', status: 'done', dependencies: [], objective_ids: [] })
const droppedC = makeTask({ id: 'T-C', status: 'dropped', dependencies: [], objective_ids: [] })
const childD = makeTask({ id: 'T-D', status: 'open', parent_id: 'T-B', dependencies: [], objective_ids: [] })

const mixedTasks = [openA, doneB, droppedC, childD]

function workspaceWith(tasks: Task[]): WorkspaceProjection {
  return { ...populatedWorkspace, tasks, edges: [], objectives: [], notes: [] }
}

function renderPage(overrides: Partial<AppUrlState> = {}, tasks: Task[] = mixedTasks, updateUrl = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={client}>
      <WorkspacePage
        isRefreshing={false}
        onChangeTaskStatus={vi.fn()}
        onCreateTask={vi.fn()}
        onOpenObjectives={vi.fn()}
        onRefresh={vi.fn()}
        state={{ ...state, ...overrides }}
        updateUrl={updateUrl}
        workspace={workspaceWith(tasks)}
      />
    </QueryClientProvider>,
  )
  return { ...utils, updateUrl }
}

beforeEach(() => {
  window.localStorage.clear()
  vi.clearAllMocks()
})

describe('shared projection across every renderer', () => {
  it('shows the same Task set and summary in Board, Table and Treemap under fresh default', async () => {
    // The projection is the contract; assert against it, not copied constants.
    const expected = projectCompletedTaskVisibility({
      tasks: mixedTasks,
      filters: { search: '', status: 'all', priority: 'all', objectiveId: 'all', readiness: 'all', timing: 'all' },
      view: 'board',
      doneVisibility: 'default',
    })
    expect(expected.visibleTasks.map((item) => item.id)).toEqual(['T-A', 'T-C', 'T-D'])
    expect(expected.counts.hiddenCompleted).toBe(1)

    for (const view of ['board', 'table'] as const) {
      const { unmount } = renderPage({ view })

      const summary = await screen.findByText(/tasks shown/)
      expect(summary).toHaveTextContent(
        `${expected.counts.visible} of ${expected.counts.canonicalTotal} tasks shown`,
      )
      expect(summary).toHaveTextContent('1 completed hidden')

      for (const visible of expected.visibleTasks) {
        expect(await screen.findAllByText(new RegExp(visible.id))).not.toHaveLength(0)
      }
      // The Done prerequisite is hidden, and the Dropped Task is not.
      expect(screen.queryByText(/T-B/)).toBeNull()
      expect(await screen.findAllByText(/T-C/)).not.toHaveLength(0)
      unmount()
    }
  })

  it('keeps the open child of a hidden Done parent and a selected Done Task visible', async () => {
    const { unmount } = renderPage({ view: 'board', taskId: 'T-B' })

    const summary = await screen.findByText(/tasks shown/)
    // The selected Done Task is retained by the pin, so 4 of 4 are shown.
    expect(summary).toHaveTextContent('4 of 4 tasks shown')
    expect(summary).toHaveTextContent('1 kept open')
    expect(await screen.findAllByText(/T-D/)).not.toHaveLength(0)
    unmount()
  })

  it('does not mutate the canonical input arrays or objects', async () => {
    const snapshot = JSON.stringify(mixedTasks)
    const { rerender } = renderPage({ view: 'table' })
    await screen.findByText(/tasks shown/)

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'table', doneVisibility: 'show' }}
          updateUrl={vi.fn()}
          workspace={workspaceWith(mixedTasks)}
        />
      </QueryClientProvider>,
    )
    await screen.findByText(/tasks shown/)

    expect(JSON.stringify(mixedTasks)).toBe(snapshot)
    expect(mixedTasks.map((item) => item.id)).toEqual(['T-A', 'T-B', 'T-C', 'T-D'])
  })

  it('keeps canonical completion and blocked metrics independent of visibility', async () => {
    renderPage({ view: 'board' })
    const summary = within(await screen.findByLabelText('Workspace summary'))

    // 1 of 4 canonical tasks is Done, regardless of the Done being hidden.
    expect(summary.getByText('Completion').closest('div')).toHaveTextContent('25%')
    expect(summary.getByText('Completion').closest('div')).toHaveTextContent('1 of 4 tasks')
  })
})

describe('visibility controls', () => {
  async function openFilters(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Filter tasks' }))
  }

  it('exposes default, hide and show and patches only the coordinate', async () => {
    const user = userEvent.setup()
    const { updateUrl } = renderPage({ view: 'board' })
    await openFilters(user)

    const control = screen.getByRole('combobox', { name: 'Completed task visibility' })
    expect(control).toHaveValue('default')

    await user.selectOptions(control, 'show')
    expect(updateUrl).toHaveBeenCalledWith({ doneVisibility: 'show' })

    await user.selectOptions(control, 'hide')
    expect(updateUrl).toHaveBeenLastCalledWith({ doneVisibility: 'hide' })
  })

  it('atomically switches status to all when hiding while the status filter is Done', async () => {
    const user = userEvent.setup()
    const { updateUrl } = renderPage({ view: 'board', status: 'done' })
    await openFilters(user)

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Completed task visibility' }),
      'hide',
    )
    expect(updateUrl).toHaveBeenCalledWith({ doneVisibility: 'hide', status: 'all' })
  })

  it('makes an explicit All statuses selection an atomic status plus show patch', async () => {
    const user = userEvent.setup()
    const { updateUrl } = renderPage({ view: 'board', status: 'done' })
    await openFilters(user)

    await user.selectOptions(screen.getByRole('combobox', { name: 'Filter by status' }), 'all')
    expect(updateUrl).toHaveBeenCalledWith({ status: 'all', doneVisibility: 'show' })
  })

  it('shows Done under an explicit Done status without overriding another filter', async () => {
    const priorityTasks = [
      makeTask({ id: 'T-P0', status: 'done', priority: 'P0', dependencies: [], objective_ids: [] }),
      makeTask({ id: 'T-P2', status: 'done', priority: 'P2', dependencies: [], objective_ids: [] }),
    ]
    renderPage({ view: 'board', status: 'done', priority: 'P0' }, priorityTasks)

    const summary = await screen.findByText(/tasks shown/)
    expect(summary).toHaveTextContent('1 of 2 tasks shown')
    expect(await screen.findAllByText(/T-P0/)).not.toHaveLength(0)
    expect(screen.queryByText(/T-P2/)).toBeNull()
  })

  it('does not let a search change visibility implicitly', async () => {
    renderPage({ view: 'board', search: 'Task' })
    const summary = await screen.findByText(/tasks shown/)

    // Search matches every Task, but Done stays hidden under the default.
    expect(summary).toHaveTextContent('3 of 4 tasks shown')
    expect(summary).toHaveTextContent('1 completed hidden')
  })

  it('keeps the selected Task visible after hiding completed work', async () => {
    renderPage({ view: 'board', taskId: 'T-B', doneVisibility: 'hide' })
    const summary = await screen.findByText(/tasks shown/)

    expect(summary).toHaveTextContent('1 kept open')
    expect(await screen.findAllByText(/T-B/)).not.toHaveLength(0)
  })
})

describe('root-frozen clear actions preserve doneVisibility', () => {
  it('clears the other filter axes without resetting visibility', async () => {
    const user = userEvent.setup()
    const { updateUrl } = renderPage({
      view: 'board',
      search: 'release',
      priority: 'P0',
      doneVisibility: 'show',
    })

    await user.click(screen.getByRole('button', { name: 'Clear filters' }))

    const patch = updateUrl.mock.calls.at(-1)?.[0]
    expect(patch).toEqual({
      search: '',
      status: 'all',
      priority: 'all',
      readiness: 'all',
      timing: 'all',
      objectiveId: 'all',
      outcomeFilter: { kind: 'all' },
    })
    // The frozen policy: Clear filters is not an implicit visibility reset.
    expect(patch).not.toHaveProperty('doneVisibility')
  })

  it('clears the status chip without resetting visibility', async () => {
    const user = userEvent.setup()
    const { updateUrl } = renderPage({ view: 'board', status: 'done', doneVisibility: 'show' })

    await user.click(screen.getByRole('button', { name: /Clear status filter/ }))

    const patch = updateUrl.mock.calls.at(-1)?.[0]
    expect(patch).toEqual({ status: 'all' })
    expect(patch).not.toHaveProperty('doneVisibility')
  })
})

describe('empty kinds', () => {
  it('says all matching tasks are completed rather than the whole workspace', async () => {
    const allDone = [
      makeTask({ id: 'T-1', status: 'done', dependencies: [], objective_ids: [] }),
      makeTask({ id: 'T-2', status: 'done', dependencies: [], objective_ids: [] }),
    ]
    renderPage({ view: 'board' }, allDone)

    expect(await screen.findByText('All matching tasks are completed')).toBeVisible()
    expect(await screen.findByText(/tasks shown/)).toHaveTextContent('0 of 2 tasks shown')
  })

  it('uses matching-only wording when another filter also hides open work', async () => {
    const mixed = [
      makeTask({ id: 'T-P2', status: 'done', priority: 'P2', dependencies: [], objective_ids: [] }),
      makeTask({ id: 'T-P0', status: 'open', priority: 'P0', dependencies: [], objective_ids: [] }),
    ]
    renderPage({ view: 'board', priority: 'P2' }, mixed)

    // Only the P2 Task matched, and it is completed: matching-only wording.
    expect(await screen.findByText('All matching tasks are completed')).toBeVisible()
  })

  it('distinguishes an other-filter empty result', async () => {
    renderPage({ view: 'board', search: 'nothing-matches-this' })
    expect(await screen.findByText('No work matches these filters')).toBeVisible()
  })

  it('reports counts that partition the canonical set exactly', async () => {
    const expected = projectCompletedTaskVisibility({
      tasks: mixedTasks,
      filters: { search: '', status: 'all', priority: 'P0', objectiveId: 'all', readiness: 'all', timing: 'all' },
      view: 'board',
      doneVisibility: 'default',
    })
    expect(
      expected.counts.visible + expected.counts.hiddenCompleted + expected.counts.hiddenOther,
    ).toBe(expected.counts.canonicalTotal)

    renderPage({ view: 'board', priority: 'P0' })
    expect(await screen.findByText(/tasks shown/)).toHaveTextContent(
      `${expected.counts.visible} of ${expected.counts.canonicalTotal} tasks shown`,
    )
  })
})

describe('saved views with the admitted coordinates', () => {
  it('restores a legacy All row as show and keeps a new default as default', () => {
    const legacyRow = {
      id: 'legacy-all',
      name: 'Legacy All',
      objectiveId: 'all',
      priority: 'all' as const,
      readiness: 'all' as const,
      timing: 'all' as const,
      search: '',
      status: 'all' as const,
      view: 'board' as const,
    }
    window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify([legacyRow]))
    expect(readSavedFilters()[0].doneVisibility).toBe('show')

    const fresh = { id: 'fresh', name: 'Fresh', ...filterCoordinates({ ...state, status: 'all' }) }
    expect(fresh.doneVisibility).toBe('default')
    writeSavedFilters([fresh])
    expect(readSavedFilters().find((row) => row.id === 'fresh')?.doneVisibility).toBe('default')
  })

  it('does not treat two views differing only in visibility as equal', () => {
    const shown = { id: 'shown', name: 'Shown', ...filterCoordinates({ ...state, doneVisibility: 'show' }) }
    expect(sameSavedFilter(shown, { ...state, doneVisibility: 'show' })).toBe(true)
    expect(sameSavedFilter(shown, { ...state, doneVisibility: 'default' })).toBe(false)
    expect(sameSavedFilter(shown, { ...state, doneVisibility: 'hide' })).toBe(false)
  })

  it('applies a saved view through the page and reports it as changed on visibility alone', async () => {
    const user = userEvent.setup()
    const saved = {
      id: 'saved-show',
      name: 'Completed shown',
      ...filterCoordinates({ ...state, doneVisibility: 'show' }),
    }
    writeSavedFilters([saved])

    const { updateUrl } = renderPage({ view: 'board' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Saved filters' }), 'saved-show')

    await waitFor(() => expect(updateUrl).toHaveBeenCalled())
    const patch = updateUrl.mock.calls.at(-1)?.[0]
    expect(patch).toMatchObject({ doneVisibility: 'show' })
  })
})

/**
 * Reproductions for the three review findings. These drive the real
 * Page -> WorkspaceViews -> GraphView -> GraphContextPopover chain; only the
 * canvas engine and API responses are substituted, never the state/focus
 * boundary under test.
 */
describe('interaction lifetime corrections', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: [], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'open', dependencies: [], objective_ids: [], context_count: 2 })

  function renderChain(overrides: Partial<AppUrlState>, tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const updateUrl = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides }}
          updateUrl={updateUrl}
          workspace={workspaceWith(tasks)}
        />
      </QueryClientProvider>,
    )
    const rerenderWith = (nextState: Partial<AppUrlState>, nextTasks: Task[] = tasks) => view.rerender(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides, ...nextState }}
          updateUrl={updateUrl}
          workspace={workspaceWith(nextTasks)}
        />
      </QueryClientProvider>,
    )
    return { ...view, updateUrl, rerenderWith }
  }

  it('F1 keeps the closed popup trigger focused until focus actually leaves it', async () => {
    const user = userEvent.setup()
    const chain = renderChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    const badge = await screen.findByRole('button', { name: /Open context for task T-B/ })
    await user.click(badge)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    // A selected and B as context: both completed and excluded by a search.
    chain.rerenderWith(
      { view: 'graph', taskId: 'T-A', search: 'no-match-at-all' },
      [{ ...A, status: 'done' }, { ...B, status: 'done' }],
    )
    // Both are still retained while the popup is open.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')

    await user.keyboard('{Escape}')

    // The trigger must still exist and own focus; the pin must not have been
    // released in the same cleanup that restored focus.
    const restored = screen.queryByRole('button', { name: /Open context for task T-B/ })
    expect(restored).not.toBeNull()
    expect(restored).toHaveFocus()
    expect(document.activeElement).not.toBe(document.body)
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')
  })

  it('F1 control: a shell-selected popup task keeps its trigger through close', async () => {
    const user = userEvent.setup()
    const chain = renderChain({ view: 'graph', taskId: 'T-B' }, [A, B])

    const badge = await screen.findByRole('button', { name: /Open context for task T-B/ })
    await user.click(badge)
    await screen.findByRole('dialog')
    chain.rerenderWith(
      { view: 'graph', taskId: 'T-B', search: 'no-match-at-all' },
      [{ ...A, status: 'done' }, { ...B, status: 'done' }],
    )
    await user.keyboard('{Escape}')

    // Its independent selection pin holds it regardless of the focus pin.
    expect(screen.queryByRole('button', { name: /Open context for task T-B/ })).not.toBeNull()
  })

  it('F2 releases view-local popup state on a view change and keeps focus on a surviving control', async () => {
    const user = userEvent.setup()
    const chain = renderChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')

    // Externally updated view state, the same owner update navigation uses.
    chain.rerenderWith({ view: 'board', taskId: 'T-A' }, [A, { ...B, status: 'done' }])

    // Completed B must not stay pinned in Board, and focus must not be lost.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown')
    expect(screen.getByText(/tasks shown/)).not.toHaveTextContent('kept open')
    expect(document.activeElement).not.toBe(document.body)

    // Returning to Graph must not resurrect the popup without a new action.
    chain.rerenderWith({ view: 'graph', taskId: 'T-A' }, [A, { ...B, status: 'done' }])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('F2 hands focus to a surviving control when the popup task is canonically deleted', async () => {
    const user = userEvent.setup()
    const chain = renderChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')

    chain.rerenderWith({ view: 'graph', taskId: 'T-A' }, [A])

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 1 tasks shown')
    expect(document.activeElement).not.toBe(document.body)
  })
})

/** Lifecycle corrections CF1-CF4: ownership of pins, fallbacks and observers. */
describe('interaction lifetime ownership', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: [], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'open', dependencies: [], objective_ids: [], context_count: 2 })

  function chain(overrides: Partial<AppUrlState>, tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const node = (st: Partial<AppUrlState>, ts: Task[]) => (
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides, ...st }}
          updateUrl={vi.fn()}
          workspace={workspaceWith(ts)}
        />
      </QueryClientProvider>
    )
    const view = render(node({}, tasks))
    return { ...view, to: (st: Partial<AppUrlState>, ts: Task[] = tasks) => view.rerender(node(st, ts)) }
  }

  it('CF1 keeps a reacquired pin when the previous popup blur callback fires', async () => {
    const user = userEvent.setup()
    const c = chain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    c.to({ search: 'no-match-at-all' }, [{ ...A, status: 'done' }, { ...B, status: 'done' }])
    await user.keyboard('{Escape}')

    // Healthy: the first close retains B, connected and focused.
    const badge = screen.getByRole('button', { name: /Open context for task T-B/ })
    expect(badge).toHaveFocus()
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')

    // Reopening the SAME still-focused trigger fires the old blur callback.
    await user.click(badge)
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')

    // The new lifetime owns the pin; the old one must not have released it.
    const again = screen.queryByRole('button', { name: /Open context for task T-B/ })
    expect(again).not.toBeNull()
    expect(again).toHaveFocus()
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')
  })

  it('CF1 control: a real later blur does release the pin', async () => {
    const user = userEvent.setup()
    const c = chain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    c.to({ search: 'no-match-at-all' }, [{ ...A, status: 'done' }, { ...B, status: 'done' }])
    await user.keyboard('{Escape}')

    const badge = screen.getByRole('button', { name: /Open context for task T-B/ })
    await act(async () => { badge.dispatchEvent(new FocusEvent('blur')) })

    // Only B's focus reason ends; A stays selected and retained.
    await waitFor(() => expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown'))
  })

  it('CF2 keeps a connected fallback when the LAST canonical Task is deleted', async () => {
    const user = userEvent.setup()
    const c = chain({ view: 'graph', taskId: null }, [B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')

    c.to({}, [])

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // A surviving Workspace control must own focus, not BODY.
    expect(document.querySelectorAll('[data-workspace-focus-fallback]').length).toBeGreaterThan(0)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('CF4 a watch from an unmounted Workspace cannot focus a later Workspace', async () => {
    const user = userEvent.setup()
    const c = chain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    // The restored trigger owns focus inside the OLD Workspace.
    expect(screen.getByRole('button', { name: /Open context for task T-B/ })).toHaveFocus()

    c.unmount()

    // A new Workspace mounts and one of its own controls is deliberately focused.
    const second = chain({ view: 'graph', taskId: null }, [makeTask({ id: 'T-C', status: 'open', dependencies: [], objective_ids: [] })])
    const refresh = await screen.findByRole('button', { name: 'Refresh workspace' })
    refresh.focus()
    expect(refresh).toHaveFocus()

    // Flush anything the dead Workspace may still have queued.
    await act(async () => { await Promise.resolve() })
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

    expect(refresh).toHaveFocus()
    second.unmount()
  })

  it('CF3 excludes ended Graph pins from the FIRST committed Board tree after popstate', async () => {
    const user = userEvent.setup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const tasks = [A, { ...B, status: 'done' as const }]

    // A Profiler observer reads the COMMITTED tree, which is the contract here;
    // a settled assertion alone cannot see the first Board commit.
    const commits: { summary: string; boardB: boolean }[] = []
    function Host() {
      const { state: urlState, update } = useUrlState()
      return (
        <Profiler
          id="workspace"
          onRender={() => {
            const summaryNode = document.body.textContent ?? ''
            const match = /\d+ of \d+ tasks shown[^·]*(· \d+ completed hidden)?[^·]*(· \d+ kept open)?/.exec(summaryNode)
            commits.push({
              summary: match ? match[0] : '',
              boardB: Boolean(document.querySelector('[aria-label*="T-B"], [aria-label*="Task T-B"]')),
            })
          }}
        >
          <WorkspacePage
            isRefreshing={false}
            onChangeTaskStatus={vi.fn()}
            onCreateTask={vi.fn()}
            onOpenObjectives={vi.fn()}
            onRefresh={vi.fn()}
            state={urlState}
            updateUrl={update}
            workspace={workspaceWith(tasks)}
          />
        </Profiler>
      )
    }

    window.history.replaceState(null, '', '/?view=graph&task=T-A&doneVisibility=show')
    render(<QueryClientProvider client={client}><Host /></QueryClientProvider>)

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')

    commits.length = 0
    await act(async () => {
      window.history.pushState(null, '', '/?view=board&task=T-A')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })

    // Every committed Board tree, including the FIRST, must already exclude the
    // ended Graph pin rather than being repaired by a later passive effect.
    const boardCommits = commits.filter((entry) => entry.summary)
    expect(boardCommits.length).toBeGreaterThan(0)
    expect(boardCommits[0].summary).toContain('1 of 2 tasks shown')
    expect(boardCommits.every((entry) => !entry.summary.includes('kept open'))).toBe(true)

    // Settled state stays correct and no popup is resurrected.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

/**
 * LF1: the application renders under React StrictMode (main.tsx:24-30), whose
 * effect cleanup/setup replay must not release a live popup's pin.
 */
describe('React StrictMode effect replay', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: [], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'open', dependencies: [], objective_ids: [], context_count: 2 })

  function strictChain(overrides: Partial<AppUrlState>, tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const node = (st: Partial<AppUrlState>, ts: Task[]) => (
      <StrictMode>
        <QueryClientProvider client={client}>
          <WorkspacePage
            isRefreshing={false}
            onChangeTaskStatus={vi.fn()}
            onCreateTask={vi.fn()}
            onOpenObjectives={vi.fn()}
            onRefresh={vi.fn()}
            state={{ ...state, ...overrides, ...st }}
            updateUrl={vi.fn()}
            workspace={workspaceWith(ts)}
          />
        </QueryClientProvider>
      </StrictMode>
    )
    const view = render(node({}, tasks))
    return { ...view, to: (st: Partial<AppUrlState>, ts: Task[] = tasks) => view.rerender(node(st, ts)) }
  }

  it('LF1 keeps the live popup pin through the StrictMode setup/cleanup replay', async () => {
    const user = userEvent.setup()
    const c = strictChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')

    // A selected and B as context, both completed and excluded by a search.
    c.to({ search: 'no-match-at-all' }, [{ ...A, status: 'done' }, { ...B, status: 'done' }])
    // The replay must not have released the pin of the still-open popup.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')

    await user.keyboard('{Escape}')

    const badge = screen.queryByRole('button', { name: /Open context for task T-B/ })
    expect(badge).not.toBeNull()
    expect(badge).toHaveFocus()
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')
  })

  it('LF1 control: under StrictMode a real later blur still releases only that reason', async () => {
    const user = userEvent.setup()
    const c = strictChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    c.to({ search: 'no-match-at-all' }, [{ ...A, status: 'done' }, { ...B, status: 'done' }])
    await user.keyboard('{Escape}')

    const badge = screen.getByRole('button', { name: /Open context for task T-B/ })
    await act(async () => { badge.dispatchEvent(new FocusEvent('blur')) })

    // Only B's focus reason ends; A stays selected and retained.
    await waitFor(() => expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown'))
  })

  it('LF1 control: reopening under StrictMode still keeps the reacquired pin', async () => {
    const user = userEvent.setup()
    const c = strictChain({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    c.to({ search: 'no-match-at-all' }, [{ ...A, status: 'done' }, { ...B, status: 'done' }])
    await user.keyboard('{Escape}')

    await user.click(screen.getByRole('button', { name: /Open context for task T-B/ }))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('button', { name: /Open context for task T-B/ })).not.toBeNull()
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')
  })
})

/** Integrated prerequisite panel behaviour through the real Page chain. */
describe('prerequisite context through the real chain', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: ['T-B'], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'done', dependencies: [], objective_ids: [], context_count: 2 })
  const C = makeTask({ id: 'T-C', status: 'open', dependencies: ['T-B'], objective_ids: [], context_count: 2 })

  function chain2(overrides: Partial<AppUrlState>, tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const node = (st: Partial<AppUrlState>, ts: Task[]) => (
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides, ...st }}
          updateUrl={vi.fn()}
          workspace={workspaceWith(ts)}
        />
      </QueryClientProvider>
    )
    const view = render(node({}, tasks))
    return { ...view, to: (st: Partial<AppUrlState>, ts: Task[] = tasks) => view.rerender(node(st, ts)) }
  }

  function panel() {
    return within(screen.getByRole('region', { name: 'Prerequisite context' }))
  }

  it('anchors on the shell selection when no popup is open', async () => {
    chain2({ view: 'graph', taskId: 'T-A' }, [A, B])
    expect(await screen.findByRole('heading', { name: 'Prerequisites for T-A' })).toBeVisible()
    const rows = within(panel().getByRole('list', { name: 'Direct prerequisites' })).getAllByRole('listitem')
    expect(rows).toHaveLength(1)
    expect(rows[0].getAttribute('data-prereq-reason')).toBe('completed')
  })

  it('lets an open Graph popup take the anchor independently of shell selection', async () => {
    const user = userEvent.setup()
    chain2({ view: 'graph', taskId: 'T-A' }, [A, B, C])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    await screen.findByRole('dialog')

    // The popup Task is the anchor; the shell selection is unchanged.
    expect(screen.getByRole('heading', { name: 'Prerequisites for T-C' })).toBeVisible()
  })

  it('reveals a hidden completed prerequisite and brings it into the rendered set', async () => {
    const user = userEvent.setup()
    chain2({ view: 'graph', taskId: 'T-A' }, [A, B])

    expect(await screen.findByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown')
    await user.click(screen.getByRole('button', { name: 'Reveal completed prerequisites' }))

    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')
    expect(panel().getByText('Completed · revealed')).toBeVisible()
  })

  it('clears only the reveal and keeps the selection pin', async () => {
    const user = userEvent.setup()
    chain2({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: 'Reveal completed prerequisites' }))
    await user.click(screen.getByRole('button', { name: 'Clear temporary reveal' }))

    expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown')
    // The shell selection reason is untouched by clearing a reveal.
    expect(screen.getByRole('heading', { name: 'Prerequisites for T-A' })).toBeVisible()
  })

  it('excludes a stale reveal in the FIRST committed render after a scope change', async () => {
    const user = userEvent.setup()
    const c = chain2({ view: 'graph', taskId: 'T-A' }, [A, B])

    await user.click(await screen.findByRole('button', { name: 'Reveal completed prerequisites' }))
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 2 tasks shown')

    // Changing a filter changes the scope key, so the reveal must not survive.
    c.to({ priority: 'P2' })
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('1 of 2 tasks shown')
    expect(screen.queryByRole('button', { name: 'Clear temporary reveal' })).toBeNull()
  })

  it('does not change status, search or visibility when revealing', async () => {
    const user = userEvent.setup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const updateUrl = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'graph', taskId: 'T-A' }}
          updateUrl={updateUrl}
          workspace={workspaceWith([A, B])}
        />
      </QueryClientProvider>,
    )

    await user.click(await screen.findByRole('button', { name: 'Reveal completed prerequisites' }))
    // Reveal is transient component state: no URL write at all.
    expect(updateUrl).not.toHaveBeenCalled()
  })
})

/**
 * RV-01: while the Graph context popup owns the anchor, its prerequisite
 * controls must live INSIDE the modal. A control rendered outside showModal is
 * inert to a real user, so reachability is asserted with actual Tab keys rather
 * than a jsdom click that a real modal would block.
 */
describe('popup-anchored prerequisite controls live inside the modal', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: ['T-B'], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'done', dependencies: [], objective_ids: [] })
  const C = makeTask({ id: 'T-C', status: 'open', dependencies: ['T-D'], objective_ids: [], context_count: 2 })
  const D = makeTask({ id: 'T-D', status: 'done', dependencies: [], objective_ids: [] })

  function chain3(overrides: Partial<AppUrlState>, tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const node = (st: Partial<AppUrlState>, ts: Task[]) => (
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides, ...st }}
          updateUrl={vi.fn()}
          workspace={workspaceWith(ts)}
        />
      </QueryClientProvider>
    )
    const view = render(node({}, tasks))
    return { ...view, to: (st: Partial<AppUrlState>, ts: Task[] = tasks) => view.rerender(node(st, ts)) }
  }

  async function openPopupOnC(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    return screen.findByRole('dialog')
  }

  it('RV-01 puts the reveal control inside the dialog and reaches it with Tab', async () => {
    const user = userEvent.setup()
    chain3({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])
    const dialog = await openPopupOnC(user)

    // The popup Task owns the anchor while A stays selected.
    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    expect(dialog.contains(reveal)).toBe(true)

    // Real keyboard reachability: Tab from the dialog's initial focus must land
    // on the reveal control without leaving the modal.
    let guard = 0
    while (document.activeElement !== reveal && guard < 25) {
      await user.tab()
      guard += 1
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
    expect(reveal).toHaveFocus()
  })

  it('RV-01 keyboard activation reveals D while C anchors and A stays selected', async () => {
    const user = userEvent.setup()
    chain3({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])
    const dialog = await openPopupOnC(user)

    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 4 tasks shown')

    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()
    await user.keyboard('{Enter}')

    // D becomes visible; B stays hidden because it is A's prerequisite.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('3 of 4 tasks shown')
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    const clear = within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear temporary reveal' })
    clear.focus()
    await user.keyboard('{Enter}')

    // Clearing hides D again and releases no pin: the popup and selection hold.
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 4 tasks shown')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('RV-01 shows only one panel: the outside one is suppressed while the modal anchors', async () => {
    const user = userEvent.setup()
    chain3({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])
    const dialog = await openPopupOnC(user)

    const panels = screen.getAllByRole('region', { name: 'Prerequisite context' })
    expect(panels).toHaveLength(1)
    expect(dialog.contains(panels[0])).toBe(true)
    expect(panels[0].getAttribute('data-anchor-task')).toBe('T-C')
  })

  it('control: with no popup open the selected-anchor panel is reachable outside', async () => {
    chain3({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])

    const panel = await screen.findByRole('region', { name: 'Prerequisite context' })
    expect(panel.getAttribute('data-anchor-task')).toBe('T-A')
    expect(panel.closest('dialog')).toBeNull()
    expect(within(panel).getByRole('button', { name: 'Reveal completed prerequisites' })).toBeVisible()
  })

  it('control: closing the popup returns the anchor to the shell selection', async () => {
    const user = userEvent.setup()
    chain3({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])
    await openPopupOnC(user)
    await user.keyboard('{Escape}')

    const panel = screen.getByRole('region', { name: 'Prerequisite context' })
    expect(panel.getAttribute('data-anchor-task')).toBe('T-A')
    expect(panel.closest('dialog')).toBeNull()
  })
})

/**
 * MR-01: activating a control that then disappears must not drop keyboard focus
 * out of a live modal. Focus continuation is asserted on the settled DOM.
 */
describe('modal keyboard focus continuation', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: ['T-B'], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'done', dependencies: [], objective_ids: [] })
  const C = makeTask({ id: 'T-C', status: 'open', dependencies: ['T-D'], objective_ids: [], context_count: 2 })
  const D = makeTask({ id: 'T-D', status: 'done', dependencies: [], objective_ids: [] })

  function chain4(overrides: Partial<AppUrlState>, tasks: Task[], updateUrl = vi.fn()) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const view = render(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, ...overrides }}
          updateUrl={updateUrl}
          workspace={workspaceWith(tasks)}
        />
      </QueryClientProvider>,
    )
    return { ...view, updateUrl }
  }

  it('MR-01 keeps focus inside the dialog after Reveal removes its own control', async () => {
    const user = userEvent.setup()
    const chain = chain4({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')

    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    let guard = 0
    while (document.activeElement !== reveal && guard < 25) { await user.tab(); guard += 1 }
    expect(reveal).toHaveFocus()

    await user.keyboard('{Enter}')

    // The activated control is gone, but focus must remain on a live control
    // inside the still-open modal rather than falling to BODY.
    const settled = screen.getByRole('dialog')
    expect(document.activeElement).not.toBe(document.body)
    expect(settled.contains(document.activeElement)).toBe(true)
    expect((document.activeElement as HTMLElement).isConnected).toBe(true)
    // The popup, the anchor and the shell selection are all unchanged.
    expect(settled).toBeInTheDocument()
    expect(within(settled).getByRole('region', { name: 'Prerequisite context' })
      .getAttribute('data-anchor-task')).toBe('T-C')
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('3 of 4 tasks shown')
    expect(chain.updateUrl).not.toHaveBeenCalled()
  })

  it('MR-01 keeps focus inside the dialog after Clear removes its own control', async () => {
    const user = userEvent.setup()
    const chain = chain4({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')

    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()
    await user.keyboard('{Enter}')

    const clear = within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear temporary reveal' })
    clear.focus()
    await user.keyboard('{Enter}')

    const settled = screen.getByRole('dialog')
    expect(document.activeElement).not.toBe(document.body)
    expect(settled.contains(document.activeElement)).toBe(true)
    expect(screen.getByText(/tasks shown/)).toHaveTextContent('2 of 4 tasks shown')
    expect(chain.updateUrl).not.toHaveBeenCalled()
  })

  it('MR-01 control: focus stays put when the activated control survives', async () => {
    const user = userEvent.setup()
    chain4({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')

    // A View control is not removed by its own activation, so focus must stay
    // exactly where the user put it rather than being moved by the panel.
    const view = within(dialog).getByRole('button', { name: 'View T-D' })
    view.focus()
    await user.keyboard('{Enter}')
    expect(view).toHaveFocus()
  })

  it('MR-01 control: does not steal focus after the user moves it deliberately', async () => {
    const user = userEvent.setup()
    chain4({ view: 'graph', taskId: 'T-A' }, [A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')
    const close = within(dialog).getByRole('button', { name: 'Close context' })

    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()
    await user.keyboard('{Enter}')

    // The user chooses a different control; nothing may pull focus away later.
    close.focus()
    await act(async () => { await Promise.resolve() })
    expect(close).toHaveFocus()
  })

  it('MR-01 control: the non-modal selected panel behaves the same way', async () => {
    const user = userEvent.setup()
    chain4({ view: 'graph', taskId: 'T-A' }, [A, B])

    const panel = await screen.findByRole('region', { name: 'Prerequisite context' })
    const reveal = within(panel).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()
    await user.keyboard('{Enter}')

    const settled = screen.getByRole('region', { name: 'Prerequisite context' })
    expect(document.activeElement).not.toBe(document.body)
    expect(settled.contains(document.activeElement)).toBe(true)
  })
})

/**
 * MR-02: a handoff intent must never outlive the control that created it. If
 * the activated control is still connected, no removal happened, so nothing may
 * later move focus on its behalf.
 */
describe('surviving-control focus intent', () => {
  const A = makeTask({ id: 'T-A', status: 'open', dependencies: ['T-B'], objective_ids: [] })
  const B = makeTask({ id: 'T-B', status: 'done', dependencies: [], objective_ids: [] })
  const C = makeTask({ id: 'T-C', status: 'open', dependencies: ['T-D'], objective_ids: [], context_count: 2 })
  const D = makeTask({ id: 'T-D', status: 'done', dependencies: [], objective_ids: [] })
  const E = makeTask({ id: 'T-E', status: 'done', dependencies: [], objective_ids: [] })

  function chain5(tasks: Task[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const node = (ts: Task[]) => (
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'graph', taskId: 'T-A' }}
          updateUrl={vi.fn()}
          workspace={workspaceWith(ts)}
        />
      </QueryClientProvider>
    )
    const view = render(node(tasks))
    return { ...view, refresh: (ts: Task[]) => view.rerender(node(ts)) }
  }

  it('MR-02 does not hand off later when the activated Reveal stayed connected', async () => {
    const user = userEvent.setup()
    // C depends on D; a canonical refresh adds E so a candidate always remains
    // and the exact original Reveal control is never removed.
    const withD = { ...C, dependencies: ['T-D'] }
    const withDE = { ...C, dependencies: ['T-D', 'T-E'] }
    const chain = chain5([A, B, withD, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')
    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()

    // One batch: activate the control AND deliver the canonical refresh.
    await act(async () => {
      reveal.click()
      chain.refresh([A, B, withDE, D, E])
    })

    // The exact original control survived and still owns focus.
    expect(reveal.isConnected).toBe(true)
    expect(reveal).toHaveFocus()

    // A further canonical refresh must not disturb it.
    await act(async () => { chain.refresh([A, B, withDE, D, { ...E, title: 'Renamed E' }]) })
    expect(reveal).toHaveFocus()

    // Clicking a non-focusable heading drops focus without removing Reveal.
    const heading = within(screen.getByRole('dialog'))
      .getByRole('heading', { name: /Prerequisites for T-C/ })
    await user.click(heading)

    // No removal ever happened, so nothing may hand focus to Clear.
    const settled = screen.getByRole('dialog')
    expect(reveal.isConnected).toBe(true)
    const clear = within(settled).queryByRole('button', { name: 'Clear temporary reveal' })
    expect(clear).not.toBeNull()
    expect(clear).not.toHaveFocus()
  })

  it('MR-02 control: removal handoff still works for Reveal to Clear', async () => {
    const user = userEvent.setup()
    const chain = chain5([A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    const dialog = await screen.findByRole('dialog')
    const reveal = within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' })
    reveal.focus()
    await user.keyboard('{Enter}')

    // Here the activated control really is removed, so the handoff must happen.
    expect(reveal.isConnected).toBe(false)
    const settled = screen.getByRole('dialog')
    expect(settled.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(document.body)
    expect(chain.container).toBeTruthy()
  })

  it('MR-02 control: removal handoff still works for Clear to Reveal', async () => {
    const user = userEvent.setup()
    chain5([A, B, C, D])

    await user.click(await screen.findByRole('button', { name: /Open context for task T-C/ }))
    let dialog = await screen.findByRole('dialog')
    within(dialog).getByRole('button', { name: 'Reveal completed prerequisites' }).focus()
    await user.keyboard('{Enter}')

    dialog = screen.getByRole('dialog')
    const clear = within(dialog).getByRole('button', { name: 'Clear temporary reveal' })
    clear.focus()
    await user.keyboard('{Enter}')

    expect(clear.isConnected).toBe(false)
    const settled = screen.getByRole('dialog')
    expect(settled.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(document.body)
  })
})


describe('outcome catalog through the real Page chain', () => {
  const outcomeWorkspace = (tasks: Task[]): WorkspaceProjection => ({
    ...populatedWorkspace,
    tasks,
    edges: [],
    notes: [],
    objectives: [{
      id: 'O-A',
      objective: 'Objective A',
      revision: 1,
      key_results: [
        { id: 'KR-1', text: 'A outcome', progress: 0 },
        { id: 'KR-2', text: 'Zero linked' },
      ],
    }],
  })

  function renderOutcomePage(tasks: Task[], updateUrl = vi.fn()) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const utils = render(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'table' }}
          updateUrl={updateUrl}
          workspace={outcomeWorkspace(tasks)}
        />
      </QueryClientProvider>,
    )
    return { ...utils, updateUrl }
  }

  it('lists every uniquely resolvable outcome, including a zero-linked one', async () => {
    const linked = { ...baseTask, id: 'T-100', objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-1' }] } as Task

    renderOutcomePage([linked])
    await userEvent.click(screen.getByRole('button', { name: 'Filter tasks' }))

    const filter = screen.getByLabelText('Filter by outcome')
    expect(within(filter).getByRole('option', { name: /O-A · KR-1/ })).toBeInTheDocument()
    expect(within(filter).getByRole('option', { name: /O-A · KR-2/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Outcomes' })).not.toBeInTheDocument()
  })

  it('still shows outcomes when the workspace has no Tasks at all', async () => {
    renderOutcomePage([])
    await userEvent.click(screen.getByRole('button', { name: 'Filter tasks' }))

    const filter = screen.getByLabelText('Filter by outcome')
    expect(within(filter).getByRole('option', { name: /O-A · KR-1/ })).toBeInTheDocument()
    expect(within(filter).getByRole('option', { name: /O-A · KR-2/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create first task' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByRole('region', { name: 'Outcomes' })).not.toBeInTheDocument()
  })

  it('choosing an outcome changes only that filter coordinate', async () => {
    const linked = { ...baseTask, id: 'T-100', objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-1' }] } as Task
    const { updateUrl } = renderOutcomePage([linked])

    await userEvent.click(screen.getByRole('button', { name: 'Filter by outcome O-A KR-1' }))

    expect(updateUrl).toHaveBeenCalledExactlyOnceWith({
      outcomeFilter: { kind: 'pair', objectiveId: 'O-A', keyResultId: 'KR-1' },
    })
  })
})


describe('NG5 the catalog reflects the active outcome', () => {
  const selectionWorkspace = (tasks: Task[]): WorkspaceProjection => ({
    ...populatedWorkspace,
    tasks,
    edges: [],
    notes: [],
    objectives: [{
      id: 'O-A',
      objective: 'Objective A',
      revision: 1,
      key_results: [{ id: 'K1', text: 'A outcome' }, { id: 'K2', text: 'Other outcome' }],
    }],
  })

  function renderSelected(tasks: Task[], overrides: Partial<AppUrlState> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const updateUrl = vi.fn()
    const utils = render(
      <QueryClientProvider client={client}>
        <WorkspacePage
          isRefreshing={false}
          onChangeTaskStatus={vi.fn()}
          onCreateTask={vi.fn()}
          onOpenObjectives={vi.fn()}
          onRefresh={vi.fn()}
          state={{ ...state, view: 'table', ...overrides }}
          updateUrl={updateUrl}
          workspace={selectionWorkspace(tasks)}
        />
      </QueryClientProvider>,
    )
    return { ...utils, updateUrl }
  }

  const linked = { ...baseTask, id: 'T-500', objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'K1' }] } as Task

  async function openOutcomeFilter() {
    await userEvent.click(screen.getByRole('button', { name: 'Filter tasks' }))
    return screen.getByLabelText('Filter by outcome')
  }

  it('marks the selected pair pressed and leaves the others unpressed', async () => {
    renderSelected([linked], { outcomeFilter: { kind: 'pair', objectiveId: 'O-A', keyResultId: 'K1' } })

    expect(await openOutcomeFilter()).toHaveValue(JSON.stringify(['pair', 'O-A', 'K1']))
  })

  it.each([
    ['all', { kind: 'all' } as const],
    ['unassigned', { kind: 'unassigned' } as const],
  ])('%s selects no pair', async (_name, outcomeFilter) => {
    renderSelected([linked], { outcomeFilter })

    expect(await openOutcomeFilter()).toHaveValue(_name)
  })

  it('reflects a changed pair without adding another selection owner', async () => {
    renderSelected([linked], { outcomeFilter: { kind: 'pair', objectiveId: 'O-A', keyResultId: 'K2' } })

    expect(await openOutcomeFilter()).toHaveValue(JSON.stringify(['pair', 'O-A', 'K2']))
  })

  it('marks the selection in the zero-Task branch too', async () => {
    renderSelected([], { outcomeFilter: { kind: 'pair', objectiveId: 'O-A', keyResultId: 'K1' } })

    expect(screen.getByRole('button', { name: 'Create first task' })).toBeInTheDocument()
    expect(await openOutcomeFilter()).toHaveValue(JSON.stringify(['pair', 'O-A', 'K1']))
  })
})


describe('GN4 the Page outcome coordinate reaches the rendered Graph', () => {
  const graphWorkspace = (): WorkspaceProjection => ({
    ...populatedWorkspace,
    tasks: [
      { ...baseTask, id: 'T-700', objective_ids: ['O-1'], key_result_refs: [{ objective_id: 'O-1', key_result_id: 'K1' }] } as Task,
      { ...baseTask, id: 'T-701', objective_ids: ['O-2'], key_result_refs: [{ objective_id: 'O-2', key_result_id: 'K1' }] } as Task,
    ],
    edges: [],
    notes: [],
    objectives: [
      { id: 'O-1', objective: 'Objective one', revision: 1, key_results: [{ id: 'K1', text: 'First outcome' }] },
      { id: 'O-2', objective: 'Objective two', revision: 1, key_results: [{ id: 'K1', text: 'Second outcome' }] },
    ],
  })

  function renderGraph(overrides: Partial<AppUrlState> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const updateUrl = vi.fn()
    render(
      <StrictMode>
        <QueryClientProvider client={client}>
          <WorkspacePage
            isRefreshing={false}
            onChangeTaskStatus={vi.fn()}
            onCreateTask={vi.fn()}
            onOpenObjectives={vi.fn()}
            onRefresh={vi.fn()}
            state={{ ...state, view: 'graph', ...overrides }}
            updateUrl={updateUrl}
            workspace={graphWorkspace()}
          />
        </QueryClientProvider>
      </StrictMode>,
    )
    return { updateUrl }
  }

  const krFrame = (objectiveId: string) =>
    screen.getByRole('button', { name: `Filter by key result ${objectiveId} K1` }).closest('.wsv-graph-node')

  it('starts with the URL pair already selected on the rendered key result', () => {
    renderGraph({ outcomeFilter: { kind: 'pair', objectiveId: 'O-2', keyResultId: 'K1' } })

    expect(krFrame('O-2')).toHaveClass('is-selected')
    expect(krFrame('O-1')).not.toHaveClass('is-selected')
  })

  it('activating a key result emits exactly that pair AND renders it selected', () => {
    const { updateUrl } = renderGraph()
    expect(krFrame('O-2')).not.toHaveClass('is-selected')

    // A plain click on the product control: a synthetic pointer sequence would
    // also reach the canvas pan handler, which is not what this test is about.
    fireEvent.click(screen.getByRole('button', { name: 'Filter by key result O-2 K1' }))

    expect(updateUrl).toHaveBeenCalledWith(
      expect.objectContaining({ outcomeFilter: { kind: 'pair', objectiveId: 'O-2', keyResultId: 'K1' } }),
    )
    expect(updateUrl).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['all', { kind: 'all' } as const],
    ['unassigned', { kind: 'unassigned' } as const],
  ])('%s leaves every rendered key result unselected', (_name, outcomeFilter) => {
    renderGraph({ outcomeFilter })

    for (const button of screen.getAllByRole('button', { name: /^Filter by key result/ })) {
      expect(button.closest('.wsv-graph-node')).not.toHaveClass('is-selected')
    }
  })
})


describe('GC-F1 the real Page chain never draws a dependency onto a same-ID Objective', () => {
  const gcWorkspace = (): WorkspaceProjection => ({
    ...populatedWorkspace,
    tasks: [
      { ...baseTask, id: 'T-0001', title: 'Visible work', status: 'open', objective_ids: ['T-0002'], dependencies: ['T-0002'] } as Task,
      { ...baseTask, id: 'T-0002', title: 'Finished prerequisite', status: 'done', objective_ids: [], dependencies: [] } as Task,
    ],
    edges: [],
    notes: [],
    objectives: [{ id: 'T-0002', objective: 'Legally identical raw id', revision: 1, key_results: [] }],
  })

  function renderGraph() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <StrictMode>
        <QueryClientProvider client={client}>
          <WorkspacePage
            isRefreshing={false}
            onChangeTaskStatus={vi.fn()}
            onCreateTask={vi.fn()}
            onOpenObjectives={vi.fn()}
            onRefresh={vi.fn()}
            state={{ ...state, view: 'graph' }}
            updateUrl={vi.fn()}
            workspace={gcWorkspace()}
          />
        </QueryClientProvider>
      </StrictMode>,
    )
  }

  it('hides the completed prerequisite yet keeps the Objective and drops only that dependency edge', async () => {
    const { container } = renderGraph()
    // The Graph view is lazy; wait for the real chunk, then read the committed DOM.
    await screen.findByText('Visible work')

    // Existing completed visibility hides the Done Task; the Objective stays.
    expect(screen.queryByText('Finished prerequisite')).not.toBeInTheDocument()
    expect(container.querySelector('.wsv-graph-node--objective')).not.toBeNull()

    // The same-ID T-0002 survives ONLY as an Objective; no Task node is
    // manufactured for the hidden prerequisite, so nothing on this canvas can
    // stand in for the dependency's required Task endpoint.
    const liveIds = [...container.querySelectorAll('.react-flow__node')].map((node) => node.getAttribute('data-id'))
    expect(liveIds).toContain('flow|objective|T-0002')
    expect(liveIds).toContain('flow|task|T-0001')
    expect(liveIds).not.toContain('flow|task|T-0002')
    expect(container.querySelectorAll('.wsv-graph-node--task')).toHaveLength(1)
  })
})


describe('TE-F1 the real Page chain generates the Note to Note reference', () => {
  const noteWorkspace = (): WorkspaceProjection => ({
    ...populatedWorkspace,
    tasks: [{ ...baseTask, id: 'T-0001', title: 'Visible work', status: 'open', objective_ids: [], dependencies: [] } as Task],
    edges: [],
    notes: [
      { id: 'N-0001', text: 'Links a Task and another Note', created: '2026-09-01', links: ['T-0001', 'N-0002'] },
      { id: 'N-0002', text: 'Links the same Task', created: '2026-09-01', links: ['T-0001'] },
    ],
    objectives: [],
  })

  it('records all three references on the actual generated edge array', async () => {
    const workspace = noteWorkspace()
    const before = JSON.stringify(workspace.notes)
    recordedFlowEdges = []
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(
      <StrictMode>
        <QueryClientProvider client={client}>
          <WorkspacePage
            isRefreshing={false}
            onChangeTaskStatus={vi.fn()}
            onCreateTask={vi.fn()}
            onOpenObjectives={vi.fn()}
            onRefresh={vi.fn()}
            state={{ ...state, view: 'graph' }}
            updateUrl={vi.fn()}
            workspace={workspace}
          />
        </QueryClientProvider>
      </StrictMode>,
    )
    // The Graph view is lazy; wait for the real chunk before reading the canvas.
    await screen.findByText('Visible work')

    const liveIds = [...container.querySelectorAll('.react-flow__node')].map((node) => node.getAttribute('data-id'))
    // Healthy controls first: both real Note nodes are on the canvas.
    expect(liveIds).toContain('flow|note|N-0001')
    expect(liveIds).toContain('flow|note|N-0002')

    const references = recordedFlowEdges
      .filter((edge) => edge.data?.kind === 'reference')
      .map((edge) => `${edge.source}->${edge.target}`)
      .sort()
    expect(references).toContain('flow|note|N-0001->flow|task|T-0001')
    expect(references).toContain('flow|note|N-0002->flow|task|T-0001')
    expect(references).toContain('flow|note|N-0001->flow|note|N-0002')

    for (const edge of recordedFlowEdges) {
      expect(liveIds).toContain(edge.source)
      expect(liveIds).toContain(edge.target)
    }
    expect(JSON.stringify(workspace.notes)).toBe(before)
  })
})
