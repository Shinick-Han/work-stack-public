import { useCallback, useEffect, useReducer, useRef, type Dispatch, type MutableRefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  archiveReportDocument,
  finalizeReportDocument,
  listReportDocuments,
  readReportDocument,
  restoreReportDocument,
} from '../../api/reportDocuments'
import { ApiError, CommitUnknownError, createIdempotencyKey } from '../../api/client'
import {
  reportWorkspaceUidSchema,
  type ReportListFilter,
  type ReportReadData,
} from '../../domain/reportDocuments'
import { getErrorMessage } from '../../utils/format'
import { useOwnerGeneration } from './DailyReportPreview'
import {
  CURSOR_INVALID_MESSAGE,
  savedReportsRefusalMessage,
  UNSETTLED_TRANSITION,
} from './savedReportsMessages'
import {
  allowedTransitions,
  canLoadMore,
  INITIAL_SAVED_REPORTS_STATE,
  isConflictCode,
  savedReportsReducer,
  type FrozenTransition,
  type SavedReportsActionKind,
  type SavedReportsEvent,
  type SavedReportsState,
  type TransitionReceipt,
} from './savedReportsModel'

export interface SavedReportsModel {
  admitted: boolean
  filter: ReportListFilter
  items: SavedReportsState['items']
  omittedCount: number
  canLoadMore: boolean
  selectedUid: string | null
  document: ReportReadData | null
  listPending: boolean
  morePending: boolean
  readPending: boolean
  transitionPending: boolean
  listError: string | null
  readError: string | null
  transitionError: string | null
  retryable: boolean
  needsReconcile: boolean
  setFilter: (filter: ReportListFilter) => void
  loadMore: () => void
  refreshList: () => void
  select: (uid: string) => void
  refreshDocument: () => void
  finalize: () => void
  archive: () => void
  restore: () => void
  retry: () => void
}

interface SavedReportsSession {
  admitted: boolean
  workspaceId: string
  stateRef: MutableRefObject<SavedReportsState>
  accept: (frozenOwner: string) => boolean
  currentOwner: () => string
  claimRead: () => number
  ownsRead: (read: number, frozenOwner: string, reportUid: string) => boolean
  dispatch: Dispatch<SavedReportsEvent>
}

function listErrorMeta(error: unknown): { message: string; cursorInvalid: boolean } {
  if (error instanceof ApiError && error.code === 'report_cursor_invalid') {
    return { message: CURSOR_INVALID_MESSAGE, cursorInvalid: true }
  }
  return { message: getErrorMessage(error), cursorInvalid: false }
}

function transitionFailure(error: unknown): { refused: boolean; message: string; needsReconcile: boolean } {
  if (error instanceof ApiError) {
    return {
      refused: true,
      message: savedReportsRefusalMessage(error.code),
      needsReconcile: isConflictCode(error.code),
    }
  }
  if (error instanceof CommitUnknownError) {
    return { refused: false, message: UNSETTLED_TRANSITION, needsReconcile: true }
  }
  return { refused: true, message: getErrorMessage(error), needsReconcile: false }
}

function postTransition(frozen: FrozenTransition, workspaceUid: string): Promise<TransitionReceipt> {
  const target = {
    workspaceUid,
    reportUid: frozen.reportUid,
    expectedRevision: frozen.expectedRevision,
  }
  if (frozen.kind === 'finalize') return finalizeReportDocument(target, frozen.key)
  if (frozen.kind === 'archive') return archiveReportDocument({ ...target, note: null }, frozen.key)
  return restoreReportDocument(target, frozen.key)
}

function fetchHead(session: SavedReportsSession, frozenOwner: string, filter: ReportListFilter) {
  if (!session.admitted || !session.accept(frozenOwner)) return
  session.dispatch({ type: 'list-start', more: false })
  void listReportDocuments({ workspaceUid: session.workspaceId, state: filter }).then(
    (page) => {
      if (!session.accept(frozenOwner) || session.stateRef.current.filter !== filter) return
      session.dispatch({ type: 'list-page', page, more: false })
    },
    (error: unknown) => {
      if (!session.accept(frozenOwner) || session.stateRef.current.filter !== filter) return
      session.dispatch({ type: 'list-error', more: false, ...listErrorMeta(error) })
    },
  )
}

function fetchMore(session: SavedReportsSession) {
  const snapshot = session.stateRef.current
  const frozenOwner = session.currentOwner()
  if (!session.admitted || !canLoadMore(snapshot) || snapshot.cursor === null) return
  const filter = snapshot.filter
  const cursor = snapshot.cursor
  session.dispatch({ type: 'list-start', more: true })
  void listReportDocuments({ workspaceUid: session.workspaceId, state: filter, cursor }).then(
    (page) => {
      if (!session.accept(frozenOwner) || session.stateRef.current.filter !== filter) return
      session.dispatch({ type: 'list-page', page, more: true })
    },
    (error: unknown) => {
      if (!session.accept(frozenOwner) || session.stateRef.current.filter !== filter) return
      session.dispatch({ type: 'list-error', more: true, ...listErrorMeta(error) })
    },
  )
}

