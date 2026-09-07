import type { Edge, Node } from '@xyflow/react'
import type { ELK, ElkNode } from 'elkjs/lib/elk.bundled.js'

let elkPromise: Promise<ELK> | null = null

async function getElk() {
  elkPromise ??= import('elkjs/lib/elk.bundled.js').then(({ default: ELK }) => new ELK())
  return elkPromise
}

/**
 * One geometry policy for every node kind. The key-result frame carries
 * Progress plus optional Target, Status and Tasks rows, so it is both wider and
 * taller than a Task box, and the CSS bounds those rows to this height.
 */
export const GRAPH_NODE_SIZES = {
  objective: { width: 240, height: 88 },
  note: { width: 220, height: 80 },
  'key-result': { width: 236, height: 132 },
  task: { width: 230, height: 96 },
} as const

function nodeSize(node: Node) {
  const kind = node.data?.kind
  if (kind === 'objective') return GRAPH_NODE_SIZES.objective
  if (kind === 'note') return GRAPH_NODE_SIZES.note
  if (kind === 'key-result') return GRAPH_NODE_SIZES['key-result']
  return GRAPH_NODE_SIZES.task
}

export type GraphRoutePoint = { x: number; y: number }

export type GraphEdgeRoute = {
  points: GraphRoutePoint[]
  label: GraphRoutePoint
}

export type PlanningGraphLayout<TNode extends Node> = {
  nodes: TNode[]
  edgeRoutes: Record<string, GraphEdgeRoute>
}

function routeLabel(points: readonly GraphRoutePoint[]): GraphRoutePoint {
  if (points.length < 2) return points[0] ?? { x: 0, y: 0 }
  const segments = points.slice(1).map((point, index) => ({
    from: points[index],
    to: point,
    length: Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y),
  }))
  const total = segments.reduce((sum, segment) => sum + segment.length, 0)
  let remaining = total / 2
  for (const segment of segments) {
    if (remaining <= segment.length) {
      const ratio = segment.length ? remaining / segment.length : 0
      return {
        x: segment.from.x + (segment.to.x - segment.from.x) * ratio,
        y: segment.from.y + (segment.to.y - segment.from.y) * ratio,
      }
    }
    remaining -= segment.length
  }
  return points[points.length - 1]
}

const LAYOUT_REVERSED_KINDS = new Set(['alignment', 'dependency', 'parent', 'reference'])

function edgeKind(edge: Edge): string | undefined {
  const data = edge.data as { kind?: string } | undefined
  return data?.kind
}

/**
 * Canonical dependent→anchor edges are reversed for ELK ranking only.
 * Outcome hierarchy edges already read Objective → KR → Task and must not be
 * flipped; rendered source/target/marker stay on the presentation edge.
 */
export function reversesEdgeForLayout(edge: Edge): boolean {
  const kind = edgeKind(edge)
  return kind === undefined || LAYOUT_REVERSED_KINDS.has(kind)
}

/**
 * Membership, geometry and ranking identity for a planning graph.
 * Selection, mute/highlight and edge paint are excluded, so a click that
 * does not change topology cannot be mistaken for a new layout problem.
 */
export function planningGraphTopologyKey(
  nodes: readonly Node[],
  edges: readonly Edge[],
): string {
  const nodeKeys = nodes
    .map((node) => {
      const size = nodeSize(node)
      const kind = node.data && typeof node.data === 'object' && 'kind' in node.data
        ? String(node.data.kind ?? '')
        : ''
      return `${node.id}\0${kind}\0${size.width}x${size.height}`
    })
    .sort()
  const edgeKeys = edges
    .map((edge) => (
      `${edge.id}\0${edge.source}\0${edge.target}\0${edgeKind(edge) ?? ''}\0${reversesEdgeForLayout(edge) ? '1' : '0'}`
    ))
    .sort()
  return `${nodeKeys.join('\n')}\n>\n${edgeKeys.join('\n')}`
}

export async function layoutPlanningGraph<TNode extends Node>(
  nodes: readonly TNode[],
  edges: readonly Edge[],
): Promise<PlanningGraphLayout<TNode>> {
  if (!nodes.length) return { nodes: [], edgeRoutes: {} }
  const elk = await getElk()
  const sortedNodes = [...nodes].sort((left, right) => left.id.localeCompare(right.id))
  const sortedEdges = [...edges].sort((left, right) => left.id.localeCompare(right.id))
  const reverseById = new Map(sortedEdges.map((edge) => [edge.id, reversesEdgeForLayout(edge)]))
  const graph: ElkNode = {
    id: 'workstack-planning-graph',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
      'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
      'elk.layered.spacing.nodeNodeBetweenLayers': '92',
      'elk.layered.spacing.edgeEdgeBetweenLayers': '18',
      'elk.spacing.edgeNode': '24',
      'elk.spacing.edgeEdge': '14',
      'elk.spacing.nodeNode': '34',
    },
    children: sortedNodes.map((node) => ({ id: node.id, ...nodeSize(node) })),
    edges: sortedEdges.map((edge) => {
      const reverse = reverseById.get(edge.id)
      return {
        id: edge.id,
        sources: [reverse ? edge.target : edge.source],
        targets: [reverse ? edge.source : edge.target],
      }
    }),
  }
  const result = await elk.layout(graph)
  const positions = new Map(
    (result.children ?? []).map((node) => [node.id, { x: node.x ?? 0, y: node.y ?? 0 }]),
  )
  const laidOutNodes = sortedNodes.map((node) => ({
    ...node,
    position: positions.get(node.id) ?? node.position,
  }))
  const edgeRoutes = Object.fromEntries((result.edges ?? []).flatMap((edge) => {
    const section = edge.sections?.[0]
    if (!section) return []
    const laid = [
      section.startPoint,
      ...(section.bendPoints ?? []),
      section.endPoint,
    ].map(({ x, y }) => ({ x, y }))
    const points = reverseById.get(edge.id) ? [...laid].reverse() : laid
    return [[edge.id, { points, label: routeLabel(points) }]]
  }))
  return { nodes: laidOutNodes, edgeRoutes }
}
