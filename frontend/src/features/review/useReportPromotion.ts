import { useCallback, useEffect, useMemo, useReducer } from 'react'

import { createReportDocument, type CreateReportDocumentInput } from '../../api/reportDocuments'
import { ApiError, createIdempotencyKey } from '../../api/transport'
import type { ReportCreateData } from '../../domain/reportDocuments'
import { FALLBACK_ADVICE } from './reportDraftMessages'
import { coordinateKey, type DraftBase } from './reportDraftEditorModel'
import { useOwnerGuard } from './useReportDraftSession'
import type { ReportDraftCoordinate } from './reportDraftStorage'

/**
 * Saving the edited text as a report document in the workspace.
 *
 * This is a DIFFERENT act from saving the local draft, and this hook keeps the
 * two apart in every direction. It never reads, writes or deletes the local
 * buffer, never rewrites the text on screen, and never compares the local
 * `localRevision` with the server's numbers — they count different things in
 * different stores (storage contract §14).
 *
 * Create only, and only when the reader asks:
 *
 * - nothing is sent on render, on mount or on a source change;
 * - the request is frozen at the click. Text typed while it is in flight belongs
 *   to the next report, not to this one;
 * - the frozen request keeps ONE key for as long as it is unresolved, so an
 *   ambiguous answer is settled by retrying the identical operation rather than
 *   by sending a second, differently-keyed one built from newer text;
 * - a result belonging to a coordinate the editor has left, or to an unmounted
 *   editor, is dropped — including after the reader has gone A → B → A;
 * - every refusal leaves the text exactly where it is and names the two ways to
 *   keep it.
 */

/** The frozen request: one exact payload and the one key that identifies it. */
export interface PromotionOperation {
  payload: CreateReportDocumentInput
  key: string
}

export type PromotionPhase =
  | { kind: 'idle' }
  | { kind: 'saving' }
  /** The workspace holds the report. `contentRevision` is the SERVER's number. */
  | { kind: 'saved'; contentRevision: number }
  /** The workspace refused, and said why. Nothing was written there. */
  | { kind: 'refused'; message: string }
  /** No answer arrived, so the same request has to be asked again to settle it. */
  | { kind: 'unsettled'; message: string }

interface PromotionState {
  operation: PromotionOperation | null
  phase: PromotionPhase
}

type PromotionAction =
  | { type: 'reset' }
  | { type: 'begin'; operation: PromotionOperation }
  | { type: 'saved'; contentRevision: number }
  | { type: 'refused'; message: string }
  | { type: 'unsettled'; message: string }

export interface ReportPromotionInput {
  /** The base identity the draft is bound to; `null` until the session loads. */
  base: DraftBase | null
  /** The pinned coordinate, not the currently rendered one. */
  coordinate: ReportDraftCoordinate
  /** The text as it stands now. Only ever read at the moment of a click. */
  text: string
}

export interface ReportPromotionModel {
  /** True while a frozen request is unresolved and must not be replaced. */
  pending: boolean
  phase: PromotionPhase
  /** Ask the workspace to hold the text as it stands now. */
  save: () => void
  /** Ask the identical question again. Never rebuilt from newer text. */
  retry: () => void
  /** True when there is a frozen request an explicit retry can settle. */
  retryable: boolean
}

const UNSETTLED_MESSAGE =
  'The workspace did not answer this save, so the report may or may not have been saved there.'
  + ' Ask the same question again to settle it without saving the report twice.'

const REFUSALS: Record<string, string> = {
  idempotency_conflict:
    'This save could not be matched to the request that was sent, so nothing new was saved.'
    + ' Close the editor and open it again, then save from there.',
  invalid_idempotency_key:
    'This save was not accepted by the workspace, so nothing was saved.',
  report_body_invalid:
    'The workspace refused this report text because it falls outside the bounds it accepts, so nothing was saved.',
  report_capability_unavailable:
    'This workspace cannot hold saved reports, so nothing was saved.',
  // Archived reports are still reports for the purpose of this bound: the
  // workspace counts every one it holds, and it offers no way to remove one. So
  // the honest thing to say is that this workspace is full, not to send the
  // reader off to archive something that would free nothing.
  report_document_limit:
    'This workspace has reached the most saved reports it can hold, so nothing was saved.'
    + ' Archived reports still count toward that limit and no saved report can be removed here,'
    + ' so asking again will be refused the same way.',
  report_duplicate_period:
    'A report for this day is already saved in this workspace. It was left exactly as it is and nothing new was saved.'
    + ' Open the saved report to work on it.',
  report_idempotency_capacity:
    'The workspace could not take another save just now, so nothing was saved. Wait a moment and save again.',
  report_source_changed:
    'The daily report this draft started from is no longer the current one, so nothing was saved.'
    + ' Read the current report first and save from that; nothing here was replaced with it.',
  report_storage_full:
    'There is no room left in this workspace for another saved report, so nothing was saved.',
  report_template_unsupported:
    'Only daily reports can be saved to this workspace, so nothing was saved.',
  store_sync_required:
    'This workspace is not in sync, so nothing was saved. Bring it back in sync, then save again.',
  workspace_mismatch:
    'This report belongs to a different workspace than the one open here, so nothing was saved.',
}

