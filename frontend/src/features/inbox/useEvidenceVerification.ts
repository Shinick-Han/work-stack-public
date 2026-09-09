import { useEffect, useRef, useState } from 'react'
import { CANONICAL_UUID, MAX_EVIDENCE_ITEMS } from '../../domain/schemaKnowledgeCapture'
import {
  CAPTURE_ID_PATTERN,
  classifyKnowledgeVerificationFailure,
  verifyCaptureSources,
  type ExpectedEvidence,
  type VerificationEvidenceEntry,
} from '../../api/knowledgeVerification'
import type { Capture } from '../../domain/types'

/**
 * Lifecycle for one explicit, transient source check.
 *
 * The rules this hook exists to keep, all of them from the R21 UI contract:
 *
 * - **Nothing is requested on render, mount, effect or reopen.** `check()` is the only
 *   thing that reaches the network, and only a click calls it.
 * - **The lock is taken synchronously.** `check()` claims `pendingRef` before its first
 *   `await`, so a double click is one POST, not two child processes.
 * - **A new view never inherits an old observation.** The view identity is recomputed
 *   during render, so a changed workspace, capture or revision clears the display in the
 *   same commit the new capture appears in — not one frame later.
 * - **Late answers are dropped, including ABA.** Each view holds a fresh token object.
 *   A→B→A replaces the token twice, so an answer minted under the first A cannot land on
 *   the second A. Unmount replaces it too, which also covers StrictMode's remount.
 * - **Unmounting cancels nothing on the server.** It only stops this view from believing
 *   the answer. The subprocess is the owner's business, and the hook claims no more.
 *
 * The result lives in local state only. Nothing here writes to the Capture, and a
 * reported source version is never promoted into a verified field.
 */

export interface EvidenceObservation {
  entries: readonly VerificationEvidenceEntry[]
  checkedAt: string
}

export interface EvidenceVerificationState {
  /** True when this capture and workspace can be checked at all. Gates the button. */
  available: boolean
  pending: boolean
  observation: EvidenceObservation | null
  /** Closed copy for a failed attempt. Never a backend-authored sentence. */
  error: string | null
  check: () => void
}

interface ViewState {
  key: string
  pending: boolean
  observation: EvidenceObservation | null
  error: string | null
}

/** The exact expected facts the owner will re-derive, in stored order, duplicates kept. */
export function expectedEvidenceOf(capture: Capture): readonly ExpectedEvidence[] {
  const evidence = capture.retrieval?.evidence ?? []
  return evidence.map((item) => ({
    document_ref: item.document_ref,
    source_type: item.reported_source_type,
    expected_source_version: item.reported_source_version,
  }))
}

/**
 * Whether this capture has a source-check surface at all: a canonical workspace UID, a
 * stored capture identifier, and between one and `MAX_EVIDENCE_ITEMS` evidence rows.
 *
 * Exported for the saved-check history hook, which gates its historical read on the same
 * predicate. A 1.0 capture with no retrieval evidence has nothing to check and nothing to
 * overlay, so it keeps the display — and the silence — it already had.
 */
export function isCaptureCheckable(capture: Capture, workspaceUid: string | undefined): workspaceUid is string {
  if (!workspaceUid || !CANONICAL_UUID.test(workspaceUid)) return false
  if (!CAPTURE_ID_PATTERN.test(capture.id)) return false
  const count = capture.retrieval?.evidence.length ?? 0
  return count >= 1 && count <= MAX_EVIDENCE_ITEMS
}

function idleView(key: string): ViewState {
  return { key, pending: false, observation: null, error: null }
}

export function useEvidenceVerification(
  capture: Capture,
  workspaceUid?: string,
): EvidenceVerificationState {
  const available = isCaptureCheckable(capture, workspaceUid)
  const viewKey = `${workspaceUid ?? ''}|${capture.id}|${capture.revision}`
  const [view, setView] = useState<ViewState>(() => idleView(viewKey))
  const tokenRef = useRef<object>({})
  const pendingRef = useRef<object | null>(null)
  const mountedRef = useRef(true)

  if (view.key !== viewKey) {
    // Adjusting state during render: the new capture must never be shown for one
    // commit under the previous capture's observation. Replacing the token here is
    // what makes an in-flight answer stale rather than merely late.
    tokenRef.current = {}
    pendingRef.current = null
    setView(idleView(viewKey))
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      tokenRef.current = {}
      pendingRef.current = null
    }
  }, [])

  const settle = (token: object, next: Omit<ViewState, 'key' | 'pending'>) => {
    if (pendingRef.current === token) pendingRef.current = null
    if (!mountedRef.current || tokenRef.current !== token) return
    setView((current) => (current.key === viewKey ? { ...next, key: current.key, pending: false } : current))
  }

  const check = () => {
    if (!available || pendingRef.current !== null) return
    const token = tokenRef.current
    pendingRef.current = token
    // Clearing here, not on success, is what stops a failed retry from leaving the
    // previous observation on screen as if it were still the current answer.
    setView({ key: viewKey, pending: true, observation: null, error: null })
    void verifyCaptureSources({
      workspaceUid,
      captureId: capture.id,
      captureRevision: capture.revision,
      expectedEvidence: expectedEvidenceOf(capture),
    }).then(
      (outcome) => settle(token, {
        observation: { entries: outcome.result.evidence, checkedAt: outcome.result.checked_at },
        error: null,
      }),
      (failure: unknown) => settle(token, {
        observation: null,
        error: classifyKnowledgeVerificationFailure(failure).message,
      }),
    )
  }

  return { available, check, error: view.error, observation: view.observation, pending: view.pending }
}
