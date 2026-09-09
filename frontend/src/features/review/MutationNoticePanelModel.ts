import {
  mutationUndoTarget,
  type MutationNotice,
  type MutationUndoTarget,
} from '../../api/mutationNotices'
import type { Task, TaskStatus } from '../../domain/types'

/**
 * The pure half of the Recent changes panel: the copy it may show, and the projection of
 * one published notice onto the row a reader can actually read.
 *
 * What this deliberately does NOT produce is as much of the contract as what it does:
 *
 * - **no timestamp.** The notice contract publishes none, so the panel orders by the
 *   owner's own newest-first paging and says nothing about when anything happened.
 * - **no idempotency key.** It is request identity, not evidence for a reader.
 * - **no raw document.** A row is named fields; a JSON dump would leak both of the above
 *   and read as debug output besides.
 * - **no invented title.** A Task title is shown only when the notice is about a Task AND
 *   this workspace's own projection carries the entity UID it named. Anything else says
 *   what it actually is: a Task out of view, or a record of another kind entirely.
 */

export const MUTATION_NOTICE_COPY = {
  heading: 'Recent changes',
  lead: 'Status changes this workspace recorded, newest first.',
  refresh: 'Refresh',
  refreshing: 'Refreshing…',
  older: 'Show older changes',
  newest: 'Back to newest',
  oldest: 'This is the oldest recorded change.',
  loading: 'Opening recent changes…',
  emptyTitle: 'No changes recorded yet',
  emptyBody: 'Status changes made here, from the CLI, or by an agent will appear in this list.',
  undo: 'Undo',
  undoing: 'Undoing…',
  undoRetry: 'Send the same undo again',
  undone: 'Status restored.',
  dismiss: 'Dismiss',
  readOnly: 'Recorded only',
  unknownTask: 'Task not in this workspace view',
  openTask: 'Open task',
  outcome: 'Undo outcome',
} as const

const STATUS_LABELS: Readonly<Record<TaskStatus, string>> = Object.freeze({
  open: 'Open',
  started: 'Started',
  done: 'Done',
  dropped: 'Dropped',
})

const SOURCE_LABELS: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    cli: 'CLI',
    gui: 'App',
    agent: 'Agent',
  }),
)

/**
 * The owner's nine `ENTITY_KINDS`, in the reader's words.
 *
 * A notice names the kind of thing it changed, and most of those kinds are not Tasks. The
 * panel shows the kind rather than diagnosing an absent Task, because "this was a Capture"
 * is what the row actually knows; claiming a Task is missing from the workspace view would
 * be a statement about a Task that was never involved.
 */
const ENTITY_KIND_LABELS: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    task: 'Task',
    subtask: 'Subtask',
    objective: 'Objective',
    key_result: 'Key result',
    worklog: 'Worklog',
    capture: 'Capture',
    workspace: 'Workspace',
    profile: 'Profile',
    storage: 'Storage',
  }),
)

export function statusLabel(status: TaskStatus): string {
  return STATUS_LABELS[status]
}

/**
 * Own properties only, and one fixed fallback, for the reason `sourceLabel` has them:
 * `entity_kind` is a closed enum on the wire, so no valid notice reaches the fallback.
 */
export function entityKindLabel(kind: string): string {
  const label = Object.hasOwn(ENTITY_KIND_LABELS, kind) ? ENTITY_KIND_LABELS[kind] : undefined
  return typeof label === 'string' ? label : 'Other record'
}

/**
 * Own properties only, and one fixed fallback. `source` is a closed enum on the wire, so
 * the fallback is unreachable through a valid notice; it exists so no lookup can resolve
 * into an inherited `Object.prototype` member and render a function as a label.
 */
export function sourceLabel(source: string): string {
  const label = Object.hasOwn(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : undefined
  return typeof label === 'string' ? label : 'Unknown source'
}

/** The workspace's own UID → Task mapping. Nothing else may name a Task on this surface. */
export function indexTasksByUid(tasks: readonly Task[]): Map<string, Task> {
  const index = new Map<string, Task>()
  for (const task of tasks) index.set(task.uid, task)
  return index
}

export interface MutationNoticeRow {
  noticeId: string
  /** The owner's own fixed sentence for this operation. Never assembled here. */
  summary: string
  actor: string
  sourceLabel: string
  /** The kind of thing the notice named, in the reader's words. */
  entityLabel: string
  /** True only for a notice about a Task, which is the only kind this map can resolve. */
  isTaskEntity: boolean
  /** The display id, only when this workspace projection knows the entity. */
  taskId: string | null
  taskTitle: string | null
  statusBefore: TaskStatus | null
  statusAfter: TaskStatus | null
  beforeRevision: number
  afterRevision: number | null
  /** Null for every row the owner or the operation does not admit as reversible. */
  undo: MutationUndoTarget | null
}

export function projectMutationNotice(
  notice: MutationNotice,
  tasksByUid: ReadonlyMap<string, Task>,
): MutationNoticeRow {
  // The UID map holds Tasks and only Tasks. A capture, a worklog or an objective UID has
  // no entry in it and never could, so asking is not a lookup that failed — it is the
  // wrong question, and its miss must not be reported as a Task this view cannot see.
  const isTaskEntity = notice.entity_kind === 'task'
  const task = isTaskEntity ? tasksByUid.get(notice.entity_uid) : undefined
  return {
    noticeId: notice.notice_id,
    summary: notice.summary,
    actor: notice.actor,
    sourceLabel: sourceLabel(notice.source),
    entityLabel: entityKindLabel(notice.entity_kind),
    isTaskEntity,
    taskId: task?.id ?? null,
    taskTitle: task?.title ?? null,
    statusBefore: notice.status_before,
    statusAfter: notice.status_after,
    beforeRevision: notice.before_revision,
    afterRevision: notice.after_revision,
    undo: mutationUndoTarget(notice),
  }
}

export function projectMutationNotices(
  notices: readonly MutationNotice[],
  tasksByUid: ReadonlyMap<string, Task>,
): MutationNoticeRow[] {
  return notices.map((notice) => projectMutationNotice(notice, tasksByUid))
}

/**
 * The revision pair, as one readable phrase. `null` is the `commit_unknown` case: the
 * owner never recorded an after-revision, so the row says the outcome is unverified
 * rather than printing a number nobody wrote.
 */
export function revisionSummary(row: MutationNoticeRow): string {
  if (row.afterRevision === null) return `Revision ${row.beforeRevision} · outcome unverified`
  return `Revision ${row.beforeRevision} → ${row.afterRevision}`
}
