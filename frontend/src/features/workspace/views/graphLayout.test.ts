import type { Edge, Node } from '@xyflow/react'
import { GRAPH_NODE_SIZES, planningGraphTopologyKey, reversesEdgeForLayout } from "./graphLayout";
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

  test('topology identity ignores selection paint, coordinates and input order', () => {
    const nodes: Node[] = [
      { id: 'task', position: { x: 1, y: 2 }, data: { kind: 'task', selected: true, related: false } },
      { id: 'objective', position: { x: 3, y: 4 }, data: { kind: 'objective', selected: false } },
    ]
    const edges: Edge[] = [
      { id: 'align', source: 'task', target: 'objective', data: { kind: 'alignment' }, style: { opacity: 0.12 } },
    ]
    const reversed: Node[] = [
      { id: 'objective', position: { x: 90, y: 90 }, data: { kind: 'objective', selected: true, related: true } },
      { id: 'task', position: { x: 0, y: 0 }, data: { kind: 'task', selected: false, related: true } },
    ]
    const restyled: Edge[] = [
      { id: 'align', source: 'task', target: 'objective', data: { kind: 'alignment' }, style: { opacity: 1, strokeWidth: 2.8 } },
    ]

    expect(planningGraphTopologyKey(reversed, restyled)).toBe(planningGraphTopologyKey(nodes, edges))
    expect(planningGraphTopologyKey(
      [...nodes, { id: 'note', position: { x: 0, y: 0 }, data: { kind: 'note' } }],
      edges,
    )).not.toBe(planningGraphTopologyKey(nodes, edges))
  })

  test('returns an empty deterministic layout for an empty graph', async () => {
    await expect(layoutPlanningGraph([], [])).resolves.toEqual({ nodes: [], edgeRoutes: {} })
  })

  test('overwrites incoming coordinates so saved positions must apply after ELK', async () => {
    const nodes = [node('T-0001', 'task')]
    nodes[0].position = { x: 99_999, y: 99_999 }
    const laid = await layoutPlanningGraph(nodes, [])
    expect(laid.nodes).toHaveLength(1)
    expect(laid.nodes[0].position).not.toEqual({ x: 99_999, y: 99_999 })
    expect(Number.isFinite(laid.nodes[0].position.x)).toBe(true)
  })

  test('ranks Objective then KR then Task without reversing rendered outcome edges', async () => {
    const nodes: Node[] = [
      { id: 'kr', position: { x: 0, y: 0 }, data: { kind: 'key-result' } },
      { id: 'task', position: { x: 0, y: 0 }, data: { kind: 'task' } },
      { id: 'objective', position: { x: 0, y: 0 }, data: { kind: 'objective' } },
    ]
    const edges: Edge[] = [
      { id: 'o-kr', source: 'objective', target: 'kr', data: { kind: 'derived.key-result-objective' } },
      { id: 'kr-t', source: 'kr', target: 'task', data: { kind: 'derived.task-key-result' } },
    ]
    expect(reversesEdgeForLayout(edges[0])).toBe(false)
    expect(reversesEdgeForLayout(edges[1])).toBe(false)
    expect(reversesEdgeForLayout({ id: 'a', source: 't', target: 'o', data: { kind: 'alignment' } })).toBe(true)

    const first = await layoutPlanningGraph(nodes, edges)
    const second = await layoutPlanningGraph([...nodes].reverse(), [...edges].reverse())
    const positions = new Map(first.nodes.map((item) => [item.id, item.position]))

    expect(second).toEqual(first)
    expect(positions.get('objective')!.x).toBeLessThan(positions.get('kr')!.x)
    expect(positions.get('kr')!.x).toBeLessThan(positions.get('task')!.x)
    expect(first.nodes.map((item) => item.id)).toEqual(['kr', 'objective', 'task'])
  })
})


describe("GR03 key-result geometry policy", () => {
  it("gives the key-result kind its own explicit size instead of the Task fallback", () => {
    expect(GRAPH_NODE_SIZES["key-result"]).toEqual({ width: 236, height: 132 });
    expect(GRAPH_NODE_SIZES["key-result"]).not.toEqual(GRAPH_NODE_SIZES.task);
    expect(GRAPH_NODE_SIZES.objective).toEqual({ width: 240, height: 88 });
    expect(GRAPH_NODE_SIZES.note).toEqual({ width: 220, height: 80 });
  });
});
