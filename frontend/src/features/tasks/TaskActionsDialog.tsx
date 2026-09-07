import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, createIdempotencyKey, type TaskDeletionPreview, type TaskPermanentDeletionReceipt } from '../../api/client'
import { Button } from '../../components/Primitives'
import { Dialog } from '../../components/Dialog'
import { TASK_PRIORITIES, TASK_STATUSES, type Task, type TaskPriority, type TaskStatus } from '../../domain/types'
import { getErrorMessage, priorityLabels, statusLabels } from '../../utils/format'

interface TaskActionsDialogProps {
  open: boolean
  task: Task
  workspaceUid: string
  onClose: () => void
  onPermanentlyDeleted?: (receipt: TaskPermanentDeletionReceipt) => void
  onSaved: (task: Task) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

function ownedRemovalSummary(preview: TaskDeletionPreview) {
  const owned = preview.removed_task_owned_records
  return [
    `${owned.activity_events} activity events`,
    `${owned.idempotency_keys} idempotency keys`,
    `${owned.notes} notes`,
    `${owned.planning_events} planning events`,
    `${owned.replies} replies`,
    `${owned.work_sessions} work sessions`,
    `${owned.worklog_entries} worklog entries`,
  ].join(', ')
}

function DropTaskSection({
  confirmation,
  onCancel,
  onConfirmation,
  onOpen,
  onSubmit,
  open,
  pending,
  task,
}: {
  confirmation: string
  onCancel: () => void
  onConfirmation: (value: string) => void
  onOpen: () => void
  onSubmit: (event: FormEvent) => void
  open: boolean
  pending: string | null
  task: Task
}) {
  if (task.status === 'dropped') {
    return <p className="task-action-danger__deleted" role="status">This Task has already been dropped from active work.</p>
  }
  if (!open) {
    return <Button disabled={pending !== null} onClick={onOpen} variant="danger">Drop Task…</Button>
  }
  return (
    <form className="task-action-delete-confirmation" onSubmit={onSubmit}>
      <label className="field">
        <span>Type <strong>{task.id}</strong> to confirm</span>
        <input
          autoComplete="off"
          autoFocus
          disabled={pending !== null}
          onChange={(event) => onConfirmation(event.target.value)}
          value={confirmation}
        />
      </label>
      <p>This appends a Dropped transition; it does not rewrite or erase prior records. The Task stays recoverable and filterable.</p>
      <div>
        <Button disabled={pending !== null} onClick={onCancel} type="button" variant="ghost">Cancel</Button>
        <Button disabled={pending !== null || confirmation !== task.id} type="submit" variant="danger">
          {pending === 'drop' ? 'Dropping…' : 'Drop Task'}
        </Button>
      </div>
    </form>
  )
}

function PermanentDeleteSummary({ preview }: { preview: TaskDeletionPreview }) {
  const modified = preview.modified_references
  const captures = preview.unlinked_captures
  return (
    <dl className="task-action-list">
      <div>
        <dt>Task</dt>
        <dd>{preview.task.id} · {preview.task.title}</dd>
      </div>
      <div>
        <dt>Revision</dt>
        <dd>{preview.task.revision}</dd>
      </div>
      <div>
        <dt>UID</dt>
        <dd>{preview.task.uid}</dd>
      </div>
      <div>
        <dt>Owned records removed</dt>
        <dd>{ownedRemovalSummary(preview)}</dd>
      </div>
      <div>
        <dt>Modified references</dt>
        <dd>{modified.notes.length} notes, {modified.tasks.length} tasks</dd>
      </div>
      <div>
        <dt>Unlinked captures</dt>
        <dd>{captures.actions} actions, {captures.captures.length} captures</dd>
      </div>
      <div>
        <dt>Backup</dt>
        <dd>{preview.backup.retention} · {preview.backup.location}</dd>
      </div>
    </dl>
  )
}

function PermanentDeleteSection({
  confirmation,
  onCancel,
  onConfirmation,
  onStart,
  onSubmit,
  pending,
  preview,
  task,
}: {
  confirmation: string
  onCancel: () => void
  onConfirmation: (value: string) => void
  onStart: () => void
  onSubmit: (event: FormEvent) => void
  pending: string | null
  preview: TaskDeletionPreview | null
  task: Task
}) {
  if (!preview) {
    return (
      <Button disabled={pending !== null} onClick={onStart} variant="danger">
        {pending === 'preview' ? 'Loading preview…' : 'Delete permanently…'}
      </Button>
    )
  }
  return (
    <form className="task-action-delete-confirmation" onSubmit={onSubmit}>
      <PermanentDeleteSummary preview={preview} />
      <label className="field">
        <span>Type <strong>{task.id}</strong> to confirm permanent deletion</span>
        <input
          autoComplete="off"
          autoFocus
          disabled={pending !== null}
          onChange={(event) => onConfirmation(event.target.value)}
          value={confirmation}
        />
      </label>
      <p>This permanently removes the Task. There is no Undo.</p>
      <div>
        <Button disabled={pending !== null} onClick={onCancel} type="button" variant="ghost">Cancel</Button>
        <Button disabled={pending !== null || confirmation !== task.id} type="submit" variant="danger">
          {pending === 'permanent-delete' ? 'Deleting permanently…' : 'Delete permanently'}
        </Button>
      </div>
    </form>
  )
}

export function TaskActionsDialog({
  open,
  task,
  workspaceUid,
  onClose,
  onPermanentlyDeleted,
  onSaved,
  onNotice,
}: TaskActionsDialogProps) {
  const [note, setNote] = useState('')
  const [subtaskTitle, setSubtaskTitle] = useState('')
  const [subtaskPriority, setSubtaskPriority] = useState<TaskPriority>('P2')
  const [dropOpen, setDropOpen] = useState(false)
  const [dropConfirmation, setDropConfirmation] = useState('')
  const [preview, setPreview] = useState<TaskDeletionPreview | null>(null)
  const [permanentConfirmation, setPermanentConfirmation] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const noteIntentKey = useRef<string | null>(null)
  const subtaskIntentKey = useRef<string | null>(null)
  const permanentIntentKey = useRef<string | null>(null)

  useEffect(() => {
    if (!open) {
      setNote('')
      setSubtaskTitle('')
      setSubtaskPriority('P2')
      setDropOpen(false)
      setDropConfirmation('')
      setPreview(null)
      setPermanentConfirmation('')
      setPending(null)
      setError(null)
      noteIntentKey.current = null
      subtaskIntentKey.current = null
      permanentIntentKey.current = null
    }
  }, [open])

  const run = async (
    key: string,
    operation: () => Promise<Task>,
    message: string,
    options?: {
      afterCommit?: (task: Task) => void
      onSuccess?: () => void
      committed?: (task: Task) => boolean
    },
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
      options?.afterCommit?.(updated)
    } catch (reason) {
      if (options?.committed) {
        try {
          const authoritative = (await api.getTask(task.id)).task
          onSaved(authoritative)
          if (options.committed(authoritative)) {
            onNotice(`${message} (verified after reconnect)`)
            onClose()
            options.afterCommit?.(authoritative)
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

  const dropTask = (event: FormEvent) => {
    event.preventDefault()
    if (dropConfirmation !== task.id || task.status === 'dropped') return
    void run(
      'drop',
      () => api.patchTask(task.id, { status: 'dropped', revision: task.revision }),
      `${task.id} dropped from active work; immutable history preserved`,
      { committed: (authoritative) => authoritative.status === 'dropped' },
    )
  }

  const restartPermanentPreview = () => {
    permanentIntentKey.current = null
    setPreview(null)
    setPermanentConfirmation('')
  }

  const startPermanentPreview = async () => {
    if (pending) return
    permanentIntentKey.current ??= createIdempotencyKey()
    const intentKey = permanentIntentKey.current
    setPending('preview')
    setError(null)
    try {
      const result = await api.previewTaskDeletion(
        task.id,
        {
          revision: task.revision,
          workspace_uid: workspaceUid,
          client_request_id: intentKey,
        },
        intentKey,
      )
      setPreview(result)
    } catch (reason) {
      setError(getErrorMessage(reason))
      onNotice(getErrorMessage(reason), 'error')
    } finally {
      setPending(null)
    }
  }

  const commitPermanentDelete = async (event: FormEvent) => {
    event.preventDefault()
    const intentKey = permanentIntentKey.current
    if (!preview || permanentConfirmation !== task.id || pending || !intentKey) return
    setPending('permanent-delete')
    setError(null)
    try {
      const receipt = await api.permanentlyDeleteTask(task.id, {
        previewToken: preview.preview_token,
        confirm: task.id,
        revision: task.revision,
        idempotencyKey: intentKey,
      })
      onNotice(`${task.id} permanently deleted`)
      onPermanentlyDeleted?.(receipt)
    } catch (reason) {
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
      <section aria-labelledby="task-actions-drop" className="task-action-danger">
        <div>
          <h3 id="task-actions-drop">Drop Task</h3>
          <p>
            Remove this Task from active work without erasing its immutable SSOT and Activity history.
            Related Tasks may remain blocked until their links are updated. Dropped work stays recoverable and filterable.
          </p>
        </div>
        <DropTaskSection
          confirmation={dropConfirmation}
          onCancel={() => { setDropOpen(false); setDropConfirmation('') }}
          onConfirmation={setDropConfirmation}
          onOpen={() => setDropOpen(true)}
          onSubmit={dropTask}
          open={dropOpen}
          pending={pending}
          task={task}
        />
      </section>
      <section aria-labelledby="task-actions-permanent" className="task-action-danger">
        <div>
          <h3 id="task-actions-permanent">Delete permanently</h3>
          <p>Permanently remove this Task and its owned records after a read-only preview. This cannot be undone.</p>
        </div>
        <PermanentDeleteSection
          confirmation={permanentConfirmation}
          onCancel={restartPermanentPreview}
          onConfirmation={setPermanentConfirmation}
          onStart={() => { void startPermanentPreview() }}
          onSubmit={(event) => { void commitPermanentDelete(event) }}
          pending={pending}
          preview={preview}
          task={task}
        />
      </section>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
    </Dialog>
  )
}
