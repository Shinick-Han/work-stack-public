import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckpointHistory, type FrozenAttempt } from './CheckpointHistory'
import { DailyReportPreview, reviewDaySourceToken, reviewWeeklySourceToken, useOwnerGeneration } from './DailyReportPreview'
import { WeeklyReportPreview } from './WeeklyReportPreview'
import { CommitUnknownError, api, createIdempotencyKey } from '../../api/client'
import { Button, ErrorState, LoadingBlock, Pill } from '../../components/Primitives'
import { DateInput } from '../../components/DateInput'
import type {
  CheckpointAudit,
  Capture,
  ReviewProjection,
  Task,
  WorkspaceProjection,
} from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { LeadershipSignalsPanel } from './LeadershipSignalsPanel'
import { SavedReportsPanel } from './SavedReportsPanel'
import { MutationNoticePanel } from './MutationNoticePanel'
import { DailyReviewEntryForm, DayEntries, DraftGuardDialog } from './DailyReviewPageForm'
import {
  REVIEW_COPY,
  buildReviewEntryInput,
  draftHasContent,
  draftResolutionActions,
  formatDuration,
  frozenAttemptStranded,
  localTime,
  progressNavigationLocked,
  resolveInitialTaskId,
  sortReviewTasks,
  type DraftGuard,
  type FrozenEntryAttempt,
} from './DailyReviewPageModel'
import './DailyReviewPage.css'

export interface DailyReviewPageProps {
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenTask: (taskId: string) => void
  onOpenCapture?: (captureId: string) => void
  captures?: Capture[]
  today: string
  workspace: WorkspaceProjection
  /** Integrator-owned Review target. This page never reads the URL. */
  initialTaskId?: string | null
  /** True while a dirty draft, pending write, or frozen commit_unknown is held. */
  onNavigationLockChange?: (locked: boolean) => void
}

function DailyReviewSummary({
  review,
  checkinPending,
  checkinError,
  onCheckin,
}: {
  review: ReviewProjection
  checkinPending: boolean
  checkinError: boolean
  onCheckin: () => void
}) {
  const doneCount = review.day.entries.reduce((count, entry) => count + entry.done.length, 0)
  const blockerCount = review.weekly.projects.reduce((count, project) => count + project.blockers.length, 0)
  return (
    <div className="review-summary" aria-label="Daily review summary">
      <div><span>Check-in</span><strong>{review.day.start_time ?? 'Not yet'}</strong><Button disabled={checkinPending} onClick={onCheckin} variant="ghost">{review.day.start_time ? 'Update time' : checkinError ? 'Retry check-in' : 'Check in now'}</Button></div>
      <div><span>Entries today</span><strong>{review.day.entries.length}</strong><small>{doneCount} done items</small></div>
      <div><span>7-day tasks</span><strong>{review.weekly.projects.length}</strong><small>{review.weekly.range.start} → {review.weekly.range.end}</small></div>
      <div className={blockerCount ? 'metric-attention' : ''}><span>Open signals</span><strong>{blockerCount}</strong><small>reported blockers</small></div>
    </div>
  )
}

function DailyReviewWeekly({
  onOpenTask,
  review,
}: {
  onOpenTask: (taskId: string) => void
  review: ReviewProjection
}) {
  return (
    <section className="weekly-review" aria-labelledby="weekly-review-heading">
      <header><div><span>Deterministic roll-up</span><h2 id="weekly-review-heading">Seven-day review</h2></div><Pill tone="neutral">{review.weekly.range.days} days</Pill></header>
      {review.weekly.projects.length ? <div className="weekly-project-grid">{review.weekly.projects.map((project) => (
        <article key={project.task_id}>
          <button onClick={() => onOpenTask(project.task_id)} type="button"><strong>{project.task_id}</strong><span>{project.task}</span></button>
          <div className="weekly-project-meta">{project.objective_ids.map((id) => <Pill key={id} tone="accent">{id}</Pill>)}{project.duration_seconds > 0 ? <small>{formatDuration(project.duration_seconds)} focused · {project.dates.length} active day{project.dates.length === 1 ? '' : 's'}</small> : <small>{project.dates.length} active day{project.dates.length === 1 ? '' : 's'}</small>}</div>
          <dl><div><dt>Done</dt><dd>{project.done.length}</dd></div><div><dt>Next</dt><dd>{project.next.length}</dd></div><div><dt>Blockers</dt><dd>{project.blockers.length}</dd></div></dl>
        </article>
      ))}</div> : <p className="review-empty">No review evidence falls in this seven-day window.</p>}
    </section>
  )
}