function fetchDocument(session: SavedReportsSession, reportUid: string, replace: boolean) {
  const frozenOwner = session.currentOwner()
  if (!session.admitted) return
  const read = session.claimRead()
  if (replace) session.dispatch({ type: 'select', uid: reportUid })
  session.dispatch({ type: 'read-start' })
  void readReportDocument({ workspaceUid: session.workspaceId, reportUid }).then(
    (document) => {
      if (!session.ownsRead(read, frozenOwner, reportUid)) return
      session.dispatch({ type: 'read-done', document })
    },
    (error: unknown) => {
      if (!session.ownsRead(read, frozenOwner, reportUid)) return
      session.dispatch({ type: 'read-error', message: getErrorMessage(error) })
    },
  )
}

function runFrozen(session: SavedReportsSession, frozen: FrozenTransition) {
  const frozenOwner = session.currentOwner()
  if (!session.admitted || !session.accept(frozenOwner) || session.stateRef.current.transitionPending) return
  session.dispatch({ type: 'transition-start', frozen })
  void postTransition(frozen, session.workspaceId).then(
    (data) => {
      if (!session.accept(frozenOwner)) return
      session.dispatch({ type: 'transition-done', data })
    },
    (error: unknown) => {
      if (!session.accept(frozenOwner)) return
      const failure = transitionFailure(error)
      if (failure.refused) {
        session.dispatch({
          type: 'transition-refused',
          message: failure.message,
          needsReconcile: failure.needsReconcile,
        })
        return
      }
      session.dispatch({ type: 'transition-unsettled', message: failure.message })
    },
  )
}

function beginTransition(session: SavedReportsSession, kind: SavedReportsActionKind) {
  const snapshot = session.stateRef.current
  const document = snapshot.document
  if (!document || snapshot.transitionPending || snapshot.needsReconcile || snapshot.frozen) return
  if (!allowedTransitions(document.state).includes(kind)) return
  runFrozen(session, {
    kind,
    reportUid: document.uid,
    expectedRevision: document.revision,
    key: createIdempotencyKey(),
  })
}

function toModel(state: SavedReportsState, session: SavedReportsSession): SavedReportsModel {
  return {
    admitted: session.admitted,
    filter: state.filter,
    items: state.items,
    omittedCount: state.omittedCount,
    canLoadMore: canLoadMore(state),
    selectedUid: state.selectedUid,
    document: state.document,
    listPending: state.listPending,
    morePending: state.morePending,
    readPending: state.readPending,
    transitionPending: state.transitionPending,
    listError: state.listError,
    readError: state.readError,
    transitionError: state.transitionError,
    retryable: state.frozen !== null && !state.transitionPending,
    needsReconcile: state.needsReconcile,
    setFilter: (filter) => {
      if (filter !== session.stateRef.current.filter) session.dispatch({ type: 'filter', filter })
    },
    loadMore: () => fetchMore(session),
    refreshList: () => {
      session.dispatch({ type: 'list-refresh' })
      fetchHead(session, session.currentOwner(), session.stateRef.current.filter)
    },
    select: (uid) => fetchDocument(session, uid, true),
    refreshDocument: () => {
      const uid = session.stateRef.current.selectedUid
      if (uid) fetchDocument(session, uid, false)
    },
    finalize: () => beginTransition(session, 'finalize'),
    archive: () => beginTransition(session, 'archive'),
    restore: () => beginTransition(session, 'restore'),
    retry: () => {
      const frozen = session.stateRef.current.frozen
      if (frozen) runFrozen(session, frozen)
    },
  }
}

export function useSavedReports(workspaceId: string): SavedReportsModel {
  const queryClient = useQueryClient()
  const { owner, currentOwner } = useOwnerGeneration(queryClient, workspaceId, 'saved-reports')
  const [state, dispatch] = useReducer(savedReportsReducer, INITIAL_SAVED_REPORTS_STATE)
  const stateRef = useRef(state)
  const seenOwner = useRef(owner)
  const alive = useRef(true)
  const read = useRef(0)
  const admitted = reportWorkspaceUidSchema.safeParse(workspaceId).success
  stateRef.current = state
  if (seenOwner.current !== owner) {
    seenOwner.current = owner
    read.current += 1
    dispatch({ type: 'reset' })
  }

  const accept = useCallback((frozenOwner: string) => (
    alive.current && frozenOwner === currentOwner()
  ), [currentOwner])

  const claimRead = useCallback(() => {
    read.current += 1
    return read.current
  }, [])

  const ownsRead = useCallback((claimed: number, frozenOwner: string, reportUid: string) => (
    accept(frozenOwner)
    && read.current === claimed
    && stateRef.current.selectedUid === reportUid
  ), [accept])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const session: SavedReportsSession = {
    admitted,
    workspaceId,
    stateRef,
    accept,
    currentOwner,
    claimRead,
    ownsRead,
    dispatch,
  }
  const sessionRef = useRef(session)
  sessionRef.current = session

  useEffect(() => {
    fetchHead(sessionRef.current, owner, state.filter)
  }, [admitted, owner, state.filter, workspaceId])

  return toModel(state, session)
}
