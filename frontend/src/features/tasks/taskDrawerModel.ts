import {
  MICROSOFT_PROVIDERS,
  type ContextItem,
  type MicrosoftProvider,
  type Task,
  type TaskDetail,
  type TaskPatch,
} from '../../domain/types'
import { statusLabels } from '../../utils/format'
import type { ReplySource } from './ReplyComposer'

export type EditableTaskPatch = Omit<TaskPatch, 'revision'>
export type EditableTaskField = keyof EditableTaskPatch

export interface SaveRun {
  taskId: string
  confirmed: Task | null
  queued: EditableTaskPatch
  inFlight: EditableTaskPatch | null
  inFlightBase: Task | null
  dirtyFields: Set<EditableTaskField>
  running: boolean
  blocked: boolean
  autoRebaseUsed: boolean
  detached: boolean
}

export function createSaveRun(taskId: string): SaveRun {
  return {
    taskId,
    confirmed: null,
    queued: {},
    inFlight: null,
    inFlightBase: null,
    dirtyFields: new Set(),
    running: false,
    blocked: false,
    autoRebaseUsed: false,
    detached: false,
  }
}

export function patchFields(patch: EditableTaskPatch): EditableTaskField[] {
  return Object.keys(patch) as EditableTaskField[]
}

export function hasPatch(patch: EditableTaskPatch) {
  return patchFields(patch).length > 0
}

export function sameValue(left: unknown, right: unknown) {
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => value === right[index])
  }
  return left === right
}

export function pruneServerEqualFields(patch: EditableTaskPatch, task: Task) {
  const pruned = { ...patch }
  for (const field of patchFields(pruned)) {
    if (sameValue(pruned[field], task[field])) delete pruned[field]
  }
  return pruned
}

export function overlayDirtyFields(task: Task, draft: Task, fields: Set<EditableTaskField>) {
  let overlaid = { ...task }
  for (const field of fields) overlaid = { ...overlaid, [field]: draft[field] }
  return overlaid
}

export function externalContext(item: ContextItem) {
  return Boolean(item.source || item.normalized || item.kind === 'capture' || item.type === 'capture')
}

export function contextTitle(item: ContextItem) {
  return item.source?.display_title ?? item.normalized?.summary ?? item.text ?? 'Context item'
}

export function activityTitle(item: TaskDetail['activity'][number]) {
  if (item.type !== 'task.planning_status' || !item.status) {
    return item.message ?? item.action ?? item.type ?? 'Task updated'
  }
  if (item.prior_status) return `${statusLabels[item.prior_status]} → ${statusLabels[item.status]}`
  return `Status recorded as ${statusLabels[item.status]}`
}

export function microsoftReplySource(item: ContextItem): ReplySource | null {
  const source = item.source
  if (
    !item.id
    || !source
    || !MICROSOFT_PROVIDERS.includes(source.provider as MicrosoftProvider)
    || !source.resource_type
    || !source.connection_ref
    || !source.container_ref
    || !source.display_title
    || !source.object_ref
    || !source.version_ref
  ) return null
  return {
    capture_id: item.id,
    provider: source.provider as MicrosoftProvider,
    resource_type: source.resource_type,
    connection_ref: source.connection_ref,
    container_ref: source.container_ref,
    display_title: source.display_title,
    object_ref: source.object_ref,
    version_ref: source.version_ref,
  }
}
