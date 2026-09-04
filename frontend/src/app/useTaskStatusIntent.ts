import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from 'react'

import { TASK_PRIORITIES, TASK_STATUSES, type Task, type TaskStatus } from '../domain/types'

/**
 * Task-status intent and its separate Undo, ahead of App and API integration.
 *
 * This hook owns lifetime and intent identity ONLY. It performs no fetch, holds
 * no cache and knows no endpoint: the caller injects one-attempt mutation, key
 * generation, authoritative reconciliation and a synchronous liveness check.
 * Nothing here mutates on render, on notice arrival or on a View action.
 *
 * Notes for the later integrator (this is the single agreed internal boundary,
 * not a new public contract family):
 * - `mutateOnce` is ONE attempt. This hook never loops, never rebases a
 *   revision and never invents a key.
 * - `newKey` is called exactly once per NEW explicit intent. An ambiguous
 *   result keeps the frozen body and key for explicit same-intent retry.
 * - `reconcile` is a display-only authoritative read for a determinate 409.
 * - `isCurrent` must answer for the CURRENT authority, including a change that
 *   has not rendered yet. Rendered props alone are not lifetime authority.
 */

export type TaskStatusIntentKind = 'forward' | 'undo'

export interface TaskStatusIntentOwner {
  /** Workspace the intent was captured in. */
  readonly workspaceUid: string
  /** Opaque owner epoch; A -> B -> A must not compare equal by value alone. */
  readonly ownerEpoch: string
  readonly taskId: string
  readonly taskUid: string
}

/** The exact keyed body the approved route accepts. */
export interface TaskStatusIntentBody {
  readonly status: TaskStatus
  readonly revision: number
}

/** A frozen intent. Every coordinate is captured before dispatch. */
export interface TaskStatusIntent {
  readonly kind: TaskStatusIntentKind
  readonly owner: TaskStatusIntentOwner
  readonly taskId: string
  readonly taskUid: string
  readonly priorStatus: TaskStatus
  readonly expectedRevision: number
  readonly requestedStatus: TaskStatus
  readonly key: string
  readonly body: TaskStatusIntentBody
}

/** The approved keyed response shape. */
export interface TaskStatusIntentResponse {
  readonly status: number
  readonly body: {
    readonly data: Task
    readonly meta: { readonly replayed: boolean }
  }
}

export type TaskStatusIntentPhase = 'idle' | 'pending' | 'unknown' | 'conflict' | 'refused'

export interface TaskStatusUndoOffer {
  /** Status to restore. */
  readonly priorStatus: TaskStatus
  /** Revision of the forward receipt the Undo is expressed against. */
  readonly atRevision: number
  /** Identity the forward intent was owned by. */
  readonly owner: TaskStatusIntentOwner
}

