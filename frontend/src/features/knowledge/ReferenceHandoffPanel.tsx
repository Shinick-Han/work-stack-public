import { useState, type ReactNode } from 'react'

import { Button, LoadingBlock } from '../../components/Primitives'
import {
  localExcerptView,
  localReferenceView,
  referenceOriginLabel,
  type KnowledgeReferenceView,
} from './knowledgeSourceView'
import type { KnowledgeSavedReference, KnowledgeTaskRef, KnowledgeVault } from './knowledgeTypes'
import { ReferenceChoiceRow, ReferenceExcerptCard } from './ReferenceRows'
import { RESUME_BRIEF_HANDOFF_NOTE } from './referenceBrief'
import {
  selectionAtLimit,
  type CaptureBriefCounts,
  type PreparedReferenceHandoff,
} from './referenceHandoff'
import {
  checkpointAttribution,
  checkpointProvenanceLabel,
  progressOmissionAcknowledgement,
  resumeProgressKey,
  UNBOUND_RESUME_PROGRESS,
  type ResumeProgressFacts,
} from './resumeProgressContract'
import {
  STALE_PROGRESS_COPY,
  useReferenceHandoff,
  type ReferenceHandoffSeed,
} from './useReferenceHandoff'
import './ReferenceHandoffPanel.css'

export interface ReferenceHandoffPanelProps {
  task: KnowledgeTaskRef
  workspaceUid: string
  progress?: ResumeProgressFacts
  /** Reference list already loaded by the owning panel for this same binding. */
  seed?: ReferenceHandoffSeed | null
  vaults?: readonly KnowledgeVault[]
  /** Source-specific per-reference actions the owning panel wants beside a row. */
  renderRowActions?: (reference: KnowledgeSavedReference) => ReactNode
}

type HandoffSession = ReturnType<typeof useReferenceHandoff>

interface ReferenceRowModel {
  reference: KnowledgeSavedReference
  view: KnowledgeReferenceView
}

function progressNotice(progress: ResumeProgressFacts) {
  if (progress.status === 'loading') return 'Loading the latest recorded progress…'
  if (progress.status === 'ready') {
    return `Includes the recorded progress snapshot. ${checkpointAttribution(progress.snapshot)}. `
      + `${checkpointProvenanceLabel(progress.snapshot)}.`
  }
  if (progress.status === 'partial') {
    return `The latest checkpoint could not be read in full (${progress.missing.join(', ')}). `
      + `${checkpointAttribution(progress.snapshot)}. ${checkpointProvenanceLabel(progress.snapshot)}.`
  }
  if (progress.status === 'none') {
    return 'No progress recorded yet. The brief will say so instead of claiming a next step.'
  }
  return `${progress.reason} You can prepare the brief without a recorded progress snapshot.`
}

function ChangedSourceAcknowledgements({
  session,
  vaults,
}: {
  session: HandoffSession
  vaults: readonly KnowledgeVault[]
}) {
  const prepared = session.state.prepared
  if (!prepared || prepared.sources !== 'vault-selection') return null
  if (prepared.changedIds.length === 0) return null
  return (
    <div aria-label="Changed sources" className="reference-handoff__ack" role="group">
      <p>
        A selected source changed after it was linked. Confirm each one to include the snapshot
        that was prepared from the current text.
      </p>
      {prepared.changedIds.map((referenceId) => {
        const index = prepared.referenceIds.indexOf(referenceId)
        const read = prepared.envelope.references[index]
        if (!read) return null
        const view = localExcerptView(read, vaults)
        const name = referenceOriginLabel(view)
        return (
          <label key={referenceId}>
            <input
              aria-label={`Acknowledge changed source for ${view.documentTitle} · ${name}`}
              checked={session.state.acknowledged.includes(referenceId)}
              onChange={(event) => session.acknowledge(referenceId, event.target.checked)}
              type="checkbox"
            />
            <span>{view.documentTitle} · {name}</span>
          </label>
        )
      })}
    </div>
  )
}

function preparedBriefSummary(prepared: PreparedReferenceHandoff) {
  if (prepared.sources === 'capture-only') {
    return 'One Markdown brief with the saved Task, its recorded progress snapshot, and the stored '
      + 'Capture catalog from that prepare. No local document excerpt is included. '
  }
  return 'One Markdown brief with the saved Task, its recorded progress snapshot, the selected '
    + 'references with their linked reasons, and the stored Capture catalog from that prepare. '
}

/** Truthful for the shape that was actually prepared: neither half applies to both. */
function oversizeBriefCopy(prepared: PreparedReferenceHandoff) {
  if (prepared.sources === 'capture-only') {
    return 'The resume brief exceeded 32KiB and was not prepared for copying. This Task’s '
      + 'recorded progress and stored Capture catalog are too large to hand off as one brief.'
  }
  return 'The resume brief exceeded 32KiB. Select fewer references, or use Other formats to take '
    + 'the JSON snapshot instead.'
}

