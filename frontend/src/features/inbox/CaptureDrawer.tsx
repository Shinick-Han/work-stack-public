import { useState, type FormEvent } from 'react'
import { Icon } from '../../components/Icon'
import { DateInput } from '../../components/DateInput'
import { Button, IconButton, Pill } from '../../components/Primitives'
import {
  TASK_PRIORITIES,
  type Capture,
  type CaptureTaskInput,
  type Task,
  type WorkspaceProjection,
} from '../../domain/types'
import { formatDateTime, getErrorMessage, getObjectiveTitle, safeExternalUrl } from '../../utils/format'
import { microsoftProviderGates, type MicrosoftProviderGates } from '../../config/providerGates'
import { captureTrust } from './captureTrust'
import { useCaptureSourceDraft } from './useCaptureSourceDraft'
import { EvidencePanel } from './EvidencePanel'
import { UPDATED_CONTEXT_ENTRY_NOTE, knowledgeLaunchSeedFromCapture } from './knowledgeLaunchSeed'
import { KnowledgeRequestLauncher } from './KnowledgeRequestLauncher'
import type { KnowledgeImportEnvelope } from '../../domain/knowledgeImport'

interface CaptureDrawerProps {
  capture: Capture
  workspace: WorkspaceProjection
  providerGates?: MicrosoftProviderGates
  onClose: () => void
  onCreateTask: (input: CaptureTaskInput) => Promise<Task>
  /** The parent's existing review handoff. Without it this drawer offers no search. */
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
}

/**
 * The entry that starts a *new* reviewed knowledge search from this saved source.
 *
 * It is not a refresh and does not say it is. Work Stack cannot promise that a search
 * re-reads the exact document this Capture was made from, and it records no link between
 * the two, so the sentence above the control says what actually happens: a new search the
 * user reviews, and a Capture that is left exactly as it is. The control is hidden
 * entirely when the parent has no review handoff to give the result to, because an
 * execute the user cannot review is a dead end rather than a feature.
 */
function UpdatedContextEntry({
  capture,
  onReviewKnowledge,
  workspaceUid,
}: {
  capture: Capture
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  workspaceUid: string
}) {
  if (!onReviewKnowledge) return null
  return (
    <>
      <p className="field-help">{UPDATED_CONTEXT_ENTRY_NOTE}</p>
      <KnowledgeRequestLauncher
        onReviewKnowledge={onReviewKnowledge}
        seed={knowledgeLaunchSeedFromCapture(capture, workspaceUid)}
        workspaceUid={workspaceUid}
      />
    </>
  )
}

/**
 * Where this Capture came from, and what may be started from it. The provenance list is
 * read-only fact about the saved record; the search entry below it starts something new
 * and leaves the record alone. They sit together because that is the question an owner
 * asks here — "what is this, and is there anything newer?" — and keeping them in one
 * small component keeps the drawer itself readable.
 */
function SourceProvenanceSection({
  capture,
  onReviewKnowledge,
  sourceUrl,
  workspaceUid,
}: {
  capture: Capture
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  sourceUrl: string | null
  workspaceUid: string
}) {
  return (
    <section className="drawer-section">
      <h3>Source & provenance</h3>
      <dl className="provenance-list">
        <div><dt>Provider</dt><dd>{capture.source.provider}</dd></div>
        <div><dt>Resource</dt><dd>{capture.source.resource_type}</dd></div>
        <div><dt>Retrieved</dt><dd>{formatDateTime(capture.source.retrieved_at)}</dd></div>
        <div><dt>Adapter</dt><dd>{capture.provenance.adapter} · {capture.provenance.adapter_version}</dd></div>
        <div><dt>Policy</dt><dd>{capture.provenance.redaction_policy_version}</dd></div>
        <div><dt>Raw retained</dt><dd>No</dd></div>
      </dl>
      {sourceUrl ? <a className="button button--secondary drawer-source-link" href={sourceUrl} rel="noopener noreferrer" target="_blank"><Icon name="arrowUpRight" size={15} /> Open Microsoft source</a> : null}
      <UpdatedContextEntry
        capture={capture}
        onReviewKnowledge={onReviewKnowledge}
        workspaceUid={workspaceUid}
      />
    </section>
  )
}

