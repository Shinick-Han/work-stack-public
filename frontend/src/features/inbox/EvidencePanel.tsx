import { Pill } from '../../components/Primitives'
import { formatDateTime } from '../../utils/format'
import type { Capture, CaptureRetrievalEvidence, RetrievalSourceType, RetrievalVersionState } from '../../domain/types'
import type { VerificationEvidenceEntry } from '../../api/knowledgeVerification'
import { SavedSourceCheckAction, SourceVerificationAction, SourceVerificationStatus } from './SourceVerificationView'
import { useEvidenceVerification } from './useEvidenceVerification'
import { useSourceCheckHistory } from './useSourceCheckHistory'
import './EvidencePanel.css'

const SCORE_NOTE = 'Retrieval score ranks how closely evidence matched the request. It is not a correctness probability.'
const TRUNCATED_WARNING = 'Some evidence was truncated. This is part of what was returned, not all of it.'
const UNVERIFIED_NOTE = 'Reported source types are claims. They are not independently verified.'

function reportedTypeLabel(type: RetrievalSourceType): string {
  if (type === 'notion.page') return 'Reported Notion page (claim)'
  if (type === 'nas.file') return 'Reported NAS file (claim)'
  return 'Knowledge answer'
}

function versionLabel(state: RetrievalVersionState): string {
  if (state === 'unreported') return 'Version unreported'
  return 'Version reported, unverified'
}

function displayVersionState(state: RetrievalVersionState): RetrievalVersionState {
  return state === 'unreported' ? 'unreported' : 'reported_unverified'
}

/**
 * One evidence row. `observation` is a transient read of the source at a timestamp; it
 * is displayed beside the stored claims and never replaces them, so a row that was
 * checked still shows the same reported type and the same unverified version state.
 */
function EvidenceRow({
  item,
  observation,
}: {
  item: CaptureRetrievalEvidence
  observation: VerificationEvidenceEntry | null
}) {
  const state = displayVersionState(item.version_state)
  return (
    <li className="evidence-panel__item">
      <strong className="evidence-panel__item-title">{item.title}</strong>
      <p className="evidence-panel__item-meta">
        <span>{reportedTypeLabel(item.reported_source_type)}</span>
        {' · '}
        <span>{versionLabel(state)}</span>
      </p>
      {observation ? <SourceVerificationStatus entry={observation} /> : null}
    </li>
  )
}

function EvidenceSummary({ capture }: { capture: Capture }) {
  const retrieval = capture.retrieval ?? null
  return (
    <dl className="evidence-panel__meta">
      <div>
        <dt>Retrieved</dt>
        <dd>{formatDateTime(capture.source.retrieved_at)}</dd>
      </div>
      <div>
        <dt>Evidence count</dt>
        <dd>{retrieval ? retrieval.evidence.length : 0}</dd>
      </div>
      {retrieval ? (
        <div>
          <dt>Retrieval score</dt>
          <dd>
            {retrieval.confidence.level}
            <small>{retrieval.confidence.score} — not a correctness probability</small>
          </dd>
        </div>
      ) : null}
    </dl>
  )
}

/**
 * `workspaceUid` is optional on purpose. Callers that do not hand one over — the 1.0
 * drawer, and every isolated test that renders this panel alone — keep exactly the
 * unverified display they had, with no action and no network at all. The same is true of
 * a capture with no checkable evidence: both hooks refuse it, so nothing is requested.
 *
 * When there *is* a checkable capture the panel reads the saved source check once, and
 * the owner's answer to that read decides which surface it offers. R27-E's saved check is
 * the normal one; the old transient check survives only for an owner that answers the
 * history route with a plain `not_found` 404, and it is labelled as keeping nothing. Any
 * other failure is undetermined: no check is offered until an explicit reload settles it,
 * because a 503 or an unreadable body is not evidence about the server's age.
 */
export function EvidencePanel({ capture, workspaceUid }: { capture: Capture; workspaceUid?: string }) {
  const retrieval = capture.retrieval ?? null
  const history = useSourceCheckHistory(capture, workspaceUid)
  // Always called, never reached except in legacy mode: this hook requests nothing until
  // its `check()` runs, so holding it costs no network on a supported owner.
  const verification = useEvidenceVerification(capture, workspaceUid)
  const legacy = history.mode === 'legacy'
  const entries = legacy ? verification.observation?.entries ?? null : history.saved?.entries ?? null
  return (
    <section aria-label="Capture evidence" className="drawer-section evidence-panel">
      <div className="evidence-panel__head">
        <h3>Evidence</h3>
        <Pill tone="neutral">Manual import / unverified source</Pill>
      </div>
      <p className="evidence-panel__state">{UNVERIFIED_NOTE}</p>
      <EvidenceSummary capture={capture} />
      {retrieval ? <p className="evidence-panel__footnote">{SCORE_NOTE}</p> : null}
      {retrieval?.truncated ? <p className="evidence-panel__warning">{TRUNCATED_WARNING}</p> : null}
      {verification.available ? (
        legacy ? (
          <SourceVerificationAction
            error={verification.error}
            observation={verification.observation}
            onCheck={verification.check}
            pending={verification.pending}
            savedChecksUnavailable
          />
        ) : (
          <SavedSourceCheckAction
            error={history.error}
            loading={history.loading}
            onRecord={history.recordCheck}
            onReload={history.reload}
            pending={history.pending}
            reloadable={history.mode === 'unavailable' || history.needsReload}
            saved={history.saved}
            supported={history.mode === 'supported'}
          />
        )
      ) : null}
      {retrieval?.evidence.length ? (
        <ol className="evidence-panel__rows">
          {retrieval.evidence.map((item, index) => (
            <EvidenceRow
              item={item}
              key={`${item.document_ref}:${item.chunk_ref ?? ''}:${index}`}
              observation={entries?.[index] ?? null}
            />
          ))}
        </ol>
      ) : (
        <p className="evidence-panel__empty">No retrieval evidence is stored on this capture.</p>
      )}
    </section>
  )
}
