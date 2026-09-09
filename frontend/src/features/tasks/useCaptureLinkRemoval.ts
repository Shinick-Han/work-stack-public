import { useEffect, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import {
  classifyCaptureUnlinkFailure,
  unlinkCaptureTask,
  type CaptureUnlinkOutcome,
} from '../../api/captureLinks'
import { classifyCaptureUndoUnlinkFailure, undoCaptureUnlink } from '../../api/captureLinkUndo'
import { createIdempotencyKey } from '../../api/transport'
import type {
  TaskContextLinkRemovalState,
  TaskContextUndoState,
} from '../../components/TaskContextTimeline'
import type { CaptureLinkRemovalTarget } from '../../utils/taskContext'

/**
 * Lifecycle for one explicit `Remove task link` and, since R33, the one bounded Undo of
 * the removal it just confirmed.
 *
 * The rules this hook exists to keep, all of them from the R22 and R33 UI contracts:
 *
 * - **Nothing is requested on render, mount, effect or reopen.** `onRemove` and `onUndo`
 *   are the only things that reach the network, and only a click calls them.
 * - **The lock is taken synchronously.** Both handlers claim `pendingRef` before their
 *   first `await`, so a double click on one card — or a click on a second card, or on
 *   Undo, while the first is in flight — is one POST, not two writes against one revision.
 * - **The displayed revision is what is sent.** The removal target comes from the card the
 *   reader is looking at; the Undo carries the post-unlink revision that same removal
 *   answered with. Neither is re-read, and there is no fetch-and-retry with a newer one.
 * - **A new scope never inherits an old attempt.** The view identity is the workspace UID,
 *   the Task UID and the Task id together, recomputed during render, so a replacement Task
 *   that reuses a display id cannot inherit an offer, a key or a message.
 * - **Late answers are dropped, including ABA.** Each view holds a fresh token object.
 *   A→B→A replaces the token twice, so an answer minted under the first A cannot land on
 *   the second A. Unmount replaces it too, which also covers StrictMode's remount.
 * - **Nothing is removed or restored optimistically.** A known success only invalidates
 *   the existing planning queries; the card leaves, and comes back, when the refreshed
 *   Task says so. An unknown outcome shows no change at all.
 * - **An ambiguous intent keeps its identity.** The idempotency key of an attempt that
 *   ended unknown is held for exactly that operation, and the retry resends the same body
 *   under the same key. A determinate answer drops it, and so does a view change.
 * - **Only the current attempt may write that identity.** Dropping a stale answer is not
 *   just about what renders: a completion minted under a replaced view must not clear the
 *   key the live view is holding, nor put its own key back in place of it. Every branch
 *   therefore tests the attempt identity *before* touching retained intent.
 * - **The Undo is a separate intent under its own new key.** It is never derived from the
 *   unlink key, never sent automatically, and never offered from a response that did not
 *   actually confirm a mutation of the Capture the reader acted on.
 * - **One click is one POST, and `restored` is a recognised answer.** The Undo route sends
 *   a single attempt, so only `Try Undo again` produces a second request; and it settles
 *   `restored` only for a 200 that carries this Capture, at the sent revision plus one,
 *   with this Task linked again. Anything else is the same unknown outcome as a lost
 *   reply: the intent and its key are held, and nothing is announced as restored.
 */

interface AmbiguousIntent {
  taskId: string
  captureId: string
  revision: number
  key: string
}

/** The one Undo this view may offer, with the identity the request needs. */
interface UndoIntent {
  taskId: string
  captureId: string
  receiptId: string
  /** The post-unlink revision the confirmed removal returned. */
  revision: number
  /** Minted once when the offer appears, so a same-key retry stays one intent. */
  key: string
}

interface ViewState {
  key: string
  pending: TaskContextLinkRemovalState['pending']
  failure: TaskContextLinkRemovalState['failure']
  undo: TaskContextUndoState | null
}

/**
 * Production identity for one Task-context view.
 *
 * The display id alone is not an identity: the same `T-0001` can name a different Task in
 * another workspace, or a replacement of the one the reader acted on. The full triple is
 * what an offer, a retained key and a pending row are scoped to.
 */
export interface CaptureLinkRemovalScope {
  workspaceUid: string
  taskUid: string
  taskId: string
}

/**
 * A bare Task id stays accepted for callers that have no fuller identity to give — the
 * pre-R33 signature. It is a compatibility default, not the production one: the drawer
 * passes the full scope, so a workspace or Task UID change is a real view change there.
 */
function normalizeScope(scope: string | CaptureLinkRemovalScope): CaptureLinkRemovalScope {
  return typeof scope === 'string' ? { workspaceUid: '', taskUid: '', taskId: scope } : scope
}

/**
 * A NUL separator, so no two distinct triples can encode alike. A printable
 * delimiter would be a boundary an id could carry itself, and that would let two
 * different Tasks share one offer — the exact confusion this identity prevents.
 */
function scopeKey(scope: CaptureLinkRemovalScope): string {
  return [scope.workspaceUid, scope.taskUid, scope.taskId].join('\u0000')
}

function idleView(key: string): ViewState {
  return { key, pending: null, failure: null, undo: null }
}

function sameIntent(intent: AmbiguousIntent | null, taskId: string, target: CaptureLinkRemovalTarget) {
  return intent !== null
    && intent.taskId === taskId
    && intent.captureId === target.captureId
    && intent.revision === target.revision
}

/**
 * Whether a settled removal may be offered as an Undo, and under what identity.
 *
 * Every clause is a refusal the contract asks for by name:
 *
 * - a duplicate changed nothing, so there is nothing to invert;
 * - a legacy owner returns no receipt, and a hint that did not parse as a canonical
 *   receipt id is not one — neither is a dead end this surface may offer a control for;
 * - the answer must be about the Capture the reader acted on, at exactly one revision
 *   past the one that was sent. Anything else is a response whose post-image this client
 *   cannot match to the removal it just made, and an Undo built on it would send a
 *   revision the owner never recorded.
 *
 * The key is minted here, once: the retry of an unknown Undo must be the same intent.
 */
function undoOfferFor(
  taskId: string,
  target: CaptureLinkRemovalTarget,
  outcome: CaptureUnlinkOutcome,
): UndoIntent | null {
  if (outcome.duplicate) return null
  const receiptId = outcome.undoReceiptId
  if (receiptId === undefined) return null
  if (outcome.capture.id !== target.captureId) return null
  const revision = outcome.capture.revision
  if (!Number.isSafeInteger(revision) || revision !== target.revision + 1) return null
  return { taskId, captureId: target.captureId, receiptId, revision, key: createIdempotencyKey() }
}

/**
 * The existing planning invalidation, and it stays unconditional on both routes: the write
 * did happen, so Task detail, context and the workspace counts are stale whichever view
 * is on screen now. It is keyed by the intent's OWN Task, not the current one.
 */
function invalidatePlanning(queryClient: QueryClient, taskId: string) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['task', taskId] }),
    queryClient.invalidateQueries({ queryKey: ['workspace'] }),
  ])
}