/** Counts only. No stored Capture title, body or identifier is shown outside the brief. */
function capturePreviewCopy(capture: CaptureBriefCounts) {
  if (capture.included === 0) {
    return 'No linked Capture source is included. This brief carries the saved Task and its '
      + 'recorded progress only.'
  }
  const included = capture.included === 1
    ? '1 saved Capture source is included.'
    : `${capture.included} saved Capture sources are included.`
  if (capture.omitted === 0) return included
  const omitted = capture.omitted === 1
    ? '1 more was omitted.'
    : `${capture.omitted} more were omitted.`
  return `${included} ${omitted}`
}

function PreparedBriefPreview({
  prepared,
  vaults,
}: {
  prepared: PreparedReferenceHandoff
  vaults: readonly KnowledgeVault[]
}) {
  if (prepared.sources === 'capture-only') {
    return (
      <div className="reference-handoff__disclosure-body">
        <p className="reference-handoff__hint">
          Prepared snapshot only. Saved Capture metadata can change after you copy it.
        </p>
        <p className="reference-handoff__hint">
          {capturePreviewCopy(prepared.capture)} No local document excerpt is included.
        </p>
      </div>
    )
  }
  return (
    <div className="reference-handoff__disclosure-body">
      <p className="reference-handoff__hint">
        Prepared snapshot only. Linked documents can change after you copy it.
      </p>
      {prepared.envelope.references.map((read, index) => (
        <ReferenceExcerptCard
          key={prepared.referenceIds[index]}
          view={localExcerptView(read, vaults)}
        />
      ))}
    </div>
  )
}

/** The JSON envelope is a vault-selection format, so it is absent rather than disabled. */
function PreparedBriefFormats({
  prepared,
  session,
}: {
  prepared: PreparedReferenceHandoff
  session: HandoffSession
}) {
  if (prepared.sources !== 'vault-selection') return null
  return (
    <details className="reference-handoff__disclosure">
      <summary>Other formats</summary>
      <div className="reference-handoff__disclosure-body">
        <p className="reference-handoff__hint">
          The machine-readable snapshot keeps the existing knowledge-context schema.
        </p>
        <div className="reference-handoff__actions">
          <Button disabled={!session.canExport} onClick={session.copy}>
            {session.state.copied ? 'JSON copied' : 'Copy JSON'}
          </Button>
          <Button disabled={!session.canExport} onClick={session.download}>Download JSON</Button>
        </div>
      </div>
    </details>
  )
}

function PreparedBrief({
  session,
  vaults,
}: {
  session: HandoffSession
  vaults: readonly KnowledgeVault[]
}) {
  const { state } = session
  const prepared = state.prepared
  if (!prepared) return null
  return (
    <div className="reference-handoff__ready">
      <h5>Resume brief ready</h5>
      <p className="reference-handoff__hint">
        {preparedBriefSummary(prepared)}
        {RESUME_BRIEF_HANDOFF_NOTE}
      </p>
      <ChangedSourceAcknowledgements session={session} vaults={vaults} />
      {session.briefStale ? (
        <p className="reference-handoff__warning" role="status">{STALE_PROGRESS_COPY}</p>
      ) : null}
      {prepared.briefMarkdown ? null : (
        <p className="reference-handoff__warning" role="status">{oversizeBriefCopy(prepared)}</p>
      )}
      <div className="reference-handoff__primary">
        <Button disabled={!session.canExportBrief} onClick={session.copyBrief} variant="primary">
          {state.briefCopied ? 'Resume brief copied' : 'Copy resume brief'}
        </Button>
      </div>
      <details className="reference-handoff__disclosure">
        <summary>Preview included content</summary>
        <PreparedBriefPreview prepared={prepared} vaults={vaults} />
      </details>
      <PreparedBriefFormats prepared={prepared} session={session} />
    </div>
  )
}

function SelectionList({
  renderRowActions,
  session,
  views,
}: {
  renderRowActions?: (reference: KnowledgeSavedReference) => ReactNode
  session: HandoffSession
  views: ReferenceRowModel[]
}) {
  const busy = session.state.pending !== null
  const limitReached = selectionAtLimit(session.selectedIds)
  return (
    <ul aria-label="Select references to include" className="reference-handoff__list">
      {views.map(({ reference, view }) => (
        <ReferenceChoiceRow
          actions={renderRowActions?.(reference)}
          disabled={busy}
          key={view.referenceId}
          limitReached={limitReached}
          onToggle={() => session.toggle(view.referenceId)}
          selected={session.selectedIds.includes(view.referenceId)}
          view={view}
        />
      ))}
    </ul>
  )
}

/**
 * What an empty region may say when no list was ever enumerated — a client with no
 * knowledge host, or a host list that has not succeeded. It states what is unavailable
 * without claiming the Task has no saved references, and without pointing at the link
 * tools, which are not rendered on such a client.
 */
