import { useCallback, useEffect, useReducer, useRef, type Dispatch, type MutableRefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { reviseReportDocument } from '../../api/reportDocuments'
import { ApiError, CommitUnknownError, createIdempotencyKey } from '../../api/client'
import {
  COMMIT_UNKNOWN_REPORT,
  type ReportReadData,
  type ReportReviseData,
} from '../../domain/reportDocuments'
import { copyTextToClipboard, isClipboardCancelled } from '../../utils/clipboard'
import { getErrorMessage } from '../../utils/format'
import { useOwnerGeneration } from './DailyReportPreview'
import {
  CLIPBOARD_FAILED,
  COPIED_STATUS,
  DOWNLOAD_FAILED,
  DOWNLOADED_STATUS,
  FALLBACK_ADVICE,
  canExport,
  canRetry,
  canSave,
  initialRevisionState,
  isDirty,
  isStaleCoordinate,
  isStaleOwner,
  noteForRequest,
  pinFromDocument,
  reportRevisionReducer,
  validateRevisionFields,
  workspaceAdmitted,
  type FrozenRevisionRequest,
  type ReportRevisionEvent,
  type ReportRevisionState,
} from './reportRevisionModel'
import { savedReportsRefusalMessage, UNSETTLED_TRANSITION } from './savedReportsMessages'

export interface ReportRevisionInput {
  document: ReportReadData
  liveSelectedUid: string | null
  liveWorkspaceId: string
  onClose: () => void
  onSaved: () => void
}

export interface ReportRevisionModel {
  actions: {
    cancelPrompt: () => void
    confirmDiscard: () => void
    copy: () => void
    download: () => void
    requestClose: () => void
    retry: () => void
    save: () => void
    setNote: (note: string) => void
    setText: (text: string) => void
  }
  admitted: boolean
  canExport: boolean
  canRetry: boolean
  canSave: boolean
  dirty: boolean
  prompting: boolean
  staleOwner: boolean
  state: ReportRevisionState
  validation: string | null
}

interface LiveCoordinate {
  selectedUid: string | null
  workspaceId: string
}

interface RevisionGate {
  alive: MutableRefObject<boolean>
  currentOwner: () => string
  /**
   * One monotonic token for the editor's asynchronous operations. It advances on
   * every observed workspace-owner change, every observed report-selection change,
   * and every new write, so a coordinate the user comes back to (A to B to A)
   * cannot reproduce the token an earlier operation claimed.
   */
  operation: MutableRefObject<number>
  /** The latest rendered coordinates, read when a callback settles - not captured. */
  live: MutableRefObject<LiveCoordinate>
}

function unknownMessage(error: unknown): string {
  if (error instanceof CommitUnknownError) return error.message || COMMIT_UNKNOWN_REPORT
  return UNSETTLED_TRANSITION
}

function refusedMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${savedReportsRefusalMessage(error.code)} ${FALLBACK_ADVICE}`
  }
  return `${getErrorMessage(error)} ${FALLBACK_ADVICE}`
}

function downloadMarkdown(date: string, markdown: string) {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `workstack-daily-${date}-revision.md`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function receiptAccepted(data: ReportReviseData, frozen: FrozenRevisionRequest): boolean {
  return (
    data.workspace_uid === frozen.workspaceUid
    && data.uid === frozen.reportUid
    && data.content_entry.markdown === frozen.markdown
    && data.content_entry.note === frozen.note
    && data.state === 'draft'
  )
}

function stillOwns(
  gate: RevisionGate,
  workspaceUid: string,
  reportUid: string,
  frozenOwner: string,
  claimed: number,
): boolean {
  const live = gate.live.current
  return (
    gate.alive.current
    && gate.operation.current === claimed
    && gate.currentOwner() === frozenOwner
    && !isStaleCoordinate(workspaceUid, reportUid, live.workspaceId, live.selectedUid)
  )
}

function postFrozen(
  frozen: FrozenRevisionRequest,
  gate: RevisionGate,
  dispatch: Dispatch<ReportRevisionEvent>,
  onSaved: () => void,
) {
  const frozenOwner = gate.currentOwner()
  const claimed = gate.operation.current + 1
  gate.operation.current = claimed
  const owned = () => stillOwns(gate, frozen.workspaceUid, frozen.reportUid, frozenOwner, claimed)
  dispatch({ type: 'save-start', frozen })
  void reviseReportDocument({
    workspaceUid: frozen.workspaceUid,
    reportUid: frozen.reportUid,
    expectedRevision: frozen.expectedRevision,
    markdown: frozen.markdown,
    note: frozen.note,
  }, frozen.key).then(
    (data) => {
      if (!owned()) return
      if (!receiptAccepted(data, frozen)) {
        dispatch({ type: 'save-unknown', message: unknownMessage(new CommitUnknownError(COMMIT_UNKNOWN_REPORT, data)) })
        return
      }
      dispatch({
        type: 'save-done',
        revision: data.revision,
        markdown: data.content_entry.markdown,
        note: data.content_entry.note,
        reopened: data.reopened,
        sourceStale: data.source_stale,
        contentRevision: data.content_entry.content_revision,
      })
      onSaved()
    },
    (error: unknown) => {
      if (!owned()) return
      if (error instanceof CommitUnknownError) {
        dispatch({ type: 'save-unknown', message: unknownMessage(error) })
        return
      }
      dispatch({ type: 'save-refused', message: refusedMessage(error) })
    },
  )
}

/**
 * Owns the gate every settling callback reads: the liveness flag, the monotonic
 * operation token, and the live coordinates. The token advances during render -
 * before any control in the same React batch can fire - so an operation started
 * against the previous owner or report selection can never look current again.
 */
function useRevisionGate(
  owner: string,
  currentOwner: () => string,
  liveWorkspaceId: string,
  liveSelectedUid: string | null,
  dispatch: Dispatch<ReportRevisionEvent>,
): RevisionGate {
  const alive = useRef(true)
  const operation = useRef(0)
  const seenOwner = useRef(owner)
  const seenSelectedUid = useRef(liveSelectedUid)
  const live = useRef<LiveCoordinate>({ selectedUid: liveSelectedUid, workspaceId: liveWorkspaceId })
  live.current = { selectedUid: liveSelectedUid, workspaceId: liveWorkspaceId }

  // Refs, not state: a control firing in the same React batch as the swap must
  // already see the advanced token. The selection is part of the coordinate the
  // editor owns, so A to B to A advances twice and never looks current again.
  if (seenOwner.current !== owner || seenSelectedUid.current !== liveSelectedUid) {
    seenOwner.current = owner
    seenSelectedUid.current = liveSelectedUid
    operation.current += 1
    dispatch({ type: 'generation-advanced' })
  }

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      operation.current += 1
    }
  }, [])

  return { alive, currentOwner, live, operation }
}

function useRevisionFallbacks(
  stateRef: MutableRefObject<ReportRevisionState>,
  gate: RevisionGate,
  dispatch: Dispatch<ReportRevisionEvent>,
  liveWorkspaceId: string,
  liveSelectedUid: string | null,
) {
  const copy = useCallback(() => {
    const snapshot = stateRef.current
    if (!canExport(snapshot, isStaleOwner(snapshot.pin, liveWorkspaceId, liveSelectedUid))) return
    const frozenOwner = gate.currentOwner()
    const claimed = gate.operation.current
    // The helper stops at every point it still can - before the native write, and
    // after a native rejection but before the textarea fallback touches the
    // document - so a copy started against this report never lands B's clipboard
    // write with A's text. A native write already submitted cannot be revoked, so
    // a stale settle reports nothing rather than claiming a result either way.
    const owned = () => stillOwns(gate, snapshot.pin.workspaceUid, snapshot.pin.reportUid, frozenOwner, claimed)
    void copyTextToClipboard(snapshot.text, owned).then(
      () => {
        if (!owned()) return
        dispatch({ type: 'status', status: COPIED_STATUS })
      },
      (error: unknown) => {
        if (isClipboardCancelled(error) || !owned()) return
        dispatch({ type: 'failed', notice: CLIPBOARD_FAILED })
      },
    )
  }, [dispatch, gate, liveSelectedUid, liveWorkspaceId, stateRef])

  const download = useCallback(() => {
    const snapshot = stateRef.current
    if (!canExport(snapshot, isStaleOwner(snapshot.pin, liveWorkspaceId, liveSelectedUid))) return
    try {
      downloadMarkdown(snapshot.pin.periodDate, snapshot.text)
      dispatch({ type: 'status', status: DOWNLOADED_STATUS })
    } catch {
      dispatch({ type: 'failed', notice: DOWNLOAD_FAILED })
    }
  }, [dispatch, liveSelectedUid, liveWorkspaceId, stateRef])

  return { copy, download }
}

export function useReportRevision({
  document,
  liveSelectedUid,
  liveWorkspaceId,
  onClose,
  onSaved,
}: ReportRevisionInput): ReportRevisionModel {
  const queryClient = useQueryClient()
  const { owner, currentOwner } = useOwnerGeneration(queryClient, liveWorkspaceId, 'saved-reports')
  const [state, dispatch] = useReducer(
    reportRevisionReducer,
    document,
    (opened) => initialRevisionState(pinFromDocument(opened, liveWorkspaceId)),
  )
  const stateRef = useRef(state)
  stateRef.current = state
  const gate = useRevisionGate(owner, currentOwner, liveWorkspaceId, liveSelectedUid, dispatch)

  const staleOwner = isStaleOwner(state.pin, liveWorkspaceId, liveSelectedUid)
  const admitted = workspaceAdmitted(state.pin.workspaceUid)
  const { copy, download } = useRevisionFallbacks(stateRef, gate, dispatch, liveWorkspaceId, liveSelectedUid)

  const save = useCallback(() => {
    const snapshot = stateRef.current
    if (!canSave(snapshot, isStaleOwner(snapshot.pin, liveWorkspaceId, liveSelectedUid), admitted)) return
    const invalid = validateRevisionFields(snapshot.text, snapshot.note)
    if (invalid) {
      dispatch({ type: 'failed', notice: invalid })
      return
    }
    postFrozen({
      key: createIdempotencyKey(),
      workspaceUid: snapshot.pin.workspaceUid,
      reportUid: snapshot.pin.reportUid,
      expectedRevision: snapshot.pin.expectedRevision,
      markdown: snapshot.text,
      note: noteForRequest(snapshot.note),
    }, gate, dispatch, onSaved)
  }, [admitted, gate, liveSelectedUid, liveWorkspaceId, onSaved])

  const retry = useCallback(() => {
    const snapshot = stateRef.current
    if (!canRetry(snapshot, isStaleOwner(snapshot.pin, liveWorkspaceId, liveSelectedUid), admitted)) return
    if (snapshot.frozen) postFrozen(snapshot.frozen, gate, dispatch, onSaved)
  }, [admitted, gate, liveSelectedUid, liveWorkspaceId, onSaved])

  const requestClose = useCallback(() => {
    const snapshot = stateRef.current
    if (snapshot.saving || snapshot.confirmation) return
    if (isDirty(snapshot)) {
      dispatch({ type: 'prompt', confirmation: 'discard' })
      return
    }
    onClose()
  }, [onClose])

  return {
    actions: {
      cancelPrompt: () => dispatch({ type: 'prompt', confirmation: null }),
      confirmDiscard: onClose,
      copy,
      download,
      requestClose,
      retry,
      save,
      setNote: (note) => dispatch({ type: 'edit-note', note }),
      setText: (text) => dispatch({ type: 'edit-text', text }),
    },
    admitted,
    canExport: canExport(state, staleOwner),
    canRetry: canRetry(state, staleOwner, admitted),
    canSave: canSave(state, staleOwner, admitted),
    dirty: isDirty(state),
    prompting: state.confirmation !== null,
    staleOwner,
    state,
    validation: validateRevisionFields(state.text, state.note),
  }
}
