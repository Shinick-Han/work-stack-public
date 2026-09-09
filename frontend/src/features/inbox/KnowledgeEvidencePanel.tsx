import { Button, Pill } from '../../components/Primitives'
import { formatDateTime } from '../../utils/format'
import {
  buildKnowledgeEvidenceView,
  evidenceVerificationLabel,
  evidenceVerificationNote,
  evidenceVerificationTone,
  type KnowledgeEvidenceInput,
  type KnowledgeEvidenceRowView,
  type KnowledgeEvidenceView,
} from './knowledgeEvidenceView'
import './KnowledgeEvidencePanel.css'

/**
 * Read-only review surface for evidence imported against a scoped knowledge request.
 *
 * The panel presents what a reviewer needs to judge the import — where it came from,
 * when it was read, how the source verified, and which items were returned — and stops
 * there. Linking or creating a Task stays with the Capture actions the parent already
 * owns, so review is not duplicated in two places.
 *
 * Deliberate boundaries: no fetch, no provider branch, no link, no image and no path or
 * command built from supplied text. The one escape hatch to the original source is a
 * callback the parent supplies, enabled only by a separately supplied permission.
 *
 * Caller contract. Every string this component renders is display copy prepared by a
 * trusted presenter; the panel escapes and bounds it, but cannot tell an authored label
 * from a raw locator pasted into one, and does not try. See `knowledgeEvidenceView` for
 * the obligations on `evidence`. The source action is an authority decision, not a text
 * one: `openSourcePermitted` must be decided independently of anything displayed here,
 * and `onOpenSource` resolves its own approved locator — this panel passes it nothing.
 */

const TRUNCATED_WARNING =
  'Some matches were left out. This is part of what the source returned, not all of it.'
const CONFLICT_WARNING =
  'The items below disagree with each other. Read them before relying on either one.'
const CONFIDENCE_NOTE =
  'Retrieval confidence describes how closely an item matched the request. It is not a judgement that the content is correct.'
const READ_ONLY_NOTE =
  'This evidence is a saved copy for review. It does not change the Task on its own.'

export interface KnowledgeEvidencePanelProps {
  /** Presenter-authored display copy. See `knowledgeEvidenceView` for the obligations. */
  evidence: KnowledgeEvidenceInput
  /**
   * Section heading and accessible name. Trusted application copy, like the rest of this
   * screen's chrome — never imported evidence text and never a caller-forwarded value.
   */
  title?: string
  /**
   * Optional escape hatch to the original source. When omitted the panel offers none: it
   * has no URL, path or handler of its own and will not invent one. It is called with no
   * arguments by design — the parent resolves the approved locator from its own state,
   * never from text this panel displayed.
   */
  onOpenSource?: () => void
  /**
   * The authority half of the action, supplied independently of the callback, because
   * being able to call something is not the same as being allowed to. It must be an
   * authorization decision the parent already holds, never something inferred from the
   * evidence. Anything other than `true` renders the action disabled.
   */
  openSourcePermitted?: boolean
  /** Trusted application copy for the action, like `title`. Not evidence-derived text. */
  openSourceLabel?: string
}

function EvidenceMeta({ view }: { view: KnowledgeEvidenceView }) {
  return (
    <dl className="evidence-panel__meta">
      <div>
        <dt>Source</dt>
        <dd>{view.sourceLabel}</dd>
      </div>
      <div>
        <dt>Retrieved</dt>
        <dd>{formatDateTime(view.retrievedAt)}</dd>
      </div>
      <div>
        <dt>Evidence items</dt>
        <dd>{view.reportedCount}</dd>
      </div>
      {view.confidence ? (
        <div>
          <dt>Retrieval confidence</dt>
          <dd>
            {view.confidence.label}
            {view.confidence.score !== null ? <small>Match score {view.confidence.score}</small> : null}
          </dd>
        </div>
      ) : null}
    </dl>
  )
}

function EvidenceWarnings({ view }: { view: KnowledgeEvidenceView }) {
  if (!view.resultsTruncated && !view.resultsConflict) return null
  return (
    <ul className="evidence-panel__warnings">
      {view.resultsTruncated ? <li className="evidence-panel__warning">{TRUNCATED_WARNING}</li> : null}
      {view.resultsConflict ? <li className="evidence-panel__warning">{CONFLICT_WARNING}</li> : null}
    </ul>
  )
}

function EvidenceItem({ row }: { row: KnowledgeEvidenceRowView }) {
  return (
    <li className="evidence-item">
      <strong className="evidence-item__title">{row.title}</strong>
      <p className={`evidence-item__meta${row.versionLabel ? '' : ' evidence-item__meta--unverified'}`}>
        {row.typeLabel ? <span>{row.typeLabel}</span> : <span>Kind not supplied</span>}
        {' · '}
        {row.versionLabel ? <span>Version {row.versionLabel}</span> : <em>Version not supplied</em>}
      </p>
      {row.note ? <p className="evidence-item__note">{row.note}</p> : null}
    </li>
  )
}

function EvidenceRows({ view }: { view: KnowledgeEvidenceView }) {
  if (!view.rows.length) {
    return (
      <p className="evidence-panel__empty">
        No evidence items were supplied with this import. Nothing here has been checked
        against the source.
      </p>
    )
  }
  return (
    <>
      <ol className="evidence-panel__rows">
        {view.rows.map((row) => <EvidenceItem key={row.key} row={row} />)}
      </ol>
      {view.hiddenRowCount > 0 ? (
        <p className="evidence-panel__more">
          Showing {view.rows.length} of {view.reportedCount} items. The rest were not
          included in this review.
        </p>
      ) : null}
    </>
  )
}

export function KnowledgeEvidencePanel({
  evidence,
  onOpenSource,
  openSourceLabel = 'Open the original source',
  openSourcePermitted = false,
  title = 'Imported evidence',
}: KnowledgeEvidencePanelProps) {
  const view = buildKnowledgeEvidenceView(evidence)
  const openAllowed = onOpenSource !== undefined && openSourcePermitted === true
  return (
    <section aria-label={title} className="drawer-section evidence-panel">
      <div className="evidence-panel__head">
        <h3>{title}</h3>
        <Pill tone={evidenceVerificationTone(view.verification)}>
          {evidenceVerificationLabel(view.verification)}
        </Pill>
      </div>
      <p className="evidence-panel__state">{evidenceVerificationNote(view)}</p>
      {view.requestLabel ? (
        <p className="evidence-panel__scope"><strong>Requested:</strong> {view.requestLabel}</p>
      ) : null}
      <EvidenceMeta view={view} />
      {view.confidence ? <p className="evidence-panel__footnote">{CONFIDENCE_NOTE}</p> : null}
      <EvidenceWarnings view={view} />
      <EvidenceRows view={view} />
      {onOpenSource ? (
        <div className="evidence-panel__actions">
          <Button
            disabled={!openAllowed}
            icon="arrowUpRight"
            onClick={openAllowed ? onOpenSource : undefined}
            variant="secondary"
          >
            {openSourceLabel}
          </Button>
        </div>
      ) : null}
      {onOpenSource && !openAllowed ? (
        <p className="evidence-panel__footnote">
          Opening the original source is not available for this evidence yet.
        </p>
      ) : null}
      <p className="evidence-panel__footnote">{READ_ONLY_NOTE}</p>
    </section>
  )
}
