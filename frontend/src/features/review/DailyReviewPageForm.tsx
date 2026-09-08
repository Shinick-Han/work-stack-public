import { useEffect, useId, useRef, type FormEvent } from 'react'
import { Button } from '../../components/Primitives'
import type { Task, WorklogEntry } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import {
  REVIEW_COPY,
  boundTargetLabel,
  canDiscardOrdinaryDraft,
  draftHasContent,
  formatDuration,
  saveButtonLabel,
  strandedIdentityLabel,
  type FrozenEntryAttempt,
} from './DailyReviewPageModel'

/** The Task title wraps in full: no clamp, so no expansion path is claimed. */
function BoundTarget({ task }: { task: Task }) {
  return (
    <div className="review-bound-target">
      <p className="review-bound-target__meta">{boundTargetLabel(task.id)}</p>
      <p className="review-bound-target__title">{task.title}</p>
    </div>
  )
}

/**
 * A commit_unknown attempt whose workspace owner was replaced. The old Task,
 * date, workspace and the original payload stay inspectable; the only offered
 * action releases the hold without asserting anything about the write.
 */
function UnresolvedPreviousWorkspace({
  attempt,
  onRelease,
}: {
  attempt: FrozenEntryAttempt
  onRelease: () => void
}) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="review-stranded" role="group">
      <h3 id={headingId}>{REVIEW_COPY.strandedHeading}</h3>
      <p className="review-stranded__identity">
        {strandedIdentityLabel(attempt.input.task_id, attempt.input.date, attempt.workspaceId)}
      </p>
      <p className="review-stranded__explanation" role="status">{REVIEW_COPY.strandedExplanation}</p>
      <div className="review-stranded__payload">
        <span>{REVIEW_COPY.strandedPayload}</span>
        {(['done', 'next', 'blockers'] as const).map((field) => (
          attempt.input[field].length ? (
            <div className={`review-facts review-facts--${field}`} key={field}>
              <span>{field}</span>
              <ul>{attempt.input[field].map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          ) : null
        ))}
      </div>
      <Button onClick={onRelease} type="button" variant="secondary">
        {REVIEW_COPY.releaseStranded}
      </Button>
      <p className="review-stranded__note">{REVIEW_COPY.releaseStrandedNote}</p>
    </section>
  )
}

function TargetNotice({ invalidTarget, taskId }: { invalidTarget: boolean; taskId: string }) {
  if (invalidTarget) {
    return <p className="review-selection-alert" role="alert">{REVIEW_COPY.invalidTarget}</p>
  }
  if (!taskId) {
    return <p className="review-selection-alert" role="status">{REVIEW_COPY.missingTarget}</p>
  }
  return null
}

function TaskPicker({
  availableTasks,
  disabled,
  invalidTarget,
  onTaskId,
  selectedTask,
  taskId,
}: {
  availableTasks: Task[]
  disabled: boolean
  invalidTarget: boolean
  onTaskId: (value: string) => void
  selectedTask: Task | undefined
  taskId: string
}) {
  const missing = !taskId
  const orphan = Boolean(taskId) && !selectedTask
  return (
    <label>
      <span>Task</span>
      <select
        aria-invalid={invalidTarget || missing}
        disabled={disabled}
        onChange={(event) => onTaskId(event.target.value)}
        value={taskId}
      >
        <option value="">{REVIEW_COPY.selectTask}</option>
        {orphan ? <option value={taskId}>{taskId} · not in this workspace</option> : null}
        {availableTasks.map((task) => (
          <option key={task.id} value={task.id}>{task.id} · {task.title}</option>
        ))}
      </select>
    </label>
  )
}

function ProgressField({
  disabled,
  label,
  onChange,
  rows,
  value,
}: {
  disabled: boolean
  label: string
  onChange: (value: string) => void
  rows: number
  value: string
}) {
  return (
    <label>
      <span>{label} <small>{REVIEW_COPY.fieldHint}</small></span>
      <textarea
        aria-label={label}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        rows={rows}
        value={value}
      />
    </label>
  )
}

function SaveProgressControl({
  canDiscard,
  canSave,
  error,
  frozen,
  onDiscard,
  pending,
  stranded,
}: {
  canDiscard: boolean
  canSave: boolean
  error: unknown
  frozen: boolean
  onDiscard: () => void
  pending: boolean
  stranded: boolean
}) {
  // Stranded disables the control outright rather than leaving a live-looking
  // button that silently does nothing; the reason sits next to it.
  const blocked = pending || stranded || (!frozen && !canSave)
  return (
    <div className="review-entry-actions">
      <Button disabled={blocked} type="submit" variant="primary">
        {saveButtonLabel(pending, frozen, Boolean(error), stranded)}
      </Button>
      {canDiscard ? (
        <Button onClick={onDiscard} type="button" variant="ghost">{REVIEW_COPY.discard}</Button>
      ) : null}
      {error ? <p className="form-error" role="alert">{getErrorMessage(error)}</p> : null}
    </div>
  )
}