interface TransitionOwnership {
  audit: ReturnType<typeof useQuery<CheckpointAudit>>
  conflictMessage: string | null
  failedExplanation: string | null
  owner: string
  pendingRetry: FrozenAttempt | null
  clearRetry: () => void
  submit: (attempt: FrozenAttempt) => void
}

/**
 * One owner lifetime for every checkpoint transition on this page: a workspace
 * and a day. A staged or pending attempt never survives a change of owner, and
 * a late completion belonging to a replaced owner is discarded, not installed.
 */
function useCheckpointTransitions(
  workspaceId: string,
  date: string,
  refresh: () => Promise<void> | void,
): TransitionOwnership {
  const queryClient = useQueryClient()
  const auditKey = useMemo(() => ['checkpoint-audit', workspaceId], [workspaceId])
  // The entire workspace audit is loaded and validated before any day filter.
  const audit = useQuery({ queryKey: auditKey, queryFn: () => api.getCheckpointAudit() })

  const { owner, currentOwner } = useOwnerGeneration(queryClient, workspaceId, date)

  const [pendingRetry, setPendingRetry] = useState<FrozenAttempt | null>(null)
  const [conflictMessage, setConflictMessage] = useState<string | null>(null)
  // The raw explanation of the last failed attempt, kept verbatim and visible.
  const [failedExplanation, setFailedExplanation] = useState<string | null>(null)

  useEffect(() => {
    // Owner change cancels, not hides: the dead intent is discarded outright.
    setPendingRetry(null)
    setConflictMessage(null)
    setFailedExplanation(null)
  }, [owner])

  const mutation = useMutation({
    mutationFn: (attempt: FrozenAttempt) => (
      api.transitionCheckpoint(attempt.checkpointId, attempt.body, attempt.idempotencyKey)
    ),
    onSuccess: (_result, attempt) => {
      if (attempt.owner !== currentOwner()) return
      setPendingRetry(null)
      setConflictMessage(null)
      setFailedExplanation(null)
      void queryClient.invalidateQueries({ queryKey: auditKey })
      void refresh()
    },
    onError: (error: unknown, attempt) => {
      if (attempt.owner !== currentOwner()) return
      setFailedExplanation(attempt.body.reason.explanation)
      if (error instanceof CommitUnknownError) {
        // Only an explicit retry of this same snapshot and key is offered.
        setPendingRetry(attempt)
        setConflictMessage(null)
        return
      }
      // A refusal is shown and the audit refreshed; nothing is resubmitted.
      setPendingRetry(null)
      setConflictMessage(getErrorMessage(error))
      void queryClient.invalidateQueries({ queryKey: auditKey })
    },
  })

  const submit = (attempt: FrozenAttempt) => {
    // A stale explicit retry fails its scope guard before reaching transport.
    if (attempt.owner !== currentOwner() || mutation.isPending) return
    setConflictMessage(null)
    mutation.mutate(attempt)
  }

  return {
    audit,
    conflictMessage,
    failedExplanation,
    owner,
    pendingRetry,
    clearRetry: () => { setPendingRetry(null); setFailedExplanation(null) },
    submit,
  }
}

/** Mounted only once the whole audit is validated; the day filter is inside. */
function CheckpointHistorySection({
  date,
  taskId,
  transitions,
}: {
  date: string
  taskId: string
  transitions: TransitionOwnership
}) {
  if (!transitions.audit.data) return null
  return (
    <details className="review-record-details" open>
      <summary>{REVIEW_COPY.details}</summary>
      <CheckpointHistory
        audit={transitions.audit.data}
        conflictMessage={transitions.conflictMessage}
        createIdempotencyKey={createIdempotencyKey}
        date={date}
        failedExplanation={transitions.failedExplanation}
        onClearRetry={transitions.clearRetry}
        onRetry={transitions.submit}
        onSubmit={transitions.submit}
        owner={transitions.owner}
        pendingRetry={transitions.pendingRetry}
        taskId={taskId || null}
      />
    </details>
  )
}

function DailyReviewHeading({
  date,
  onDate,
  today,
}: {
  date: string
  onDate: (value: string) => void
  today: string
}) {
  return (
    <header className="page-heading">
      <div>
        <h1 id="review-heading">{REVIEW_COPY.heading}</h1>
        <p>{REVIEW_COPY.lead}</p>
      </div>
      <DateInput
        className="review-date"
        label="Review date"
        max={today}
        onChange={onDate}
        value={date}
      />
    </header>
  )
}

