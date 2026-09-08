import type {
  ReportArchiveData,
  ReportFinalizeData,
  ReportListData,
  ReportListFilter,
  ReportListItem,
  ReportReadData,
  ReportRestoreData,
  ReportStoredState,
} from '../../domain/reportDocuments'

export type SavedReportsActionKind = 'finalize' | 'archive' | 'restore'

export interface FrozenTransition {
  kind: SavedReportsActionKind
  reportUid: string
  expectedRevision: number
  key: string
}

export type TransitionReceipt = ReportFinalizeData | ReportArchiveData | ReportRestoreData

export interface SavedReportsState {
  filter: ReportListFilter
  items: ReportListItem[]
  cursor: string | null
  omittedCount: number
  selectedUid: string | null
  document: ReportReadData | null
  listPending: boolean
  morePending: boolean
  readPending: boolean
  transitionPending: boolean
  listError: string | null
  readError: string | null
  transitionError: string | null
  frozen: FrozenTransition | null
  needsReconcile: boolean
  cursorInvalid: boolean
}

export const INITIAL_SAVED_REPORTS_STATE: SavedReportsState = {
  filter: 'active',
  items: [],
  cursor: null,
  omittedCount: 0,
  selectedUid: null,
  document: null,
  listPending: false,
  morePending: false,
  readPending: false,
  transitionPending: false,
  listError: null,
  readError: null,
  transitionError: null,
  frozen: null,
  needsReconcile: false,
  cursorInvalid: false,
}

export type SavedReportsEvent =
  | { type: 'reset' }
  | { type: 'filter'; filter: ReportListFilter }
  | { type: 'list-start'; more: boolean }
  | { type: 'list-page'; page: ReportListData; more: boolean }
  | { type: 'list-error'; message: string; more: boolean; cursorInvalid: boolean }
  | { type: 'list-refresh' }
  | { type: 'select'; uid: string }
  | { type: 'read-start' }
  | { type: 'read-done'; document: ReportReadData }
  | { type: 'read-error'; message: string }
  | { type: 'transition-start'; frozen: FrozenTransition }
  | { type: 'transition-done'; data: TransitionReceipt }
  | { type: 'transition-refused'; message: string; needsReconcile: boolean }
  | { type: 'transition-unsettled'; message: string }

export function matchesListFilter(state: ReportStoredState, filter: ReportListFilter): boolean {
  if (filter === 'active') return state === 'draft' || state === 'finalized'
  if (filter === 'archived') return state === 'archived'
  return true
}

export function allowedTransitions(state: ReportStoredState): readonly SavedReportsActionKind[] {
  if (state === 'draft') return ['finalize', 'archive']
  if (state === 'finalized') return ['archive']
  return ['restore']
}

export function applyTransitionToItem(item: ReportListItem, data: TransitionReceipt): ReportListItem {
  return {
    ...item,
    state: data.state,
    revision: data.revision,
    archived_from_state: data.archived_from_state,
    updated_at: data.updated_at,
  }
}

export function applyTransitionToRead(document: ReportReadData, data: TransitionReceipt): ReportReadData {
  return {
    ...document,
    state: data.state,
    revision: data.revision,
    archived_from_state: data.archived_from_state,
    updated_at: data.updated_at,
    source_stale: 'source_stale' in data ? data.source_stale : document.source_stale,
  }
}

function handleListEvent(state: SavedReportsState, event: SavedReportsEvent): SavedReportsState | null {
  if (event.type === 'filter') {
    return {
      ...state,
      filter: event.filter,
      items: [],
      cursor: null,
      omittedCount: 0,
      listPending: false,
      morePending: false,
      listError: null,
      cursorInvalid: false,
    }
  }
  if (event.type === 'list-refresh') {
    return {
      ...state,
      items: [],
      cursor: null,
      omittedCount: 0,
      listPending: false,
      morePending: false,
      listError: null,
      cursorInvalid: false,
    }
  }
  if (event.type === 'list-start') {
    return {
      ...state,
      listPending: event.more ? state.listPending : true,
      morePending: event.more,
      listError: event.more ? state.listError : null,
      cursorInvalid: event.more ? state.cursorInvalid : false,
    }
  }
  if (event.type === 'list-page') {
    return {
      ...state,
      items: event.more ? [...state.items, ...event.page.reports] : event.page.reports,
      cursor: event.page.cursor,
      omittedCount: event.page.omitted_count,
      listPending: false,
      morePending: false,
      listError: null,
      cursorInvalid: false,
    }
  }
  if (event.type === 'list-error') {
    return {
      ...state,
      listPending: false,
      morePending: false,
      listError: event.message,
      cursorInvalid: event.cursorInvalid,
    }
  }
  return null
}

function handleReadEvent(state: SavedReportsState, event: SavedReportsEvent): SavedReportsState | null {
  if (event.type === 'select') {
    return {
      ...state,
      selectedUid: event.uid,
      document: null,
      readPending: false,
      readError: null,
      transitionError: state.frozen ? state.transitionError : null,
      needsReconcile: false,
    }
  }
  if (event.type === 'read-start') {
    return { ...state, readPending: true, readError: null }
  }
  if (event.type === 'read-done') {
    return {
      ...state,
      document: event.document,
      selectedUid: event.document.uid,
      readPending: false,
      readError: null,
      needsReconcile: false,
    }
  }
  if (event.type === 'read-error') {
    return { ...state, readPending: false, readError: event.message }
  }
  return null
}

function handleTransitionEvent(state: SavedReportsState, event: SavedReportsEvent): SavedReportsState {
  if (event.type === 'transition-start') {
    return {
      ...state,
      frozen: event.frozen,
      transitionPending: true,
      transitionError: null,
    }
  }
  if (event.type === 'transition-done') {
    const items = state.items
      .map((item) => (item.uid === event.data.uid ? applyTransitionToItem(item, event.data) : item))
      .filter((item) => matchesListFilter(item.state, state.filter))
    const document = state.document?.uid === event.data.uid
      ? applyTransitionToRead(state.document, event.data)
      : state.document
    return {
      ...state,
      items,
      document,
      transitionPending: false,
      frozen: null,
      transitionError: null,
      needsReconcile: false,
    }
  }
  if (event.type === 'transition-refused') {
    return {
      ...state,
      transitionPending: false,
      frozen: null,
      transitionError: event.message,
      needsReconcile: event.needsReconcile,
    }
  }
  if (event.type === 'transition-unsettled') {
    return {
      ...state,
      transitionPending: false,
      transitionError: event.message,
      needsReconcile: true,
    }
  }
  return state
}

export function savedReportsReducer(state: SavedReportsState, event: SavedReportsEvent): SavedReportsState {
  if (event.type === 'reset') return INITIAL_SAVED_REPORTS_STATE
  const listed = handleListEvent(state, event)
  if (listed) return listed
  const read = handleReadEvent(state, event)
  if (read) return read
  return handleTransitionEvent(state, event)
}

export function canLoadMore(state: SavedReportsState): boolean {
  return state.cursor !== null && state.omittedCount > 0 && !state.listPending && !state.morePending
}

export function isConflictCode(code: string): boolean {
  return code === 'report_revision_conflict' || code === 'report_source_changed'
}
