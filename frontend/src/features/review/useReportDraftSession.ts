import { useCallback, useEffect, useReducer, useRef, type Dispatch } from 'react'

import { loadErrorMessage } from './reportDraftMessages'
import {
  INITIAL_DRAFT_STATE,
  baseFromSource,
  coordinateKey,
  isDirty,
  reportDraftReducer,
  type DailyReportDraftSource,
  type DraftAction,
  type DraftSessionState,
} from './reportDraftEditorModel'
import type { ReportDraftCoordinate, ReportDraftStore } from './reportDraftStorage'

/**
 * One editing session: which coordinate is pinned, what was loaded for it, and
 * which asynchronous results are still allowed to speak.
 *
 * A result belonging to a coordinate the editor has left, or to an unmounted
 * editor, is dropped. Starting a session also releases the previous one's
 * operation state, or a save left in flight would leave the new coordinate
 * permanently disabled and impossible to close.
 */

export interface OwnerGuard {
  owner: { current: number }
  owned: <T>(token: number, apply: (value: T) => void) => (value: T) => void
}

export interface DraftSession {
  dirty: boolean
  dispatch: Dispatch<DraftAction>
  guard: OwnerGuard
  pinned: ReportDraftCoordinate
  state: DraftSessionState
}

/** A token per session, and a guard that drops results from any other one. */
export function useOwnerGuard(): OwnerGuard {
  const owner = useRef(0)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      owner.current += 1
    }
  }, [])
  const owned = useCallback(
    <T,>(token: number, apply: (value: T) => void) => (value: T) => {
      if (!alive.current || owner.current !== token) return
      apply(value)
    },
    [],
  )
  return { owned, owner }
}

/** Warn on unload only while something on screen is not persisted. */
function useUnloadGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
}

export function useReportDraftSession({
  coordinate,
  source,
  store,
}: {
  coordinate: ReportDraftCoordinate
  source: DailyReportDraftSource
  store: ReportDraftStore
}): DraftSession {
  const [state, dispatch] = useReducer(reportDraftReducer, INITIAL_DRAFT_STATE)
  const guard = useOwnerGuard()

  // Read through refs so the load effect can key on the coordinate ALONE: a
  // regenerated `source` for the same report must not restart the session.
  const pinned = useRef(coordinate)
  const latest = useRef({ coordinate, source })
  latest.current = { coordinate, source }
  const key = coordinateKey(coordinate)
  const { owned, owner } = guard

  useEffect(() => {
    owner.current += 1
    const token = owner.current
    const opening = latest.current.source
    const base = baseFromSource(opening)
    pinned.current = latest.current.coordinate
    dispatch({ type: 'session-start' })
    const fallback = (notice: string) =>
      dispatch({
        type: 'loaded',
        base,
        localRevision: null,
        markdown: opening.markdown,
        notice,
      })
    void store.load(pinned.current).then(
      owned(token, (result: Awaited<ReturnType<ReportDraftStore['load']>>) => {
        if (!result.ok) return fallback(loadErrorMessage(result.code))
        const saved = result.value
        if (saved === null) {
          return dispatch({ type: 'loaded', base, localRevision: null, markdown: opening.markdown })
        }
        // The saved draft keeps its own base identity; a newer generated report
        // is reported by the view, never substituted for edited text.
        return dispatch({
          type: 'loaded',
          base: {
            sourceDigest: saved.sourceDigest,
            baseGeneratedAt: saved.baseGeneratedAt,
            baseMarkdown: saved.baseMarkdown,
          },
          localRevision: saved.localRevision,
          markdown: saved.markdown,
        })
      }),
      owned(token, () => fallback(loadErrorMessage('storage_unavailable'))),
    )
  }, [key, owned, owner, store])

  const dirty = isDirty(state)
  useUnloadGuard(dirty)
  return { dirty, dispatch, guard, pinned: pinned.current, state }
}
