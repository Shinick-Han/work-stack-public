import { expect, test } from 'vitest'
import { GRAPH_NODE_SIZES } from './graphLayout'
import { projectKeyResults } from './keyResultModel'
import type { WorkspaceEdge, WorkspaceObjective, WorkspaceTask } from './types'
import { makeGraphModel, miniMapNodeClassName, miniMapNodeColor, roundedOrthogonalPath, shouldVirtualizeGraph } from './GraphView'

test('maps each planning node to a visible semantic minimap mark', () => {
  const { nodes } = makeGraphModel(
    [{ id: 'T-0001', title: 'Ship minimap', status: 'open', priority: 'P2', objective_ids: ['O-1'] }],
    [{ id: 'O-1', objective: 'Make work legible', quarter: '2026-Q3' }],
    [{ id: 'N-0001', text: 'A useful note', created: '2026-08-31', links: ['T-0001'] }],
    [],
    'T-0001',
  )

  expect(nodes.map(miniMapNodeColor)).toEqual([
    'var(--wsv-minimap-node-objective)',
    'var(--wsv-minimap-node-task)',
    'var(--wsv-minimap-node-note)',
  ])
  expect(nodes.map(miniMapNodeClassName)).toEqual([
    'wsv-minimap-node wsv-minimap-node--objective',
    'wsv-minimap-node wsv-minimap-node--task is-selected',
    'wsv-minimap-node wsv-minimap-node--note',
  ])
  expect(nodes.map(({ initialWidth, initialHeight }) => [initialWidth, initialHeight])).toEqual([
    [221, 78],
    [216, 82],
    [232, 78],
  ])
  expect(nodes.every((node) => (node.initialWidth || 0) > 0 && (node.initialHeight || 0) > 0)).toBe(true)
})

test('enables viewport rendering only beyond the small-graph interaction threshold', () => {
  expect(shouldVirtualizeGraph(250)).toBe(false)
  expect(shouldVirtualizeGraph(251)).toBe(true)
})

test('builds a deterministic 1,000-Task graph projection within a bounded local budget', () => {
  const objectives: WorkspaceObjective[] = Array.from({ length: 20 }, (_, index) => ({
    id: `O-${index + 1}`,
    objective: `Objective ${index + 1}`,
  }))
  const tasks: WorkspaceTask[] = Array.from({ length: 1_000 }, (_, index) => ({
    dependencies: index ? [`T-${String(index).padStart(4, '0')}`] : [],
    id: `T-${String(index + 1).padStart(4, '0')}`,
    objective_ids: [`O-${(index % objectives.length) + 1}`],
    priority: index % 25 === 0 ? 'P0' : 'P2',
    status: index % 3 === 0 ? 'started' : 'open',
    title: `Synthetic scale task ${index + 1}`,
  }))
  const edges: WorkspaceEdge[] = tasks.slice(1).map((task, index) => ({
    id: `E-${index + 1}`,
    kind: 'dependency',
    source: task.id,
    target: tasks[index].id,
  }))

  const started = performance.now()
  const first = makeGraphModel(tasks, objectives, [], edges, 'T-0500')
  const elapsed = performance.now() - started
  const second = makeGraphModel(tasks, objectives, [], edges, 'T-0500')

  expect(first.nodes).toHaveLength(1_020)
  expect(first.edges.length).toBeGreaterThanOrEqual(1_999)
  expect(new Set(first.nodes.map((node) => node.id)).size).toBe(first.nodes.length)
  expect(new Set(first.edges.map((edge) => edge.id)).size).toBe(first.edges.length)
  expect(second).toEqual(first)
  expect(elapsed).toBeLessThan(1_000)
  expect(shouldVirtualizeGraph(first.nodes.length)).toBe(true)
})

