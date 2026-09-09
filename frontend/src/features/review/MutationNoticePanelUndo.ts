import { useEffect, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import {
  classifyMutationUndoFailure,
  undoMutationNotice,
  type MutationUndoTarget,
} from '../../api/mutationNotices'
import { createIdempotencyKey } from '../../api/transport'
import { MUTATION_NOTICE_COPY } from './MutationNoticePanelModel'
import { useOwnerGeneration } from './DailyReportPreview'

/**
 * Lifecycle for the one bounded Undo the Recent changes panel may offer.
 *
 * It is the R33 removal lifecycle applied to the mutation-notice route, and it keeps the
 * same rules, because they are the ones this route's ambiguity needs:
 *
 * - **Nothing is requested on render, mount or effect.** `onUndo` reaches the network,
 *   and only a click calls it.
 * - **The lock is taken synchronously**, before the first `await` inside the request, so
 *   a double click on one row — or a click on a second row while the first is in flight —
 *   is one POST, not two writes against one revision.
 * - **The displayed revision is what is sent.** The target comes from the row the reader
 *   is looking at; it is never re-read, and there is no fetch-and-retry with a newer one.
 * - **A new owner never inherits an old attempt.** The owner generation is the existing
 *   per-workspace one this page already uses, and it advances both when the workspace
 *   prop changes and when the cached `['workspace']` projection changes underneath. Every
 *   completion tests the owner it was minted under before it writes anything — before the
 *   query invalidation as well as before the feedback, because a refetch aimed at the
 *   replaced workspace's Task lands on the new one's screen just as visibly as a message
 *   would. An answer belonging to a replaced workspace changes nothing at all.
 * - **An ambiguous intent keeps its identity.** The key of an attempt that ended unknown
 *   is held for exactly that notice, and the retry resends the same body under the same
 *   key. A determinate answer drops it, and so does an owner change.
 * - **Nothing is claimed optimistically, and nothing ambiguous is claimed at all.** A
 *   known success only invalidates the existing queries; the list, the Task and the
 *   workspace change when the refreshed projections say so. An unknown outcome announces
 *   no undo and leaves the row exactly as it was.
 */

export type MutationUndoPhase = 'pending' | 'undone' | 'conflict' | 'failed' | 'unknown'

export interface MutationUndoState {
  noticeId: string
  phase: MutationUndoPhase
  message: string | null
}

/** The frozen request one intent owns: never re-derived, never re-keyed on retry. */
interface UndoIntent {
  target: MutationUndoTarget
  /** Minted once per intent, so a same-key retry stays one intent. */
  key: string
  /** Resolved when the click happened, so a stale view cannot redirect the refresh. */
  taskId: string | null
}

export interface MutationUndoModel {
  state: MutationUndoState | null
  /** True while any outcome is in flight or unresolved: no row may start another write. */
  locked: boolean
  onUndo: (target: MutationUndoTarget, taskId: string | null) => void
  onDismiss: () => void
}

/**
 * The existing planning invalidation for this page, plus the notices list the panel reads.
 * It is keyed by the intent's OWN Task, not the current one, and it runs only for an
 * attempt that still owns the panel: invalidating is itself a write onto whichever
 * workspace is on screen now, so the owner gate decides before it, not after.
 */
function invalidateAfterUndo(queryClient: QueryClient, taskId: string | null) {
  const invalidations = [
    queryClient.invalidateQueries({ queryKey: ['workspace'] }),
    queryClient.invalidateQueries({ queryKey: ['review'] }),
    queryClient.invalidateQueries({ queryKey: ['mutation-notices'] }),
  ]
  if (taskId !== null) invalidations.push(queryClient.invalidateQueries({ queryKey: ['task', taskId] }))
  return Promise.all(invalidations)
}

/**
 * The mutable identity of ONE panel, in one place. The request outlives the render that
 * started it, so its completion cannot close over rendered state; it reaches the live refs
 * through this instead, which is what lets a stale answer discover it owns nothing.
 */
interface UndoContext {
  owner: string
  currentOwner: () => string
  queryClient: QueryClient
  setState: Dispatch<SetStateAction<MutationUndoState | null>>
  tokenRef: RefObject<object>
  pendingRef: RefObject<object | null>
  intentRef: RefObject<UndoIntent | null>
  mountedRef: RefObject<boolean>
}

/**
 * Is this answer still the one the reader is waiting on?
 *
 * Two independent tests, because they fail at different moments. The token is replaced
 * during the render that observes a new owner, which covers unmount and an A→B→A return.
 * The frozen owner string is compared against the live one, which covers the window
 * between a cache write advancing the generation and React committing that render — the
 * exact window a late answer from a replaced workspace would otherwise land in.
 */
function isCurrentAttempt(ctx: UndoContext, token: object, owner: string): boolean {
  return ctx.mountedRef.current && ctx.tokenRef.current === token && ctx.currentOwner() === owner
}

/** Drops the lock this attempt holds, then reports whether it may still write anything. */
function release(ctx: UndoContext, token: object, owner: string): boolean {
  if (ctx.pendingRef.current === token) ctx.pendingRef.current = null
  return isCurrentAttempt(ctx, token, owner)
}

function settle(
  ctx: UndoContext,
  token: object,
  owner: string,
  state: MutationUndoState,
): void {
  if (!release(ctx, token, owner)) return
  // Only an unknown outcome keeps the intent alive, and only for its same-key retry.
  if (state.phase !== 'unknown') ctx.intentRef.current = null
  ctx.setState(state)
}

function sameIntent(held: UndoIntent | null, target: MutationUndoTarget): boolean {
  return held !== null
    && held.target.noticeId === target.noticeId
    && held.target.revision === target.revision
    && held.target.restoredStatus === target.restoredStatus
}

function startUndo(ctx: UndoContext, target: MutationUndoTarget, taskId: string | null): void {
  const token = ctx.tokenRef.current
  const owner = ctx.owner
  const held = ctx.intentRef.current
  // A retry of the SAME unresolved notice reuses its key; anything else is a new intent.
  const intent: UndoIntent = sameIntent(held, target) && held
    ? held
    : { target, key: createIdempotencyKey(), taskId }
  ctx.pendingRef.current = token
  ctx.intentRef.current = intent
  ctx.setState({ noticeId: target.noticeId, phase: 'pending', message: null })
  void undoMutationNotice(intent.target, intent.key).then(
    async () => {
      // Ownership is decided BEFORE any success write, and a refetch is a write: an
      // answer minted under workspace A must not invalidate B's task, workspace, review
      // or notices queries, and must not overwrite B's own attempt. It drops its lock
      // and returns having touched nothing.
      if (!isCurrentAttempt(ctx, token, owner)) {
        release(ctx, token, owner)
        return
      }
      try {
        await invalidateAfterUndo(ctx.queryClient, intent.taskId)
      } finally {
        settle(ctx, token, owner, {
          noticeId: intent.target.noticeId,
          phase: 'undone',
          message: MUTATION_NOTICE_COPY.undone,
        })
      }
    },
    (error: unknown) => {
      const classified = classifyMutationUndoFailure(error)
      settle(ctx, token, owner, {
        noticeId: intent.target.noticeId,
        phase: classified.kind === 'ambiguous' ? 'unknown' : classified.kind === 'conflict' ? 'conflict' : 'failed',
        message: classified.message,
      })
    },
  )
}

export function useMutationNoticeUndo(workspaceId: string): MutationUndoModel {
  const queryClient = useQueryClient()
  // The established per-workspace owner generation this page already runs on. The empty
  // date is deliberate: this panel has no day scope, so the workspace is the whole owner.
  const { owner, currentOwner } = useOwnerGeneration(queryClient, workspaceId, '')
  const [state, setState] = useState<MutationUndoState | null>(null)
  const viewOwner = useRef(owner)
  const tokenRef = useRef<object>({})
  const pendingRef = useRef<object | null>(null)
  const intentRef = useRef<UndoIntent | null>(null)
  const mountedRef = useRef(true)

  if (viewOwner.current !== owner) {
    // Adjusting state during render: the new owner must never be shown for one commit
    // under the previous one's pending row, message or retained intent. Replacing the
    // token here is what makes an in-flight answer stale rather than merely late.
    viewOwner.current = owner
    tokenRef.current = {}
    pendingRef.current = null
    intentRef.current = null
    setState(null)
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      tokenRef.current = {}
      pendingRef.current = null
      intentRef.current = null
    }
  }, [])

  const ctx: UndoContext = {
    owner, currentOwner, queryClient, setState,
    tokenRef, pendingRef, intentRef, mountedRef,
  }

  // An unknown outcome is not settled: it holds the lock until the reader retries or
  // dismisses it, so no row may start another write on top of an outcome nobody knows.
  const locked = state?.phase === 'pending' || state?.phase === 'unknown'

  return {
    state,
    locked,
    onUndo: (target: MutationUndoTarget, taskId: string | null) => {
      // The lock is claimed synchronously, before the first await inside the request.
      if (pendingRef.current !== null) return
      // Only the offered row may retry an unknown outcome; a different row must wait.
      if (state?.phase === 'unknown' && state.noticeId !== target.noticeId) return
      startUndo(ctx, target, taskId)
    },
    /**
     * Hides SETTLED feedback and nothing else. It sends no request, so it never claims an
     * undo. An unknown outcome is deliberately not dismissable: dropping its retained key
     * would leave the only way forward a new intent for a change whose outcome nobody
     * knows, which is the one thing this lifecycle exists to prevent. The same-key retry
     * is the way out of that state, exactly as it is for a frozen progress write.
     */
    onDismiss: () => {
      if (state === null || state.phase === 'pending' || state.phase === 'unknown') return
      intentRef.current = null
      setState(null)
    },
  }
}
