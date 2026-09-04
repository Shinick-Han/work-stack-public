import { useCallback, useLayoutEffect, useRef } from 'react'
import { useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'
import { api } from '../api/client'
import type { CheckpointAudit, Task, TaskDetail, WorkspaceProjection } from '../domain/types'
import type {
  CheckpointCommittedEvent,
  CheckpointTransitionEvent,
} from './workspaceChangeEvents'
import { transitionNoticeKey } from './workspaceChangeEvents'

/**
 * The already-verified Task and the owner lifetime this notice was raised in.
 * Nothing here is added to either wire event schema: these are internal
 * capabilities handed to App so it can offer the approved explicit action.
 */
export interface CheckpointNoticeIntent {
  workspaceUid: string
  taskId: string
  taskUid: string
  /** Opaque lifetime token. Never a repeatable workspace or date string. */
  ownerEpoch: string
  /** Synchronous liveness, including a Task ID/UID replacement in the cache. */
  isCurrent: () => boolean
}

interface CheckpointNotice {
  message: string
  view: () => void
  intent?: CheckpointNoticeIntent
}

interface Options {
  workspace?: WorkspaceProjection
  onNotice: (notice: CheckpointNotice) => void
  onView: (event: CheckpointCommittedEvent) => void
}

interface Scope {
  workspaceId?: string
  active: boolean
  pending: Set<string>
  queue: Promise<void>
}

/**
 * GI-F3: an issued capability owns ONE Task identity. Every observed cache
 * update is compared against it, so a same-batch Task UID A -> B -> A replaces
 * the owner and the old capability stays disposed even though the restored
 * value looks identical and React never rendered the intermediate state.
 */
interface IntentCapability {
  taskId: string
  taskUid: string
  disposed: boolean
}

function disposeReplacedCapabilities(
  capabilities: Set<IntentCapability>,
  workspace: WorkspaceProjection | undefined,
) {
  for (const capability of capabilities) {
    const live = workspace?.tasks.find((item) => item.id === capability.taskId)
    // A missing Task or a different UID is an ownership replacement, not an advance.
    if (!live || live.uid !== capability.taskUid) capability.disposed = true
  }
}

let noticeEpochCounter = 0

/** An opaque, non-repeatable lifetime token for one verified notice. */
function nextOwnerEpoch() {
  noticeEpochCounter += 1
  return `checkpoint-intent:${noticeEpochCounter}`
}

function message(event: CheckpointCommittedEvent): string {
  return `Agent CLI recorded a checkpoint for ${event.task_id} on ${event.date}: ${event.done_count} done, ${event.next_count} next, ${event.blocker_count} blockers.`
}

function newScope(workspaceId?: string): Scope {
  return { workspaceId, active: true, pending: new Set(), queue: Promise.resolve() }
}

/** Use query identity and data counters, not timestamps or React's batched render. */
function unchangedData(client: QueryClient, queryKey: QueryKey): () => boolean {
  const query = client.getQueryCache().find({ queryKey, exact: true })
  const updates = query?.state.dataUpdateCount
  const data = query?.state.data
  return () => client.getQueryCache().find({ queryKey, exact: true }) === query
    && query?.state.dataUpdateCount === updates && query?.state.data === data
}

function acceptsTask(current: Task | undefined, incoming: Task): boolean {
  return !current || (current.uid === incoming.uid && current.revision <= incoming.revision)
}

function acceptsSnapshots(client: QueryClient, workspace: WorkspaceProjection, detail: TaskDetail): boolean {
  const cached = client.getQueryData<WorkspaceProjection>(['workspace'])
  const previousTask = client.getQueryData<TaskDetail>(['task', detail.task.id])?.task
  return acceptsTask(previousTask, detail.task) && workspace.tasks.every((incoming) => (
    acceptsTask(cached?.tasks.find((item) => item.id === incoming.id), incoming)
  ))
}

function canViewTask(client: QueryClient, taskId: string, taskUid: string): boolean {
  const workspaceTask = client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks.find((item) => item.id === taskId)
  const detail = client.getQueryData<TaskDetail>(['task', taskId])
  return workspaceTask?.uid === taskUid
    && (!detail || (detail.task.id === taskId && detail.task.uid === taskUid))
}

/** An owner-local notification lifetime; no storage, polling or optimistic mutation. */
export function useCheckpointNotices(options: Options): (event: CheckpointCommittedEvent) => void {
  const queryClient = useQueryClient()
  const workspaceId = options.workspace?.workspace.id
  const callbacks = useRef(options)
  const scopeRef = useRef<Scope | null>(null)
  // Identity is the durable CP, not the process-local cursor. Keep it across effect replay.
  const seen = useRef(new Set<string>())
  const capabilitiesRef = useRef(new Set<IntentCapability>())
  useLayoutEffect(() => { callbacks.current = options })
  useLayoutEffect(() => {
    let owned = newScope(workspaceId)
    scopeRef.current = owned
    let cachedId = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id
    const capabilities = capabilitiesRef.current
    // QueryCache listeners run synchronously for every update, including A -> B -> A
    // inside one React batch. A rendered workspace ID alone loses that transition.
    const unsubscribe = queryClient.getQueryCache().subscribe(({ query }) => {
      if (query.queryKey.length !== 1 || query.queryKey[0] !== 'workspace') return
      const workspace = queryClient.getQueryData<WorkspaceProjection>(['workspace'])
      // GI-F3: Task identity is observed on EVERY workspace update, not only
      // when the workspace ID itself changed.
      disposeReplacedCapabilities(capabilities, workspace)
      const nextId = workspace?.workspace.id
      if (nextId === cachedId) return
      cachedId = nextId
      owned.active = false
      owned = newScope(nextId)
      scopeRef.current = owned
    })
    return () => { unsubscribe(); owned.active = false }
  }, [queryClient, workspaceId])

  return useCallback((event: CheckpointCommittedEvent) => {
    const scope = scopeRef.current
    if (!scope?.active || event.workspace_uid !== scope.workspaceId) return
    const key = `${event.workspace_uid}:${event.checkpoint_id}:${event.kind}`
    if (seen.current.has(key) || scope.pending.has(key)) return
    scope.pending.add(key)
    const current = () => scope.active && scopeRef.current === scope
      && queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id === event.workspace_uid

    // Preserve observed order even when different checkpoints' HTTP reads take different times.
    scope.queue = scope.queue.then(async () => {
      try {
        if (!current()) return
        const unchanged = [
          unchangedData(queryClient, ['workspace']),
          unchangedData(queryClient, ['task', event.task_id]),
          unchangedData(queryClient, ['review', event.date, 7]),
        ]
        // Direct API reads also fetch inactive Task/Review observers. Invalidation alone does not.
        const [workspace, detail, review] = await Promise.all([
          api.getWorkspace(), api.getTask(event.task_id), api.getReview(event.date, 7),
        ])
        if (!current() || !unchanged.every((check) => check()) || workspace.workspace.id !== event.workspace_uid) return
        const task = workspace.tasks.find((item) => item.id === event.task_id)
        if (!task || detail.task.id !== task.id || detail.task.uid !== task.uid
          || detail.task.revision !== task.revision || review.day.date !== event.date) return
        if (!acceptsSnapshots(queryClient, workspace, detail)) return
        const taskUid = task.uid
        queryClient.setQueryData(['workspace'], workspace)
        queryClient.setQueryData(['task', event.task_id], detail)
        queryClient.setQueryData(['review', event.date, 7], review)
        seen.current.add(key)
        const ownerEpoch = nextOwnerEpoch()
        const capability: IntentCapability = { disposed: false, taskId: event.task_id, taskUid }
        capabilitiesRef.current.add(capability)
        callbacks.current.onNotice({
          intent: {
            isCurrent: () => {
              // Disposal is irreversible: a restored value cannot revive it.
              if (capability.disposed || !current()) return false
              const live = queryClient
                .getQueryData<WorkspaceProjection>(['workspace'])
                ?.tasks.find((item) => item.id === event.task_id)
              // A replaced Task UID is a different Task, not an advance.
              return !!live && live.uid === taskUid
            },
            ownerEpoch,
            taskId: event.task_id,
            taskUid,
            workspaceUid: event.workspace_uid,
          },
          message: message(event),
          view: () => {
            if (current() && canViewTask(queryClient, event.task_id, taskUid)) {
              callbacks.current.onView(event)
            }
          },
        })
      } catch {
        // A failed authoritative read cannot support a success notice. Manual/sync refresh stays available.
      } finally {
        scope.pending.delete(key)
      }
    })
  }, [queryClient])
}

/** The hint is only a prompt; these authoritative facts decide the outcome. */
function authoritativeAgrees(
  event: CheckpointTransitionEvent,
  workspace: WorkspaceProjection,
  audit: CheckpointAudit,
  detail: TaskDetail | null,
): boolean {
  if (audit.workspace_uid !== event.workspace_uid) return false
  const entry = audit.entries.find((row) => row.checkpoint_id === event.checkpoint_id)
  if (!entry) return false
  if (entry.locator.workspace_uid !== event.workspace_uid) return false
  if (entry.locator.task_id !== event.task_id) return false
  if (entry.locator.date !== event.date) return false
  if (entry.locator.ordinal !== event.ordinal) return false
  if (entry.locator.entry_digest !== event.entry_digest) return false
  // An older or contradicting audit state cannot announce this hint.
  if (entry.revision !== event.transition_revision) return false
  if (entry.state !== event.state) return false
  // A missing current Task is legitimate; a disagreeing one is not. The
  // fetched detail must be the same Task identity, by ID and by UID.
  const workspaceTask = workspace.tasks.find((task) => task.id === event.task_id)
  if (detail) {
    if (detail.task.id !== event.task_id) return false
    if (!workspaceTask) return false
    if (detail.task.uid !== workspaceTask.uid) return false
    if (detail.task.revision !== workspaceTask.revision) return false
  }
  return true
}

/**
 * A cache that is ALREADY ahead of what we fetched makes this hint obsolete,
 * whether or not it moved while the read was in flight.
 */
function cacheIsAhead(
  client: QueryClient,
  event: CheckpointTransitionEvent,
  workspace: WorkspaceProjection,
  auditKey: QueryKey,
  detail: TaskDetail | null,
): boolean {
  const cachedWorkspace = client.getQueryData<WorkspaceProjection>(['workspace'])
  const cachedTask = cachedWorkspace?.tasks.find((task) => task.id === event.task_id)
  const fetchedTask = workspace.tasks.find((task) => task.id === event.task_id)
  if (cachedTask && fetchedTask) {
    if (cachedTask.uid !== fetchedTask.uid) return true
    if (cachedTask.revision > fetchedTask.revision) return true
  }
  const cachedDetail = client.getQueryData<TaskDetail>(['task', event.task_id])
  if (cachedDetail) {
    // An entry under this key holding a different Task is an incompatible
    // cache identity. It is decided here, with the aggregate, rather than
    // being skipped later while the other writes and the notice go ahead.
    if (cachedDetail.task.id !== event.task_id) return true
    if (detail) {
      if (cachedDetail.task.uid !== detail.task.uid) return true
      if (cachedDetail.task.revision > detail.task.revision) return true
    }
  }
  const cachedAudit = client.getQueryData<CheckpointAudit>(auditKey)
  const cachedEntry = cachedAudit?.entries.find((row) => row.checkpoint_id === event.checkpoint_id)
  if (cachedEntry && cachedEntry.revision > event.transition_revision) return true
  return false
}

function installTask(client: QueryClient, taskId: string, detail: TaskDetail): void {
  const cached = client.getQueryData<TaskDetail>(['task', taskId])
  if (cached) {
    // A different Task now occupies this identity, by ID or by UID: a lower
    // revision is not a reason to replace it.
    if (cached.task.id !== detail.task.id) return
    if (cached.task.uid !== detail.task.uid) return
    // A late older revision must not replace a newer cached Task.
    if (cached.task.revision > detail.task.revision) return
  }
  client.setQueryData(['task', taskId], detail)
}


/**
 * The Task this notice was published against must still be the Task the cache
 * and workspace hold. A Task that legitimately never existed stays valid.
 */
function taskIdentityHolds(
  client: QueryClient,
  taskId: string,
  published: TaskDetail | null,
): boolean {
  const workspace = client.getQueryData<WorkspaceProjection>(['workspace'])
  const currentTask = workspace?.tasks.find((task) => task.id === taskId)
  if (published === null) return currentTask === undefined
  if (!currentTask) return false
  // Identity is the ID and the UID. An ordinary later revision of the SAME
  // Task is compatible; equality with the published revision is not identity.
  if (currentTask.uid !== published.task.uid) return false
  if (currentTask.revision < published.task.revision) return false
  const cached = client.getQueryData<TaskDetail>(['task', taskId])
  if (cached) {
    if (cached.task.id !== taskId) return false
    if (cached.task.uid !== published.task.uid) return false
    if (cached.task.revision < published.task.revision) return false
  }
  return true
}

interface TransitionOptions {
  workspace?: WorkspaceProjection
  onNotice: (notice: CheckpointNotice) => void
  onView: (event: CheckpointTransitionEvent) => void
  /** The day currently shown, so its Review read can be refreshed too. */
  reviewDate?: string
}

function transitionMessage(event: CheckpointTransitionEvent): string {
  const action = event.state === 'superseded' ? 'superseded' : 'restored'
  return `Agent CLI ${action} checkpoint ${event.checkpoint_id} for ${event.task_id} on ${event.date}.`
}

/**
 * The transition notice is a stale-state hint, never the announcement itself.
 * Authoritative workspace, Task, Review and audit reads happen first, and a
 * notice older than the audit we already hold is dropped rather than announced.
 */
export function useCheckpointTransitionNotices(
  options: TransitionOptions,
): (event: CheckpointTransitionEvent) => void {
  const queryClient = useQueryClient()
  const workspaceId = options.workspace?.workspace.id
  const callbacks = useRef(options)
  const scopeRef = useRef<Scope | null>(null)
  const seen = useRef(new Set<string>())
  useLayoutEffect(() => { callbacks.current = options })
  useLayoutEffect(() => {
    let owned = newScope(workspaceId)
    scopeRef.current = owned
    let cachedId = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id
    const unsubscribe = queryClient.getQueryCache().subscribe(({ query }) => {
      if (query.queryKey.length !== 1 || query.queryKey[0] !== 'workspace') return
      const nextId = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id
      if (nextId === cachedId) return
      cachedId = nextId
      owned.active = false
      owned = newScope(nextId)
      scopeRef.current = owned
    })
    return () => { unsubscribe(); owned.active = false }
  }, [queryClient, workspaceId])

  return useCallback((event: CheckpointTransitionEvent) => {
    const scope = scopeRef.current
    if (!scope?.active || event.workspace_uid !== scope.workspaceId) return
    // Identity includes the revision, so 1 and 3 are separate hints.
    const key = transitionNoticeKey(event)
    if (seen.current.has(key) || scope.pending.has(key)) return
    scope.pending.add(key)
    const current = () => scope.active && scopeRef.current === scope
      && queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id === event.workspace_uid

    scope.queue = scope.queue.then(async () => {
      try {
        if (!current()) return
        const reviewDate = callbacks.current.reviewDate ?? event.date
        // Captured before the reads: a cache written while we wait is newer.
        const workspaceUnchanged = unchangedData(queryClient, ['workspace'])
        const auditKey: QueryKey = ['checkpoint-audit', event.workspace_uid]
        const auditUnchanged = unchangedData(queryClient, auditKey)
        const reviewUnchanged = unchangedData(queryClient, ['review', reviewDate, 7])
        const detailUnchanged = unchangedData(queryClient, ['task', event.task_id])
        const [workspace, audit] = await Promise.all([
          api.getWorkspace(),
          api.getCheckpointAudit(),
        ])
        // A Task may legitimately be gone; its audit row stays valid.
        const workspaceTask = workspace.tasks.find((task) => task.id === event.task_id)
        const detail = workspaceTask ? await api.getTask(event.task_id) : null
        const review = await api.getReview(reviewDate, 7)
        if (!current() || workspace.workspace.id !== event.workspace_uid) return
        // The authoritative snapshots must agree with the hint before any
        // success is announced; a hint is never the announcement itself.
        if (!authoritativeAgrees(event, workspace, audit, detail)) return
        // Nothing at all happens for an obsolete hint: no install, no notice.
        if (cacheIsAhead(queryClient, event, workspace, auditKey, detail)) return
        if (!detailUnchanged()) return

        // Chronology: never move a cache backwards with an older reply.
        // A newer authoritative cache does not merely escape overwrite: it
        // makes this hint obsolete, so nothing is announced either.
        const reviewKey: QueryKey = ['review', reviewDate, 7]
        if (!workspaceUnchanged() || !auditUnchanged() || !reviewUnchanged()) return
        if (review.day.date !== reviewDate) return
        queryClient.setQueryData(['workspace'], workspace)
        queryClient.setQueryData(auditKey, audit)
        if (detail) installTask(queryClient, event.task_id, detail)
        queryClient.setQueryData(reviewKey, review)

        seen.current.add(key)
        callbacks.current.onNotice({
          message: transitionMessage(event),
          // Owner AND applicable Task identity are re-checked at click time,
          // not at publication time.
          view: () => {
            if (!current()) return
            if (!taskIdentityHolds(queryClient, event.task_id, detail)) return
            callbacks.current.onView(event)
          },
        })
      } catch {
        // A failed authoritative read is contained: it is never announced and
        // never leaves the queue rejected for the next valid hint.
      } finally {
        scope.pending.delete(key)
      }
    })
  }, [queryClient])
}