export const UNLISTED_REFERENCES_COPY =
  'Linked local documents are not available to list here, so no local document excerpt can be '
  + 'selected for this brief.'

function ReferenceRegion({
  enumerated,
  renderRowActions,
  session,
  views,
}: {
  enumerated: boolean
  renderRowActions?: (reference: KnowledgeSavedReference) => ReactNode
  session: HandoffSession
  views: ReferenceRowModel[]
}) {
  const { error, loading } = session.state
  if (loading) return <LoadingBlock label="Loading linked references…" />
  if (views.length === 0) {
    if (error) return null
    // "None are linked yet" is a claim about the Task, so it needs a list that was really
    // enumerated. Absence of the host is absence of local document access, not an answer.
    if (!enumerated) return <p className="reference-handoff__hint">{UNLISTED_REFERENCES_COPY}</p>
    return (
      <p className="reference-handoff__hint">
        No references are linked to this task yet. Use Find or link a document below.
      </p>
    )
  }
  return <SelectionList renderRowActions={renderRowActions} session={session} views={views} />
}

function ProgressConsent({
  acknowledged,
  copy,
  onChange,
}: {
  acknowledged: boolean
  copy: string | null
  onChange: (checked: boolean) => void
}) {
  if (!copy) return null
  return (
    <label className="reference-handoff__consent">
      <input
        checked={acknowledged}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
      <span>{copy}</span>
    </label>
  )
}

function HandoffError({ session }: { session: HandoffSession }) {
  const { error, loading, pending } = session.state
  if (!error) return null
  return (
    <div className="inline-error" role="alert">
      <span>{error}</span>
      {pending === null && !loading ? (
        <Button onClick={session.reload} variant="ghost">Try again</Button>
      ) : null}
    </div>
  )
}

/**
 * Preparation needs a settled reference selection and a settled decision about the
 * recorded progress. Neither an in-flight audit nor an unreadable record is allowed to
 * become a silently omitted section.
 *
 * Selecting nothing is a real choice — a Capture-only brief — but only once the linked
 * reference list itself is settled. A list that failed is not an empty one.
 */
function canPrepareBrief(
  session: HandoffSession,
  progress: ResumeProgressFacts,
  progressAcknowledged: boolean,
) {
  return session.state.pending === null
    && !session.state.loading
    && (session.selectedIds.length > 0 || session.state.referencesKnown)
    && progress.status !== 'loading'
    && progressAcknowledged
}

export function ReferenceHandoffPanel({
  progress = UNBOUND_RESUME_PROGRESS,
  renderRowActions,
  seed = null,
  task,
  vaults = [],
  workspaceUid,
}: ReferenceHandoffPanelProps) {
  const session = useReferenceHandoff(workspaceUid, task, true, { progress, seed, vaults })
  const { state } = session
  const views = state.references.map((reference) => ({
    reference,
    view: localReferenceView(reference, vaults),
  }))
  // Acknowledging is bound to the facts that were on screen, so a later change asks again.
  const [acknowledgedProgressKey, setAcknowledgedProgressKey] = useState<string | null>(null)
  const omissionCopy = progressOmissionAcknowledgement(progress)
  const progressKey = resumeProgressKey(progress)
  const progressAcknowledged = omissionCopy === null || acknowledgedProgressKey === progressKey

  return (
    <section aria-label="References for this task" className="reference-handoff">
      <h4>References for this task</h4>
      <p className="reference-handoff__hint">
        Select references to include. None are selected by default, at most eight. The brief uses
        this saved Task revision, not unsaved draft text. With nothing selected, the brief covers
        the saved Task, its recorded progress and its stored Capture catalog, without any local
        document excerpt.
      </p>
      {/* Without a seed this panel lists for itself, where a host it cannot reach becomes
          the visible error above instead of an empty list. */}
      <ReferenceRegion
        enumerated={seed?.enumerated ?? true}
        renderRowActions={renderRowActions}
        session={session}
        views={views}
      />
      {/* A permanent description of the recorded facts, not a status update: the live
          regions stay on the transient stale and over-cap warnings. */}
      <p className="reference-handoff__hint">{progressNotice(progress)}</p>
      <ProgressConsent
        acknowledged={progressAcknowledged}
        copy={omissionCopy}
        onChange={(checked) => setAcknowledgedProgressKey(checked ? progressKey : null)}
      />
      <div className="reference-handoff__actions">
        <span className="reference-handoff__count">
          {session.selectedIds.length} of {views.length} selected
        </span>
        <Button
          disabled={!canPrepareBrief(session, progress, progressAcknowledged)}
          onClick={session.prepare}
          variant="primary"
        >
          {state.prepared ? 'Prepare brief again' : 'Prepare brief'}
        </Button>
        {state.pending === 'prepare' ? (
          <Button onClick={session.cancel} variant="ghost">Cancel</Button>
        ) : null}
      </div>
      <PreparedBrief session={session} vaults={vaults} />
      <HandoffError session={session} />
    </section>
  )
}