export interface UseTaskStatusIntentOptions {
  readonly owner: TaskStatusIntentOwner
  readonly currentTask: Task | null
  readonly mutateOnce: (intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>
  readonly newKey: (kind: TaskStatusIntentKind) => string
  readonly reconcile: (intent: TaskStatusIntent) => void | Promise<void>
  readonly isCurrent: (owner: TaskStatusIntentOwner) => boolean
}

export interface UseTaskStatusIntentResult {
  readonly phase: TaskStatusIntentPhase
  readonly pending: boolean
  /** True only while a frozen intent is waiting for an explicit same-intent retry. */
  readonly retryable: boolean
  readonly canMarkInProgress: boolean
  readonly undoOffer: TaskStatusUndoOffer | null
  readonly markInProgress: () => void
  readonly undoLast: () => void
  readonly retry: () => void
}

const OPEN: TaskStatus = 'open'
const IN_PROGRESS: TaskStatus = 'started'

/** UI4: an opaque per-attempt token carrying the lifetime it was born in. */
interface AttemptSlot {
  readonly lifetime: number
}

/**
 * UIC-F2: everything the owner can see is published as ONE state value stamped
 * with the lifetime it describes. A new object is always stored, so React can
 * never bail out of a transition such as pending -> pending in a NEW lifetime,
 * and an update from a dead lifetime is dropped instead of leaking across.
 */
interface PublishedState {
  readonly lifetime: number
  readonly owner: TaskStatusIntentOwner
  readonly phase: TaskStatusIntentPhase
  readonly pending: boolean
  readonly retryable: boolean
  readonly undo: TaskStatusUndoOffer | null
}

const IDLE_PUBLICATION = {
  phase: 'idle' as TaskStatusIntentPhase,
  pending: false,
  retryable: false,
  undo: null,
}

function freshPublication(lifetime: number, owner: TaskStatusIntentOwner): PublishedState {
  return { lifetime, owner: Object.freeze({ ...owner }), ...IDLE_PUBLICATION }
}

type Publish = (lifetime: number, patch: Partial<Omit<PublishedState, 'lifetime' | 'owner'>>) => void

function sameOwner(left: TaskStatusIntentOwner, right: TaskStatusIntentOwner) {
  return (
    left.workspaceUid === right.workspaceUid &&
    left.ownerEpoch === right.ownerEpoch &&
    left.taskId === right.taskId &&
    left.taskUid === right.taskUid
  )
}

function freezeIntent(intent: TaskStatusIntent): TaskStatusIntent {
  return Object.freeze({
    ...intent,
    owner: Object.freeze({ ...intent.owner }),
    body: Object.freeze({ ...intent.body }),
  })
}

/** UI1: a legal Task revision is a non-negative safe integer, never a float. */
function isLegalRevision(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isMember(values: readonly string[], value: unknown) {
  return typeof value === 'string' && values.includes(value)
}

function isStringArray(value: unknown) {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * UIC-F1: the declared Subtask domain - required id/title, and priority/status
 * from the declared enums when present. Unknown extra keys stay opaque.
 */
function isSubtaskElement(value: unknown) {
  const item = asRecord(value)
  if (!item) return false
  if (typeof item.id !== 'string' || typeof item.title !== 'string') return false
  if (item.priority !== undefined && !isMember(TASK_PRIORITIES, item.priority)) return false
  return item.status === undefined || isMember(TASK_STATUSES, item.status)
}

/** UIC-F1: a Note carries required text and an optional string date. */
function isNoteElement(value: unknown) {
  const item = asRecord(value)
  if (!item) return false
  if (typeof item.text !== 'string') return false
  return item.date === undefined || typeof item.date === 'string'
}

/** UIC-F1: a key-result reference carries two non-blank scoped identities. */
function isKeyResultRefElement(value: unknown) {
  const item = asRecord(value)
  if (!item || Object.keys(item).length !== 2 ||
      !Object.hasOwn(item, 'objective_id') || !Object.hasOwn(item, 'key_result_id')) return false
  return [item.objective_id, item.key_result_id].every(
    (id) => typeof id === 'string' && id.trim().length > 0,
  )
}

/**
 * UIC-F1: estimate_minutes is optional and nullable, and when it is a number the
 * declared domain is a whole 1..1440. NaN, 1.5 and 1441 are outside it.
 */
function isLegalEstimate(value: unknown) {
  if (value === undefined || value === null) return true
  return Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 1440
}

/**
 * UI1: the injected response is runtime data, not a compile-time promise. The
 * receipt must actually carry the declared REQUIRED Task fields; a four-field
 * stand-in is not a Task.
 */
function hasRequiredTaskFields(task: Record<string, unknown>) {
  const strings = ['id', 'uid', 'title', 'detail']
  const nullableStrings = ['due', 'parent_id']
  const stringArrays = ['tags', 'objective_ids', 'dependencies']
  return (
    strings.every((field) => typeof task[field] === 'string') &&
    nullableStrings.every((field) => task[field] === null || typeof task[field] === 'string') &&
    // UIC-F1: enums are enums, and the collections are checked element by
    // element. No value is defaulted, coerced or synthesised from a missing one.
    isMember(TASK_STATUSES, task.status) &&
    isMember(TASK_PRIORITIES, task.priority) &&
    stringArrays.every((field) => isStringArray(task[field])) &&
    Array.isArray(task.subtasks) &&
    task.subtasks.every(isSubtaskElement) &&
    Array.isArray(task.notes) &&
    task.notes.every(isNoteElement) &&
    isLegalRevision(task.revision) &&
    isLegalRevision(task.context_count)
  )
}

/**
 * UI1: created, updated_at, scheduled, estimate_minutes and key_result_refs are
 * DECLARED OPTIONAL. They are checked only when present; their absence is not a
 * defect and no creation-time schema is invented here.
 */
function hasWellFormedOptionalTaskFields(task: Record<string, unknown>) {
  const optionalStrings = ['created', 'updated_at']
  const optionalNullableStrings = ['scheduled']
  return (
    optionalStrings.every((field) => task[field] === undefined || typeof task[field] === 'string') &&
    optionalNullableStrings.every(
      (field) =>
        task[field] === undefined || task[field] === null || typeof task[field] === 'string',
    ) &&
    isLegalEstimate(task.estimate_minutes) &&
    (task.key_result_refs === undefined ||
      (Array.isArray(task.key_result_refs) && task.key_result_refs.every(isKeyResultRefElement)))
  )
}

function isTaskReceiptBody(value: unknown): value is Task {
  if (!value || typeof value !== 'object') return false
  const task = value as Record<string, unknown>
  return hasRequiredTaskFields(task) && hasWellFormedOptionalTaskFields(task)
}

/**
 * UI1/UI2: how the receipt revision relates to the revision the caller froze.
 * A changing write advances exactly once; an unchanged revision is the
 * receipt-only no-op. Anything else does not answer this request.
 */
type ReceiptRelation = 'advanced' | 'unchanged' | 'unrelated'

function receiptRelation(intent: TaskStatusIntent, revision: number): ReceiptRelation {
  if (revision === intent.expectedRevision + 1) return 'advanced'
  if (revision === intent.expectedRevision) return 'unchanged'
  return 'unrelated'
}

/**
 * A receipt is only success when it answers the frozen intent: the exact
 * envelope and status, boolean replay metadata, a real Task with the frozen
 * identity and requested status, and a revision related to the frozen one.
 * Anything malformed or contradictory stays UNKNOWN.
 */
function isSettledReceipt(intent: TaskStatusIntent, response: TaskStatusIntentResponse | null) {
  if (!response || response.status !== 200) return false
  const body = response.body as { data?: unknown; meta?: { replayed?: unknown } } | undefined
  if (!body || !body.meta || typeof body.meta.replayed !== 'boolean') return false
  if (!isTaskReceiptBody(body.data)) return false
  const data = body.data
  if (data.id !== intent.taskId || data.uid !== intent.taskUid) return false
  if (data.status !== intent.requestedStatus) return false
  return receiptRelation(intent, data.revision) !== 'unrelated'
}

/**
 * UI2: reversibility is decided by the RECEIPT, not by the intent's own
 * prior/requested statuses. Only a forward whose receipt actually advanced the
 * Task revision is an actual change.
 */
function isReversibleReceipt(intent: TaskStatusIntent, revision: number) {
  return (
    intent.kind === 'forward' &&
    intent.requestedStatus !== intent.priorStatus &&
    receiptRelation(intent, revision) === 'advanced'
  )
}

type IntentOutcome = 'settled' | 'conflict' | 'unknown'

/** One place decides what a completion means for the frozen intent. */
function classifyOutcome(
  intent: TaskStatusIntent,
  response: TaskStatusIntentResponse | null,
  failed: boolean,
): IntentOutcome {
  if (isSettledReceipt(intent, response)) return 'settled'
  if (!failed && response && response.status === 409) return 'conflict'
  return 'unknown'
}

/**
 * UI3: the authoritative currentTask is chronology too. A receipt that is not
 * newer than what the caller already holds for the SAME Task is historical
 * evidence: it may neither install older state nor open a NEW Undo.
 */
function isHistoricalReceipt(
  intent: TaskStatusIntent,
  revision: number,
  acceptedRevision: number | null,
  currentTask: Task | null,
) {
  const authoritative =
    currentTask && currentTask.uid === intent.taskUid ? currentTask.revision : null
  return [acceptedRevision, authoritative].some((known) => known !== null && revision <= known)
}

function buildForwardIntent(
  owner: TaskStatusIntentOwner,
  task: Task,
  key: string,
): TaskStatusIntent {
  return freezeIntent({
    kind: 'forward',
    owner,
    taskId: task.id,
    taskUid: task.uid,
    priorStatus: task.status,
    expectedRevision: task.revision,
    requestedStatus: IN_PROGRESS,
    key,
    body: { status: IN_PROGRESS, revision: task.revision },
  })
}

function buildUndoIntent(offer: TaskStatusUndoOffer, key: string): TaskStatusIntent {
  return freezeIntent({
    kind: 'undo',
    owner: offer.owner,
    taskId: offer.owner.taskId,
    taskUid: offer.owner.taskUid,
    priorStatus: IN_PROGRESS,
    // Undo is expressed at the forward receipt revision; it never rebases.
    expectedRevision: offer.atRevision,
    requestedStatus: offer.priorStatus,
    key,
    body: { status: offer.priorStatus, revision: offer.atRevision },
  })
}

/** The forward action is offered only for the authoritative OPEN owned Task. */
function forwardEligible(task: Task | null, owner: TaskStatusIntentOwner): task is Task {
  return !!task && task.status === OPEN && task.id === owner.taskId && task.uid === owner.taskUid
}

/**
 * The synchronous coordination refs. They live in one place so the runner and
 * the exposed actions cannot drift apart: a second click in the same tick must
 * be suppressed before any state update has rendered.
 */
interface IntentRefs {
  pendingRef: MutableRefObject<boolean>
  frozenRef: MutableRefObject<TaskStatusIntent | null>
  undoRef: MutableRefObject<TaskStatusUndoOffer | null>
  /** UI4: identity of the attempt that currently owns the single pending slot. */
  attemptRef: MutableRefObject<AttemptSlot | null>
  /**
   * A -> B -> A returns the same owner VALUE to a different lifetime, so an
   * attempt and an Undo offer carry the lifetime they were born in rather than
   * being compared by owner value alone.
   */
  lifetimeRef: MutableRefObject<number>
  latestRevisionRef: MutableRefObject<number | null>
  aliveRef: MutableRefObject<boolean>
}

/**
 * Exactly ONE attempt. A synchronous throw and a rejected promise are the same
 * ambiguous outcome; neither is retried here.
 */
function runOneAttempt(
  intent: TaskStatusIntent,
  mutateOnce: (intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>,
  done: (response: TaskStatusIntentResponse | null, failed: boolean) => void,
) {
  let call: Promise<TaskStatusIntentResponse>
  try {
    call = mutateOnce(intent)
  } catch {
    done(null, true)
    return
  }
  call.then((response) => done(response, false), () => done(null, true))
}

/** The single pending slot is released as one unit, never field by field. */
function releaseSlot(refs: IntentRefs) {
  refs.pendingRef.current = false
  refs.frozenRef.current = null
  refs.attemptRef.current = null
}

function useIntentRefs(): IntentRefs {
  return {
    pendingRef: useRef(false),
    frozenRef: useRef<TaskStatusIntent | null>(null),
    undoRef: useRef<TaskStatusUndoOffer | null>(null),
    attemptRef: useRef<AttemptSlot | null>(null),
    lifetimeRef: useRef(0),
    latestRevisionRef: useRef<number | null>(null),
    aliveRef: useRef(true),
  }
}

/** Everything that happens between an explicit action and its completion. */
function useIntentRunner(
  refs: IntentRefs,
  optionsRef: MutableRefObject<UseTaskStatusIntentOptions>,
  publish: Publish,
) {
  const { pendingRef, frozenRef, undoRef, attemptRef, lifetimeRef, latestRevisionRef, aliveRef } =
    refs

  const forgetIntent = useCallback(() => releaseSlot(refs), [refs])

  const dropUndo = useCallback(() => {
    // The offer is both a synchronous fact and a published one; releasing it
    // has to reach the owner's rendered state, not just the ref.
    undoRef.current = null
    publish(lifetimeRef.current, { undo: null })
  }, [undoRef, lifetimeRef, publish])

  const acceptReceipt = useCallback(
    (intent: TaskStatusIntent, response: TaskStatusIntentResponse, lifetime: number) => {
      const revision = response.body.data.revision
      if (
        isHistoricalReceipt(
          intent,
          revision,
          latestRevisionRef.current,
          optionsRef.current.currentTask,
        )
      ) {
        // Never roll newer state backward and never resurrect stale Undo.
        publish(lifetime, IDLE_PUBLICATION)
        return
      }
      latestRevisionRef.current = revision
      if (!isReversibleReceipt(intent, revision)) {
        // A no-op receipt is not a reversible change; an Undo receipt closes
        // its own offer rather than opening another one.
        dropUndo()
        publish(lifetime, IDLE_PUBLICATION)
        return
      }
      const offer: TaskStatusUndoOffer = Object.freeze({
        priorStatus: intent.priorStatus,
        atRevision: revision,
        owner: intent.owner,
      })
      undoRef.current = offer
      publish(lifetime, { ...IDLE_PUBLICATION, undo: offer })
    },
    [dropUndo, publish],
  )

  const settle = useCallback(
    (
      intent: TaskStatusIntent,
      attempt: AttemptSlot,
      response: TaskStatusIntentResponse | null,
      failed: boolean,
    ) => {
      // UI4: a completion may only touch the slot and lifetime it was
      // dispatched into. An obsolete one changes NOTHING - not pending, not the
      // frozen intent, not the phase, not the Undo offer, and it never
      // reconciles - so it can neither clear a newer attempt nor resurrect
      // state under a repeated owner value.
      if (!aliveRef.current) return
      if (attemptRef.current !== attempt || attempt.lifetime !== lifetimeRef.current) return
      if (!optionsRef.current.isCurrent(intent.owner)) {
        // Still our slot, but no longer the current authority: release it only.
        forgetIntent()
        publish(attempt.lifetime, { phase: 'idle', pending: false, retryable: false })
        return
      }
      const outcome = classifyOutcome(intent, response, failed)
      if (outcome === 'settled') {
        forgetIntent()
        acceptReceipt(intent, response as TaskStatusIntentResponse, attempt.lifetime)
        return
      }
      if (outcome === 'conflict') {
        // Determinate conflict: display reconciliation only, never a second write.
        forgetIntent()
        dropUndo()
        publish(attempt.lifetime, { ...IDLE_PUBLICATION, phase: 'conflict' })
        void optionsRef.current.reconcile(intent)
        return
      }
      // Missing, malformed or contradictory success after a possible send is
      // UNKNOWN. The frozen body and key survive for explicit retry.
      pendingRef.current = false
      attemptRef.current = null
      frozenRef.current = intent
      publish(attempt.lifetime, { phase: 'unknown', pending: false, retryable: true })
    },
    [acceptReceipt, dropUndo, forgetIntent, publish],
  )

  const dispatch = useCallback(
    (intent: TaskStatusIntent) => {
      // Suppress a duplicate pending dispatch synchronously.
      if (pendingRef.current) return
      // UI4: the attempt identity and the lifetime are captured HERE, at
      // dispatch, not when a receipt is later accepted.
      const attempt: AttemptSlot = { lifetime: lifetimeRef.current }
      attemptRef.current = attempt
      pendingRef.current = true
      frozenRef.current = intent
      publish(attempt.lifetime, { phase: 'pending', pending: true, retryable: false })
      // ONE attempt, started synchronously with the action so a second click in
      // the same tick can find the pending flag already raised.
      runOneAttempt(intent, optionsRef.current.mutateOnce, (response, failed) =>
        settle(intent, attempt, response, failed),
      )
    },
    [settle, publish],
  )

  return { forgetIntent, dropUndo, dispatch }
}

/** Commit and explicit actions share an idempotent owner transition. */
function useOwnerPublication(options: UseTaskStatusIntentOptions, refs: IntentRefs) {
  const [published, setPublished] = useState(() => freshPublication(0, options.owner))
  const boundOwnerRef = useRef(published.owner)
  const optionsRef = useRef(options)
  const renderCommit = { committed: false }
  const publish = useCallback<Publish>((lifetime, patch) => {
    setPublished((previous) => (previous.lifetime === lifetime ? { ...previous, ...patch } : previous))
  }, [])

  const commitOptions = useCallback((live: UseTaskStatusIntentOptions) => {
    optionsRef.current = live
    if (sameOwner(boundOwnerRef.current, live.owner)) return
    boundOwnerRef.current = Object.freeze({ ...live.owner })
    refs.lifetimeRef.current += 1
    releaseSlot(refs)
    refs.undoRef.current = null
    refs.latestRevisionRef.current = null
    setPublished(freshPublication(refs.lifetimeRef.current, live.owner))
  }, [refs])

  useLayoutEffect(() => {
    renderCommit.committed = true
    commitOptions(options)
  }, [commitOptions, options, renderCommit])

  const activate = useCallback(() => {
    // A retained action from a committed render uses the latest callbacks for
    // its owner. A new commit callback supplies its new options before the
    // parent effect has had a chance to publish them to optionsRef.
    const live = renderCommit.committed ? optionsRef.current : options
    if (!refs.aliveRef.current || !sameOwner(live.owner, options.owner) ||
        !live.isCurrent(live.owner)) return null
    // A child's commit callback can run BEFORE this hook's layout effect.
    // Establish its owner before checking/claiming the slot. The later layout
    // effect sees the same owner and cannot erase this newly started attempt.
    commitOptions(live)
    renderCommit.committed = true
    return live
  }, [commitOptions, options, refs.aliveRef, renderCommit])

  // Derive the first new-owner publication without mutating ownership in render.
  // The commit/action transition will install that owner's stamped state.
  const visible = sameOwner(published.owner, options.owner) ? published : IDLE_PUBLICATION
  return { published: visible, publish, optionsRef, activate }
}

export function useTaskStatusIntent(
  options: UseTaskStatusIntentOptions,
): UseTaskStatusIntentResult {
  const { owner, currentTask } = options
  const refs = useIntentRefs()
  const { pendingRef, frozenRef, undoRef, lifetimeRef, aliveRef } = refs
  const { published, publish, optionsRef, activate } = useOwnerPublication(options, refs)
  const { forgetIntent, dropUndo, dispatch } = useIntentRunner(refs, optionsRef, publish)

  const canMarkInProgress =
    forwardEligible(currentTask, owner) && !published.pending && published.phase !== 'unknown'

  const markInProgress = useCallback(() => {
    const live = activate()
    if (!live) return
    if (!forwardEligible(live.currentTask, live.owner)) return
    if (pendingRef.current || frozenRef.current) return
    dispatch(buildForwardIntent(live.owner, live.currentTask, live.newKey('forward')))
  }, [activate, dispatch])

  const undoLast = useCallback(() => {
    const live = activate()
    if (!live) {
      const stale = undoRef.current
      if (stale && !optionsRef.current.isCurrent(stale.owner)) dropUndo()
      return
    }
    const offer = undoRef.current
    if (!offer || pendingRef.current || frozenRef.current) return
    if (!sameOwner(offer.owner, live.owner) || !live.isCurrent(offer.owner)) {
      dropUndo()
      return
    }
    dispatch(buildUndoIntent(offer, live.newKey('undo')))
  }, [activate, dispatch, dropUndo])

  const retry = useCallback(() => {
    if (!activate()) return
    const frozen = frozenRef.current
    if (!frozen || pendingRef.current) return
    if (!optionsRef.current.isCurrent(frozen.owner)) {
      forgetIntent()
      publish(lifetimeRef.current, IDLE_PUBLICATION)
      return
    }
    // Exactly the same frozen body and key; no new key from a status coincidence.
    dispatch(frozen)
  }, [activate, dispatch, forgetIntent, publish, lifetimeRef])

  const ownedUndo =
    published.undo && sameOwner(published.undo.owner, owner) ? published.undo : null

  useUnmountFlag(aliveRef)

  return {
    phase: published.phase,
    pending: published.pending,
    retryable: published.retryable,
    canMarkInProgress,
    undoOffer: ownedUndo,
    markInProgress,
    undoLast,
    retry,
  }
}

/**
 * StrictMode mounts, unmounts and mounts again. Liveness is re-armed on each
 * mount rather than latched off by the first simulated unmount, so a real
 * unmount still silences a late completion.
 */
function useUnmountFlag(aliveRef: { current: boolean }) {
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [aliveRef])
}