function useNonRepeatingOwner(coordinate: string): { owner: string; currentOwner: () => string } {
  const generation = useRef(0)
  const last = useRef(coordinate)
  const identity = useRef(`${coordinate}#0`)
  if (last.current !== coordinate) {
    last.current = coordinate
    generation.current += 1
    identity.current = `${coordinate}#${generation.current}`
  }
  return { owner: identity.current, currentOwner: () => identity.current }
}

function useProgressNavigationLock(
  locked: boolean,
  onNavigationLockChange?: (locked: boolean) => void,
) {
  const callbackRef = useRef(onNavigationLockChange)
  callbackRef.current = onNavigationLockChange
  const lastLocked = useRef<boolean | undefined>(undefined)
  useEffect(() => {
    if (lastLocked.current === locked) return
    lastLocked.current = locked
    callbackRef.current?.(locked)
  }, [locked])
  useEffect(() => () => { callbackRef.current?.(false) }, [])
}

function useReviewDraft() {
  const [done, setDone] = useState('')
  const [next, setNext] = useState('')
  const [blockers, setBlockers] = useState('')
  const [pendingRetry, setPendingRetry] = useState<FrozenEntryAttempt | null>(null)
  const entryIntentKey = useRef<string | null>(null)
  const dirty = draftHasContent(done, next, blockers)
  const resetText = () => {
    entryIntentKey.current = null
    setDone('')
    setNext('')
    setBlockers('')
  }
  const clearDraft = () => {
    setPendingRetry(null)
    resetText()
  }
  return {
    blockers, clearDraft, dirty, done, entryIntentKey, next, pendingRetry,
    resetText, setBlockers, setDone, setNext, setPendingRetry,
  }
}

function useBoundReviewTarget(
  availableTasks: Task[],
  initialTaskId: string | null | undefined,
  blocked: boolean,
) {
  const initial = resolveInitialTaskId(initialTaskId, availableTasks)
  const [taskId, setTaskId] = useState(initial.taskId)
  const [invalidTarget, setInvalidTarget] = useState(initial.invalid)
  const lastInitial = useRef(initialTaskId)
  // A target arriving while the draft is blocked is held, never consumed: the
  // old draft stays bound to the old Task until the block is resolved.
  const [deferredInitial, setDeferredInitial] = useState<{ taskId: string | null } | null>(null)

  const bindTarget = (value: string | null | undefined) => {
    const bound = resolveInitialTaskId(value, availableTasks)
    setTaskId(bound.taskId)
    setInvalidTarget(bound.invalid)
  }

  const applyTask = (value: string) => {
    // An explicit picker choice supersedes any held parent target.
    setDeferredInitial(null)
    setInvalidTarget(false)
    setTaskId(value)
  }

  useEffect(() => {
    if (lastInitial.current === initialTaskId) return
    lastInitial.current = initialTaskId
    if (blocked) {
      setDeferredInitial({ taskId: initialTaskId ?? null })
      return
    }
    bindTarget(initialTaskId)
  }, [availableTasks, blocked, initialTaskId])

  useEffect(() => {
    if (blocked || !deferredInitial) return
    setDeferredInitial(null)
    bindTarget(deferredInitial.taskId)
  }, [availableTasks, blocked, deferredInitial])

  useEffect(() => {
    // A held parent target owns the next binding; do not race it to ''.
    if (deferredInitial) return
    if (!taskId) return
    if (availableTasks.some((task) => task.id === taskId)) {
      setInvalidTarget(false)
      return
    }
    if (blocked) {
      setInvalidTarget(true)
      return
    }
    setTaskId('')
  }, [availableTasks, blocked, deferredInitial, taskId])

  return {
    applyTask,
    invalidTarget,
    selectedTask: availableTasks.find((task) => task.id === taskId),
    taskId,
  }
}

