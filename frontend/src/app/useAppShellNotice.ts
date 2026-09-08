import { useEffect, useMemo, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { type QueryClient, type QueryKey } from '@tanstack/react-query'
import { api, createIdempotencyKey } from '../api/client'
import type { AppUrlState, SyncStatus, TaskDetail, WorkspaceProjection } from '../domain/types'
import { subscribeSyncStatusEvents } from './syncStatusEvents'
import type { Notice, TaskStatusIntentAffordance } from './appShellPresentation'
import { deriveSyncPresentation } from './useAppShellSync'
import {
  useCheckpointNotices,
  type CheckpointNoticeIntent,
  useCheckpointTransitionNotices,
} from './useCheckpointNotices'
import {
  useTaskStatusIntent,
  type TaskStatusIntentResponse,
} from './useTaskStatusIntent'

const NO_INTENT_OWNER = {
  ownerEpoch: 'checkpoint-intent:none',
  taskId: '',
  taskUid: '',
  workspaceUid: '',
}

/**
 * GI-F1: the LIVE authority for one intent. The rendered snapshot is never the
 * input: status, revision, detail identity and write authority are read from
 * the cache at the moment the hook asks.
 */
function liveIntentAuthority(
  client: QueryClient,
  intent: CheckpointNoticeIntent | null,
) {
  if (!intent) return { detailAgrees: false, task: null, writable: false }
  const live = client.getQueryData<WorkspaceProjection>(['workspace'])
    ?.tasks.find((item) => item.id === intent.taskId)
  const detail = client.getQueryData<TaskDetail>(['task', intent.taskId])
  const task = live && live.uid === intent.taskUid ? live : null
  // A cached detail for this Task must agree; an absent detail is not a defect.
  const detailAgrees = !detail || (detail.task.id === intent.taskId && detail.task.uid === intent.taskUid)
  const sync = client.getQueryState<SyncStatus>(['sync-status'])
  const workspaceFailed = client.getQueryState(['workspace'])?.status === 'error'
  const { writeBlocked } = deriveSyncPresentation(sync?.error, sync?.data, workspaceFailed)
  return { detailAgrees, task, writable: !writeBlocked }
}

/** Every explicit dispatch and every completion re-reads that authority. */
function isIntentAuthorityCurrent(
  client: QueryClient,
  intent: CheckpointNoticeIntent | null,
  ownerEpoch: string,
) {
  if (!intent || intent.ownerEpoch !== ownerEpoch || !intent.isCurrent()) return false
  const authority = liveIntentAuthority(client, intent)
  return !!authority.task && authority.detailAgrees && authority.writable
}

interface ReconcileChronology {
  revisionFloor: number
  unchanged: () => boolean
}

/**
 * GI-F2: query identity and update chronology are captured BEFORE the reads, so
 * a newer arrival during the fetch survives instead of being overwritten.
 */
function captureReconcileChronology(
  client: QueryClient,
  intent: CheckpointNoticeIntent,
): ReconcileChronology {
  const cache = client.getQueryCache()
  const keys: QueryKey[] = [['workspace'], ['task', intent.taskId]]
  const snapshots = keys.map((key) => {
    const query = cache.find({ exact: true, queryKey: key })
    return { data: query?.state.data, key, query, updates: query?.state.dataUpdateCount }
  })
  const cachedTask = client.getQueryData<WorkspaceProjection>(['workspace'])
    ?.tasks.find((item) => item.id === intent.taskId)
  const cachedDetail = client.getQueryData<TaskDetail>(['task', intent.taskId])
  return {
    // An inactive detail can already be newer than the workspace when reads begin.
    revisionFloor: Math.max(cachedTask?.revision ?? 0, cachedDetail?.task.revision ?? 0),
    unchanged: () => snapshots.every((snapshot) => {
      const live = cache.find({ exact: true, queryKey: snapshot.key })
      // Removal, recreation, a newer update count or a replaced value all refuse.
      return live === snapshot.query
        && live?.state.dataUpdateCount === snapshot.updates
        && live?.state.data === snapshot.data
    }),
  }
}

/** The fetched pair must answer the captured owner and may not roll back. */
function acceptsReconciledPair(
  before: ReconcileChronology,
  intent: CheckpointNoticeIntent,
  workspace: WorkspaceProjection,
  detail: TaskDetail,
) {
  if (workspace.workspace.id !== intent.workspaceUid) return false
  const live = workspace.tasks.find((item) => item.id === intent.taskId)
  if (!live || live.uid !== intent.taskUid) return false
  if (detail.task.id !== intent.taskId || detail.task.uid !== intent.taskUid) return false
  if (detail.task.revision !== live.revision) return false
  return live.revision >= before.revisionFloor
}

/** The capability a notice carries, if any. */
function noticeIntentOf(notice: Notice | null) {
  return notice?.intent ?? null
}

/**
 * GI-F4: an unresolved intent keeps its own owner. Presentation may be replaced
 * underneath it; ownership may not.
 */
function currentIntentOwnerOf(owned: CheckpointNoticeIntent | null, notice: Notice | null) {
  const presented = noticeIntentOf(notice)
  if (!owned || !owned.isCurrent()) return presented
  if (presented && !sameIntentTask(owned, presented)) return presented
  return owned
}

function sameIntentTask(left: CheckpointNoticeIntent, right: CheckpointNoticeIntent) {
  return left.workspaceUid === right.workspaceUid
    && left.taskId === right.taskId
    && left.taskUid === right.taskUid
}

function retainedIntentOwner(
  intent: CheckpointNoticeIntent | null,
  state: ReturnType<typeof useTaskStatusIntent>,
) {
  return holdsNotice(state) ? intent : null
}

/** A pending or ambiguous intent holds its notice, and therefore its key. */
function holdsNotice(state: ReturnType<typeof useTaskStatusIntent>) {
  return state.pending || state.retryable
}

/** Liveness for a captured owner: same lifetime AND same live Task identity. */
function isIntentOwnerCurrent(intent: CheckpointNoticeIntent | null, ownerEpoch: string) {
  if (!intent || intent.ownerEpoch !== ownerEpoch) return false
  return intent.isCurrent()
}

function intentOwnerOf(intent: CheckpointNoticeIntent | null) {
  if (!intent) return NO_INTENT_OWNER
  const { ownerEpoch, taskId, taskUid, workspaceUid } = intent
  return { ownerEpoch, taskId, taskUid, workspaceUid }
}

/** The authoritative current Task for this intent, or null when it is gone. */
function liveIntentTask(workspace: WorkspaceProjection | undefined, intent: CheckpointNoticeIntent | null) {
  if (!intent) return null
  const live = workspace?.tasks.find((item) => item.id === intent.taskId)
  return live && live.uid === intent.taskUid ? live : null
}

function intentAffordanceOf(
  intent: CheckpointNoticeIntent | null,
  state: ReturnType<typeof useTaskStatusIntent>,
): TaskStatusIntentAffordance | null {
  if (!intent) return null
  return {
    canMarkInProgress: state.canMarkInProgress,
    markInProgress: state.markInProgress,
    pending: state.pending,
    retry: state.retry,
    retryable: state.retryable,
    undo: state.undoLast,
    undoAvailable: !!state.undoOffer,
  }
}

function useAppShellCheckpointNotices({
  queryClient,
  refreshAuthoritativeState,
  showNotice,
  update,
  workspace,
}: {
  queryClient: QueryClient
  refreshAuthoritativeState: () => void | Promise<unknown>
  showNotice: (
    message: string,
    tone?: Notice['tone'],
    action?: Notice['action'],
    intent?: CheckpointNoticeIntent,
  ) => void
  update: (patch: Partial<AppUrlState>) => boolean
  workspace?: WorkspaceProjection
}) {
  const receiveCheckpoint = useCheckpointNotices({
    workspace,
    onNotice: ({ intent, message, view }) => showNotice(message, 'success', { label: 'View', run: view }, intent),
    onView: (event) => { update({ surface: 'workspace', taskId: event.task_id, captureId: null }) },
  })
  // The transition notice is a separate strict variant on the SAME stream.
  const receiveTransition = useCheckpointTransitionNotices({
    workspace,
    onNotice: ({ message, view }) => showNotice(message, 'success', { label: 'View', run: view }),
    onView: (event) => { update({ surface: 'review', taskId: null, captureId: null }) },
  })
  useEffect(
    () => subscribeSyncStatusEvents(refreshAuthoritativeState, receiveCheckpoint, receiveTransition),
    [queryClient, receiveCheckpoint, receiveTransition],
  )
}

function useAppShellNoticeTimeout(
  notice: Notice | null,
  noticeHoldRef: MutableRefObject<boolean>,
  noticeHoldToken: string,
  setNotice: Dispatch<SetStateAction<Notice | null>>,
) {
  useEffect(() => {
    if (!notice) return
    // An in-flight or ambiguous keyed intent keeps its notice, and therefore its
    // frozen key, until it settles: the timeout must not manufacture a new one.
    if (noticeHoldRef.current) return
    const timer = window.setTimeout(() => setNotice((current) => current?.id === notice.id ? null : current), 3200)
    return () => window.clearTimeout(timer)
  }, [notice, noticeHoldToken])
}

function createIntentReconcile(
  queryClient: QueryClient,
  noticeIntentRef: MutableRefObject<CheckpointNoticeIntent | null>,
) {
  return async () => {
    const captured = noticeIntentRef.current
    if (!captured) return
    const before = captureReconcileChronology(queryClient, captured)
    const [workspace, detail] = await Promise.all([
      api.getWorkspace(),
      api.getTask(captured.taskId),
    ])
    if (noticeIntentRef.current !== captured || !before.unchanged()) return
    if (!acceptsReconciledPair(before, captured, workspace, detail)) return
    if (!isIntentAuthorityCurrent(queryClient, captured, captured.ownerEpoch)) return
    queryClient.setQueryData(['workspace'], workspace)
    queryClient.setQueryData(['task', captured.taskId], detail)
  }
}

export function useAppShellNotice({
  notice,
  queryClient,
  refreshAuthoritativeState,
  setNotice,
  showNotice,
  update,
  workspace,
}: {
  notice: Notice | null
  queryClient: QueryClient
  refreshAuthoritativeState: () => void | Promise<unknown>
  setNotice: Dispatch<SetStateAction<Notice | null>>
  showNotice: (
    message: string,
    tone?: Notice['tone'],
    action?: Notice['action'],
    intent?: CheckpointNoticeIntent,
  ) => void
  update: (patch: Partial<AppUrlState>) => boolean
  workspace?: WorkspaceProjection
}): TaskStatusIntentAffordance | null {
  const noticeHoldRef = useRef(false)
  const ownedIntentRef = useRef<CheckpointNoticeIntent | null>(null)
  useAppShellCheckpointNotices({
    queryClient, refreshAuthoritativeState, showNotice, update, workspace,
  })
  // The ONLY status-intent state machine: the admitted hook, with its frozen
  // options. No second hook, transport, mailbox or status setter is introduced.
  // GI-F4: presentation is replaceable, ownership of an UNRESOLVED intent is
  // not. While one is pending or ambiguous its owner survives a new
  // informational notice and the ordinary presentation timeout, so the frozen
  // key can never be replaced by a fresh Mark.
  const noticeIntent = currentIntentOwnerOf(ownedIntentRef.current, notice)
  const intentOwner = useMemo(() => intentOwnerOf(noticeIntent), [noticeIntent])
  const noticeIntentRef = useRef(noticeIntent)
  noticeIntentRef.current = noticeIntent
  const statusIntent = useTaskStatusIntent({
    // GI-F1: a getter, so the accepted hook reads the LIVE authoritative Task at
    // its own dispatch and receipt decision points instead of a render snapshot.
    // The hook's options contract is unchanged and nothing is synthesized.
    get currentTask() {
      return liveIntentAuthority(queryClient, noticeIntentRef.current).task ?? null
    },
    // Liveness is answered from the cache at call time, not from this render.
    isCurrent: (owner) => isIntentAuthorityCurrent(
      queryClient, noticeIntentRef.current, owner.ownerEpoch,
    ),
    mutateOnce: (intent) => api.patchTaskStatusIntent(
      intent.taskId,
      { revision: intent.body.revision, status: intent.body.status },
      intent.key,
    ) as Promise<TaskStatusIntentResponse>,
    newKey: () => createIdempotencyKey(),
    owner: intentOwner,
    reconcile: createIntentReconcile(queryClient, noticeIntentRef),
  })
  // Holding is a fact about the CURRENT intent, read by the dismissal timer.
  noticeHoldRef.current = holdsNotice(statusIntent)
  // An unresolved intent keeps its owner; a settled one releases it.
  ownedIntentRef.current = retainedIntentOwner(noticeIntent, statusIntent)
  useAppShellNoticeTimeout(
    notice,
    noticeHoldRef,
    `${statusIntent.pending}:${statusIntent.retryable}`,
    setNotice,
  )
  return intentAffordanceOf(noticeIntent, statusIntent)
}
