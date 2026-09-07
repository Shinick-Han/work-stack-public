import { normalizeStoredOrder } from "./localViewState"
import {
  idsForScope,
  legacyLeafScope,
  outcomeLeafScope,
  rootScope,
  type TreemapLocalViewData,
  type TreemapMode,
} from "./treemapViewState"

export type TreemapOrderNode = {
  name?: string
  nodeKind?: string
  groupKey?: string
  objectiveId?: string | null
  taskId?: string
  objectiveIds?: readonly string[]
  children?: TreemapOrderNode[]
  moveScope?: string
  syntheticBucket?: "operations" | "multiple"
}

const OPERATIONS_NAME = "Unaligned / Operations"
const MULTIPLE_NAME = "Multiple objectives"

export function isNavigatorGroup(group: TreemapOrderNode, mode: TreemapMode): boolean {
  if (mode === "outcome") {
    return group.nodeKind === "objective" && typeof group.objectiveId === "string"
  }
  if (group.syntheticBucket || group.name === OPERATIONS_NAME || group.name === MULTIPLE_NAME) {
    return false
  }
  return typeof group.objectiveId === "string"
}

export function isSyntheticRootGroup(group: TreemapOrderNode, mode: TreemapMode): boolean {
  return !isNavigatorGroup(group, mode)
}

export function isOutcomeHierarchy(group: TreemapOrderNode): boolean {
  if (group.nodeKind === "objective") return true
  const first = group.children?.[0]
  return first?.nodeKind === "key-result" || first?.nodeKind === "bucket"
}

function navigatorId(group: TreemapOrderNode): string | null {
  return typeof group.objectiveId === "string" ? group.objectiveId : null
}

function leafScopeForRoot(group: TreemapOrderNode): string {
  return legacyLeafScope(group)
}

function orderByStored<T>(items: readonly T[], stored: readonly string[], idOf: (item: T) => string | null): T[] {
  const ids = items.map(idOf).filter((id): id is string => Boolean(id))
  const ordered = normalizeStoredOrder(stored, ids)
  const byId = new Map<string, T>()
  for (const item of items) {
    const id = idOf(item)
    if (id && !byId.has(id)) byId.set(id, item)
  }
  return ordered.map((id) => byId.get(id)).filter((item): item is T => Boolean(item))
}

export function orderRootGroups<T extends TreemapOrderNode>(
  groups: readonly T[],
  stored: readonly string[],
  mode: TreemapMode,
): T[] {
  const real: T[] = []
  const synthetic: T[] = []
  for (const group of groups) {
    if (isNavigatorGroup(group, mode)) real.push(group)
    else synthetic.push(group)
  }
  return [...orderByStored(real, stored, navigatorId), ...synthetic]
}

function stampLeaf(leaf: TreemapOrderNode, scope: string): TreemapOrderNode {
  return { ...leaf, moveScope: scope }
}

function orderLegacyLeaves(group: TreemapOrderNode, data: TreemapLocalViewData): TreemapOrderNode {
  const scope = legacyLeafScope(group)
  const children = (group.children ?? []).map((leaf) => stampLeaf(leaf, scope))
  return { ...group, children: orderByStored(children, idsForScope(data, scope), (item) => item.taskId ?? null) }
}

function orderOutcomeTerminal(terminal: TreemapOrderNode, data: TreemapLocalViewData): TreemapOrderNode {
  const scope = outcomeLeafScope(terminal.groupKey ?? "")
  const children = (terminal.children ?? []).map((leaf) => stampLeaf(leaf, scope))
  return {
    ...terminal,
    children: orderByStored(children, idsForScope(data, scope), (item) => item.taskId ?? null),
  }
}

function orderOutcomeRoot(group: TreemapOrderNode, data: TreemapLocalViewData): TreemapOrderNode {
  return { ...group, children: (group.children ?? []).map((child) => orderOutcomeTerminal(child, data)) }
}

function isRealNamedLeaf(leaf: TreemapOrderNode, objectiveId: string): boolean {
  const ids = leaf.objectiveIds
  return Array.isArray(ids) && ids.length === 1 && ids[0] === objectiveId
}

function peelReservedGroup(
  group: TreemapOrderNode,
  objectiveId: "none" | "multiple",
  synthetic: "operations" | "multiple",
  syntheticName: string,
): TreemapOrderNode[] {
  const children = group.children ?? []
  const real = children.filter((leaf) => isRealNamedLeaf(leaf, objectiveId))
  const rest = children.filter((leaf) => !isRealNamedLeaf(leaf, objectiveId))
  const split: TreemapOrderNode[] = []
  if (real.length) {
    split.push({
      ...group,
      name: objectiveId,
      nodeKind: undefined,
      objectiveId,
      syntheticBucket: undefined,
      children: real,
    })
  }
  if (rest.length) {
    split.push({
      ...group,
      name: syntheticName,
      nodeKind: undefined,
      objectiveId: synthetic === "operations" ? "none" : "multiple",
      syntheticBucket: synthetic,
      children: rest,
    })
  }
  return split
}