function useReviewEntryMutation({
  clearDraft,
  currentOwner,
  onNotice,
  refresh,
  setPendingRetry,
}: {
  clearDraft: () => void
  currentOwner: () => string
  onNotice: (message: string, tone?: 'success' | 'error') => void
  refresh: () => Promise<void> | void
  setPendingRetry: (attempt: FrozenEntryAttempt | null) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (attempt: FrozenEntryAttempt) => api.addReviewEntry(attempt.input, attempt.key),
    onSuccess: async (_result, attempt) => {
      if (attempt.owner !== currentOwner()) return
      clearDraft()
      await refresh()
      await queryClient.invalidateQueries({ queryKey: ['checkpoint-audit', attempt.workspaceId] })
      onNotice(REVIEW_COPY.saved)
    },
    onError: (error, attempt) => {
      if (attempt.owner !== currentOwner()) return
      onNotice(getErrorMessage(error), 'error')
      if (error instanceof CommitUnknownError) setPendingRetry(attempt)
    },
  })
}

function useDraftTargetGuard({
  applyTask,
  blocked,
  date,
  frozen,
  onDate,
  resetIntent,
  resetText,
  taskId,
}: {
  applyTask: (value: string) => void
  blocked: boolean
  date: string
  frozen: boolean
  onDate: (value: string) => void
  resetIntent: () => void
  resetText: () => void
  taskId: string
}) {
  const [guard, setGuard] = useState<DraftGuard | null>(null)
  const requestTaskId = (value: string) => {
    if (value === taskId) return
    if (blocked) setGuard({ kind: 'task', value })
    else {
      resetIntent()
      applyTask(value)
    }
  }
  const requestDate = (value: string) => {
    if (value === date) return
    if (blocked) setGuard({ kind: 'date', value })
    else onDate(value)
  }
  const confirmGuard = () => {
    if (!guard || frozen) {
      setGuard(null)
      return
    }
    const nextGuard = guard
    setGuard(null)
    resetText()
    if (nextGuard.kind === 'task') applyTask(nextGuard.value)
    else onDate(nextGuard.value)
  }
  return {
    confirmGuard,
    dismissGuard: () => setGuard(null),
    guard,
    requestDate,
    requestTaskId,
  }
}

interface EntrySubmitContext {
  blockers: string
  currentOwner: () => string
  date: string
  done: string
  entryIntentKey: { current: string | null }
  invalidTarget: boolean
  mutate: (attempt: FrozenEntryAttempt) => void
  next: string
  pending: boolean
  pendingRetry: FrozenEntryAttempt | null
  taskId: string
  workspaceId: string
}

function submitFrozenOrFreshEntry(event: FormEvent, context: EntrySubmitContext) {
  const { currentOwner, entryIntentKey, mutate, pendingRetry, taskId } = context
  event.preventDefault()
  if (pendingRetry) {
    if (pendingRetry.owner !== currentOwner() || context.pending) return
    mutate(pendingRetry)
    return
  }
  if (!taskId || context.invalidTarget) return
  const input = buildReviewEntryInput(context.date, taskId, context.done, context.next, context.blockers)
  if (!input) return
  entryIntentKey.current ??= createIdempotencyKey()
  mutate({ owner: currentOwner(), workspaceId: context.workspaceId, input, key: entryIntentKey.current })
}

