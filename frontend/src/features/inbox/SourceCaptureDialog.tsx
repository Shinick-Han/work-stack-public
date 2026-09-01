import { useEffect, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Dialog } from '../../components/Dialog'
import { Button } from '../../components/Primitives'
import type { Task, TaskPriority, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { sourceProvider, type SourceProviderKey } from './sourceProviders'
import { classifyMicrosoftSourceUrl, createSourceCaptureIntentId, sanitizeMicrosoftSourceUrlForProvider, type ExternalSourceCapture, type SourceCaptureDraft } from './sourceCapture'

interface SourceCaptureDialogProps {
  open: boolean
  seed: ExternalSourceCapture | null
  provider: SourceProviderKey
  workspace: WorkspaceProjection
  sourceUrlManaged?: boolean
  onClose: () => void
  onSubmit: (draft: SourceCaptureDraft) => Promise<unknown>
}

function matchingTasks(tasks: Task[], query: string) {
  const normalized = query.trim().toLocaleLowerCase()
  return [...tasks]
    .filter((task) => !normalized || `${task.id} ${task.title}`.toLocaleLowerCase().includes(normalized))
    .sort((left, right) => left.id.localeCompare(right.id))
}

function sourceLinkDescription(providerLabel: string, kind: ReturnType<typeof classifyMicrosoftSourceUrl>) {
  if (kind === 'item') return { title: `Specific ${providerLabel} item link captured`, detail: 'This view appears to identify the open message, conversation, or note.' }
  if (kind === 'auth') return { title: 'Sign-in link captured', detail: `Open the exact ${providerLabel} item before capturing so Work Stack can retain a useful reference.` }
  if (kind === 'app') return { title: `General ${providerLabel} app link captured`, detail: 'This opens the app, not a guaranteed message or note. Open the exact item and capture again for a deep link.' }
  return { title: 'No safe source link available', detail: 'The Task can still be created, but it will not include a Microsoft link.' }
}

function submitLabel(destination: 'new' | 'existing', pending: boolean) {
  if (pending) return destination === 'new' ? 'Creating…' : 'Attaching…'
  return destination === 'new' ? 'Create Task from source' : 'Attach to selected Task'
}

function ReviewNotice() {
  return <div className="privacy-note"><span className="privacy-note__mark">REVIEW</span><div><strong>You control what enters Work Stack.</strong><p>Outlook reads the visible subject and body only when you capture. Teams and OneNote use explicit clipboard text. Recipients, attachments, and hidden page content are not collected.</p></div></div>
}

function SourceReviewFields({
  captureTitle,
  onCaptureTitle,
  onSourceUrl,
  onText,
  provider,
  sourceUrl,
  sourceUrlManaged,
  text,
}: {
  captureTitle: string
  onCaptureTitle: (value: string) => void
  onSourceUrl: (value: string) => void
  onText: (value: string) => void
  provider: SourceProviderKey
  sourceUrl: string
  sourceUrlManaged: boolean
  text: string
}) {
  const definition = sourceProvider(provider)
  const safeSourceUrl = sanitizeMicrosoftSourceUrlForProvider(provider, sourceUrl)
  const sourceLinkKind = classifyMicrosoftSourceUrl(provider, sourceUrl)
  const copy = sourceLinkDescription(definition.label, sourceLinkKind)
  return <>
    <label className="field"><span>Source provider <small>Detected from the capture entry point</small></span><input aria-label="Source provider" readOnly value={`${definition.label} · ${definition.captureMode === 'selection' ? 'Visible selection' : 'Explicit clipboard'}`} /></label>
    <label className="field"><span>Capture title</span><input autoFocus maxLength={500} onChange={(event) => onCaptureTitle(event.target.value)} placeholder="A short label for the source" value={captureTitle} /></label>
    <label className="field"><span>Captured source content</span><textarea maxLength={4000} onChange={(event) => onText(event.target.value)} placeholder={provider === 'outlook' ? 'Open an Outlook message and capture again, or paste the relevant text.' : 'Copy the relevant source text before capturing.'} rows={6} value={text} /></label>
    {sourceUrlManaged ? (
      <div className={`source-link-status ${sourceLinkKind === 'item' ? 'is-captured' : sourceLinkKind === 'app' || sourceLinkKind === 'auth' ? 'is-warning' : ''}`}>
        <span aria-hidden="true">{sourceLinkKind === 'item' ? '✓' : sourceLinkKind === 'missing' ? '—' : '!'}</span>
        <div><strong>{copy.title}</strong><small>{copy.detail}</small>{safeSourceUrl ? <a className="source-link-status__url" href={safeSourceUrl} rel="noopener noreferrer" target="_blank">{safeSourceUrl}</a> : null}</div>
      </div>
    ) : (
      <label className="field"><span>Source URL</span><input onChange={(event) => onSourceUrl(event.target.value)} placeholder="https://… (optional; unsafe or unsupported URLs are omitted)" type="url" value={sourceUrl} /></label>
    )}
  </>
}

function DestinationChoice({ destination, onChange }: { destination: 'new' | 'existing'; onChange: (value: 'new' | 'existing') => void }) {
  return <fieldset className="source-destination-choice">
    <legend>Use this reviewed source</legend>
    <label className={destination === 'new' ? 'is-selected' : ''}><input checked={destination === 'new'} name="source-destination" onChange={() => onChange('new')} type="radio" /><span><strong>Create a new Task</strong><small>Turn the reviewed context into new work.</small></span></label>
    <label className={destination === 'existing' ? 'is-selected' : ''}><input checked={destination === 'existing'} name="source-destination" onChange={() => onChange('existing')} type="radio" /><span><strong>Attach to existing Task</strong><small>Preserve this as source context for work already tracked.</small></span></label>
  </fieldset>
}

function ExistingTaskFields({ destination, onQuery, onTask, query, taskId, tasks }: { destination: 'new' | 'existing'; onQuery: (value: string) => void; onTask: (value: string) => void; query: string; taskId: string; tasks: Task[] }) {
  if (destination !== 'existing') return null
  const matches = matchingTasks(tasks, query)
  return <>
    <label className="field"><span>Find existing Task <small>Search by Task ID or title</small></span><input aria-label="Find existing Task" onChange={(event) => onQuery(event.target.value)} placeholder="e.g. T-0021 or metric ownership" type="search" value={query} /></label>
    <label className="field"><span>Existing Task <small>{matches.length} matching Task{matches.length === 1 ? '' : 's'}</small></span><select aria-label="Existing Task" onChange={(event) => onTask(event.target.value)} value={taskId}><option value="">{matches.length ? 'Choose a Task…' : 'No matching Tasks'}</option>{matches.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
  </>
}

function NewTaskFields({
  detail,
  destination,
  due,
  objectiveId,
  objectives,
  onAcceptSuggestion,
  onDetail,
  onDue,
  onObjective,
  onPriority,
  onTitle,
  priority,
  suggestion,
  title,
}: {
  detail: string
  destination: 'new' | 'existing'
  due: string
  objectiveId: string
  objectives: WorkspaceProjection['objectives']
  onAcceptSuggestion: (event: KeyboardEvent<HTMLInputElement>) => void
  onDetail: (value: string) => void
  onDue: (value: string) => void
  onObjective: (value: string) => void
  onPriority: (value: TaskPriority) => void
  onTitle: (value: string) => void
  priority: TaskPriority
  suggestion: string
  title: string
}) {
  if (destination !== 'new') return null
  return <>
    <label className="field"><span>Task title <small>T-00xx · press Tab or → to accept the Capture title suggestion</small></span><div className="task-title-composer"><span aria-hidden="true">T-00xx</span><input aria-label="Task title" maxLength={240} onChange={(event) => onTitle(event.target.value)} onKeyDown={onAcceptSuggestion} value={title} />{!title && suggestion ? <i aria-hidden="true">{suggestion}</i> : null}</div></label>
    <label className="field"><span>Action detail</span><textarea maxLength={4000} onChange={(event) => onDetail(event.target.value)} placeholder="Describe what needs to be done." rows={5} value={detail} /></label>
    <div className="form-grid form-grid--three">
      <label className="field"><span>Objective</span><select onChange={(event) => onObjective(event.target.value)} value={objectiveId}><option value="">No objective</option>{objectives.map((objective) => <option key={objective.id} value={objective.id}>{objective.id} · {objective.title}</option>)}</select></label>
      <label className="field"><span>Priority</span><select onChange={(event) => onPriority(event.target.value as TaskPriority)} value={priority}>{(['P0', 'P1', 'P2', 'P3'] as const).map((value) => <option key={value}>{value}</option>)}</select></label>
      <label className="field"><span>Due</span><div className="date-input"><input className={due ? '' : 'is-empty'} lang="en-US" onChange={(event) => onDue(event.target.value)} type="date" value={due} />{due ? null : <i aria-hidden="true">YYYY-MM-DD</i>}</div></label>
    </div>
  </>
}

export function SourceCaptureDialog({ onClose, onSubmit, open, provider, seed, sourceUrlManaged = false, workspace }: SourceCaptureDialogProps) {
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
  const [taskQuery, setTaskQuery] = useState('')
  const [intentId, setIntentId] = useState(createSourceCaptureIntentId)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
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
    setTaskQuery('')
    setIntentId(createSourceCaptureIntentId())
    setError(null)
  }, [open, provider, seed])

  const inferredProvider = seed?.provider ?? provider
  const taskTitleSuggestion = useMemo(() => captureTitle.trim().slice(0, 240), [captureTitle])

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
        intentId,
        provider: inferredProvider,
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
      footer={<><Button disabled={pending} onClick={onClose} variant="ghost">Cancel</Button><Button disabled={pending || !captureTitle.trim() || !text.trim() || (destination === 'new' && !taskTitle.trim()) || (destination === 'existing' && !taskId)} form="source-capture-form" icon="plus" type="submit" variant="primary">{submitLabel(destination, pending)}</Button></>}
      onClose={onClose}
      open={open}
      size="large"
      title="Microsoft source → Task"
    >
      <form className="source-capture-form" id="source-capture-form" onSubmit={(event) => void submit(event)}>
        <ReviewNotice />
        <SourceReviewFields captureTitle={captureTitle} onCaptureTitle={setCaptureTitle} onSourceUrl={setSourceUrl} onText={setText} provider={inferredProvider} sourceUrl={sourceUrl} sourceUrlManaged={sourceUrlManaged} text={text} />
        <DestinationChoice destination={destination} onChange={setDestination} />
        <ExistingTaskFields destination={destination} onQuery={(value) => { setTaskQuery(value); setTaskId('') }} onTask={setTaskId} query={taskQuery} taskId={taskId} tasks={workspace.tasks} />
        <NewTaskFields detail={taskDetail} destination={destination} due={due} objectiveId={objectiveId} objectives={workspace.objectives} onAcceptSuggestion={acceptTaskTitleSuggestion} onDetail={setTaskDetail} onDue={setDue} onObjective={setObjectiveId} onPriority={setPriority} onTitle={setTaskTitle} priority={priority} suggestion={taskTitleSuggestion} title={taskTitle} />
        {error ? <p className="inline-error" role="alert">{error}</p> : null}
      </form>
    </Dialog>
  )
}
