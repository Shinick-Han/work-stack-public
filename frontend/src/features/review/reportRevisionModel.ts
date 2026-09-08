import {
  MAX_REPORT_MARKDOWN_CHARS,
  MAX_REPORT_NOTE_BYTES,
  MAX_REPORT_NOTE_CHARS,
  reportWorkspaceUidSchema,
  type ReportReadData,
  type ReportStoredState,
} from '../../domain/reportDocuments'

export const FALLBACK_ADVICE =
  'Your text is still here. Use Copy Markdown or Download .md to keep it.'

export const REVISION_DIALOG_DESCRIPTION =
  'Saving writes a new authored version in Work Stack. A finalized report becomes a draft. It does not send the report anywhere else.'

export const REVISION_SAVED_DRAFT =
  'Saved as a new authored version. The report is a draft. It was not sent anywhere else.'

export const REVISION_REOPENED =
  'Saved as a new authored version. The finalized report is now a draft. It was not sent anywhere else.'

export const ARCHIVED_LOCKED =
  'This report is archived. Restore it before editing.'

export const STALE_OWNER_MESSAGE =
  'This editor still holds the original report’s text. The open workspace or report changed, so save and export are paused. Discard the buffer to leave, or switch back to the original report.'

export const DIRTY_CLOSE_BODY =
  'This report has unsaved changes. Closing now discards them. Copy Markdown or Download .md first if you want to keep them.'

export const VALIDATION_MARKDOWN_LIMIT =
  `This text is longer than the report limit, so it cannot be saved. ${FALLBACK_ADVICE}`

export const VALIDATION_MARKDOWN_UTF8 =
  `This text is not valid UTF-8, so it cannot be saved. ${FALLBACK_ADVICE}`

export const VALIDATION_NOTE_LIMIT =
  `The note is longer than the report allows, so it cannot be saved. ${FALLBACK_ADVICE}`

export const VALIDATION_NOTE_UTF8 =
  `The note is not valid UTF-8, so it cannot be saved. ${FALLBACK_ADVICE}`

export const CLIPBOARD_FAILED =
  'The clipboard is unavailable in this browser. Use Download .md instead.'

export const DOWNLOAD_FAILED =
  'The file could not be downloaded in this browser. Use Copy Markdown instead.'

export const COPIED_STATUS = 'The current text was copied as Markdown.'
export const DOWNLOADED_STATUS = 'The current text was downloaded as a .md file.'

const SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/

export interface RevisionPin {
  workspaceUid: string
  reportUid: string
  expectedRevision: number
  openedMarkdown: string
  periodDate: string
  openedState: ReportStoredState
  sourceStale: boolean
  contentRevision: number
}

export interface FrozenRevisionRequest {
  key: string
  workspaceUid: string
  reportUid: string
  expectedRevision: number
  markdown: string
  note: string | null
}

export interface ReportRevisionState {
  pin: RevisionPin
  text: string
  note: string
  markdownBaseline: string
  noteBaseline: string
  saving: boolean
  frozen: FrozenRevisionRequest | null
  unknown: boolean
  notice: string | null
  status: string | null
  confirmation: 'discard' | null
}

export type ReportRevisionEvent =
  | { type: 'edit-text'; text: string }
  | { type: 'edit-note'; note: string }
  | { type: 'save-start'; frozen: FrozenRevisionRequest }
  | {
      type: 'save-done'
      revision: number
      markdown: string
      note: string | null
      reopened: boolean
      sourceStale: boolean
      contentRevision: number
    }
  | { type: 'save-unknown'; message: string }
  | { type: 'save-refused'; message: string }
  | { type: 'generation-advanced' }
  | { type: 'prompt'; confirmation: 'discard' | null }
  | { type: 'status'; status: string }
  | { type: 'failed'; notice: string }

export function codePointCount(value: string): number {
  return [...value].length
}

export function isEncodableUtf8(value: string): boolean {
  return !SURROGATE.test(value)
}

export function noteForRequest(note: string): string | null {
  return note.trim() === '' ? null : note
}

export function workspaceAdmitted(workspaceId: string): boolean {
  return reportWorkspaceUidSchema.safeParse(workspaceId).success
}

export function validateRevisionFields(markdown: string, note: string): string | null {
  if (!isEncodableUtf8(markdown)) return VALIDATION_MARKDOWN_UTF8
  if (codePointCount(markdown) > MAX_REPORT_MARKDOWN_CHARS) return VALIDATION_MARKDOWN_LIMIT
  if (!isEncodableUtf8(note)) return VALIDATION_NOTE_UTF8
  if (codePointCount(note) > MAX_REPORT_NOTE_CHARS) return VALIDATION_NOTE_LIMIT
  if (new TextEncoder().encode(note).length > MAX_REPORT_NOTE_BYTES) return VALIDATION_NOTE_LIMIT
  return null
}

