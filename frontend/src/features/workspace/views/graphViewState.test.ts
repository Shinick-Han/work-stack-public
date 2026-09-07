import { afterEach, describe, expect, it } from "vitest"

import { createLocalViewStore, localViewStorageKey } from "./localViewState"
import {
  applySavedPositions,
  arrowMoveDelta,
  clampGraphCoord,
  EMPTY_GRAPH_LOCAL_VIEW,
  GRAPH_COORD_MAX,
  GRAPH_COORD_MIN,
  GRAPH_KEYBOARD_SHIFT_STEP,
  GRAPH_KEYBOARD_STEP,
  GRAPH_POSITION_CAP,
  GRAPH_ZOOM_MAX,
  GRAPH_ZOOM_MIN,
  parseGraphLocalViewData,
  pruneGraphLocalViewData,
  shouldDropElkRoute,
  upsertGraphPosition,
} from "./graphViewState"

const WORKSPACE = "ws-graph-1"
const KEY = localViewStorageKey(WORKSPACE, "graph")

afterEach(() => {
  window.localStorage.clear()
})

describe("parseGraphLocalViewData", () => {
  it("accepts a bounded payload and rejects unknown keys, duplicates, and out-of-range values", () => {
    expect(parseGraphLocalViewData({
      positions: [{ id: "flow|task|T-1", x: 10, y: -4, touchedAt: 1 }],
      viewport: { x: 0, y: 0, zoom: 1 },
    })).toEqual({
      positions: [{ id: "flow|task|T-1", x: 10, y: -4, touchedAt: 1 }],
      viewport: { x: 0, y: 0, zoom: 1 },
    })
    expect(parseGraphLocalViewData({
      positions: [],
      viewport: null,
      extra: true,
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: [
        { id: "a", x: 1, y: 1, touchedAt: 1 },
        { id: "a", x: 2, y: 2, touchedAt: 2 },
      ],
      viewport: null,
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: [{ id: "a", x: GRAPH_COORD_MAX + 1, y: 0, touchedAt: 1 }],
      viewport: null,
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: [{ id: "a", x: 0, y: 0, touchedAt: Number.POSITIVE_INFINITY }],
      viewport: null,
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: [],
      viewport: { x: 0, y: 0, zoom: GRAPH_ZOOM_MIN - 0.01 },
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: [],
      viewport: { x: 0, y: 0, zoom: GRAPH_ZOOM_MAX + 0.01 },
    })).toBeNull()
    expect(parseGraphLocalViewData({
      positions: Array.from({ length: GRAPH_POSITION_CAP + 1 }, (_, index) => ({
        id: `n-${index}`,
        x: 0,
        y: 0,
        touchedAt: index,
      })),
      viewport: null,
    })).toBeNull()
  })
})

