import {
  applyVisibleReorder,
  exactPlainObject,
  isPayloadId,
  uniqueStringArray,
} from "./localViewState"

export const TREEMAP_SCOPE_CAP = 512
export const TREEMAP_IDS_PER_SCOPE_CAP = 2_048
export const TREEMAP_TOTAL_IDS_CAP = 10_000
export const TREEMAP_DRAG_THRESHOLD_PX = 6
export const TREEMAP_MOVE_HANDLE_CLASS = "wsv-treemap-move-handle"

export type TreemapMode = "legacy" | "outcome"

export type TreemapOrderRecord = {
  scope: string
  ids: string[]
  touchedAt: number
}

export type TreemapLocalViewData = {
  orders: TreemapOrderRecord[]
}

const DATA_KEYS = ["orders"] as const
const ORDER_KEYS = ["ids", "scope", "touchedAt"] as const

export const EMPTY_TREEMAP_LOCAL_VIEW: TreemapLocalViewData = {
  orders: [],
}

export const LEGACY_ROOT_SCOPE = "legacy/root"
export const OUTCOME_ROOT_SCOPE = "outcome/root"
export const LEGACY_OPERATIONS_SCOPE = "legacy/operations"
export const LEGACY_MULTIPLE_SCOPE = "legacy/multiple"
const LEGACY_OBJECTIVE_PREFIX = "legacy/objective:"
const OUTCOME_GROUP_PREFIX = "outcome/group:"