/**
 * The mutable identity of ONE Task-context view, in one place.
 *
 * Both requests outlive the render that started them, so their completions cannot close
 * over rendered state. They reach the live refs through this instead, which is exactly
 * what lets a stale answer discover that it no longer owns the view, the retained
 * idempotency key or the Undo offer.
 */
interface RemovalContext {
  key: string
  taskId: string
  queryClient: QueryClient
  setView: Dispatch<SetStateAction<ViewState>>
  tokenRef: RefObject<object>
  pendingRef: RefObject<object | null>
  ambiguousRef: RefObject<AmbiguousIntent | null>
  undoRef: RefObject<UndoIntent | null>
  mountedRef: RefObject<boolean>
}

/**
 * Is this answer still the one the reader is waiting on?
 *
 * The token is replaced on every scope change and on unmount, so A→B→A replaces it twice
 * and an answer minted under the first A can never be current again. Every write that
 * outlives the request — the rendered view, the retained ambiguous intent and the Undo
 * offer alike — goes through this, because an obsolete completion owns none of them.
 */
function isCurrentAttempt(ctx: RemovalContext, token: object): boolean {
  return ctx.mountedRef.current && ctx.tokenRef.current === token
}

/** Drops the lock this attempt holds, then reports whether it may still write anything. */
function release(ctx: RemovalContext, token: object): boolean {
  if (ctx.pendingRef.current === token) ctx.pendingRef.current = null
  return isCurrentAttempt(ctx, token)
}

