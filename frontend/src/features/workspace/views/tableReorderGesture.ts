import type { KeyboardEvent, MouseEvent, MutableRefObject, PointerEvent } from 'react'
import type { WorkspaceTask } from './types'
import {
  meetsTableDragThreshold,
  moveVisibleId,
  orderTasksManually,
  placeVisibleId,
  projectVisibleTableOrder,
  reorderTableVisible,
  sortTasksByField,
  tablePositionLabel,
} from './tableOrdering'
import type { TableLocalViewData, TableSortField } from './tablePreferences'
import { decodeTableMoveKey, type TableMoveCommand } from './tableMoveKeys'

export type TableGesture = {
  id: string
  source: 'pointer' | 'keyboard'
  baseVisible: string[]
  visible: string[]
}

export type HandlePointerEvent = PointerEvent<HTMLButtonElement> | MouseEvent<HTMLButtonElement>

export type PendingPointer = {
  id: string
  pointerId: number
  clientX: number
  clientY: number
  handle: HTMLButtonElement
}

export type TableExternalGate = {
  beginGesture(): void
  endGesture(): void
  cancelGesture(): unknown
  onExternal(data: TableLocalViewData): unknown
}

export type TableReorderController = {
  manual: boolean
  gestureRef: MutableRefObject<TableGesture | null>
  pendingPointerRef: MutableRefObject<PendingPointer | null>
  ignoreLostCaptureRef: MutableRefObject<boolean>
  tableLocalRef: MutableRefObject<TableLocalViewData>
  canonicalRef: MutableRefObject<readonly string[]>
  visibleRef: MutableRefObject<readonly string[]>
  gateRef: MutableRefObject<TableExternalGate>
  setGesture: (gesture: TableGesture | null) => void
  announce: (message: string) => void
  writeLocal: (mutator: (current: TableLocalViewData) => TableLocalViewData) => TableLocalViewData
}

export function eventPointerId(event: HandlePointerEvent): number {
  return 'pointerId' in event && event.pointerId ? event.pointerId : 1
}

export function rowIdFromPoint(clientX: number, clientY: number): string | null {
  const locator = document.elementFromPoint
  if (typeof locator !== 'function') return null
  const node = locator.call(document, clientX, clientY)
  if (!(node instanceof Element)) return null
  return node.closest('[data-task-id]')?.getAttribute('data-task-id') ?? null
}

export function visibleTableTasks(
  tasks: readonly WorkspaceTask[],
  sortField: TableSortField,
  descending: boolean,
  gesture: TableGesture | null,
  order: readonly string[],
  canonicalIds: readonly string[],
): WorkspaceTask[] {
  if (sortField !== 'manual') return sortTasksByField(tasks, sortField, descending)
  if (!gesture) return orderTasksManually(tasks, order, canonicalIds)
  const byId = new Map(tasks.map((task) => [task.id, task]))
  return gesture.visible.flatMap((id) => {
    const task = byId.get(id)
    return task ? [task] : []
  })
}

function releaseHandleCapture(handle: HTMLButtonElement, pointerId: number) {
  try {
    handle.releasePointerCapture?.(pointerId)
  } catch {
    // Capture may already be gone during workspace teardown.
  }
}

export function abortTableInteraction(controller: TableReorderController) {
  controller.ignoreLostCaptureRef.current = true
  const pending = controller.pendingPointerRef.current
  if (pending) releaseHandleCapture(pending.handle, pending.pointerId)
  controller.ignoreLostCaptureRef.current = false
  controller.pendingPointerRef.current = null
  controller.gestureRef.current = null
  controller.gateRef.current.endGesture()
}

export function cancelTableGesture(controller: TableReorderController, shouldAnnounce = true) {
  controller.ignoreLostCaptureRef.current = true
  const pending = controller.pendingPointerRef.current
  if (pending) {
    try {
      pending.handle.releasePointerCapture?.(pending.pointerId)
    } catch {
      // Capture may already be gone.
    }
  }
  controller.ignoreLostCaptureRef.current = false
  controller.pendingPointerRef.current = null
  controller.gateRef.current.cancelGesture()
  controller.setGesture(null)
  controller.gestureRef.current = null
  if (shouldAnnounce) controller.announce('Reorder cancelled')
}

export function commitTableGesture(
  controller: TableReorderController,
  nextVisible: string[],
  active: TableGesture,
) {
  controller.gateRef.current.endGesture()
  controller.writeLocal((current) => ({
    ...current,
    order: reorderTableVisible(
      current.order,
      controller.canonicalRef.current,
      active.baseVisible,
      nextVisible,
    ),
  }))
  controller.setGesture(null)
  controller.gestureRef.current = null
  controller.pendingPointerRef.current = null
  const index = nextVisible.indexOf(active.id)
  controller.announce(`Moved ${active.id} to position ${index + 1} of ${nextVisible.length}`)
}

export function beginTableGesture(
  controller: TableReorderController,
  id: string,
  source: TableGesture['source'],
  visible: string[],
) {
  const next: TableGesture = { id, source, baseVisible: [...visible], visible: [...visible] }
  controller.gestureRef.current = next
  controller.setGesture(next)
  controller.gateRef.current.beginGesture()
  const index = visible.indexOf(id)
  controller.announce(`Picked up ${tablePositionLabel(id, Math.max(0, index), visible.length)}`)
}

