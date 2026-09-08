import { useId } from 'react'

import { Button } from '../../components/Primitives'
import { MAX_REPORT_MARKDOWN_CHARS } from '../../domain/reportDocuments'
import { FALLBACK_ADVICE } from './reportDraftMessages'
import { isStaleSource, type DailyReportDraftSource } from './reportDraftEditorModel'
import { useReportPromotion, type PromotionPhase } from './useReportPromotion'
import type { ReportDraftEditorModel } from './useReportDraftEditor'
import './reportPromotionPanel.css'

/**
 * The one control that saves the edited text to the workspace.
 *
 * It is deliberately a separate act from "Save local draft" in the footer, and
 * the copy says so in both directions: saving here does not touch the local
 * draft, and saving locally does not put anything in the workspace. It also says
 * what the workspace ends up holding — a DRAFT report, not a finalized one.
 *
 * The panel creates. Reading the saved history, revising it and finalizing it
 * are not offered here and are not implied by anything it says.
 */

const DESCRIPTION =
  'This keeps the text above as a report in your workspace. It is a separate copy:'
  + ' the local draft on this device is not changed, moved or removed, and the'
  + ' saved report starts as a draft — saving does not finalize it.'

const STALE_MESSAGE =
  'A newer daily report has been generated since this draft started, so this text'
  + ' cannot be saved to the workspace as it stands. Nothing here was replaced.'
  + ' Read the current report first and save from that.'

const OVER_LIMIT_MESSAGE =
  'This text is longer than one saved report can hold, so it cannot be saved to the workspace.'

function savedMessage(contentRevision: number): string {
  return (
    'Saved to your workspace as report version '
    + String(contentRevision)
    + '. It is a draft there and is not finalized. The local draft on this device was not changed.'
  )
}

/** Code points, because the workspace bound counts characters, not UTF-16 units. */
function codePointLength(text: string): number {
  return [...text].length
}

/**
 * What the workspace last said, and what stands in the way of asking it.
 *
 * At most one refusal is ever on screen: a phase message only exists once a
 * request has been sent, and no request is sent while the source or the length
 * blocks it.
 */
function PromotionNotices({
  overLimit,
  phase,
  stale,
}: {
  overLimit: boolean
  phase: PromotionPhase
  stale: boolean
}) {
  if (stale) {
    return <p className="report-promotion__stale" role="status">{STALE_MESSAGE} {FALLBACK_ADVICE}</p>
  }
  if (overLimit) {
    return (
      <p className="report-promotion__notice" role="alert">{OVER_LIMIT_MESSAGE} {FALLBACK_ADVICE}</p>
    )
  }
  if (phase.kind === 'refused') {
    return <p className="report-promotion__notice" role="alert">{phase.message}</p>
  }
  if (phase.kind === 'unsettled') {
    return <p className="report-promotion__unsettled" role="alert">{phase.message}</p>
  }
  if (phase.kind === 'saved') {
    return (
      <p className="report-promotion__saved" role="status">{savedMessage(phase.contentRevision)}</p>
    )
  }
  return null
}

export function ReportPromotionPanel({
  model,
  source,
}: {
  model: ReportDraftEditorModel
  source: DailyReportDraftSource
}) {
  const titleId = useId()
  const { pinned, prompting, state } = model
  const promotion = useReportPromotion({ base: state.base, coordinate: pinned, text: state.text })

  const stale = isStaleSource(state.base, source)
  const overLimit = codePointLength(state.text) > MAX_REPORT_MARKDOWN_CHARS
  const saved = promotion.phase.kind === 'saved'
  // An unsettled write owns the panel until the reader settles it: offering a
  // second, differently-keyed save here is exactly how a report gets saved twice.
  const unsettled = promotion.phase.kind === 'unsettled'
  const blocked =
    prompting
    || state.loading
    || state.base === null
    || stale
    || overLimit
    || promotion.pending
    || saved
    || unsettled

  return (
    <section aria-labelledby={titleId} className="report-promotion">
      <h3 className="report-promotion__title" id={titleId}>Save to workspace</h3>
      <p className="report-promotion__description">{DESCRIPTION}</p>
      <p className="report-promotion__coordinate">Daily report for {pinned.date}</p>

      <PromotionNotices overLimit={overLimit} phase={promotion.phase} stale={stale} />

      <div className="report-promotion__actions">
        <Button disabled={blocked} onClick={promotion.save} variant="primary">
          Save report to workspace
        </Button>
        {unsettled ? (
          <Button disabled={!promotion.retryable} onClick={promotion.retry}>
            Ask the workspace again
          </Button>
        ) : null}
        {promotion.pending ? (
          <span className="report-promotion__progress" role="status">Saving to the workspace…</span>
        ) : null}
      </div>
    </section>
  )
}