export function DraftGuardDialog({
  frozen = false,
  onDiscard,
  onKeep,
  stranded = false,
}: {
  frozen?: boolean
  onDiscard: () => void
  onKeep: () => void
  stranded?: boolean
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal()
    const first = dialog.querySelector('button')
    if (first instanceof HTMLElement) first.focus()
    return () => {
      if (dialog.open && typeof dialog.close === 'function') dialog.close()
    }
  }, [])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const handleCancel = (event: Event) => {
      event.preventDefault()
      onKeep()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => dialog.removeEventListener('cancel', handleCancel)
  }, [onKeep])

  return (
    <dialog aria-labelledby={titleId} className="review-guard" ref={dialogRef} role="alertdialog">
      <h3 id={titleId}>{frozen ? REVIEW_COPY.frozenGuardTitle : REVIEW_COPY.guardTitle}</h3>
      <p>{stranded ? REVIEW_COPY.strandedGuardBody : frozen ? REVIEW_COPY.frozenGuardBody : REVIEW_COPY.guardBody}</p>
      <div className="review-guard__actions">
        <Button onClick={onKeep} variant="secondary">
          {frozen ? REVIEW_COPY.keepFrozen : REVIEW_COPY.keepDraft}
        </Button>
        {frozen ? null : (
          <Button onClick={onDiscard} variant="danger">{REVIEW_COPY.discardDraft}</Button>
        )}
      </div>
    </dialog>
  )
}

export function DayEntries({
  entries,
  empty,
  heading,
  headingId,
  label,
  onOpenTask,
  variant,
}: {
  entries: WorklogEntry[]
  empty: string
  heading: string
  headingId: string
  label: string
  onOpenTask: (taskId: string) => void
  variant: 'day' | 'task'
}) {
  const className = variant === 'day' ? 'review-day-card' : 'review-task-progress'
  if (!entries.length) {
    return (
      <section className={className} aria-labelledby={headingId}>
        <header><span>{label}</span><h2 id={headingId}>{heading}</h2></header>
        <p className="review-empty">{empty}</p>
      </section>
    )
  }
  return (
    <section className={className} aria-labelledby={headingId}>
      <header><span>{label}</span><h2 id={headingId}>{heading}</h2></header>
      <div className="review-entry-list">{entries.map((entry, index) => (
        <DayEntryCard entry={entry} key={`${entry.task_id}-${index}`} onOpenTask={onOpenTask} />
      ))}</div>
    </section>
  )
}

function DayEntryCard({
  entry,
  onOpenTask,
}: {
  entry: WorklogEntry
  onOpenTask: (taskId: string) => void
}) {
  const focused = entry.session_id && entry.duration_seconds !== undefined
  return (
    <article>
      <button onClick={() => onOpenTask(entry.task_id)} type="button">
        <strong>{entry.task_id}</strong><span>{entry.task}</span>
      </button>
      {focused ? (
        <small className="review-entry-duration">
          {formatDuration(entry.duration_seconds as number)} focused · {entry.session_id}
        </small>
      ) : null}
      {(['done', 'next', 'blockers'] as const).map((field) => (
        entry[field].length ? (
          <div className={`review-facts review-facts--${field}`} key={field}>
            <span>{field}</span>
            <ul>{entry[field].map((item) => <li key={item}>{item}</li>)}</ul>
          </div>
        ) : null
      ))}
    </article>
  )
}

export function DailyReviewEntryForm({
  availableTasks,
  blockers,
  date,
  done,
  entryError,
  entryPending,
  frozen,
  invalidTarget,
  next,
  onBlockers,
  onDiscardDraft,
  onDone,
  onNext,
  onOpenTask,
  onReleaseStranded,
  onSubmitEntry,
  onTaskId,
  selectedTask,
  stranded,
  strandedAttempt,
  taskId,
}: {
  availableTasks: Task[]
  blockers: string
  date: string
  done: string
  entryError: unknown
  entryPending: boolean
  frozen: boolean
  invalidTarget: boolean
  next: string
  onBlockers: (value: string) => void
  onDiscardDraft: () => void
  onDone: (value: string) => void
  onNext: (value: string) => void
  onOpenTask: (taskId: string) => void
  onReleaseStranded: () => void
  onSubmitEntry: (event: FormEvent) => void
  onTaskId: (value: string) => void
  selectedTask: Task | undefined
  stranded: boolean
  strandedAttempt: FrozenEntryAttempt | null
  taskId: string
}) {
  const fieldsLocked = entryPending || frozen || !taskId
  const canSave = draftHasContent(done, next, blockers) && Boolean(taskId) && !invalidTarget
  const canDiscard = canDiscardOrdinaryDraft(draftHasContent(done, next, blockers), frozen, entryPending)
  return (
    <form className="review-entry-card" onSubmit={onSubmitEntry}>
      <header><span>{date}</span><strong>Progress</strong></header>
      {strandedAttempt ? (
        <UnresolvedPreviousWorkspace attempt={strandedAttempt} onRelease={onReleaseStranded} />
      ) : null}
      {selectedTask ? <BoundTarget task={selectedTask} /> : null}
      <TargetNotice invalidTarget={invalidTarget} taskId={taskId} />
      <TaskPicker
        availableTasks={availableTasks}
        disabled={entryPending}
        invalidTarget={invalidTarget}
        onTaskId={onTaskId}
        selectedTask={selectedTask}
        taskId={taskId}
      />
      {selectedTask ? (
        <button className="review-task-link" onClick={() => onOpenTask(selectedTask.id)} type="button">
          Open {selectedTask.id} planning detail
        </button>
      ) : null}
      <ProgressField disabled={fieldsLocked} label={REVIEW_COPY.changed} onChange={onDone} rows={4} value={done} />
      <ProgressField disabled={fieldsLocked} label={REVIEW_COPY.next} onChange={onNext} rows={4} value={next} />
      <ProgressField disabled={fieldsLocked} label={REVIEW_COPY.blockers} onChange={onBlockers} rows={3} value={blockers} />
      <SaveProgressControl
        canDiscard={canDiscard}
        canSave={canSave}
        error={entryError}
        frozen={frozen}
        onDiscard={onDiscardDraft}
        pending={entryPending}
        stranded={stranded}
      />
    </form>
  )
}