function useReviewEntryWrite({
  availableTasks,
  date,
  initialTaskId,
  onDate,
  onNotice,
  refresh,
  workspaceId,
}: {
  availableTasks: Task[]
  date: string
  initialTaskId?: string | null
  onDate: (value: string) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  refresh: () => Promise<void> | void
  workspaceId: string
}) {
  const draft = useReviewDraft()
  const frozen = draft.pendingRetry !== null
  const blocked = draft.dirty || frozen
  const target = useBoundReviewTarget(availableTasks, initialTaskId, blocked)
  const { owner, currentOwner } = useNonRepeatingOwner(`${workspaceId}|${date}|${target.taskId}`)
  // The frozen attempt outlived its owner (a workspace replacement). It keeps
  // its own Task, date, workspace and payload, and is never re-POSTed here.
  const stranded = frozenAttemptStranded(draft.pendingRetry, owner)
  const mutation = useReviewEntryMutation({
    clearDraft: draft.clearDraft,
    currentOwner,
    onNotice,
    refresh,
    setPendingRetry: draft.setPendingRetry,
  })
  const actions = draftResolutionActions({
    clearDraft: draft.clearDraft,
    frozen,
    pending: mutation.isPending,
    reset: mutation.reset,
    resetText: draft.resetText,
    stranded,
  })
  const targetGuard = useDraftTargetGuard({
    applyTask: target.applyTask,
    blocked,
    date,
    frozen,
    onDate,
    resetIntent: () => { draft.entryIntentKey.current = null },
    resetText: actions.discardText,
    taskId: target.taskId,
  })

  const submitEntry = (event: FormEvent) => submitFrozenOrFreshEntry(event, {
    blockers: draft.blockers, currentOwner, date, done: draft.done,
    entryIntentKey: draft.entryIntentKey, invalidTarget: target.invalidTarget,
    mutate: mutation.mutate, next: draft.next, pending: mutation.isPending,
    pendingRetry: draft.pendingRetry, taskId: target.taskId, workspaceId,
  })

  const changeDraft = (setter: (value: string) => void) => (value: string) => {
    if (draft.pendingRetry) return
    draft.entryIntentKey.current = null
    setter(value)
  }

  return {
    blockers: draft.blockers,
    changeDraft,
    confirmGuard: targetGuard.confirmGuard,
    discardOrdinaryDraft: actions.discardOrdinaryDraft,
    dismissGuard: targetGuard.dismissGuard,
    done: draft.done,
    entryError: mutation.error,
    entryPending: mutation.isPending,
    guard: targetGuard.guard,
    invalidTarget: target.invalidTarget,
    locked: progressNavigationLocked(draft.dirty, mutation.isPending, frozen),
    next: draft.next,
    pendingRetry: draft.pendingRetry,
    releaseStrandedAttempt: actions.releaseStrandedAttempt,
    requestDate: targetGuard.requestDate,
    requestTaskId: targetGuard.requestTaskId,
    selectedTask: target.selectedTask,
    stranded,
    setBlockers: draft.setBlockers,
    setDone: draft.setDone,
    setNext: draft.setNext,
    submitEntry,
    taskId: target.taskId,
  }
}

function DailyReviewLoaded({
  availableTasks,
  captures,
  checkinError,
  checkinPending,
  date,
  entry,
  onCheckin,
  onNotice,
  onOpenCapture,
  onOpenTask,
  review,
  transitions,
}: {
  availableTasks: Task[]
  captures: Capture[]
  checkinError: boolean
  checkinPending: boolean
  date: string
  entry: ReturnType<typeof useReviewEntryWrite>
  onCheckin: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenCapture?: (captureId: string) => void
  onOpenTask: (taskId: string) => void
  review: ReviewProjection
  transitions: TransitionOwnership
}) {
  const taskEntries = entry.taskId
    ? review.day.entries.filter((item) => item.task_id === entry.taskId)
    : []
  return (
    <>
      <DailyReviewSummary
        checkinError={checkinError}
        checkinPending={checkinPending}
        onCheckin={onCheckin}
        review={review}
      />
      <div className="review-layout">
        <DailyReviewEntryForm
          availableTasks={availableTasks}
          blockers={entry.blockers}
          date={date}
          done={entry.done}
          entryError={entry.entryError}
          entryPending={entry.entryPending}
          frozen={entry.pendingRetry !== null}
          invalidTarget={entry.invalidTarget}
          next={entry.next}
          onBlockers={entry.changeDraft(entry.setBlockers)}
          onDiscardDraft={entry.discardOrdinaryDraft}
          onDone={entry.changeDraft(entry.setDone)}
          onNext={entry.changeDraft(entry.setNext)}
          onOpenTask={onOpenTask}
          onReleaseStranded={entry.releaseStrandedAttempt}
          onSubmitEntry={entry.submitEntry}
          onTaskId={entry.requestTaskId}
          selectedTask={entry.selectedTask}
          stranded={entry.stranded}
          strandedAttempt={entry.stranded ? entry.pendingRetry : null}
          taskId={entry.taskId}
        />
        {entry.taskId ? (
          <DayEntries
            empty="No progress recorded for this task on this day."
            entries={taskEntries}
            heading={REVIEW_COPY.recent}
            headingId="review-task-progress-heading"
            label={entry.taskId}
            onOpenTask={onOpenTask}
            variant="task"
          />
        ) : null}
        <CheckpointHistorySection date={date} taskId={entry.taskId} transitions={transitions} />
        <DayEntries
          empty="Record a concrete Done, Next, or Blocker item for a Task."
          entries={review.day.entries}
          heading={review.day.entries.length ? `${review.day.entries.length} entries` : 'No entries yet'}
          headingId="review-day-heading"
          label="Day record"
          onOpenTask={onOpenTask}
          variant="day"
        />
      </div>
      <DailyReviewWeekly onOpenTask={onOpenTask} review={review} />
      {onOpenCapture ? <LeadershipSignalsPanel captures={captures} onNotice={onNotice} onOpenCapture={onOpenCapture} /> : null}
      {entry.guard ? (
        <DraftGuardDialog
          frozen={entry.pendingRetry !== null}
          onDiscard={entry.confirmGuard}
          onKeep={entry.dismissGuard}
          stranded={entry.stranded}
        />
      ) : null}
    </>
  )
}

