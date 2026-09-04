import { useEffect, useRef, useState, type FormEvent } from 'react'
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

interface CaptureDrawerProps {
  capture: Capture
  workspace: WorkspaceProjection
  providerGates?: MicrosoftProviderGates
  onClose: () => void
  onCreateTask: (input: CaptureTaskInput) => Promise<Task>
}

export function CaptureDrawer({ capture, onClose, onCreateTask, providerGates = microsoftProviderGates, workspace }: CaptureDrawerProps) {
  const sourceUrl = safeExternalUrl(capture.source.web_url)
  const trust = captureTrust(capture, providerGates)
  const loadedSource = useRef({ id: capture.id, revision: capture.revision, fingerprint: capture.source.fingerprint })
  const [formOpen, setFormOpen] = useState(false)
  const [title, setTitle] = useState(capture.source.display_title)
  const [detail, setDetail] = useState(capture.normalized.context || capture.normalized.summary)
  const [priority, setPriority] = useState<CaptureTaskInput['priority']>('P2')
  const [due, setDue] = useState('')
  const [objectiveId, setObjectiveId] = useState('')
  const [tags, setTags] = useState(capture.normalized.tags.join(', '))
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sourceDirty, setSourceDirty] = useState(false)
  const [sourceConflict, setSourceConflict] = useState<{ from: number; to: number } | null>(null)
  const [refreshedRevision, setRefreshedRevision] = useState<number | null>(null)

  const refreshSourceFields = () => {
    setTitle(capture.source.display_title)
    setDetail(capture.normalized.context || capture.normalized.summary)
    setTags(capture.normalized.tags.join(', '))
    setSourceDirty(false)
    setSourceConflict(null)
    setRefreshedRevision(capture.revision)
    loadedSource.current = { id: capture.id, revision: capture.revision, fingerprint: capture.source.fingerprint }
  }

  useEffect(() => {
    const loaded = loadedSource.current
    if (capture.id !== loaded.id) {
      loadedSource.current = { id: capture.id, revision: capture.revision, fingerprint: capture.source.fingerprint }
      setFormOpen(false)
      setTitle(capture.source.display_title)
      setDetail(capture.normalized.context || capture.normalized.summary)
      setPriority('P2')
      setDue('')
      setObjectiveId('')
      setTags(capture.normalized.tags.join(', '))
      setError(null)
      setSourceDirty(false)
      setSourceConflict(null)
      setRefreshedRevision(null)
      return
    }
    if (capture.revision <= loaded.revision) return
    if (capture.source.fingerprint === loaded.fingerprint) {
      loadedSource.current = { ...loaded, revision: capture.revision }
      return
    }
    if (formOpen && sourceDirty) {
      setSourceConflict({ from: loaded.revision, to: capture.revision })
      setRefreshedRevision(null)
      return
    }
    refreshSourceFields()
  }, [capture.id, capture.revision, capture.source.fingerprint, formOpen, sourceDirty])

  const keepDraft = () => {
    loadedSource.current = { id: capture.id, revision: capture.revision, fingerprint: capture.source.fingerprint }
    setSourceConflict(null)
    setRefreshedRevision(null)
  }

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
                <div className="capture-revision-note__actions"><Button onClick={keepDraft} variant="ghost">Keep my draft</Button><Button onClick={refreshSourceFields} variant="secondary">Refresh source fields</Button></div>
              </div>
            ) : refreshedRevision !== null ? <p className="capture-revision-note" role="status">Source draft refreshed to Capture revision {refreshedRevision}.</p> : null}
            <form className="form-stack" onSubmit={(event) => void submit(event)}>
              <label className="field field--prominent"><span>Task title</span><input autoFocus maxLength={240} onChange={(event) => { setSourceDirty(true); setRefreshedRevision(null); setTitle(event.target.value) }} required value={title} /></label>
              <label className="field"><span>Definition of done / source context</span><textarea onChange={(event) => { setSourceDirty(true); setRefreshedRevision(null); setDetail(event.target.value) }} rows={5} value={detail} /></label>
              <div className="form-grid">
                <label className="field"><span>Priority</span><select onChange={(event) => setPriority(event.target.value as CaptureTaskInput['priority'])} value={priority}>{TASK_PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
                <DateInput className="field" label="Due" disabled={pending} onChange={setDue} value={due} />
              </div>
              <label className="field"><span>Objective <small>optional</small></span><select onChange={(event) => setObjectiveId(event.target.value)} value={objectiveId}><option value="">Unaligned / Operations</option>{workspace.objectives.map((objective) => <option key={objective.id} value={objective.id}>{objective.id} · {getObjectiveTitle(objective)}</option>)}</select></label>
              <label className="field"><span>Tags <small>comma separated</small></span><input onChange={(event) => { setSourceDirty(true); setRefreshedRevision(null); setTags(event.target.value) }} value={tags} /></label>
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
        </section>
      </div>
    </aside>
  )
}
