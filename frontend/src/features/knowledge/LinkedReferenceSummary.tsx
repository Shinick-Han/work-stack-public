import { Button, LoadingBlock } from '../../components/Primitives'
import { localReferenceView } from './knowledgeSourceView'
import type { KnowledgeTaskRef } from './knowledgeTypes'
import { ReferenceSummaryRow } from './ReferenceRows'
import { useLinkedReferences } from './useLinkedReferences'
import './LinkedReferenceSummary.css'

const SUMMARY_ROW_LIMIT = 3

export interface LinkedReferenceSummaryProps {
  task: KnowledgeTaskRef
  workspaceUid: string
  /** Opens the in-drawer reference subview. The summary never opens documents itself. */
  onOpenReferences: () => void
}

export function LinkedReferenceSummary({
  onOpenReferences,
  task,
  workspaceUid,
}: LinkedReferenceSummaryProps) {
  const { reload, state } = useLinkedReferences(workspaceUid, task)
  const views = state.references.map((reference) => localReferenceView(reference, state.vaults))
  const shown = views.slice(0, SUMMARY_ROW_LIMIT)
  const hidden = views.length - shown.length
  const settled = state.hostAvailable && !state.loading && !state.error

  return (
    <section aria-label="References for this task" className="linked-summary">
      <h4>References for this task</h4>
      {!state.hostAvailable ? (
        <p className="linked-summary__note">
          Open this Task in the Work Stack desktop app to see and link documents on this device.
        </p>
      ) : null}
      {state.loading ? <LoadingBlock label="Loading linked references…" /> : null}
      {settled && views.length === 0 ? (
        <p className="linked-summary__note">No references are linked to this task yet.</p>
      ) : null}
      {shown.length ? (
        <ul className="linked-summary__list">
          {shown.map((view) => <ReferenceSummaryRow key={view.referenceId} view={view} />)}
        </ul>
      ) : null}
      {hidden > 0 ? (
        <p className="linked-summary__note">
          {hidden === 1 ? '1 more reference is linked.' : `${hidden} more references are linked.`}
        </p>
      ) : null}
      {state.error ? (
        <div className="inline-error" role="alert">
          <span>{state.error}</span>
          <Button onClick={reload} variant="ghost">Try again</Button>
        </div>
      ) : null}
      <Button onClick={onOpenReferences} variant="primary">Prepare resume brief</Button>
    </section>
  )
}