function settle(
  ctx: RemovalContext,
  token: object,
  failure: ViewState['failure'],
  undo: TaskContextUndoState | null,
): void {
  if (!release(ctx, token)) return
  ctx.setView((current) => (
    current.key === ctx.key ? { key: current.key, pending: null, failure, undo } : current
  ))
}

function settleUndo(ctx: RemovalContext, token: object, undo: TaskContextUndoState): void {
  if (!release(ctx, token)) return
  // Only an unknown outcome keeps the intent alive, and only for its same-key retry.
  if (undo.phase !== 'unknown') ctx.undoRef.current = null
  ctx.setView((current) => (
    current.key === ctx.key ? { key: current.key, pending: null, failure: null, undo } : current
  ))
}

/** One explicit `Remove task link`, and the offer its confirmed answer may earn. */
function startRemoval(ctx: RemovalContext, target: CaptureLinkRemovalTarget): void {
  const token = ctx.tokenRef.current
  const held = ctx.ambiguousRef.current
  const retry = sameIntent(held, ctx.taskId, target)
  const intent: AmbiguousIntent = retry && held
    ? held
    : { taskId: ctx.taskId, captureId: target.captureId, revision: target.revision, key: createIdempotencyKey() }
  ctx.pendingRef.current = token
  // A later explicit removal replaces any earlier settled offer: this view carries the
  // feedback of one removal, never a history of them.
  ctx.undoRef.current = null
  ctx.setView({
    key: ctx.key,
    pending: { captureId: target.captureId, revision: target.revision, retry },
    failure: null,
    undo: null,
  })
  void unlinkCaptureTask({
    captureId: target.captureId,
    taskId: ctx.taskId,
    revision: target.revision,
    idempotencyKey: intent.key,
  }).then(
    async (outcome) => {
      // The server settled it, so the intent is spent whether it removed the link or
      // reported it already absent — but it is only *this* attempt's intent that is
      // spent. An answer belonging to a replaced view must leave the key the live view
      // is holding for `Try again` exactly where it is; clearing it would silently mint
      // a new intent for a removal whose outcome is still unknown. The same test gates
      // the offer: a dead view's confirmed removal may not put an Undo on screen.
      const offer = undoOfferFor(intent.taskId, target, outcome)
      if (isCurrentAttempt(ctx, token)) {
        ctx.ambiguousRef.current = null
        ctx.undoRef.current = offer
      }
      // The refresh is the existing planning invalidation and stays unconditional: the
      // write did happen, so the original Task and the counts are stale either way.
      try {
        await invalidatePlanning(ctx.queryClient, intent.taskId)
      } finally {
        settle(ctx, token, null, offer && { captureId: offer.captureId, phase: 'offer', message: null })
      }
    },
    (error: unknown) => {
      const classified = classifyCaptureUnlinkFailure(error)
      const ambiguous = classified.kind === 'ambiguous'
      // Same identity test on the failing side: a stale ambiguity must not reinstate its
      // own older key over the one the current view retained, and a stale refusal must
      // not drop that key either.
      if (isCurrentAttempt(ctx, token)) ctx.ambiguousRef.current = ambiguous ? intent : null
      settle(ctx, token, {
        captureId: target.captureId,
        revision: target.revision,
        message: classified.message,
        retry: ambiguous,
      }, null)
    },
  )
}

