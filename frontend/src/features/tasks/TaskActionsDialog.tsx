import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, createIdempotencyKey } from '../../api/client'
import { Button } from '../../components/Primitives'
import { Dialog } from '../../components/Dialog'
import { TASK_PRIORITIES, TASK_STATUSES, type Task, type TaskPriority, type TaskStatus } from '../../domain/types'
import { getErrorMessage, priorityLabels, statusLabels } from '../../utils/format'

interface TaskActionsDialogProps {
  open: boolean
  task: Task
  onClose: () => void
  onSaved: (task: Task) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

export function TaskActionsDialog({ open, task, onClose, onSaved, onNotice }: TaskActionsDialogProps) {
  const [note, setNote] = useState('')
  const [subtaskTitle, setSubtaskTitle] = useState('')
  const [subtaskPriority, setSubtaskPriority] = useState<TaskPriority>('P2')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const noteIntentKey = useRef<string | null>(null)
  const subtaskIntentKey = useRef<string | null>(null)

  useEffect(() => {
    if (!open) {
      setNote('')
      setSubtaskTitle('')
      setSubtaskPriority('P2')
      setPending(null)
      setError(null)
      noteIntentKey.current = null
      subtaskIntentKey.current = null
    }
  }, [open])

  const run = async (
    key: string,
    operation: () => Promise<Task>,
    message: string,
    options?: { onSuccess?: () => void; committed?: (task: Task) => boolean },
  ) => {
    if (pending) return
    setPending(key)
    setError(null)
    try {
      const updated = await operation()
      options?.onSuccess?.()
      onSaved(updated)
      onNotice(message)
      onClose()
    } catch (reason) {
      if (options?.committed) {
        try {
          const authoritative = (await api.getTask(task.id)).task
          onSaved(authoritative)
          if (options.committed(authoritative)) {
            onNotice(`${message} (verified after reconnect)`)
            onClose()
            return
          }
        } catch {
          // Preserve the original mutation error when authoritative refresh also fails.
        }
      }
      setError(getErrorMessage(reason))
      onNotice(getErrorMessage(reason), 'error')
    } finally {
      setPending(null)
    }
  }

  const addNote = (event: FormEvent) => {
    event.preventDefault()
    const text = note.trim()
    if (!text) return
    noteIntentKey.current ??= createIdempotencyKey()
    void run(
      'note',
      () => api.addTaskNote(task.id, text, task.revision, noteIntentKey.current!),
      'Task log entry added',
      { onSuccess: () => { noteIntentKey.current = null } },
    )
  }

  const addSubtask = (event: FormEvent) => {
    event.preventDefault()
    const title = subtaskTitle.trim()
    if (!title) return
    subtaskIntentKey.current ??= createIdempotencyKey()
    void run(
      'subtask',
      () => api.addSubtask(task.id, title, subtaskPriority, task.revision, subtaskIntentKey.current!),
      'Step added',
      { onSuccess: () => { subtaskIntentKey.current = null } },
    )
  }

  if (!open) return null

  return (
    <Dialog
      description="Add supporting detail without crowding the main Task form. Every change advances the Task revision."
      onClose={() => { if (!pending) onClose() }}
      open={open}
      size="large"
      title="Task actions"
    >
      <div className="task-actions-grid">
        <section className="task-action-section" aria-labelledby="task-actions-subtasks">
          <header><h3 id="task-actions-subtasks">Steps</h3><span>{task.subtasks.length}</span></header>
          {task.subtasks.length ? (
            <div className="task-action-list">
              {task.subtasks.map((subtask) => (
                <label key={subtask.id}>
                  <span><strong>{subtask.title}</strong><small>{subtask.id} · {subtask.priority ?? 'P2'}</small></span>
                  <select
                    aria-label={`Change ${subtask.id} status`}
                    disabled={pending !== null}
                    onChange={(event) => {
                      const status = event.target.value as TaskStatus
                      void run(
                        `subtask:${subtask.id}`,
                        () => api.setSubtaskStatus(task.id, subtask.id, status, task.revision),
                        `${subtask.id} moved to ${statusLabels[status]}`,
                        {
                          committed: (authoritative) => authoritative.subtasks.some((item) => (
                            item.id === subtask.id && item.status === status
                          )),
                        },
                      )
                    }}
                    value={subtask.status ?? 'open'}
                  >
                    {TASK_STATUSES.map((status) => <option key={status} value={status}>{statusLabels[status]}</option>)}
                  </select>
                </label>
              ))}
            </div>
          ) : <p className="task-action-empty">No steps yet.</p>}
          <form className="task-action-form" onSubmit={addSubtask}>
            <label className="field"><span>New step</span><input disabled={pending !== null} onChange={(event) => { subtaskIntentKey.current = null; setSubtaskTitle(event.target.value) }} placeholder="A concrete next step" value={subtaskTitle} /></label>
            <label className="field"><span>Priority</span><select disabled={pending !== null} onChange={(event) => { subtaskIntentKey.current = null; setSubtaskPriority(event.target.value as TaskPriority) }} value={subtaskPriority}>{TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{priority} · {priorityLabels[priority]}</option>)}</select></label>
            <Button disabled={pending !== null || !subtaskTitle.trim()} type="submit" variant="primary">{pending === 'subtask' ? 'Adding…' : 'Add step'}</Button>
          </form>
        </section>

        <section className="task-action-section" aria-labelledby="task-actions-notes">
          <header><h3 id="task-actions-notes">Task log</h3><span>{task.notes.length}</span></header>
          {task.notes.length ? <ul className="task-note-list">{task.notes.map((item, index) => <li key={`${item.date ?? 'note'}-${index}`}><p>{item.text}</p>{item.date ? <time>{item.date}</time> : null}</li>)}</ul> : <p className="task-action-empty">No log entries yet.</p>}
          <form className="task-action-form" onSubmit={addNote}>
            <label className="field"><span>New log entry</span><textarea disabled={pending !== null} onChange={(event) => { noteIntentKey.current = null; setNote(event.target.value) }} placeholder="Preserve a decision, constraint, or useful context." rows={4} value={note} /></label>
            <Button disabled={pending !== null || !note.trim()} type="submit">{pending === 'note' ? 'Adding…' : 'Add log entry'}</Button>
          </form>
        </section>
      </div>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
    </Dialog>
  )
}
