import type { ReportDraftCoordinate } from './reportDraftStorage'

/**
 * The pure state model behind the local report draft editor.
 *
 * Everything that decides whether text is at risk lives here, with no React, no
 * DOM and no storage, so the rules can be read and tested on their own:
 *
 * - starting a session RELEASES any operation state left by the previous one,
 *   because a save or delete still in flight for a coordinate the editor has
 *   left can no longer report anything, and a stuck `busy` would leave the new
 *   coordinate permanently disabled and impossible to close;
 * - a successful save moves the baseline to the SNAPSHOT that was written, so
 *   anything typed while the write was in flight is still unsaved;
 * - a successful delete marks the retained text unsaved on its own, because two
 *   equal strings cannot express "this used to be saved and no longer is".
 */

export interface DailyReportDraftSource {
  /** `sha256:` plus 64 lowercase hex, as the preview reports it. */
  sourceDigest: string
  /** Server-generated UTC instant at second or millisecond precision. */
  generatedAt: string
  markdown: string
}

/** The base identity a draft is bound to; retained even when `source` moves on. */
export interface DraftBase {
  sourceDigest: string
  baseGeneratedAt: string
  baseMarkdown: string
}

/**
 * A prompt carries the exact state it was raised against. `revision` is
 * snapshotted when the prompt opens, so confirming can only act on the draft the
 * reader was looking at.
 */
export type Confirmation =
  | { kind: 'discard' }
  | { kind: 'delete'; revision: number }
  | null

export interface DraftSessionState {
  base: DraftBase | null
  baseline: string
  busy: boolean
  confirmation: Confirmation
  loading: boolean
  localRevision: number | null
  notice: string | null
  retainedUnsaved: boolean
  status: string | null
  text: string
}

export type DraftAction =
  | { type: 'session-start' }
  | {
      type: 'loaded'
      base: DraftBase
      localRevision: number | null
      markdown: string
      notice?: string
    }
  | { type: 'edit'; text: string }
  | { type: 'begin' }
  | { type: 'saved'; localRevision: number; markdown: string }
  | { type: 'deleted' }
  | { type: 'failed'; notice: string }
  | { type: 'refused'; notice: string }
  | { type: 'note'; status: string }
  | { type: 'prompt'; confirmation: Confirmation }

export const INITIAL_DRAFT_STATE: DraftSessionState = {
  base: null,
  baseline: '',
  busy: false,
  confirmation: null,
  loading: true,
  localRevision: null,
  notice: null,
  retainedUnsaved: false,
  status: null,
  text: '',
}

export function reportDraftReducer(
  state: DraftSessionState,
  action: DraftAction,
): DraftSessionState {
  switch (action.type) {
    case 'session-start':
      return {
        ...state,
        busy: false,
        confirmation: null,
        loading: true,
        notice: null,
        retainedUnsaved: false,
        status: null,
      }
    case 'loaded':
      return {
        ...state,
        base: action.base,
        baseline: action.markdown,
        loading: false,
        localRevision: action.localRevision,
        notice: action.notice ?? null,
        retainedUnsaved: false,
        text: action.markdown,
      }
    case 'edit':
      return { ...state, text: action.text }
    case 'begin':
      return { ...state, busy: true, notice: null, status: null }
    case 'saved':
      return {
        ...state,
        baseline: action.markdown,
        busy: false,
        localRevision: action.localRevision,
        retainedUnsaved: false,
        status: 'Saved locally as revision ' + String(action.localRevision) + '.',
      }
    case 'deleted':
      return {
        ...state,
        busy: false,
        localRevision: null,
        retainedUnsaved: true,
        status:
          'The saved draft was deleted on this device. The text below is still here and is now unsaved.',
      }
    case 'failed':
      return { ...state, busy: false, notice: action.notice }
    case 'refused':
      return { ...state, confirmation: null, notice: action.notice }
    case 'note':
      return { ...state, status: action.status }
    case 'prompt':
      return { ...state, confirmation: action.confirmation }
    default:
      return state
  }
}

/** Text that is not persisted as it stands, for any of the two reasons. */
export function isDirty(state: DraftSessionState): boolean {
  return state.retainedUnsaved || state.text !== state.baseline
}

export function coordinateKey(coordinate: ReportDraftCoordinate): string {
  return [coordinate.workspaceUid, coordinate.date, coordinate.template].join('|')
}

/**
 * The saved base and the current report are no longer the same REPORT.
 *
 * Identity is the source digest alone. Regenerating the same facts produces the
 * same digest at a later instant, and that is not a stale draft — reporting it
 * as one would train the reader to ignore the notice that matters.
 */
export function isStaleSource(
  base: DraftBase | null,
  source: DailyReportDraftSource,
): boolean {
  return base !== null && base.sourceDigest !== source.sourceDigest
}

/** The report was generated again from identical facts, so only the instant moved. */
export function isRegeneratedUnchanged(
  base: DraftBase | null,
  source: DailyReportDraftSource,
): boolean {
  return (
    base !== null
    && base.sourceDigest === source.sourceDigest
    && base.baseGeneratedAt !== source.generatedAt
  )
}

export function baseFromSource(source: DailyReportDraftSource): DraftBase {
  return {
    sourceDigest: source.sourceDigest,
    baseGeneratedAt: source.generatedAt,
    baseMarkdown: source.markdown,
  }
}
