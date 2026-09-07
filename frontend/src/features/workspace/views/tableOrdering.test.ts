import { describe, expect, it } from 'vitest'
import type { WorkspaceTask } from './types'
import {
  TABLE_DRAG_THRESHOLD_PX,
  TABLE_ORDER_CAP,
  compareTableTasks,
  meetsTableDragThreshold,
  moveVisibleId,
  normalizeTableOrder,
  orderTasksManually,
  placeVisibleId,
  pruneDeletedTableOrder,
  projectVisibleTableOrder,
  reorderTableVisible,
  sortTasksByField,
  tableMoveHandleLabel,
  tablePositionLabel,
} from './tableOrdering'

function task(id: string, extra: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    id,
    title: `Task ${id}`,
    status: 'open',
    priority: 'P2',
    due: null,
    ...extra,
  }
}

function ids(count: number, prefix = 'T-'): string[] {
  return Array.from({ length: count }, (_, index) => `${prefix}${index}`)
}

describe('normalizeTableOrder and visible projection', () => {
  it('deduplicates, prunes only proven-deleted IDs, and appends unseen canonical IDs', () => {
    const stored = ['A', 'H', 'B', 'A', 'GONE']
    const canonical = ['A', 'B', 'C', 'H']
    expect(normalizeTableOrder(stored, canonical)).toEqual(['A', 'H', 'B', 'C'])
    expect(projectVisibleTableOrder(stored, canonical, ['A', 'B'])).toEqual(['A', 'B'])
  })

  it('uses the canonical reference order when the stored list is empty', () => {
    expect(normalizeTableOrder([], ['C', 'A', 'B'])).toEqual(['C', 'A', 'B'])
    expect(projectVisibleTableOrder([], ['C', 'A', 'B'], ['B', 'C'])).toEqual(['C', 'B'])
    expect(pruneDeletedTableOrder(['A', 'GONE', 'A', 'B'], ['B', 'A'])).toEqual(['A', 'B'])
    expect(pruneDeletedTableOrder([], ['A', 'B'])).toEqual([])
  })

  it('keeps hidden slots when replacing only the visible permutation', () => {
    expect(reorderTableVisible(['A', 'H', 'B', 'GONE'], ['A', 'B', 'C', 'H'], ['A', 'B'], ['B', 'A']))
      .toEqual(['B', 'H', 'A', 'C'])
  })

  it('ignores a non-permutation visible request and keeps the normalized stored order', () => {
    expect(reorderTableVisible(['A', 'B'], ['A', 'B', 'C'], ['A', 'B'], ['A']))
      .toEqual(['A', 'B', 'C'])
  })

  it('accepts 10,000 IDs and caps a larger canonical catalog', () => {
    const stored = ids(TABLE_ORDER_CAP)
    expect(normalizeTableOrder(stored, stored)).toHaveLength(TABLE_ORDER_CAP)
    const oversized = ids(TABLE_ORDER_CAP + 1)
    expect(normalizeTableOrder(oversized, oversized)).toHaveLength(TABLE_ORDER_CAP)
    expect(normalizeTableOrder(oversized, oversized)[0]).toBe('T-0')
    expect(normalizeTableOrder(oversized, oversized).at(-1)).toBe(`T-${TABLE_ORDER_CAP - 1}`)
  })
})

describe('named-sort comparators', () => {
  it('breaks ties with Task ID and never consults a manual order', () => {
    const alpha = task('T-2', { title: 'Same' })
    const beta = task('T-10', { title: 'Same' })
    expect(compareTableTasks(alpha, beta, 'title', false)).toBeLessThan(0)
    expect(compareTableTasks(alpha, beta, 'title', true)).toBeLessThan(0)
    const ordered = sortTasksByField([beta, alpha, task('T-1', { title: 'Same' })], 'title', true)
    expect(ordered.map((item) => item.id)).toEqual(['T-1', 'T-2', 'T-10'])
  })

  it('orders the named field and still uses a stable ID tie-breaker', () => {
    const rows = [
      task('T-3', { priority: 'P1' }),
      task('T-1', { priority: 'P1' }),
      task('T-2', { priority: 'P0' }),
    ]
    expect(sortTasksByField(rows, 'priority', false).map((item) => item.id)).toEqual(['T-2', 'T-1', 'T-3'])
    expect(sortTasksByField(rows, 'priority', true).map((item) => item.id)).toEqual(['T-1', 'T-3', 'T-2'])
  })
})

describe('manual list helpers', () => {
  it('projects stored order onto the visible Tasks and appends newly seen visible IDs last', () => {
    const visible = [task('B'), task('A'), task('D')]
    expect(orderTasksManually(visible, ['A', 'H', 'B'], ['A', 'B', 'C', 'H']).map((item) => item.id))
      .toEqual(['A', 'B', 'D'])
  })

  it('moves an ID previous/next/first/last without dropping siblings', () => {
    expect(moveVisibleId(['A', 'B', 'C'], 'B', 'previous')).toEqual(['B', 'A', 'C'])
    expect(moveVisibleId(['A', 'B', 'C'], 'B', 'next')).toEqual(['A', 'C', 'B'])
    expect(moveVisibleId(['A', 'B', 'C'], 'B', 'first')).toEqual(['B', 'A', 'C'])
    expect(moveVisibleId(['A', 'B', 'C'], 'B', 'last')).toEqual(['A', 'C', 'B'])
    expect(moveVisibleId(['A', 'B', 'C'], 'A', 'previous')).toEqual(['A', 'B', 'C'])
    expect(moveVisibleId(['A', 'B', 'C'], 'Z', 'next')).toBeNull()
  })

  it('places a dragged ID at the target index using the original list positions', () => {
    expect(placeVisibleId(['A', 'B', 'C'], 'A', 'C')).toEqual(['B', 'C', 'A'])
    expect(placeVisibleId(['A', 'B', 'C'], 'C', 'A')).toEqual(['C', 'A', 'B'])
    expect(placeVisibleId(['A', 'B', 'C'], 'B', 'B')).toEqual(['A', 'B', 'C'])
    expect(placeVisibleId(['A', 'B', 'C'], 'A', 'Z')).toBeNull()
  })
})

describe('pointer threshold and labels', () => {
  it('activates only at a 6 CSS-pixel distance', () => {
    const origin = { clientX: 10, clientY: 10 }
    expect(meetsTableDragThreshold(origin, { clientX: 10 + TABLE_DRAG_THRESHOLD_PX - 1, clientY: 10 })).toBe(false)
    expect(meetsTableDragThreshold(origin, { clientX: 10, clientY: 10 + TABLE_DRAG_THRESHOLD_PX })).toBe(true)
  })

  it('labels enabled and disabled handles and 1-based positions', () => {
    expect(tableMoveHandleLabel('T-1', true)).toBe('Reorder T-1')
    expect(tableMoveHandleLabel('T-1', false)).toBe('Choose Manual order to reorder T-1')
    expect(tablePositionLabel('T-1', 0, 4)).toBe('T-1 at position 1 of 4')
  })
})