test('focuses an Objective and its immediate relationships without opening another surface', () => {
  const objectives: WorkspaceObjective[] = [
    { id: 'O-1', objective: 'Release quality' },
    { id: 'O-2', objective: 'Operating rhythm' },
  ]
  const tasks: WorkspaceTask[] = [
    { id: 'T-0001', title: 'Release gate', objective_ids: ['O-1'] },
    { id: 'T-0002', title: 'Weekly review', objective_ids: ['O-2'] },
  ]
  const model = makeGraphModel(tasks, objectives, [], [], null, 'O-1')
  // Locate by canonical data identity; node.id is now a typed presentation value.
  const byId = new Map(model.nodes.map((node) => [node.data.id, node.data]))

  expect(byId.get('O-1')).toMatchObject({ selected: true, related: true })
  expect(byId.get('T-0001')).toMatchObject({ selected: false, related: true })
  expect(byId.get('O-2')).toMatchObject({ selected: false, related: false })
  expect(byId.get('T-0002')).toMatchObject({ selected: false, related: false })
})

test('rounds orthogonal ELK bends without changing their obstacle-avoiding route', () => {
  expect(roundedOrthogonalPath([
    { x: 300, y: 120 },
    { x: 260, y: 120 },
    { x: 260, y: 40 },
    { x: 220, y: 40 },
  ])).toBe('M 300 120 L 270 120 Q 260 120 260 110 L 260 50 Q 260 40 250 40 L 220 40')
})


test('GN2 selecting an Objective never selects a Task that legally shares its raw id', () => {
  const model = makeGraphModel(
    [{ id: 'T-0001', title: 'Same raw id as the Objective', status: 'open', priority: 'P2', objective_ids: ['T-0001'] }],
    [{ id: 'T-0001', objective: 'Legally identical raw id', quarter: '2026-Q3' }],
    [],
    [],
    null,
    'T-0001',
  )
  // Keyed by KIND plus canonical identity: a string-only map collapses this case.
  const byKind = new Map(model.nodes.map((node) => [`${node.data.kind}|${node.data.id}`, node.data]))

  expect(byKind.get('objective|T-0001')?.selected).toBe(true)
  expect(byKind.get('task|T-0001')?.selected).toBe(false)
  // Relationships stay real: the Task aligned to that Objective is still related.
  expect(byKind.get('task|T-0001')?.related).toBe(true)
})

test('GN2 healthy control: ordinary distinct Task and Objective ids keep their exact selection', () => {
  const model = makeGraphModel(
    [{ id: 'T-0001', title: 'Ordinary task', status: 'open', priority: 'P2', objective_ids: ['O-1'] }],
    [{ id: 'O-1', objective: 'Ordinary objective', quarter: '2026-Q3' }],
    [],
    [],
    'T-0001',
  )
  const byKind = new Map(model.nodes.map((node) => [`${node.data.kind}|${node.data.id}`, node.data]))

  expect(byKind.get('task|T-0001')?.selected).toBe(true)
  expect(byKind.get('objective|O-1')?.selected).toBe(false)
  expect(byKind.get('objective|O-1')?.related).toBe(true)
})

test('GN3 the initial key-result geometry is the SAME policy the layout engine is given', () => {
  const objectives = [{ id: 'O-1', objective: 'Objective', revision: 1, key_results: [{ id: 'K1', text: 'Outcome' }] }]
  const tasks = [{ id: 'T-0001', title: 'Linked', status: 'open', priority: 'P2', objective_ids: ['O-1'], key_result_refs: [{ objective_id: 'O-1', key_result_id: 'K1' }] }]
  const projection = projectKeyResults({ workspaceId: 'W1', tasks: tasks as never, objectives: objectives as never })
  const { nodes } = makeGraphModel(tasks as never, objectives as never, [], [], null, null, projection)

  const kr = nodes.find((node) => node.data.kind === 'key-result')
  expect(kr).toBeDefined()
  expect([kr!.initialWidth, kr!.initialHeight]).toEqual([
    GRAPH_NODE_SIZES['key-result'].width,
    GRAPH_NODE_SIZES['key-result'].height,
  ])
  // Other kinds keep their own existing initial geometry.
  const objective = nodes.find((node) => node.data.kind === 'objective')
  expect([objective!.initialWidth, objective!.initialHeight]).toEqual([221, 78])
})
