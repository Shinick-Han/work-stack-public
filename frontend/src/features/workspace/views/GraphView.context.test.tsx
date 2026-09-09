import React from 'react'
import { readFileSync } from 'node:fs'
import { projectKeyResults } from './keyResultModel'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, test, vi } from 'vitest'
import { api } from '../../../api/client'
import { taskDetailSchema } from '../../../domain/schemas'
import { task } from '../../../test/fixtures'
import type { TaskDetail } from '../../../domain/types'
import { GraphView } from './GraphView'

const { layoutPlanningGraphMock } = vi.hoisted(() => ({
  layoutPlanningGraphMock: vi.fn(),
}))

vi.mock('./graphLayout', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./graphLayout')>()
  return {
    ...actual,
    layoutPlanningGraph: layoutPlanningGraphMock,
  }
})

// Keep the product node controls and GraphView coordination; replace only the
// canvas engine, whose layout/virtualization is retained in the scale suite.
let commitObserver: ((ids: string[]) => void) | null = null
export function observeCommits(fn: ((ids: string[]) => void) | null) { commitObserver = fn }
let lastFlowNodes: { id: string; position: { x: number; y: number }; data: { id: string } }[] = []

vi.mock('@xyflow/react', async (importOriginal) => ({
  ...await importOriginal<typeof import('@xyflow/react')>(),
  ReactFlow: ({ nodes, nodeTypes, onNodeClick }: { nodes: { id: string; position: { x: number; y: number }; data: { id: string } }[]; nodeTypes: { workspace: React.ComponentType<{ data: object }> }; onNodeClick?: (event: React.MouseEvent, node: object) => void }) => {
    const Node = nodeTypes.workspace
    lastFlowNodes = nodes
    // A layout effect records the node IDs of the COMMITTED tree, which is what
    // the F3 contract is about; a settled waitFor could not observe it.
    // Membership is the CANONICAL identity carried in node.data.id; the live
    // Flow id is a typed presentation value and is asserted separately.
    React.useLayoutEffect(() => { commitObserver?.(nodes.map((node) => node.data.id)) })
    return <div>{nodes.map((node) => <div key={node.id} onClick={(event) => onNodeClick?.(event, node)}><Node data={node.data} /></div>)}</div>
  },
  Handle: () => null,
}))

function defaultLayoutMock(nodes: { id: string; position: { x: number; y: number } }[]) {
  return {
    nodes: nodes.map((node, index) => ({
      ...node,
      position: { x: 40 + index, y: 80 },
    })),
    edgeRoutes: {},
  }
}

beforeEach(() => {
  layoutPlanningGraphMock.mockReset()
  layoutPlanningGraphMock.mockImplementation(async (nodes) => defaultLayoutMock(nodes))
})

async function settleMockedLayout() {
  await act(async () => {
    await Promise.all(layoutPlanningGraphMock.mock.results.map((result) => result.value))
  })
}