describe("write clamp, catalog prune, and ELK fallback", () => {
  it("clamps writes, keeps filtered catalog IDs, and evicts least-recent extras", () => {
    expect(clampGraphCoord(GRAPH_COORD_MAX + 50)).toBe(GRAPH_COORD_MAX)
    expect(clampGraphCoord(GRAPH_COORD_MIN - 50)).toBe(GRAPH_COORD_MIN)

    const catalog = new Set(["keep-old", "keep-new", "filtered"])
    const data = {
      positions: [
        { id: "deleted", x: 1, y: 1, touchedAt: 9 },
        { id: "keep-old", x: 2, y: 2, touchedAt: 1 },
        { id: "filtered", x: 3, y: 3, touchedAt: 5 },
      ],
      viewport: { x: 1, y: 2, zoom: 1 },
    }
    expect(pruneGraphLocalViewData(data, catalog).positions.map((item) => item.id))
      .toEqual(["keep-old", "filtered"])

    const many = {
      positions: Array.from({ length: GRAPH_POSITION_CAP }, (_, index) => ({
        id: `n-${index}`,
        x: 0,
        y: 0,
        touchedAt: index,
      })),
      viewport: null,
    }
    const catalogAll = new Set([...many.positions.map((item) => item.id), "fresh"])
    const updated = upsertGraphPosition(many, "fresh", 9, 8, catalogAll, 10_000)
    expect(updated?.positions).toHaveLength(GRAPH_POSITION_CAP)
    expect(updated?.positions.some((item) => item.id === "fresh")).toBe(true)
    expect(updated?.positions.some((item) => item.id === "n-0")).toBe(false)
  })

  it("overlays saved coordinates and drops incident ELK routes only", () => {
    const nodes = [
      { id: "flow|task|T-1", position: { x: 0, y: 0 } },
      { id: "flow|task|T-2", position: { x: 40, y: 40 } },
    ]
    const overlaid = applySavedPositions(nodes, [
      { id: "flow|task|T-1", x: 100, y: 200, touchedAt: 1 },
    ])
    expect(overlaid[0].position).toEqual({ x: 100, y: 200 })
    expect(overlaid[1].position).toEqual({ x: 40, y: 40 })
    const manual = new Set(["flow|task|T-1"])
    expect(shouldDropElkRoute("flow|task|T-1", "flow|objective|O-1", manual)).toBe(true)
    expect(shouldDropElkRoute("flow|task|T-2", "flow|objective|O-1", manual)).toBe(false)
  })
})

describe("graph store integration", () => {
  it("round-trips through the shared adapter and isolates the graph key", () => {
    const adapter = createLocalViewStore({
      workspaceId: WORKSPACE,
      view: "graph",
      defaultData: EMPTY_GRAPH_LOCAL_VIEW,
      parseData: parseGraphLocalViewData,
    })
    const written = adapter.update(() => ({
      positions: [{ id: "flow|task|T-1", x: 12, y: 24, touchedAt: 3 }],
      viewport: { x: 5, y: 6, zoom: 0.5 },
    }))
    expect(adapter.read()).toEqual(written)
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).view).toBe("graph")
    adapter.reset()
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(window.localStorage.getItem(localViewStorageKey(WORKSPACE, "table"))).toBeNull()
  })
})

describe("keyboard arrow modifier matrix", () => {
  it("accepts unmodified and Shift arrows and ignores Ctrl/Alt/Meta", () => {
    expect(arrowMoveDelta({ key: "ArrowRight" })).toEqual({ x: GRAPH_KEYBOARD_STEP, y: 0 })
    expect(arrowMoveDelta({ key: "ArrowLeft" })).toEqual({ x: -GRAPH_KEYBOARD_STEP, y: 0 })
    expect(arrowMoveDelta({ key: "ArrowUp" })).toEqual({ x: 0, y: -GRAPH_KEYBOARD_STEP })
    expect(arrowMoveDelta({ key: "ArrowDown" })).toEqual({ x: 0, y: GRAPH_KEYBOARD_STEP })
    expect(arrowMoveDelta({ key: "ArrowRight", shiftKey: true })).toEqual({ x: GRAPH_KEYBOARD_SHIFT_STEP, y: 0 })
    expect(arrowMoveDelta({ key: "ArrowDown", shiftKey: true })).toEqual({ x: 0, y: GRAPH_KEYBOARD_SHIFT_STEP })
    expect(arrowMoveDelta({ key: "ArrowRight", ctrlKey: true })).toBeNull()
    expect(arrowMoveDelta({ key: "ArrowRight", altKey: true })).toBeNull()
    expect(arrowMoveDelta({ key: "ArrowRight", metaKey: true })).toBeNull()
    expect(arrowMoveDelta({ key: "ArrowRight", ctrlKey: true, shiftKey: true })).toBeNull()
    expect(arrowMoveDelta({ key: "ArrowRight", altKey: true, shiftKey: true })).toBeNull()
    expect(arrowMoveDelta({ key: "Enter" })).toBeNull()
  })
})
