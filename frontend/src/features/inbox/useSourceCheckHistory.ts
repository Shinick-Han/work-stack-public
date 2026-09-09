import { useEffect, useRef, useState } from 'react'
import {
  fetchCaptureObservation,
  recordCaptureSourceCheck,
} from '../../api/knowledgeObservation'
import type { Capture } from '../../domain/types'
import { expectedEvidenceOf, isCaptureCheckable } from './useEvidenceVerification'
import {
  applyHistoryLoadFailure,
  applyHistoryLoadStart,
  applyHistoryLoadSuccess,
  applyHistoryRecordFailure,
  applyHistoryRecordStart,
  applyHistoryRecordSuccess,
  idleHistoryView,
  type HistoryViewState,
  type SavedSourceCheck,
  type SourceCheckHistoryMode,
} from './sourceCheckHistoryState'

export type { SavedSourceCheck, SourceCheckHistoryMode }

/**
 * Lifecycle for the saved source check on one open capture.
 *
 * R27-E replaces R21's transient check with a *history*: the owner keeps the last
 * accepted check, this view reads it when the capture is opened, and a click asks for a
 * new one to be recorded. The rules that shape this hook:
 *
 * - **One historical read per view lifecycle, and no other automatic request.** The GET
 *   fires from an effect keyed by the view identity — opening, reopening or switching to
 *   another capture reads once, and nothing polls. This is the one place R27-E updates
 *   R21's "no network at mount", and it updates it for this read only: no render, mount
 *   or reopen path in this file can reach the POST.
 * - **One POST per explicit click.** `pendingRef` is claimed synchronously before the
 *   first await, so a double click is one recorded check and one child process.
 * - **A failed record does not silently try again.** The row observations are cleared, a
 *   closed sentence is shown, and the record action stays disabled until the reader
 *   explicitly reloads the saved check. Nothing here falls back to the old route, and
 *   nothing repeats provider I/O on its own.
 * - **A late read never overwrites a newer answer.** Every request takes the next epoch
 *   before its first await; a response whose epoch is no longer current is dropped. That
 *   is a decision about what this view believes — the owner's work is its own business
 *   and this hook claims nothing about cancelling it.
 * - **A new view never inherits an old capture's history.** The view identity is
 *   recomputed during render and the token object is replaced there, so an answer minted
 *   under a previous capture — or under the first A of an A→B→A swap — cannot land.
 *
 * Nothing here writes to the Capture, and a saved status is never promoted into a
 * verified field.
 */

export interface SourceCheckHistoryState {
  mode: SourceCheckHistoryMode
  /** A history read is open. */
  loading: boolean
  /** A record check is open. */
  pending: boolean
  saved: SavedSourceCheck | null
  /** Closed copy for a failed read or record. Never a backend-authored sentence. */
  error: string | null
  /** An explicit reload has to succeed before another check can be recorded. */
  needsReload: boolean
  recordCheck: () => void
  reload: () => void
}

function observationRequest(capture: Capture, workspaceUid: string) {
  return {
    workspaceUid,
    captureId: capture.id,
    captureRevision: capture.revision,
    expectedEvidence: expectedEvidenceOf(capture),
  }
}

function abandonInFlight(
  tokenRef: { current: object },
  pendingRef: { current: object | null },
  epochRef: { current: number },
) {
  tokenRef.current = {}
  pendingRef.current = null
  epochRef.current += 1
}

export function useSourceCheckHistory(
  capture: Capture,
  workspaceUid?: string,
): SourceCheckHistoryState {
  const enabled = isCaptureCheckable(capture, workspaceUid)
  const viewKey = `${workspaceUid ?? ''}|${capture.id}|${capture.revision}`
  const [view, setView] = useState<HistoryViewState>(() => idleHistoryView(viewKey, enabled))
  const tokenRef = useRef<object>({})
  const pendingRef = useRef<object | null>(null)
  const epochRef = useRef(0)
  const mountedRef = useRef(true)

  if (view.key !== viewKey) {
    abandonInFlight(tokenRef, pendingRef, epochRef)
    setView(idleHistoryView(viewKey, enabled))
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abandonInFlight(tokenRef, pendingRef, epochRef)
    }
  }, [])

  const settle = (token: object, epoch: number, next: (current: HistoryViewState) => HistoryViewState) => {
    if (!mountedRef.current || tokenRef.current !== token || epochRef.current !== epoch) return
    setView((current) => (current.key === viewKey ? next(current) : current))
  }

  const load = () => {
    if (!enabled) return
    const token = tokenRef.current
    const epoch = ++epochRef.current
    setView(applyHistoryLoadStart)
    void fetchCaptureObservation(observationRequest(capture, workspaceUid)).then(
      (outcome) => settle(token, epoch, (current) => applyHistoryLoadSuccess(current, outcome.observation)),
      (failure: unknown) => settle(token, epoch, (current) => applyHistoryLoadFailure(current, failure)),
    )
  }

  useEffect(() => {
    load()
    // The identity and its readability are the whole dependency set: one read per opened
    // capture. StrictMode's deliberate double effect reads twice, which the contract
    // permits — it is the same read, and the later epoch wins.
  }, [viewKey, enabled])

  const canRecord = view.mode === 'supported' && !view.loading && !view.pending && !view.needsReload

  const recordCheck = () => {
    if (!canRecord || pendingRef.current !== null) return
    const token = tokenRef.current
    pendingRef.current = token
    const epoch = ++epochRef.current
    setView(applyHistoryRecordStart)
    const release = () => { if (pendingRef.current === token) pendingRef.current = null }
    void recordCaptureSourceCheck(observationRequest(capture, workspaceUid as string)).then(
      (recorded) => {
        release()
        settle(token, epoch, (current) => applyHistoryRecordSuccess(current, recorded.observation))
      },
      (failure: unknown) => {
        release()
        settle(token, epoch, (current) => applyHistoryRecordFailure(current, failure))
      },
    )
  }

  const reload = () => {
    if (!enabled || view.loading || view.pending) return
    load()
  }

  return {
    error: view.error,
    loading: view.loading,
    mode: view.mode,
    needsReload: view.needsReload,
    pending: view.pending,
    recordCheck,
    reload,
    saved: view.saved,
  }
}
