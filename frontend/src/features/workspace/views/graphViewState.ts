import {
  exactPlainObject,
  isPayloadId,
} from "./localViewState"

export const GRAPH_POSITION_CAP = 4_096
export const GRAPH_COORD_MIN = -1_000_000
export const GRAPH_COORD_MAX = 1_000_000
/** Canvas and fit floor: low enough to contain a 40-neighbor span in an 800x600 pane. */
export const GRAPH_ZOOM_MIN = 0.02
export const GRAPH_ZOOM_MAX = 1.7
export const GRAPH_KEYBOARD_STEP = 16
export const GRAPH_KEYBOARD_SHIFT_STEP = 64
export const GRAPH_DRAG_THRESHOLD_PX = 6
export const GRAPH_MOVE_HANDLE_CLASS = "wsv-graph-move-handle"
export const GRAPH_MOVE_HANDLE_SELECTOR = `.${GRAPH_MOVE_HANDLE_CLASS}`

export type GraphPositionRecord = {
  id: string
  x: number
  y: number
  touchedAt: number
}

export type GraphViewport = {
  x: number
  y: number
  zoom: number
}

export type GraphLocalViewData = {
  positions: GraphPositionRecord[]
  viewport: GraphViewport | null
}

const DATA_KEYS = ["positions", "viewport"] as const
const POSITION_KEYS = ["id", "touchedAt", "x", "y"] as const
const VIEWPORT_KEYS = ["x", "y", "zoom"] as const

export const EMPTY_GRAPH_LOCAL_VIEW: GraphLocalViewData = {
  positions: [],
  viewport: null,
}

export function clampGraphCoord(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(GRAPH_COORD_MAX, Math.max(GRAPH_COORD_MIN, value))
}

export function clampGraphZoom(value: number): number {
  if (!Number.isFinite(value)) return 1
  return Math.min(GRAPH_ZOOM_MAX, Math.max(GRAPH_ZOOM_MIN, value))
}

function isInCoordRange(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value)
    && value >= GRAPH_COORD_MIN && value <= GRAPH_COORD_MAX
}

function isTouchedAt(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

function parsePosition(value: unknown): GraphPositionRecord | null {
  const record = exactPlainObject(value, POSITION_KEYS)
  if (!record) return null
  if (!isPayloadId(record.id)) return null
  if (!isInCoordRange(record.x) || !isInCoordRange(record.y)) return null
  if (!isTouchedAt(record.touchedAt)) return null
  return {
    id: record.id,
    x: record.x,
    y: record.y,
    touchedAt: record.touchedAt,
  }
}

function parseViewport(value: unknown): GraphViewport | null | undefined {
  if (value === null) return null
  const record = exactPlainObject(value, VIEWPORT_KEYS)
  if (!record) return undefined
  if (!isInCoordRange(record.x) || !isInCoordRange(record.y)) return undefined
  if (typeof record.zoom !== "number" || !Number.isFinite(record.zoom)) return undefined
  if (record.zoom < GRAPH_ZOOM_MIN || record.zoom > GRAPH_ZOOM_MAX) return undefined
  return { x: record.x, y: record.y, zoom: record.zoom }
}

export function parseGraphLocalViewData(value: unknown): GraphLocalViewData | null {
  const record = exactPlainObject(value, DATA_KEYS)
  if (!record) return null
  if (!Array.isArray(record.positions) || record.positions.length > GRAPH_POSITION_CAP) return null
  const positions: GraphPositionRecord[] = []
  const seen = new Set<string>()
  for (const item of record.positions) {
    const parsed = parsePosition(item)
    if (!parsed || seen.has(parsed.id)) return null
    seen.add(parsed.id)
    positions.push(parsed)
  }
  const viewport = parseViewport(record.viewport)
  if (viewport === undefined) return null
  return { positions, viewport }
}

export function pruneGraphPositions(
  positions: readonly GraphPositionRecord[],
  catalogIds: ReadonlySet<string>,
): GraphPositionRecord[] {
  const kept = positions.filter((item) => catalogIds.has(item.id))
  if (kept.length <= GRAPH_POSITION_CAP) return kept
  return [...kept]
    .sort((left, right) => right.touchedAt - left.touchedAt || left.id.localeCompare(right.id))
    .slice(0, GRAPH_POSITION_CAP)
}

export function pruneGraphLocalViewData(
  data: GraphLocalViewData,
  catalogIds: ReadonlySet<string>,
): GraphLocalViewData {
  return {
    viewport: data.viewport,
    positions: pruneGraphPositions(data.positions, catalogIds),
  }
}

export function upsertGraphPosition(
  data: GraphLocalViewData,
  id: string,
  x: number,
  y: number,
  catalogIds: ReadonlySet<string>,
  touchedAt = Date.now(),
): GraphLocalViewData | null {
  if (!isPayloadId(id)) return null
  const next: GraphPositionRecord = {
    id,
    x: clampGraphCoord(x),
    y: clampGraphCoord(y),
    touchedAt: Number.isFinite(touchedAt) && touchedAt >= 0 ? touchedAt : 0,
  }
  const positions = data.positions.filter((item) => item.id !== id)
  positions.push(next)
  return pruneGraphLocalViewData({ ...data, positions }, catalogIds)
}

export function applySavedPositions<T extends { id: string; position: { x: number; y: number } }>(
  nodes: readonly T[],
  positions: readonly GraphPositionRecord[],
): T[] {
  const byId = new Map(positions.map((item) => [item.id, item]))
  return nodes.map((node) => {
    const saved = byId.get(node.id)
    return saved ? { ...node, position: { x: saved.x, y: saved.y } } : node
  })
}

export function manualPositionIds(
  positions: readonly GraphPositionRecord[],
  extraId?: string | null,
): Set<string> {
  const ids = new Set(positions.map((item) => item.id))
  if (extraId) ids.add(extraId)
  return ids
}

export function shouldDropElkRoute(
  source: string,
  target: string,
  manualIds: ReadonlySet<string>,
): boolean {
  return manualIds.has(source) || manualIds.has(target)
}

export function graphMoveHandleLabel(kind: string, id: string): string {
  return `Move ${kind} ${id}`
}

export function formatGraphCoordinates(x: number, y: number): string {
  return `${Math.round(x)}, ${Math.round(y)}`
}

type ArrowKeyEvent = {
  key: string
  shiftKey?: boolean
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
}

/** Unmodified arrows nudge 16px; Shift+Arrow nudges 64px. Ctrl/Alt/Meta never move. */
export function arrowMoveDelta(event: ArrowKeyEvent): { x: number; y: number } | null {
  if (event.altKey || event.ctrlKey || event.metaKey) return null
  const step = event.shiftKey ? GRAPH_KEYBOARD_SHIFT_STEP : GRAPH_KEYBOARD_STEP
  if (event.key === "ArrowLeft") return { x: -step, y: 0 }
  if (event.key === "ArrowRight") return { x: step, y: 0 }
  if (event.key === "ArrowUp") return { x: 0, y: -step }
  if (event.key === "ArrowDown") return { x: 0, y: step }
  return null
}