function isTouchedAt(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

export function rootScope(mode: TreemapMode): string {
  return mode === "legacy" ? LEGACY_ROOT_SCOPE : OUTCOME_ROOT_SCOPE
}

export function scopeMode(scope: string): TreemapMode | null {
  if (scope.startsWith("legacy/")) return "legacy"
  if (scope.startsWith("outcome/")) return "outcome"
  return null
}

export function legacyObjectiveScope(objectiveId: string): string {
  return `${LEGACY_OBJECTIVE_PREFIX}${objectiveId}`
}

export function legacyLeafScope(group: {
  objectiveId?: string | null
  syntheticBucket?: "operations" | "multiple"
  name?: string
}): string {
  if (group.syntheticBucket === "operations" || group.name === "Unaligned / Operations") {
    return LEGACY_OPERATIONS_SCOPE
  }
  if (group.syntheticBucket === "multiple" || group.name === "Multiple objectives") {
    return LEGACY_MULTIPLE_SCOPE
  }
  if (group.objectiveId == null) return LEGACY_OPERATIONS_SCOPE
  return legacyObjectiveScope(group.objectiveId)
}

export function outcomeLeafScope(groupKey: string): string {
  return `${OUTCOME_GROUP_PREFIX}${groupKey}`
}

export function isTreemapScope(value: unknown): value is string {
  if (!isPayloadId(value)) return false
  if (value === LEGACY_ROOT_SCOPE || value === OUTCOME_ROOT_SCOPE) return true
  if (value === LEGACY_OPERATIONS_SCOPE || value === LEGACY_MULTIPLE_SCOPE) return true
  if (value.startsWith(LEGACY_OBJECTIVE_PREFIX) && value.length > LEGACY_OBJECTIVE_PREFIX.length) {
    return true
  }
  return value.startsWith(OUTCOME_GROUP_PREFIX) && value.length > OUTCOME_GROUP_PREFIX.length
}

function parseOrderRecord(value: unknown): TreemapOrderRecord | null {
  const record = exactPlainObject(value, ORDER_KEYS)
  if (!record) return null
  if (!isTreemapScope(record.scope)) return null
  const ids = uniqueStringArray(record.ids)
  if (!ids || ids.length > TREEMAP_IDS_PER_SCOPE_CAP) return null
  if (!isTouchedAt(record.touchedAt)) return null
  return { scope: record.scope, ids, touchedAt: record.touchedAt }
}

function totalIds(orders: readonly TreemapOrderRecord[]): number {
  return orders.reduce((sum, item) => sum + item.ids.length, 0)
}

export function parseTreemapLocalViewData(value: unknown): TreemapLocalViewData | null {
  const record = exactPlainObject(value, DATA_KEYS)
  if (!record || !Array.isArray(record.orders)) return null
  if (record.orders.length > TREEMAP_SCOPE_CAP) return null
  const orders: TreemapOrderRecord[] = []
  const seen = new Set<string>()
  for (const item of record.orders) {
    const parsed = parseOrderRecord(item)
    if (!parsed || seen.has(parsed.scope)) return null
    seen.add(parsed.scope)
    orders.push(parsed)
  }
  if (totalIds(orders) > TREEMAP_TOTAL_IDS_CAP) return null
  return { orders }
}

export function idsForScope(data: TreemapLocalViewData, scope: string): string[] {
  return data.orders.find((item) => item.scope === scope)?.ids ?? []
}

function compareTouched(left: TreemapOrderRecord, right: TreemapOrderRecord): number {
  if (left.touchedAt !== right.touchedAt) return left.touchedAt - right.touchedAt
  return left.scope.localeCompare(right.scope)
}

export function boundTreemapOrders(orders: readonly TreemapOrderRecord[]): TreemapOrderRecord[] {
  const capped = orders.map((item) => (
    item.ids.length <= TREEMAP_IDS_PER_SCOPE_CAP
      ? item
      : { ...item, ids: item.ids.slice(0, TREEMAP_IDS_PER_SCOPE_CAP) }
  ))
  const next = [...capped]
  while (next.length > TREEMAP_SCOPE_CAP || totalIds(next) > TREEMAP_TOTAL_IDS_CAP) {
    if (!next.length) break
    next.sort(compareTouched)
    next.shift()
  }
  return next
}

export function pruneTreemapLocalViewData(
  data: TreemapLocalViewData,
  catalog: ReadonlyMap<string, readonly string[]>,
  activeMode: TreemapMode,
): TreemapLocalViewData {
  const pruned: TreemapOrderRecord[] = []
  for (const item of data.orders) {
    const mode = scopeMode(item.scope)
    if (mode !== activeMode) {
      pruned.push(item)
      continue
    }
    const allowed = catalog.get(item.scope)
    if (!allowed) continue
    const known = new Set(allowed)
    const ids = item.ids.filter((id) => known.has(id))
    if (!ids.length) continue
    pruned.push({ ...item, ids })
  }
  return { orders: boundTreemapOrders(pruned) }
}

export function upsertTreemapScope(
  data: TreemapLocalViewData,
  scope: string,
  ids: readonly string[],
  catalog: ReadonlyMap<string, readonly string[]>,
  activeMode: TreemapMode,
  touchedAt = Date.now(),
): TreemapLocalViewData | null {
  if (!isTreemapScope(scope) || scopeMode(scope) !== activeMode) return null
  const unique = uniqueStringArray([...ids])
  if (!unique) return null
  const allowed = catalog.get(scope)
  const kept = allowed ? unique.filter((id) => allowed.includes(id)) : unique
  const stamp = Number.isFinite(touchedAt) && touchedAt >= 0 ? touchedAt : 0
  const orders = data.orders.filter((item) => item.scope !== scope)
  if (kept.length) orders.push({ scope, ids: kept, touchedAt: stamp })
  const pruneCatalog = new Map(catalog)
  if (!pruneCatalog.has(scope)) pruneCatalog.set(scope, kept)
  return pruneTreemapLocalViewData({ orders }, pruneCatalog, activeMode)
}

export function commitVisibleTreemapReorder(
  data: TreemapLocalViewData,
  scope: string,
  catalogIds: readonly string[],
  visibleIds: readonly string[],
  nextVisibleIds: readonly string[],
  catalog: ReadonlyMap<string, readonly string[]>,
  activeMode: TreemapMode,
  touchedAt = Date.now(),
): TreemapLocalViewData | null {
  const stored = idsForScope(data, scope)
  const nextIds = applyVisibleReorder(stored, catalogIds, visibleIds, nextVisibleIds)
  return upsertTreemapScope(data, scope, nextIds, catalog, activeMode, touchedAt)
}

export function treemapMoveHandleLabel(kind: "objective" | "task", id: string): string {
  return `Move ${kind} ${id}`
}
