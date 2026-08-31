import { useEffect, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Dialog } from '../../components/Dialog'
import { Button } from '../../components/Primitives'
import type { TaskPriority, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { sourceProviders, type SourceProviderKey } from './sourceProviders'
import { classifyMicrosoftSourceUrl, sanitizeMicrosoftSourceUrl, type ExternalSourceCapture, type SourceCaptureDraft } from './sourceCapture'

interface SourceCaptureDialogProps {
  open: boolean
  seed: ExternalSourceCapture | null
  provider: SourceProviderKey
  workspace: WorkspaceProjection
  sourceUrlManaged?: boolean
  onClose: () => void
  onSubmit: (draft: SourceCaptureDraft) => Promise<unknown>
}

export function SourceCaptureDialog({ onClose, onSubmit, open, provider, seed, sourceUrlManaged = false, workspace }: SourceCaptureDialogProps) {
  const [selectedProvider, setSelectedProvider] = useState<SourceProviderKey>(provider)
  const [captureTitle, setCaptureTitle] = useState('')
  const [text, setText] = useState('')
  const [taskTitle, setTaskTitle] = useState('')
  const [taskDetail, setTaskDetail] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [capturedAt, setCapturedAt] = useState('')
  const [priority, setPriority] = useState<TaskPriority>('P2')
  const [due, setDue] = useState('')
  const [objectiveId, setObjectiveId] = useState('')
  const [destination, setDestination] = useState<'new' | 'existing'>('new')
  const [taskId, setTaskId] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setSelectedProvider(seed?.provider ?? provider)
    setCaptureTitle(seed?.title ?? '')
    setText(seed?.text ?? '')
    setTaskTitle('')
    setTaskDetail(seed?.text ?? '')
    setSourceUrl(seed?.sourceUrl ?? '')
    setCapturedAt(seed?.capturedAt ?? new Date().toISOString())
    setPriority('P2')
    setDue('')
    setObjectiveId('')
    setDestination('new')
    setTaskId('')
    setError(null)
  }, [open, provider, seed])

  const activeProvider = sourceProviders.find((item) => item.key === selectedProvider)!
  const taskTitleSuggestion = useMemo(() => captureTitle.trim().slice(0, 240), [captureTitle])
  const safeSourceUrl = sanitizeMicrosoftSourceUrl(sourceUrl)
  const sourceLinkKind = classifyMicrosoftSourceUrl(selectedProvider, sourceUrl)
  const sourceLinkCopy = sourceLinkKind === 'item'
    ? { title: `Specific ${activeProvider.label} item link captured`, detail: 'This view appears to identify the open message, conversation, or note.' }
    : sourceLinkKind === 'auth'
      ? { title: 'Sign-in link captured', detail: `Open the exact ${activeProvider.label} item before capturing so Work Stack can retain a useful reference.` }
      : sourceLinkKind === 'app'
        ? { title: `General ${activeProvider.label} app link captured`, detail: 'This opens the app, not a guaranteed message or note. Open the exact item and capture again for a deep link.' }
        : { title: 'No safe source link available', detail: 'The Task can still be created, but it will not include a Microsoft link.' }

  const acceptTaskTitleSuggestion = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!taskTitle && taskTitleSuggestion && (event.key === 'Tab' || event.key === 'ArrowRight')) {
      event.preventDefault()
      setTaskTitle(taskTitleSuggestion)
    }
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!captureTitle.trim() || !text.trim()) {
      setError('A Capture title and reviewed source content are required.')
      return
    }
    if (destination === 'existing' && !taskId) {
      setError('Choose the existing Task that should receive this source.')
      return
    }
    if (destination === 'new' && !taskTitle.trim()) {
      setError('A Task title is required. Press Tab or Right Arrow to accept the suggestion.')
      return
    }
    setPending(true)
    setError(null)
    try {
      await onSubmit({
        provider: selectedProvider,
        captureTitle: captureTitle.trim(),
        text: text.trim(),
        taskTitle: destination === 'new' ? taskTitle.trim() : null,
        taskDetail: destination === 'new' ? taskDetail.trim() : null,
        sourceUrl: sourceUrl.trim(),
        capturedAt,
        priority,
        due: due || null,
        objectiveIds: objectiveId ? [objectiveId] : [],
        taskId: destination === 'existing' ? taskId : null,
      })
      onClose()
    } catch (submitError) {
      setError(getErrorMessage(submitError))
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog
      description="Review the selected Microsoft content before it becomes planning state. This is recorded as a manual web capture, never as OOB-verified provenance."
      footer={<><Button disabled={pending} onClick={onClose} variant="ghost">Cancel</Button><Button disabled={pending || !captureTitle.trim() || !text.trim() || (destination === 'new' && !taskTitle.trim()) || (destination === 'existing' && !taskId)} form="source-capture-form" icon="plus" type="submit" variant="primary">{pending ? (destination === 'new' ? 'Creating…' : 'Attaching…') : (destination === 'new' ? 'Create Task from source' : 'Attach to selected Task')}</Button></>}
      onClose={onClose}
      open={open}
      size="large"
      title="Microsoft source → Task"
    >
      <form className="source-capture-form" id="source-capture-form" onSubmit={(event) => void submit(event)}>
        <div className="privacy-note">
          <span className="privacy-note__mark">REVIEW</span>
          <div><strong>You control what enters Work Stack.</strong><p>Outlook reads the visible subject and body only when you capture. Teams and OneNote use explicit clipboard text. Recipients, attachments, and hidden page content are not collected.</p></div>
        </div>
        {!sourceUrlManaged ? (
          <fieldset className="source-provider-choice">
            <legend>Source</legend>
            {sourceProviders.map((item) => (
              <label className={selectedProvider === item.key ? 'is-selected' : ''} key={item.key}>
                <input checked={selectedProvider === item.key} name="source-provider" onChange={() => setSelectedProvider(item.key)} type="radio" />
                <span><strong>{item.label}</strong><small>{item.captureMode === 'selection' ? 'Direct selection' : 'Explicit clipboard'}</small></span>
              </label>
            ))}
          </fieldset>
        ) : null}
        <fieldset className="source-destination-choice">
          <legend>Use this source</legend>
          <label className={destination === 'new' ? 'is-selected' : ''}><input checked={destination === 'new'} name="source-destination" onChange={() => setDestination('new')} type="radio" /><span><strong>Create a new Task</strong><small>Turn the reviewed context into new work.</small></span></label>
          <label className={destination === 'existing' ? 'is-selected' : ''}><input checked={destination === 'existing'} name="source-destination" onChange={() => setDestination('existing')} type="radio" /><span><strong>Attach to existing Task</strong><small>Preserve this as source context for work already tracked.</small></span></label>
        </fieldset>
        {destination === 'existing' ? (
          <label className="field"><span>Existing Task</span><select onChange={(event) => setTaskId(event.target.value)} value={taskId}><option value="">Choose a Task…</option>{[...workspace.tasks].sort((left, right) => left.id.localeCompare(right.id)).map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
        ) : null}
        <label className="field"><span>Capture title</span><input autoFocus maxLength={500} onChange={(event) => setCaptureTitle(event.target.value)} placeholder="A short label for the source" value={captureTitle} /></label>
        <label className="field"><span>Captured source content</span><textarea maxLength={4000} onChange={(event) => setText(event.target.value)} placeholder={selectedProvider === 'outlook' ? 'Open an Outlook message and capture again, or paste the relevant text.' : 'Copy the relevant source text before capturing.'} rows={6} value={text} /></label>
        {destination === 'new' ? <>
          <label className="field"><span>Task title <small>T-00xx · press Tab or → to accept the suggestion</small></span><div className="task-title-composer"><span aria-hidden="true">T-00xx</span><input aria-label="Task title" maxLength={240} onChange={(event) => setTaskTitle(event.target.value)} onKeyDown={acceptTaskTitleSuggestion} value={taskTitle} />{!taskTitle && taskTitleSuggestion ? <i aria-hidden="true">{taskTitleSuggestion}</i> : null}</div></label>
          <label className="field"><span>Action detail</span><textarea maxLength={4000} onChange={(event) => setTaskDetail(event.target.value)} placeholder="Describe what needs to be done." rows={5} value={taskDetail} /></label>
        </> : null}
        {sourceUrlManaged ? (
          <div className={`source-link-status ${sourceLinkKind === 'item' ? 'is-captured' : sourceLinkKind === 'app' || sourceLinkKind === 'auth' ? 'is-warning' : ''}`}>
            <span aria-hidden="true">{sourceLinkKind === 'item' ? '✓' : sourceLinkKind === 'missing' ? '—' : '!'}</span>
            <div>
              <strong>{sourceLinkCopy.title}</strong>
              <small>{sourceLinkCopy.detail}</small>
              {safeSourceUrl ? <a className="source-link-status__url" href={safeSourceUrl} rel="noopener noreferrer" target="_blank">{safeSourceUrl}</a> : null}
            </div>
          </div>
        ) : (
          <label className="field"><span>Source URL</span><input onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://… (optional; unsafe or unsupported URLs are omitted)" type="url" value={sourceUrl} /></label>
        )}
        {destination === 'new' ? <div className="form-grid form-grid--three">
          <label className="field"><span>Objective</span><select onChange={(event) => setObjectiveId(event.target.value)} value={objectiveId}><option value="">No objective</option>{workspace.objectives.map((objective) => <option key={objective.id} value={objective.id}>{objective.id} · {objective.title}</option>)}</select></label>
          <label className="field"><span>Priority</span><select onChange={(event) => setPriority(event.target.value as TaskPriority)} value={priority}>{(['P0', 'P1', 'P2', 'P3'] as const).map((value) => <option key={value}>{value}</option>)}</select></label>
          <label className="field"><span>Due</span><div className="date-input"><input className={due ? '' : 'is-empty'} lang="en-US" onChange={(event) => setDue(event.target.value)} type="date" value={due} />{due ? null : <i aria-hidden="true">YYYY-MM-DD</i>}</div></label>
        </div> : null}
        {error ? <p className="inline-error" role="alert">{error}</p> : null}
      </form>
    </Dialog>
  )
}
