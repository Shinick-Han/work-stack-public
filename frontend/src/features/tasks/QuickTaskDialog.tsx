import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Button } from '../../components/Primitives'
import { Dialog } from '../../components/Dialog'
import { DateInput } from '../../components/DateInput'
import { TASK_PRIORITIES, type QuickTaskInput, type WorkspaceProjection } from '../../domain/types'
import { getObjectiveTitle } from '../../utils/format'
import {
  EMPTY_QUICK_TASK_DRAFT,
  clearQuickTaskDraft,
  draftToInput,
  readQuickTaskDraft,
  writeQuickTaskDraft,
  type QuickTaskDraft,
} from './quickTaskDraft'
import { parseQuickTaskSyntax, QuickTaskSyntaxError } from './quickTaskSyntax'

interface QuickTaskDialogProps {
  initialObjectiveId?: string
  open: boolean
  workspace?: WorkspaceProjection
  pending: boolean
  error: string | null
  onClose: () => void
  onSubmit: (input: QuickTaskInput) => void
  resetDraftToken?: number
}

export function QuickTaskDialog({ error, initialObjectiveId = '', onClose, onSubmit, open, pending, resetDraftToken = 0, workspace }: QuickTaskDialogProps) {
  const submitGateRef = useRef(false)
  const appliedObjectiveRef = useRef<string | null>(null)
  const [draft, setDraft] = useState(readQuickTaskDraft)
  const [dateResetKey, setDateResetKey] = useState(0)
  const [syntaxMessage, setSyntaxMessage] = useState<string | null>(null)
  const { detail, due, estimateMinutes, objectiveId, priority, scheduled, tags, title } = draft

  useEffect(() => {
    if (!open) {
      submitGateRef.current = false
      appliedObjectiveRef.current = null
    }
  }, [open])

  useEffect(() => {
    if (!open || !initialObjectiveId || appliedObjectiveRef.current === initialObjectiveId) return
    if (!workspace?.objectives.some((objective) => objective.id === initialObjectiveId)) return
    appliedObjectiveRef.current = initialObjectiveId
    setDraft((current) => ({ ...current, objectiveId: initialObjectiveId }))
  }, [initialObjectiveId, open, workspace?.objectives])

  useEffect(() => writeQuickTaskDraft(draft), [draft])

  useEffect(() => {
    if (!resetDraftToken) return
    clearQuickTaskDraft()
    setDraft(EMPTY_QUICK_TASK_DRAFT)
    setDateResetKey((current) => current + 1)
  }, [resetDraftToken])

  useEffect(() => {
    if (!pending) submitGateRef.current = false
  }, [pending])

  const close = () => {
    if (pending || submitGateRef.current) return
    onClose()
  }

  const canEdit = () => !pending && !submitGateRef.current

  const changeDraft = <Key extends keyof QuickTaskDraft>(key: Key, value: QuickTaskDraft[Key]) => {
    if (canEdit()) setDraft((current) => ({ ...current, [key]: value }))
  }

  const clearDraft = () => {
    if (!canEdit()) return
    clearQuickTaskDraft()
    setDraft(EMPTY_QUICK_TASK_DRAFT)
    setDateResetKey((current) => current + 1)
    setSyntaxMessage(null)
  }

  const applyShorthand = () => {
    if (!canEdit()) return
    try {
      const parsed = parseQuickTaskSyntax(title, workspace?.objectives.map((item) => item.id) ?? [])
      setDraft((current) => ({
        ...current,
        title: parsed.title,
        priority: parsed.priority ?? current.priority,
        objectiveId: parsed.objectiveId ?? current.objectiveId,
        scheduled: parsed.scheduled ?? current.scheduled,
        due: parsed.due ?? current.due,
        estimateMinutes: parsed.estimateMinutes?.toString() ?? current.estimateMinutes,
        tags: [...new Set([
          ...current.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
          ...parsed.tags,
        ])].join(', '),
      }))
      setSyntaxMessage('Shorthand applied. Review the fields before creating the task.')
    } catch (syntaxError) {
      setSyntaxMessage(syntaxError instanceof QuickTaskSyntaxError ? syntaxError.message : 'Could not apply shorthand.')
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!title.trim() || pending || submitGateRef.current) return
    submitGateRef.current = true
    try {
      onSubmit(draftToInput(draft))
    } catch (submitError) {
      submitGateRef.current = false
      throw submitError
    }
  }

  return (
    <Dialog
      description="Capture the outcome now. This planning draft is saved locally until creation or an explicit clear."
      footer={<><Button disabled={pending} onClick={clearDraft} variant="ghost">Clear draft</Button><Button disabled={pending} onClick={close} variant="ghost">Close</Button><Button disabled={pending || !title.trim()} form="quick-task-form" type="submit" variant="primary">{pending ? 'Creating…' : 'Create task'}</Button></>}
      onClose={close}
      open={open}
      title="New task"
    >
      <form className="form-stack" id="quick-task-form" onSubmit={submit}>
        <label className="field field--prominent">
          <span>Task title</span>
          <input autoFocus disabled={pending} maxLength={240} onChange={(event) => changeDraft('title', event.target.value)} placeholder="What outcome needs to move?" required value={title} />
        </label>
        <div className="quick-task-shorthand">
          <Button disabled={pending || !title.trim()} onClick={applyShorthand} type="button" variant="ghost">Apply shorthand</Button>
          <small>!P1 @O-4 /plan 2026-09-02 /due 2026-09-04 #tag /estimate 90m</small>
        </div>
        {syntaxMessage ? <p className="field-hint" role="status">{syntaxMessage}</p> : null}
        <label className="field">
          <span>Definition of done <small>optional</small></span>
          <textarea disabled={pending} maxLength={4096} onChange={(event) => changeDraft('detail', event.target.value)} placeholder="Describe the result, not just the activity." rows={4} value={detail} />
        </label>
        <div className="form-grid">
          <label className="field"><span>Priority</span><select disabled={pending} onChange={(event) => changeDraft('priority', event.target.value as QuickTaskDraft['priority'])} value={priority}>{TASK_PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
          <DateInput className="field" label="Plan for" disabled={pending} onChange={(value) => changeDraft('scheduled', value)} resetKey={dateResetKey} value={scheduled} />
          <DateInput className="field" label="Due" disabled={pending} onChange={(value) => changeDraft('due', value)} resetKey={dateResetKey} value={due} />
          <label className="field"><span>Estimate <small>minutes</small></span><input disabled={pending} max={1440} min={1} onChange={(event) => changeDraft('estimateMinutes', event.target.value)} placeholder="90" type="number" value={estimateMinutes} /></label>
        </div>
        <label className="field"><span>Objective <small>optional</small></span><select disabled={pending} onChange={(event) => changeDraft('objectiveId', event.target.value)} value={objectiveId}><option value="">Unaligned / Operations</option>{workspace?.objectives.map((objective) => <option key={objective.id} value={objective.id}>{objective.id} · {getObjectiveTitle(objective)}</option>)}</select></label>
        <label className="field"><span>Tags <small>comma separated</small></span><input disabled={pending} maxLength={1000} onChange={(event) => changeDraft('tags', event.target.value)} placeholder="release, quality" value={tags} /></label>
        {error ? <p className="inline-error" role="alert">{error}</p> : null}
      </form>
    </Dialog>
  )
}
