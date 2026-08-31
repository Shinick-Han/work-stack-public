import type { Edge, Node } from '@xyflow/react'
import { describe, expect, test } from 'vitest'

import { layoutPlanningGraph } from './graphLayout'

function node(id: string, kind: 'objective' | 'task' | 'note'): Node {
  return {
    id,
    position: { x: 0, y: 0 },
    data: { kind },
  }
}

describe('layoutPlanningGraph', () => {
  test('places planning anchors before dependent work without changing graph identity', async () => {
    const nodes = [node('N-0001', 'note'), node('T-0002', 'task'), node('O-1', 'objective'), node('T-0001', 'task')]
    const edges: Edge[] = [
      { id: 'alignment:T-0001:O-1', source: 'T-0001', target: 'O-1', data: { kind: 'alignment' } },
      { id: 'dependency:T-0002:T-0001', source: 'T-0002', target: 'T-0001', data: { kind: 'dependency' } },
      { id: 'reference:N-0001:T-0002', source: 'N-0001', target: 'T-0002', data: { kind: 'reference' } },
    ]

    const first = await layoutPlanningGraph(nodes, edges)
    const second = await layoutPlanningGraph([...nodes].reverse(), [...edges].reverse())
    const positions = new Map(first.nodes.map((item) => [item.id, item.position]))

    expect(first.nodes.map((item) => item.id)).toEqual(['N-0001', 'O-1', 'T-0001', 'T-0002'])
    expect(second).toEqual(first)
    expect(positions.get('O-1')!.x).toBeLessThan(positions.get('T-0001')!.x)
    expect(positions.get('T-0001')!.x).toBeLessThan(positions.get('T-0002')!.x)
    expect(positions.get('T-0002')!.x).toBeLessThan(positions.get('N-0001')!.x)
    expect(first.nodes.every((item) => Number.isFinite(item.position.x) && Number.isFinite(item.position.y))).toBe(true)
    expect(Object.keys(first.edgeRoutes).sort()).toEqual(edges.map((edge) => edge.id).sort())
    expect(Object.values(first.edgeRoutes).every((route) => route.points.length >= 2)).toBe(true)
  })

  test('returns an empty deterministic layout for an empty graph', async () => {
    await expect(layoutPlanningGraph([], [])).resolves.toEqual({ nodes: [], edgeRoutes: {} })
  })
})
