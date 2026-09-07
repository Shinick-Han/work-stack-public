import { applyVisibleReorder, normalizeStoredOrder } from './localViewState'
import type { WorkspaceTask } from './types'

export const TABLE_ORDER_CAP = 10_000
export const TABLE_DRAG_THRESHOLD_PX = 6

export type TableNamedSortField = 'id' | 'title' | 'status' | 'priority' | 'due'

function fieldValue(task: WorkspaceTask, sortField: TableNamedSortField): string {
  return String(task[sortField] ?? '')
}

export function meetsTableDragThreshold(
  from: { clientX: number; clientY: number },
  to: { clientX: number; clientY: number },
): boolean {
  const dx = to.clientX - from.clientX
  const dy = to.clientY - from.clientY
  return (dx * dx) + (dy * dy) >= TABLE_DRAG_THRESHOLD_PX * TABLE_DRAG_THRESHOLD_PX
}

export function pruneDeletedTableOrder(
  stored: readonly string[],
  canonicalIds: readonly string[],
): string[] {
  const canonical = new Set(canonicalIds)
  const seen = new Set<string>()
  const kept: string[] = []
  for (const id of stored) {
    if (!canonical.has(id) || seen.has(id)) continue
    seen.add(id)
    kept.push(id)
  }
  return kept.slice(0, TABLE_ORDER_CAP)
}

export function normalizeTableOrder(
  stored: readonly string[],
  canonicalIds: readonly string[],
): string[] {
  return normalizeStoredOrder(stored, canonicalIds).slice(0, TABLE_ORDER_CAP)
}

export function projectVisibleTableOrder(
  stored: readonly string[],
  canonicalIds: readonly string[],
  visibleIds: readonly string[],
): string[] {
  const visible = new Set(visibleIds)
  return normalizeTableOrder(stored, canonicalIds).filter((id) => visible.has(id))
}

export function sameIdSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const ids = new Set(left)
  return right.every((id) => ids.has(id))
}

export function reorderTableVisible(
  stored: readonly string[],
  canonicalIds: readonly string[],
  visibleIds: readonly string[],
  nextVisibleIds: readonly string[],
): string[] {
  if (!sameIdSet(visibleIds, nextVisibleIds)) {
    return normalizeTableOrder(stored, canonicalIds)
  }
  return applyVisibleReorder(stored, canonicalIds, visibleIds, nextVisibleIds).slice(0, TABLE_ORDER_CAP)
}

export function compareTableTasks(
  left: WorkspaceTask,
  right: WorkspaceTask,
  sortField: TableNamedSortField,
  descending: boolean,
): number {
  const result = fieldValue(left, sortField).localeCompare(
    fieldValue(right, sortField),
    undefined,
    { numeric: true },
  )
  const signed = descending ? -result : result
  if (signed !== 0) return signed
  return left.id.localeCompare(right.id, undefined, { numeric: true })
}

export function sortTasksByField(
  tasks: readonly WorkspaceTask[],
  sortField: TableNamedSortField,
  descending: boolean,
): WorkspaceTask[] {
  return [...tasks].sort((left, right) => compareTableTasks(left, right, sortField, descending))
}

export function orderTasksManually(
  tasks: readonly WorkspaceTask[],
  stored: readonly string[],
  canonicalIds: readonly string[],
): WorkspaceTask[] {
  const byId = new Map(tasks.map((task) => [task.id, task]))
  const ordered: WorkspaceTask[] = []
  const seen = new Set<string>()
  for (const id of projectVisibleTableOrder(stored, canonicalIds, tasks.map((task) => task.id))) {
    const task = byId.get(id)
    if (!task || seen.has(id)) continue
    seen.add(id)
    ordered.push(task)
  }
  for (const task of tasks) {
    if (seen.has(task.id)) continue
    ordered.push(task)
  }
  return ordered
}

export function moveVisibleId(
  visibleIds: readonly string[],
  id: string,
  action: 'previous' | 'next' | 'first' | 'last',
): string[] | null {
  const index = visibleIds.indexOf(id)
  if (index < 0) return null
  const next = [...visibleIds]
  next.splice(index, 1)
  const insertAt = action === 'previous' ? Math.max(0, index - 1)
    : action === 'next' ? Math.min(next.length, index + 1)
      : action === 'first' ? 0
        : next.length
  next.splice(insertAt, 0, id)
  return next
}

export function placeVisibleId(
  visibleIds: readonly string[],
  draggedId: string,
  targetId: string,
): string[] | null {
  const from = visibleIds.indexOf(draggedId)
  const to = visibleIds.indexOf(targetId)
  if (from < 0 || to < 0) return null
  const next = [...visibleIds]
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  return next
}

export function tableMoveHandleLabel(id: string, manual: boolean): string {
  return manual ? `Reorder ${id}` : `Choose Manual order to reorder ${id}`
}

export function tablePositionLabel(id: string, index: number, total: number): string {
  return `${id} at position ${index + 1} of ${total}`
}