/** One explicit Undo of the removal this view just confirmed, under its own key. */
function startUndo(ctx: RemovalContext, intent: UndoIntent): void {
  const token = ctx.tokenRef.current
  ctx.pendingRef.current = token
  ctx.setView({
    key: ctx.key,
    pending: null,
    failure: null,
    undo: { captureId: intent.captureId, phase: 'pending', message: null },
  })
  void undoCaptureUnlink({
    captureId: intent.captureId,
    receiptId: intent.receiptId,
    revision: intent.revision,
    // Recognition only, never a body field: the answer has to be about THIS Task before
    // this view is allowed to say the link came back.
    taskId: intent.taskId,
    idempotencyKey: intent.key,
  }).then(
    async () => {
      try {
        await invalidatePlanning(ctx.queryClient, intent.taskId)
      } finally {
        settleUndo(ctx, token, { captureId: intent.captureId, phase: 'restored', message: null })
      }
    },
    (error: unknown) => {
      const classified = classifyCaptureUndoUnlinkFailure(error)
      settleUndo(ctx, token, {
        captureId: intent.captureId,
        phase: classified.kind === 'ambiguous' ? 'unknown' : 'failed',
        message: classified.message,
      })
    },
  )
}

export function useCaptureLinkRemoval(
  scope: string | CaptureLinkRemovalScope,
): TaskContextLinkRemovalState {
  const identity = normalizeScope(scope)
  const taskId = identity.taskId
  const key = scopeKey(identity)
  const queryClient = useQueryClient()
  const [view, setView] = useState<ViewState>(() => idleView(key))
  const tokenRef = useRef<object>({})
  const pendingRef = useRef<object | null>(null)
  const ambiguousRef = useRef<AmbiguousIntent | null>(null)
  const undoRef = useRef<UndoIntent | null>(null)
  const mountedRef = useRef(true)

  if (view.key !== key) {
    // Adjusting state during render: the new scope must never be shown for one commit
    // under the previous one's pending row, message or Undo offer. Replacing the token
    // here is what makes an in-flight answer stale rather than merely late.
    tokenRef.current = {}
    pendingRef.current = null
    ambiguousRef.current = null
    undoRef.current = null
    setView(idleView(key))
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      tokenRef.current = {}
      pendingRef.current = null
      undoRef.current = null
    }
  }, [])

  const ctx: RemovalContext = {
    key, taskId, queryClient, setView,
    tokenRef, pendingRef, ambiguousRef, undoRef, mountedRef,
  }

  // An unknown Undo is not settled: it holds the lock until the reader retries it or
  // dismisses it, so no card may start another write on top of an outcome nobody knows.
  const undoPhase = view.undo?.phase
  const undoBlocking = undoPhase === 'pending' || undoPhase === 'unknown'

  const onRemove = (target: CaptureLinkRemovalTarget) => {
    // The lock is claimed synchronously, before the first await inside the request.
    if (pendingRef.current !== null || undoBlocking) return
    startRemoval(ctx, target)
  }

  const onUndo = () => {
    const intent = undoRef.current
    // Only the two offered phases may send, and only when nothing else is in flight.
    if (intent === null || pendingRef.current !== null) return
    if (undoPhase !== 'offer' && undoPhase !== 'unknown') return
    startUndo(ctx, intent)
  }

  /**
   * Hides settled feedback and nothing else. It sends no request, so it never claims a
   * restoration; dropping the retained intent is what makes the lock honest afterwards.
   */
  const onDismissUndo = () => {
    if (view.undo === null || view.undo.phase === 'pending') return
    undoRef.current = null
    setView((current) => (current.key === key ? { ...current, undo: null } : current))
  }

  return {
    taskId,
    pending: view.pending,
    failure: view.failure,
    undo: view.undo,
    locked: view.pending !== null || undoBlocking,
    onRemove,
    onUndo,
    onDismissUndo,
  }
}