function useDailyReviewPage({
  initialTaskId,
  onNavigationLockChange,
  onNotice,
  today,
  workspace,
}: DailyReviewPageProps) {
  const queryClient = useQueryClient()
  const availableTasks = useMemo(() => sortReviewTasks(workspace.tasks), [workspace.tasks])
  const [date, setDate] = useState(today)
  const checkinIntent = useRef<{ date: string; time: string; key: string } | null>(null)

  const reviewQuery = useQuery({
    queryKey: ['review', date, 7],
    queryFn: () => api.getReview(date, 7),
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['review', date, 7] })
  const transitions = useCheckpointTransitions(workspace.workspace.id, date, refresh)
  const changeDate = (value: string) => {
    setDate(value)
    checkinIntent.current = null
  }
  const entry = useReviewEntryWrite({
    availableTasks,
    date,
    initialTaskId,
    onDate: changeDate,
    onNotice,
    refresh,
    workspaceId: workspace.workspace.id,
  })
  useProgressNavigationLock(entry.locked, onNavigationLockChange)

  const checkinMutation = useMutation({
    mutationFn: ({ date: intentDate, time, key }: { date: string; time: string; key: string }) => (
      api.checkinReview(intentDate, time, key)
    ),
    onSuccess: async (result) => {
      checkinIntent.current = null
      await refresh()
      onNotice(`Checked in at ${result.start_time}`)
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  const startCheckin = () => {
    if (!checkinIntent.current || checkinIntent.current.date !== date) {
      checkinIntent.current = { date, time: localTime(), key: createIdempotencyKey() }
    }
    checkinMutation.mutate(checkinIntent.current)
  }

  const review = reviewQuery.data
  return {
    availableTasks,
    checkinMutation,
    date,
    entry,
    review,
    reviewQuery,
    startCheckin,
    transitions,
    sourceAvailable: Boolean(review) && !reviewQuery.isError && !reviewQuery.isPending,
    workspaceId: workspace.workspace.id,
  }
}

export function DailyReviewPage({
  captures = [],
  initialTaskId = null,
  onNavigationLockChange,
  onNotice,
  onOpenCapture,
  onOpenTask,
  today,
  workspace,
}: DailyReviewPageProps) {
  const page = useDailyReviewPage({
    captures,
    initialTaskId,
    onNavigationLockChange,
    onNotice,
    onOpenCapture,
    onOpenTask,
    today,
    workspace,
  })
  return (
    <section className="review-page" aria-labelledby="review-heading">
      <DailyReviewHeading date={page.date} onDate={page.entry.requestDate} today={today} />
      {page.reviewQuery.isPending ? <LoadingBlock label="Opening the review…" /> : page.reviewQuery.isError || !page.review ? (
        <ErrorState message={getErrorMessage(page.reviewQuery.error)} onRetry={() => void page.reviewQuery.refetch()} />
      ) : (
        <DailyReviewLoaded
          availableTasks={page.availableTasks}
          captures={captures}
          checkinError={page.checkinMutation.isError}
          checkinPending={page.checkinMutation.isPending}
          date={page.date}
          entry={page.entry}
          onCheckin={page.startCheckin}
          onNotice={onNotice}
          onOpenCapture={onOpenCapture}
          onOpenTask={onOpenTask}
          review={page.review}
          transitions={page.transitions}
        />
      )}
      <DailyReportPreview
        date={page.date}
        sourceAvailable={page.sourceAvailable}
        sourceUpdatedAt={reviewDaySourceToken(page.review)}
        workspaceId={page.workspaceId}
      />
      <WeeklyReportPreview
        endDate={page.date}
        sourceAvailable={page.sourceAvailable}
        sourceUpdatedAt={reviewWeeklySourceToken(page.review)}
        workspaceId={page.workspaceId}
      />
      <SavedReportsPanel workspaceId={page.workspaceId} />
      <MutationNoticePanel onOpenTask={onOpenTask} tasks={workspace.tasks} workspaceId={page.workspaceId} />
    </section>
  )
}