export function previewTableVisible(
  controller: TableReorderController,
  visible: string[],
  id: string,
) {
  const active = controller.gestureRef.current
  if (!active) return
  const next = { ...active, visible }
  controller.gestureRef.current = next
  controller.setGesture(next)
  const index = visible.indexOf(id)
  controller.announce(tablePositionLabel(id, Math.max(0, index), visible.length))
}

function projectedVisible(controller: TableReorderController): string[] {
  return projectVisibleTableOrder(
    controller.tableLocalRef.current.order,
    controller.canonicalRef.current,
    controller.visibleRef.current,
  )
}

function beginKeyboardGesture(controller: TableReorderController, id: string) {
  const active = controller.gestureRef.current
  if (active) cancelTableGesture(controller, false)
  beginTableGesture(controller, id, 'keyboard', projectedVisible(controller))
}

function runTableMoveCommand(
  controller: TableReorderController,
  id: string,
  command: Exclude<TableMoveCommand, { kind: 'ignore' }>,
) {
  if (command.kind === 'consume') return
  if (command.kind === 'cancel') {
    cancelTableGesture(controller)
    return
  }
  if (command.kind === 'begin') {
    beginKeyboardGesture(controller, id)
    return
  }
  if (command.kind === 'commit') {
    const active = controller.gestureRef.current
    if (active) commitTableGesture(controller, active.visible, active)
    return
  }
  const active = controller.gestureRef.current
  if (!active) return
  const moved = moveVisibleId(active.visible, id, command.action)
  if (moved) previewTableVisible(controller, moved, id)
}

export function applyTableMoveKeyDown(
  controller: TableReorderController,
  id: string,
  event: KeyboardEvent<HTMLButtonElement>,
) {
  if (!controller.manual) return
  const command = decodeTableMoveKey(event.key, controller.gestureRef.current, id)
  if (command.kind === 'ignore') return
  event.preventDefault()
  event.stopPropagation()
  runTableMoveCommand(controller, id, command)
}

export function onTableHandlePointerDown(
  controller: TableReorderController,
  id: string,
  event: HandlePointerEvent,
) {
  if (!controller.manual || event.button > 0) return
  if (controller.pendingPointerRef.current) return
  event.stopPropagation()
  event.preventDefault()
  controller.pendingPointerRef.current = {
    id,
    pointerId: eventPointerId(event),
    clientX: event.clientX,
    clientY: event.clientY,
    handle: event.currentTarget,
  }
  try {
    event.currentTarget.setPointerCapture?.(eventPointerId(event))
  } catch {
    // jsdom and some test drivers do not implement pointer capture.
  }
}

export function onTableHandlePointerMove(
  controller: TableReorderController,
  event: HandlePointerEvent,
) {
  const pending = controller.pendingPointerRef.current
  if (!pending || pending.pointerId !== eventPointerId(event)) return
  event.stopPropagation()
  let active = controller.gestureRef.current
  if (!active || active.source !== 'pointer' || active.id !== pending.id) {
    if (!meetsTableDragThreshold(pending, event)) return
    beginTableGesture(controller, pending.id, 'pointer', projectedVisible(controller))
    active = controller.gestureRef.current
  }
  if (!active) return
  const targetId = rowIdFromPoint(event.clientX, event.clientY)
  if (!targetId) return
  const placed = placeVisibleId(active.visible, pending.id, targetId)
  if (placed) previewTableVisible(controller, placed, pending.id)
}

export function onTableHandlePointerUp(
  controller: TableReorderController,
  event: HandlePointerEvent,
) {
  const pending = controller.pendingPointerRef.current
  if (!pending || pending.pointerId !== eventPointerId(event)) return
  event.stopPropagation()
  controller.ignoreLostCaptureRef.current = true
  try {
    event.currentTarget.releasePointerCapture?.(eventPointerId(event))
  } catch {
    // jsdom and some test drivers do not implement pointer capture.
  }
  controller.ignoreLostCaptureRef.current = false
  const active = controller.gestureRef.current
  controller.pendingPointerRef.current = null
  if (!active || active.source !== 'pointer' || active.id !== pending.id) {
    pending.handle.focus()
    return
  }
  const targetId = rowIdFromPoint(event.clientX, event.clientY)
  if (!targetId || !active.baseVisible.includes(targetId)) {
    cancelTableGesture(controller)
    return
  }
  commitTableGesture(controller, active.visible, active)
}

export function onTableHandlePointerCancel(
  controller: TableReorderController,
  event: HandlePointerEvent,
) {
  const pending = controller.pendingPointerRef.current
  if (pending && pending.pointerId !== eventPointerId(event)) return
  controller.pendingPointerRef.current = null
  if (controller.gestureRef.current?.source === 'pointer') cancelTableGesture(controller)
}

export function tableVisibleSetChanged(active: TableGesture, visibleIds: readonly string[]): boolean {
  const current = new Set(visibleIds)
  return active.baseVisible.length !== current.size || active.baseVisible.some((id) => !current.has(id))
}