async function setup(options: { settleLayout?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const props = { tasks: [{ ...task, context_count: 2 }, { ...task, id: 'T-0002', context_count: 0 }],
    objectives: [{ id: 'O-1', objective: 'Quality' }], notes: [], edges: [],
    onSelectTask: vi.fn(), onSelectObjective: vi.fn() }
  const view = render(<QueryClientProvider client={client}><GraphView {...props} referenceTasks={props.tasks} /></QueryClientProvider>)
  if (options.settleLayout !== false) await settleMockedLayout()
  const rerenderWith = async (extra: Record<string, unknown>) => {
    view.rerender(
      <QueryClientProvider client={client}><GraphView {...props} referenceTasks={props.tasks} {...extra} /></QueryClientProvider>,
    )
    if (options.settleLayout !== false) await settleMockedLayout()
  }
  return {
    ...view,
    props,
    rerenderWith,
    /** Canonical deletion: the Task leaves BOTH the projected and canonical arrays. */
    async removeTasks() { await rerenderWith({ tasks: [], referenceTasks: [] }) },
    /** Visibility filtering only: the Task leaves the projected array but stays canonical. */
    async hideTasks() { await rerenderWith({ tasks: [], emptyKind: 'all-complete' }) },
  }
}

test('badge click, Enter and Space open context without selecting the task; node activation stays separate', async () => {
  const request = vi.spyOn(api, 'getTask').mockResolvedValue({ task, context: [], activity: [], replies: [] })
  const view = await setup()
  expect(request).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: /Open context for task T-0002/ })).not.toBeInTheDocument()
  const badge = await screen.findByRole('button', { name: /Open context for task T-0001/ })
  expect(badge.parentElement?.closest('[role="button"]')).toBeNull()
  for (const action of ['click', 'Enter', 'Space']) {
    badge.focus()
    if (action === 'click') await userEvent.click(badge)
    else await userEvent.keyboard(action === 'Enter' ? '{Enter}' : ' ')
    expect(await screen.findByRole('dialog', { name: task.title })).toBeInTheDocument()
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(request).toHaveBeenLastCalledWith(task.id)
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(badge).toHaveFocus()
  }
  const node = screen.getByRole('button', { name: 'Open task T-0001' })
  await userEvent.click(node)
  node.focus()
  await userEvent.keyboard('{Enter} ')
  expect(view.props.onSelectTask).toHaveBeenCalledTimes(3)
  await userEvent.click(screen.getByRole('button', { name: 'Focus objective O-1' }))
  expect(view.props.onSelectObjective).toHaveBeenCalledExactlyOnceWith('O-1')
})

test('zero-context + Context opens the popup without selecting the task, then a saved card uses that Task', async () => {
  let created = false
  vi.spyOn(api, 'getTask').mockImplementation(async (id) => {
    if (id === 'T-0002' && created) {
      return taskDetailSchema.parse({
        task: { ...task, id: 'T-0002' }, activity: [], replies: [],
        context: [{
          id: 'N-9', text: 'Release assumption', links: ['T-0002'], created: '2026-09-10',
          ref: { kind: 'note', id: 'N-9' }, connections: [], date_precision: 'date',
        }],
      })
    }
    return { task: { ...task, id }, context: [], activity: [], replies: [] }
  })
  const create = vi.spyOn(api, 'createNote').mockImplementation(async (text, links) => {
    created = true
    return { id: 'N-9', text, links }
  })
  const view = await setup()
  const layouts = layoutPlanningGraphMock.mock.calls.length
  const add = await screen.findByRole('button', { name: 'Add context for task T-0002' })
  expect(add).toHaveTextContent('+ Context')
  await userEvent.click(add)
  expect(view.props.onSelectTask).not.toHaveBeenCalled()
  expect(layoutPlanningGraphMock.mock.calls.length).toBe(layouts)
  const dialog = await screen.findByRole('dialog', { name: task.title })
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'Release assumption')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  await waitFor(() => expect(create).toHaveBeenCalledExactlyOnceWith(
    'Release assumption',
    ['T-0002'],
    expect.stringMatching(/^workstack:/),
  ))
  expect(await screen.findByRole('heading', { name: 'Release assumption' })).toBeInTheDocument()
  expect(layoutPlanningGraphMock.mock.calls.length).toBe(layouts)
  await view.rerenderWith({
    tasks: [{ ...task, context_count: 2 }, { ...task, id: 'T-0002', title: task.title, context_count: 1 }],
  })
  expect(screen.getByRole('button', { name: /Open context for task T-0002: 1 linked/ })).toBeInTheDocument()
  expect(dialog).toBeInTheDocument()
})