export function pinFromDocument(document: ReportReadData, workspaceUid: string): RevisionPin {
  const latest = document.revisions.at(-1)
  return {
    workspaceUid,
    reportUid: document.uid,
    expectedRevision: document.revision,
    openedMarkdown: latest?.markdown ?? '',
    periodDate: document.period.date,
    openedState: document.state,
    sourceStale: document.source_stale,
    contentRevision: document.content_revision,
  }
}

export function initialRevisionState(pin: RevisionPin): ReportRevisionState {
  return {
    pin,
    text: pin.openedMarkdown,
    note: '',
    markdownBaseline: pin.openedMarkdown,
    noteBaseline: '',
    saving: false,
    frozen: null,
    unknown: false,
    notice: null,
    status: null,
    confirmation: null,
  }
}

export function isStaleCoordinate(
  pinnedWorkspaceUid: string,
  pinnedReportUid: string,
  liveWorkspaceId: string,
  liveSelectedUid: string | null,
): boolean {
  return liveWorkspaceId !== pinnedWorkspaceUid || liveSelectedUid !== pinnedReportUid
}

export function isStaleOwner(
  pin: RevisionPin,
  liveWorkspaceId: string,
  liveSelectedUid: string | null,
): boolean {
  return isStaleCoordinate(pin.workspaceUid, pin.reportUid, liveWorkspaceId, liveSelectedUid)
}

export function isDirty(state: ReportRevisionState): boolean {
  return state.text !== state.markdownBaseline || state.note !== state.noteBaseline
}

export function canExport(state: ReportRevisionState, staleOwner: boolean): boolean {
  return !staleOwner
}

export function canRetry(
  state: ReportRevisionState,
  staleOwner: boolean,
  admitted: boolean,
): boolean {
  return Boolean(
    admitted
    && !staleOwner
    && !state.saving
    && state.unknown
    && state.frozen
    && state.confirmation === null,
  )
}

export function canSave(
  state: ReportRevisionState,
  staleOwner: boolean,
  admitted: boolean,
): boolean {
  if (!admitted || staleOwner || state.saving || state.unknown || state.confirmation) return false
  if (state.pin.openedState === 'archived') return false
  if (!isDirty(state)) return false
  return validateRevisionFields(state.text, state.note) === null
}

function applySaved(
  state: ReportRevisionState,
  event: Extract<ReportRevisionEvent, { type: 'save-done' }>,
): ReportRevisionState {
  return {
    ...state,
    pin: {
      ...state.pin,
      expectedRevision: event.revision,
      openedState: 'draft',
      sourceStale: event.sourceStale,
      contentRevision: event.contentRevision,
    },
    markdownBaseline: event.markdown,
    noteBaseline: event.note ?? '',
    saving: false,
    frozen: null,
    unknown: false,
    notice: null,
    status: event.reopened ? REVISION_REOPENED : REVISION_SAVED_DRAFT,
  }
}

function applyWriteEvent(
  state: ReportRevisionState,
  event: ReportRevisionEvent,
): ReportRevisionState | null {
  if (event.type === 'save-start') {
    return {
      ...state,
      frozen: event.frozen,
      saving: true,
      unknown: false,
      notice: null,
      status: null,
    }
  }
  if (event.type === 'save-done') return applySaved(state, event)
  if (event.type === 'save-unknown') {
    return {
      ...state,
      saving: false,
      unknown: true,
      notice: event.message,
      status: null,
    }
  }
  if (event.type === 'save-refused') {
    return {
      ...state,
      saving: false,
      frozen: null,
      unknown: false,
      notice: event.message,
      status: null,
    }
  }
  if (event.type === 'generation-advanced') {
    return {
      ...state,
      saving: false,
      unknown: state.saving || state.unknown,
    }
  }
  return null
}

export function reportRevisionReducer(
  state: ReportRevisionState,
  event: ReportRevisionEvent,
): ReportRevisionState {
  if (event.type === 'edit-text') return { ...state, text: event.text }
  if (event.type === 'edit-note') return { ...state, note: event.note }
  if (event.type === 'prompt') return { ...state, confirmation: event.confirmation }
  if (event.type === 'status') return { ...state, status: event.status, notice: null }
  if (event.type === 'failed') return { ...state, notice: event.notice, status: null }
  return applyWriteEvent(state, event) ?? state
}
