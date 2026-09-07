import { describe, expect, it } from "vitest"

import { GRAPH_ZOOM_MIN } from "./graphViewState"
import {
  GRAPH_NEIGHBORHOOD_MAX_ZOOM,
  GRAPH_NEIGHBORHOOD_MIN_ZOOM,
  GRAPH_NEIGHBORHOOD_PADDING,
  neighborhoodFitsPane,
  neighborhoodLayoutReady,
  neighborhoodWorldBounds,
  objectiveNeighborhoodFitView,
  objectiveNeighborhoodIds,
  originObjectiveNode,
  viewportForBounds,
  type GraphCameraEdge,
  type GraphCameraNode,
} from "./graphCamera"

function node(
  kind: "objective" | "task" | "note" | "key-result",
  id: string,
  x: number,
  y: number,
): GraphCameraNode {
  return {
    id: `flow|${kind}|${id}`,
    position: { x, y },
    data: { id, kind },
  }
}

const origin = node("objective", "O-1", 0, 0)
const aligned = node("task", "T-1", 400, 20)
const stranger = node("task", "T-2", 4000, 3000)
const otherObjective = node("objective", "O-2", 2000, 0)
const note = node("note", "N-1", 80, 240)
const twoHop = node("task", "T-3", 800, 20)

const edges: GraphCameraEdge[] = [
  { source: aligned.id, target: origin.id },
  { source: note.id, target: origin.id },
  { source: aligned.id, target: twoHop.id },
  { source: stranger.id, target: otherObjective.id },
]

const nodes = [origin, aligned, stranger, otherObjective, note, twoHop]

describe("objective neighborhood membership", () => {
  it("includes the Objective and one-hop visible nodes only", () => {
    const ids = objectiveNeighborhoodIds(origin.id, edges)
    expect(ids).toEqual(new Set([origin.id, aligned.id, note.id]))
    expect(ids.has(twoHop.id)).toBe(false)
    expect(ids.has(stranger.id)).toBe(false)
    expect(ids.has(otherObjective.id)).toBe(false)
  })

  it("finds the Objective by kind plus canonical id, not a Task that shares the raw id", () => {
    const colliding = node("task", "O-1", 9, 9)
    colliding.id = "flow|task|O-1"
    expect(originObjectiveNode([colliding, origin], "O-1")).toBe(origin)
  })
})

describe("objectiveNeighborhoodFitView", () => {
  it("returns a stable fitView nodes option and does not mutate positions", () => {
    const snapshot = structuredClone(nodes)
    const first = objectiveNeighborhoodFitView("O-1", nodes, edges)
    const second = objectiveNeighborhoodFitView("O-1", nodes, edges)
    expect(first).toEqual({
      nodes: [{ id: note.id }, { id: origin.id }, { id: aligned.id }],
      padding: GRAPH_NEIGHBORHOOD_PADDING,
      minZoom: GRAPH_NEIGHBORHOOD_MIN_ZOOM,
      maxZoom: GRAPH_NEIGHBORHOOD_MAX_ZOOM,
      duration: 0,
    })
    expect(second).toEqual(first)
    expect(nodes).toEqual(snapshot)
  })

  it("ignores two-hop and disconnected nodes, including a manually placed neighbor id", () => {
    const dragged = { ...aligned, position: { x: 120, y: 480 } }
    const focused = objectiveNeighborhoodFitView("O-1", nodes, edges)
    const withManual = objectiveNeighborhoodFitView(
      "O-1",
      [origin, dragged, stranger, otherObjective, note, twoHop],
      edges,
    )
    expect(withManual).toEqual(focused)
    expect(dragged.position).toEqual({ x: 120, y: 480 })
    expect(objectiveNeighborhoodFitView("O-2", nodes, edges)?.nodes.map((item) => item.id))
      .toEqual([otherObjective.id, stranger.id].sort())
  })

  it("returns null when the Objective or selection is missing", () => {
    expect(objectiveNeighborhoodFitView(null, nodes, edges)).toBeNull()
    expect(objectiveNeighborhoodFitView("O-9", nodes, edges)).toBeNull()
  })

  it("projects a 40-neighbor world box into the pane instead of clipping at 0.25", () => {
    const crowd = Array.from({ length: 40 }, (_, index) => (
      node("task", `T-${String(index + 1).padStart(2, "0")}`, index * 480, index * 360)
    ))
    const crowdEdges: GraphCameraEdge[] = crowd.map((item) => ({
      source: item.id,
      target: origin.id,
    }))
    const world = [origin, ...crowd]
    const bounds = neighborhoodWorldBounds("O-1", world, crowdEdges)
    expect(bounds).not.toBeNull()
    const pane = { width: 800, height: 600 }
    const clipped = viewportForBounds(bounds!, pane, GRAPH_NEIGHBORHOOD_PADDING, 0.25, GRAPH_NEIGHBORHOOD_MAX_ZOOM)
    expect(neighborhoodFitsPane(bounds!, clipped, pane)).toBe(false)
    const viewport = viewportForBounds(bounds!, pane)
    expect(viewport.zoom).toBeLessThan(0.25)
    expect(viewport.zoom).toBeGreaterThanOrEqual(GRAPH_ZOOM_MIN)
    expect(neighborhoodFitsPane(bounds!, viewport, pane)).toBe(true)
    const fit = objectiveNeighborhoodFitView("O-1", world, crowdEdges)
    expect(fit?.minZoom).toBe(GRAPH_NEIGHBORHOOD_MIN_ZOOM)
    expect(fit?.minZoom).toBeLessThanOrEqual(viewport.zoom)
    expect(fit?.maxZoom).toBe(GRAPH_NEIGHBORHOOD_MAX_ZOOM)
    expect(fit?.duration).toBe(0)
  })
})

describe("neighborhoodLayoutReady", () => {
  it("is true only when the settled layout matches the requested topology", () => {
    expect(neighborhoodLayoutReady(null, "next")).toBe(false)
    expect(neighborhoodLayoutReady("prev", "next")).toBe(false)
    expect(neighborhoodLayoutReady("next", "next")).toBe(true)
  })
})