test('Open task closes the panel and selects its exact task', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue({ task, context: [], activity: [], replies: [] })
  const view = await setup()
  await userEvent.click(await screen.findByRole('button', { name: /Open context for task T-0001/ }))
  await userEvent.click(await screen.findByRole('button', { name: 'Open task' }))
  expect(view.props.onSelectTask).toHaveBeenCalledExactlyOnceWith(task.id)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('canonical removal closes the popup and ignores its late response', async () => {
  let resolve!: (value: TaskDetail) => void
  vi.spyOn(api, 'getTask').mockReturnValue(new Promise((done) => { resolve = done }))
  const view = await setup()
  await userEvent.click(await screen.findByRole('button', { name: /Open context for task T-0001/ }))
  expect(screen.getByRole('status')).toHaveTextContent('Loading context')
  await view.removeTasks()
  await act(async () => resolve({ task, context: [], activity: [], replies: [] }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByText('No work matches these filters')).toBeInTheDocument()
})

test('visibility filtering keeps an open popup while canonical removal closes it', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue({ task, context: [], activity: [], replies: [] })
  const view = await setup()
  await userEvent.click(await screen.findByRole('button', { name: /Open context for task T-0001/ }))
  expect(await screen.findByRole('dialog', { name: task.title })).toBeInTheDocument()

  // Hidden by the Done rule, still canonical: the popup must survive.
  await view.hideTasks()
  expect(screen.getByRole('dialog', { name: task.title })).toBeInTheDocument()
  expect(screen.getByText('All matching tasks are completed')).toBeInTheDocument()

  // Gone from the canonical array: only now is it dismissed.
  await view.removeTasks()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('the popup keeps its own response identity when its task becomes hidden', async () => {
  let resolve!: (value: TaskDetail) => void
  const request = vi.spyOn(api, 'getTask').mockReturnValue(new Promise((done) => { resolve = done }))
  const view = await setup()
  await userEvent.click(await screen.findByRole('button', { name: /Open context for task T-0001/ }))
  await view.hideTasks()

  await act(async () => resolve({ task, context: [], activity: [], replies: [] }))
  expect(await screen.findByRole('dialog', { name: task.title })).toBeInTheDocument()
  // No refetch and no identity switch caused by the visibility change.
  expect(request).toHaveBeenCalledExactlyOnceWith(task.id)
})

test('reports context target and focus pin separately to a controlling owner', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue({ task, context: [], activity: [], replies: [] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const onContextTargetChange = vi.fn()
  const onFocusPinChange = vi.fn()
  const tasks = [{ ...task, context_count: 2 }]
  render(<QueryClientProvider client={client}><GraphView
    tasks={tasks}
    referenceTasks={tasks}
    objectives={[]}
    notes={[]}
    edges={[]}
    contextTargetTaskId={null}
    focusPinnedTaskId={null}
    onContextTargetChange={onContextTargetChange}
    onFocusPinChange={onFocusPinChange}
    onSelectTask={vi.fn()}
    onSelectObjective={vi.fn()}
  /></QueryClientProvider>)
  await settleMockedLayout()

  await userEvent.click(await screen.findByRole('button', { name: /Open context for task T-0001/ }))
  expect(onContextTargetChange).toHaveBeenCalledWith(task.id)
  expect(onFocusPinChange).toHaveBeenCalledWith(task.id)
})

test('keeps one canvas instance and makes no camera call across nonempty to empty to nonempty', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue({ task, context: [], activity: [], replies: [] })
  const view = await setup()

  const canvas = await screen.findByLabelText('Task relationship graph')
  // A sentinel proves the same DOM element survives, so its viewport does too.
  canvas.setAttribute('data-viewport-sentinel', 'kept')
  expect(canvas.getAttribute('data-graph-empty')).toBe('false')

  await view.hideTasks()
  const emptied = screen.getByLabelText('Task relationship graph')
  expect(emptied).toBe(canvas)
  expect(emptied.getAttribute('data-viewport-sentinel')).toBe('kept')
  expect(emptied.getAttribute('data-graph-empty')).toBe('true')
  // The overlay replaced the old early return, so nothing unmounted.
  expect(screen.getByText('All matching tasks are completed')).toBeInTheDocument()

  await view.rerenderWith({})
  const restored = screen.getByLabelText('Task relationship graph')
  expect(restored).toBe(canvas)
  expect(restored.getAttribute('data-viewport-sentinel')).toBe('kept')
  expect(restored.getAttribute('data-graph-empty')).toBe('false')
  // jsdom with a substituted canvas engine cannot prove real-browser camera
  // behaviour; this asserts instance continuity only.
})

test('feeds only visible tasks to the model without synthesizing a bypass edge', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const a = { ...task, id: 'T-A', title: 'A', dependencies: ['T-B'], context_count: 0 }
  const b = { ...task, id: 'T-B', title: 'B', status: 'done' as const, dependencies: ['T-C'], context_count: 0 }
  const c = { ...task, id: 'T-C', title: 'C', status: 'done' as const, dependencies: [], context_count: 0 }
  const canonical = [a, b, c]

  render(<QueryClientProvider client={client}><GraphView
    tasks={[a]}
    referenceTasks={canonical}
    objectives={[]}
    notes={[]}
    edges={[]}
    onSelectTask={vi.fn()}
    onSelectObjective={vi.fn()}
  /></QueryClientProvider>)
  await settleMockedLayout()

  expect(await screen.findByRole('button', { name: 'Open task T-A' })).toBeInTheDocument()
  // B is hidden, and no A-to-C node appears in its place.
  expect(screen.queryByRole('button', { name: 'Open task T-B' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Open task T-C' })).not.toBeInTheDocument()
})

test('F3 committed node membership matches the authoritative model on the same commit', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const a = { ...task, id: 'T-A', title: 'A', status: 'open' as const, dependencies: [], context_count: 0 }
  const b = { ...task, id: 'T-B', title: 'B', status: 'done' as const, dependencies: [], context_count: 0 }

  // Observe what the canvas actually receives on each committed render.
  const commits: string[][] = []
  observeCommits((ids) => commits.push(ids))
  try {
    const view = render(<QueryClientProvider client={client}><GraphView
      tasks={[a, b]} referenceTasks={[a, b]} objectives={[]} notes={[]} edges={[]}
      onSelectTask={vi.fn()} onSelectObjective={vi.fn()}
    /></QueryClientProvider>)
    await settleMockedLayout()
    await screen.findByRole('button', { name: 'Open task T-A' })

    commits.length = 0
    // The projection now hides the completed Task while layout is still pending.
    view.rerender(<QueryClientProvider client={client}><GraphView
      tasks={[a]} referenceTasks={[a, b]} objectives={[]} notes={[]} edges={[]}
      onSelectTask={vi.fn()} onSelectObjective={vi.fn()}
    /></QueryClientProvider>)

    // The FIRST commit after the change must already agree with the model.
    expect(commits[0]).toEqual(['T-A'])
    expect(commits.every((ids) => !ids.includes('T-B'))).toBe(true)
    await settleMockedLayout()
  } finally {
    observeCommits(null)
  }
})


test('GN3 the key-result frame CSS targets the class names the real frame renders', async () => {
  const objectives = [{ id: 'O-1', objective: 'Objective', revision: 1, key_results: [{ id: 'K1', text: 'A legitimately very long outcome statement that must clamp instead of growing the node' }] }]
  const tasks = [{ ...task, id: 'T-0001', objective_ids: ['O-1'], key_result_refs: [{ objective_id: 'O-1', key_result_id: 'K1' }] }]
  const projection = projectKeyResults({ workspaceId: 'W1', tasks: tasks as never, objectives: objectives as never })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const { container } = render(
    <QueryClientProvider client={client}>
      <GraphView
        tasks={tasks as never}
        referenceTasks={tasks as never}
        objectives={objectives as never}
        notes={[]}
        edges={[]}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />
    </QueryClientProvider>,
  )
  await settleMockedLayout()

  const frame = container.querySelector('.wsv-graph-node--key-result')
  expect(frame).not.toBeNull()
  expect(frame!.querySelector('.wsv-graph-node__title')).not.toBeNull()
  expect(frame!.querySelector('.wsv-graph-node__eyebrow')).not.toBeNull()
  expect(frame!.querySelector('.wsv-graph-node__outcome')).not.toBeNull()

  // The stylesheet must bind to THAT frame, not to a class family nothing renders.
  const css = readFileSync('src/features/workspace/views/workspace-views.css', 'utf-8')
  const bounded = css.slice(css.indexOf('GR03/GR04'))
  for (const selector of bounded.match(/\.wsv-[a-z-]+(?:__[a-z-]+)?(?:--[a-z-]+)?/g) ?? []) {
    if (selector.startsWith('.wsv-relation') || selector.startsWith('.wsv-minimap') || selector === '.wsv-root') continue
    expect(container.querySelector(selector), `no element matches ${selector}`).not.toBeNull()
  }
})


test('selection-only rerenders skip topology-equivalent layout and keep coordinates', async () => {
  const view = await setup()
  await screen.findByRole('button', { name: 'Open task T-0001' })
  await waitFor(() => {
    expect(layoutPlanningGraphMock).toHaveBeenCalled()
    expect(lastFlowNodes[0]?.position.x).toBe(40)
  })
  const calls = layoutPlanningGraphMock.mock.calls.length
  const positions = lastFlowNodes.map((node) => [node.id, node.position.x, node.position.y])
  await view.rerenderWith({ selectedTaskId: 'T-0001', selectedObjectiveId: 'O-1' })
  await act(async () => { await Promise.resolve() })
  expect(layoutPlanningGraphMock.mock.calls.length).toBe(calls)
  expect(lastFlowNodes.map((node) => [node.id, node.position.x, node.position.y])).toEqual(positions)
})

test('keeps last coordinates until one complete replacement layout exists', async () => {
  const pending: Array<(value: { nodes: { id: string; position: { x: number; y: number } }[]; edgeRoutes: Record<string, never> }) => void> = []
  layoutPlanningGraphMock.mockImplementation(async (nodes: { id: string; position: { x: number; y: number } }[]) => (
    new Promise((resolve) => {
      pending.push((value) => resolve(value ?? {
        nodes: nodes.map((node) => ({ ...node, position: { x: 15, y: 25 } })),
        edgeRoutes: {},
      }))
    })
  ))
  const view = await setup({ settleLayout: false })
  await screen.findByRole('button', { name: 'Open task T-0001' })
  expect(pending).toHaveLength(1)
  await act(async () => {
    pending.shift()?.({
      nodes: lastFlowNodes.map((node) => ({ ...node, position: { x: 15, y: 25 } })),
      edgeRoutes: {},
    })
  })
  await waitFor(() => {
    expect(lastFlowNodes.every((node) => node.position.x === 15 && node.position.y === 25)).toBe(true)
  })
  await view.rerenderWith({ tasks: [{ ...task, context_count: 2 }] })
  expect(lastFlowNodes.map((node) => node.data.id).sort()).toEqual(['O-1', 'T-0001'])
  expect(lastFlowNodes.find((node) => node.data.id === 'T-0001')?.position).toEqual({ x: 15, y: 25 })
  expect(pending).toHaveLength(1)
  await act(async () => {
    pending.shift()?.({
      nodes: lastFlowNodes.map((node) => ({ ...node, position: { x: 90, y: 90 } })),
      edgeRoutes: {},
    })
  })
  await waitFor(() => expect(lastFlowNodes[0]?.position).toEqual({ x: 90, y: 90 }))
})

test('note activation shows existing content and toggles closed without Task navigation', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const onSelectTask = vi.fn()
  const onSelectObjective = vi.fn()
  const notes = [{ id: 'N-0001', text: 'Full planning note body', created: '2026-09-01', links: ['T-0001'] }]
  const props = {
    tasks: [{ ...task, context_count: 0 }],
    objectives: [{ id: 'O-1', objective: 'Quality' }],
    notes,
    edges: [],
    onSelectTask,
    onSelectObjective,
  }
  render(<QueryClientProvider client={client}><GraphView {...props} referenceTasks={props.tasks} /></QueryClientProvider>)
  await settleMockedLayout()

  const note = await screen.findByRole('button', { name: 'Show note N-0001' })
  await userEvent.click(note)
  const dialog = screen.getByRole('dialog', { name: 'Full planning note body' })
  expect(dialog).toHaveTextContent('T-0001')
  expect(onSelectTask).not.toHaveBeenCalled()
  expect(onSelectObjective).not.toHaveBeenCalled()
  await userEvent.click(note)
  expect(screen.queryByRole('dialog', { name: 'Full planning note body' })).not.toBeInTheDocument()
})