export function splitLegacyReservedGroups(
  groups: readonly TreemapOrderNode[],
): TreemapOrderNode[] {
  const next: TreemapOrderNode[] = []
  for (const group of groups) {
    if (isOutcomeHierarchy(group)) {
      next.push(group)
      continue
    }
    if (group.objectiveId === "none" || group.name === OPERATIONS_NAME) {
      const peeled = peelReservedGroup(group, "none", "operations", OPERATIONS_NAME)
      if (peeled.length) {
        next.push(...peeled)
        continue
      }
    }
    if (group.objectiveId === "multiple" || group.name === MULTIPLE_NAME) {
      const peeled = peelReservedGroup(group, "multiple", "multiple", MULTIPLE_NAME)
      if (peeled.length) {
        next.push(...peeled)
        continue
      }
    }
    next.push(group)
  }
  return next
}

export function applyStoredTreemapOrders<T extends TreemapOrderNode>(
  groups: readonly T[],
  data: TreemapLocalViewData,
  mode: TreemapMode,
): T[] {
  const source = mode === "legacy" ? splitLegacyReservedGroups(groups) : [...groups]
  const rooted = orderRootGroups(source, idsForScope(data, rootScope(mode)), mode)
  return rooted.map((group) => (
    mode === "outcome" || isOutcomeHierarchy(group)
      ? orderOutcomeRoot(group, data)
      : orderLegacyLeaves(group, data)
  )) as T[]
}

export function collectTreemapCatalog(
  groups: readonly TreemapOrderNode[],
  mode: TreemapMode,
): Map<string, string[]> {
  const source = mode === "legacy" ? splitLegacyReservedGroups(groups) : groups
  const catalog = new Map<string, string[]>()
  const navIds: string[] = []
  for (const group of source) {
    const id = navigatorId(group)
    if (isNavigatorGroup(group, mode) && id) navIds.push(id)
    if (mode === "outcome" || isOutcomeHierarchy(group)) {
      for (const terminal of group.children ?? []) {
        const key = terminal.groupKey
        if (!key) continue
        catalog.set(
          outcomeLeafScope(key),
          (terminal.children ?? []).map((leaf) => leaf.taskId).filter((taskId): taskId is string => Boolean(taskId)),
        )
      }
    } else {
      catalog.set(
        leafScopeForRoot(group),
        (group.children ?? []).map((leaf) => leaf.taskId).filter((taskId): taskId is string => Boolean(taskId)),
      )
    }
  }
  catalog.set(rootScope(mode), navIds)
  return catalog
}

export function previewMove(
  visibleIds: readonly string[],
  fromIndex: number,
  toIndex: number,
): string[] {
  if (
    fromIndex === toIndex
    || fromIndex < 0
    || toIndex < 0
    || fromIndex >= visibleIds.length
    || toIndex >= visibleIds.length
  ) {
    return [...visibleIds]
  }
  const next = [...visibleIds]
  const [item] = next.splice(fromIndex, 1)
  if (!item) return [...visibleIds]
  next.splice(toIndex, 0, item)
  return next
}

export function applyPreviewToGroups<T extends TreemapOrderNode>(
  groups: readonly T[],
  scope: string,
  previewIds: readonly string[],
  mode: TreemapMode,
): T[] {
  const source = mode === "legacy" ? splitLegacyReservedGroups(groups) : [...groups]
  if (scope === rootScope(mode)) return orderRootGroups(source, previewIds, mode) as T[]
  return source.map((group) => {
    if (mode === "outcome" || isOutcomeHierarchy(group)) {
      return {
        ...group,
        children: (group.children ?? []).map((terminal) => {
          if (outcomeLeafScope(terminal.groupKey ?? "") !== scope) return terminal
          return {
            ...terminal,
            children: orderByStored(terminal.children ?? [], previewIds, (item) => item.taskId ?? null),
          }
        }),
      }
    }
    if (legacyLeafScope(group) !== scope) return group
    return {
      ...group,
      children: orderByStored(group.children ?? [], previewIds, (item) => item.taskId ?? null),
    }
  }) as T[]
}

export function visibleIdsForScope(
  groups: readonly TreemapOrderNode[],
  scope: string,
  mode: TreemapMode,
): string[] {
  const source = mode === "legacy" ? splitLegacyReservedGroups(groups) : groups
  if (scope === rootScope(mode)) {
    return source.filter((group) => isNavigatorGroup(group, mode)).map((group) => group.objectiveId as string)
  }
  for (const group of source) {
    if (mode === "outcome" || isOutcomeHierarchy(group)) {
      for (const terminal of group.children ?? []) {
        if (outcomeLeafScope(terminal.groupKey ?? "") !== scope) continue
        return (terminal.children ?? []).map((leaf) => leaf.taskId).filter((id): id is string => Boolean(id))
      }
    } else if (legacyLeafScope(group) === scope) {
      return (group.children ?? []).map((leaf) => leaf.taskId).filter((id): id is string => Boolean(id))
    }
  }
  return []
}

export function formatTreemapPosition(index: number, total: number): string {
  return `${index + 1} of ${total}`
}

export function keyboardTargetIndex(
  current: number,
  total: number,
  key: string,
): number | null {
  if (key === "Home") return 0
  if (key === "End") return Math.max(0, total - 1)
  if (key === "ArrowLeft" || key === "ArrowUp") return Math.max(0, current - 1)
  if (key === "ArrowRight" || key === "ArrowDown") return Math.min(total - 1, current + 1)
  return null
}