export function CaptureDrawer({ capture, onClose, onCreateTask, onReviewKnowledge, providerGates = microsoftProviderGates, workspace }: CaptureDrawerProps) {
  const sourceUrl = capture.retrieval ? null : safeExternalUrl(capture.source.web_url)
  const trust = captureTrust(capture, providerGates)
  const [formOpen, setFormOpen] = useState(false)
  const [priority, setPriority] = useState<CaptureTaskInput['priority']>('P2')
  const [due, setDue] = useState('')
  const [objectiveId, setObjectiveId] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const source = useCaptureSourceDraft(capture, formOpen, () => {
    setFormOpen(false)
    setPriority('P2')
    setDue('')
    setObjectiveId('')
    setError(null)
  })
  const { detail, refreshedRevision, sourceConflict, tags, title } = source

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!title.trim() || pending) return
    setPending(true)
    setError(null)
    try {
      await onCreateTask({
        title: title.trim(),
        detail: detail.trim() || undefined,
        priority,
        due: due || null,
        tags: tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        objective_ids: objectiveId ? [objectiveId] : [],
      })
    } catch (operationError) {
      setError(getErrorMessage(operationError))
    } finally {
      setPending(false)
    }
  }

  return (
    <aside aria-label={`Capture ${capture.id}`} className="detail-drawer">
      <header className="drawer-header">
        <div className="drawer-header__identity"><span className="capture-glyph"><Icon name="inbox" size={17} /></span><div><span>Capture</span><strong>{capture.id}</strong></div></div>
        <IconButton icon="close" label="Close capture drawer" onClick={onClose} variant="ghost" />
      </header>
      <div className="drawer-body capture-drawer">
        <div className="capture-drawer__heading">
          <Pill tone={trust.tone}>{trust.label}</Pill>
          <h2>{capture.source.display_title}</h2>
          <p>{capture.normalized.summary}</p>
          <Button className="capture-task-button" icon="plus" onClick={() => setFormOpen((value) => !value)} variant={formOpen ? 'ghost' : 'primary'}>
            {formOpen ? 'Cancel task creation' : 'Create task from this source'}
          </Button>
        </div>

        {formOpen ? (
          <section aria-label="Create task from this source" className="drawer-section capture-task-form">
            <h3>New source-based task</h3>
            {sourceConflict ? (
              <div aria-label="Capture source updated" className="capture-revision-note capture-revision-note--conflict" role="status">
                <div><strong>Sanitized source updated</strong><span>Capture changed from revision {sourceConflict.from} to {sourceConflict.to}. Your edited source fields were preserved.</span></div>
                <div className="capture-revision-note__actions"><Button onClick={source.keepDraft} variant="ghost">Keep my draft</Button><Button onClick={source.refreshSourceFields} variant="secondary">Refresh source fields</Button></div>
              </div>
            ) : refreshedRevision !== null ? <p className="capture-revision-note" role="status">Source draft refreshed to Capture revision {refreshedRevision}.</p> : null}
            <form className="form-stack" onSubmit={(event) => void submit(event)}>
              <label className="field field--prominent"><span>Task title</span><input autoFocus maxLength={240} onChange={(event) => source.editTitle(event.target.value)} required value={title} /></label>
              <label className="field"><span>Definition of done / source context</span><textarea onChange={(event) => source.editDetail(event.target.value)} rows={5} value={detail} /></label>
              <div className="form-grid">
                <label className="field"><span>Priority</span><select onChange={(event) => setPriority(event.target.value as CaptureTaskInput['priority'])} value={priority}>{TASK_PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
                <DateInput className="field" label="Due" disabled={pending} onChange={setDue} value={due} />
              </div>
              <label className="field"><span>Objective <small>optional</small></span><select onChange={(event) => setObjectiveId(event.target.value)} value={objectiveId}><option value="">Unaligned / Operations</option>{workspace.objectives.map((objective) => <option key={objective.id} value={objective.id}>{objective.id} · {getObjectiveTitle(objective)}</option>)}</select></label>
              <label className="field"><span>Tags <small>comma separated</small></span><input onChange={(event) => source.editTags(event.target.value)} value={tags} /></label>
              <p className="field-help">The new Task will stay linked to this sanitized Capture as its source context.</p>
              {error ? <p className="inline-error" role="alert">{error}</p> : null}
              <Button disabled={pending || !title.trim()} type="submit" variant="primary">{pending ? 'Creating…' : 'Create linked task'}</Button>
            </form>
          </section>
        ) : null}

        <section className="drawer-section">
          <h3>Sanitized context</h3>
          <p className="long-copy">{capture.normalized.context || 'No additional context was provided.'}</p>
        </section>
        <section className="drawer-section">
          <h3>Action items</h3>
          {capture.normalized.action_items.length ? <ol className="drawer-action-list">{capture.normalized.action_items.map((action, index) => <li key={action.id ?? index}><span>{index + 1}</span><div><strong>{action.title}</strong><p>{action.detail}</p><small>{action.priority}{action.due ? ` · due ${action.due}` : ''}</small></div></li>)}</ol> : <p className="muted-copy">No action items. You can still create a Task from the source above.</p>}
        </section>
        <SourceProvenanceSection
          capture={capture}
          onReviewKnowledge={onReviewKnowledge}
          sourceUrl={sourceUrl}
          workspaceUid={workspace.workspace.id}
        />
        <EvidencePanel capture={capture} workspaceUid={workspace.workspace.id} />
      </div>
    </aside>
  )
}