/** What the workspace refused, and what the reader can still do about it. */
export function promotionRefusalMessage(code: string): string {
  const detail = REFUSALS[code] ?? 'This report could not be saved to the workspace, so nothing was saved.'
  return detail + ' ' + FALLBACK_ADVICE
}

const INITIAL_PROMOTION_STATE: PromotionState = { operation: null, phase: { kind: 'idle' } }

export function reportPromotionReducer(
  state: PromotionState,
  action: PromotionAction,
): PromotionState {
  switch (action.type) {
    case 'reset':
      // Identity changed. Returning the SAME object when there is nothing to
      // clear keeps the mount pass from costing an extra render.
      return state === INITIAL_PROMOTION_STATE ? state : INITIAL_PROMOTION_STATE
    case 'begin':
      return { operation: action.operation, phase: { kind: 'saving' } }
    case 'saved':
      // The frozen request is spent: it has an answer, and this slice creates
      // once. Nothing further may reuse its key.
      return { operation: null, phase: { kind: 'saved', contentRevision: action.contentRevision } }
    case 'refused':
      // The operation is RETAINED. A determinate refusal is still an answer to
      // one exact question, and asking it again must not invent a new key.
      return { ...state, phase: { kind: 'refused', message: action.message } }
    case 'unsettled':
      return { ...state, phase: { kind: 'unsettled', message: action.message } }
    default:
      return state
  }
}

/** The same report, asked for twice. Field by field, with no key in the compare. */
export function isSamePayload(
  left: CreateReportDocumentInput | undefined,
  right: CreateReportDocumentInput,
): boolean {
  return (
    left !== undefined
    && left.workspaceUid === right.workspaceUid
    && left.template === right.template
    && left.period.kind === right.period.kind
    && left.period.date === right.period.date
    && left.sourceDigest === right.sourceDigest
    && left.sourceGeneratedAt === right.sourceGeneratedAt
    && left.markdown === right.markdown
  )
}

function payloadFor(
  coordinate: ReportDraftCoordinate,
  base: DraftBase,
  text: string,
): CreateReportDocumentInput {
  return {
    workspaceUid: coordinate.workspaceUid,
    template: coordinate.template,
    period: { kind: 'day', date: coordinate.date },
    // The digest and instant the draft is BOUND to, never the ones a newer
    // generation happens to carry. Adopting those silently would save the
    // reader's prose under a report identity they never looked at.
    sourceDigest: base.sourceDigest,
    sourceGeneratedAt: base.baseGeneratedAt,
    markdown: text,
  }
}

export function useReportPromotion({
  base,
  coordinate,
  text,
}: ReportPromotionInput): ReportPromotionModel {
  const [state, dispatch] = useReducer(reportPromotionReducer, INITIAL_PROMOTION_STATE)
  const guard = useOwnerGuard()
  const { owned, owner } = guard
  const identity = coordinateKey(coordinate)

  useEffect(() => {
    // A new identity owns the panel from here. The token moves even on A → B → A,
    // so a result from the first A can no longer speak for the third.
    owner.current += 1
    dispatch({ type: 'reset' })
  }, [identity, owner])

  const run = useCallback(
    (operation: PromotionOperation) => {
      const token = owner.current
      dispatch({ type: 'begin', operation })
      void createReportDocument(operation.payload, operation.key).then(
        owned(token, (data: ReportCreateData) =>
          dispatch({ type: 'saved', contentRevision: data.content_entry.content_revision }),
        ),
        owned(token, (error: unknown) =>
          dispatch(
            error instanceof ApiError
              ? { type: 'refused', message: promotionRefusalMessage(error.code) }
              : { type: 'unsettled', message: UNSETTLED_MESSAGE + ' ' + FALLBACK_ADVICE },
          ),
        ),
      )
    },
    [owned, owner],
  )

  const pending = state.phase.kind === 'saving'

  const save = useCallback(() => {
    if (base === null || pending) return
    const payload = payloadFor(coordinate, base, text)
    // One key per exact payload. Asking the identical question again reuses the
    // key that identifies it; a payload that differs by so much as a character
    // is a different report and gets its own.
    const operation = isSamePayload(state.operation?.payload, payload)
      ? (state.operation as PromotionOperation)
      : { payload, key: createIdempotencyKey() }
    run(operation)
  }, [base, coordinate, pending, run, state.operation, text])

  const retry = useCallback(() => {
    // Deliberately blind to `text`: the point of a retry is that the workspace
    // is asked the SAME question, so an unsettled write can be reconciled
    // instead of duplicated.
    if (state.operation === null || pending) return
    run(state.operation)
  }, [pending, run, state.operation])

  return useMemo(
    () => ({
      pending,
      phase: state.phase,
      retry,
      retryable: state.operation !== null && !pending,
      save,
    }),
    [pending, retry, save, state.operation, state.phase],
  )
}
