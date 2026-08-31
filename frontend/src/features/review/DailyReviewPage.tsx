import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, createIdempotencyKey } from '../../api/client'
import { Button, ErrorState, LoadingBlock, Pill } from '../../components/Primitives'
import type { Capture, ReviewEntryInput, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { LeadershipSignalsPanel } from './LeadershipSignalsPanel'

interface DailyReviewPageProps {
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenTask: (taskId: string) => void
  onOpenCapture?: (captureId: string) => void
  captures?: Capture[]
  today: string
  workspace: WorkspaceProjection
}

function localTime() {
  const now = new Date()
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
}

function splitItems(value: string) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function formatDuration(totalSeconds: number) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  if (hours && minutes) return `${hours}h ${minutes}m`
  if (hours) return `${hours}h`
  if (minutes) return `${minutes}m`
  return `${totalSeconds}s`
}

export function DailyReviewPage({ captures = [], onNotice, onOpenCapture, onOpenTask, today, workspace }: DailyReviewPageProps) {
  const queryClient = useQueryClient()
  const availableTasks = useMemo(() => [...workspace.tasks].sort((left, right) => (
    left.status === 'done' || left.status === 'dropped' ? 1 : -1
  ) - (right.status === 'done' || right.status === 'dropped' ? 1 : -1) || left.id.localeCompare(right.id)), [workspace.tasks])
  const [date, setDate] = useState(today)
  const [taskId, setTaskId] = useState(availableTasks[0]?.id ?? '')
  const [done, setDone] = useState('')
  const [next, setNext] = useState('')
  const [blockers, setBlockers] = useState('')
  const checkinIntent = useRef<{ date: string; time: string; key: string } | null>(null)
  const entryIntentKey = useRef<string | null>(null)

  useEffect(() => {
    if (!availableTasks.some((task) => task.id === taskId)) {
      setTaskId(availableTasks[0]?.id ?? '')
    }
  }, [availableTasks, taskId])

  const reviewQuery = useQuery({
    queryKey: ['review', date, 7],
    queryFn: () => api.getReview(date, 7),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['review', date, 7] })

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

  const entryMutation = useMutation({
    mutationFn: ({ input, key }: { input: ReviewEntryInput; key: string }) => api.addReviewEntry(input, key),
    onSuccess: async () => {
      entryIntentKey.current = null
      setDone('')
      setNext('')
      setBlockers('')
      await refresh()
      onNotice('Daily review entry added')
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  const startCheckin = () => {
    if (!checkinIntent.current || checkinIntent.current.date !== date) {
      checkinIntent.current = { date, time: localTime(), key: createIdempotencyKey() }
    }
    checkinMutation.mutate(checkinIntent.current)
  }

  const submitEntry = (event: FormEvent) => {
    event.preventDefault()
    const input = {
      date,
      task_id: taskId,
      done: splitItems(done),
      next: splitItems(next),
      blockers: splitItems(blockers),
    }
    if (!input.done.length && !input.next.length && !input.blockers.length) return
    entryIntentKey.current ??= createIdempotencyKey()
    entryMutation.mutate({ input, key: entryIntentKey.current })
  }

  const changeDraft = (setter: (value: string) => void) => (value: string) => {
    entryIntentKey.current = null
    setter(value)
  }

  const review = reviewQuery.data
  const selectedTask = workspace.tasks.find((task) => task.id === taskId)

  return (
    <section className="review-page" aria-labelledby="review-heading">
      <header className="page-heading">
        <div>
          <div className="eyebrow"><span className="live-dot" /> Daily review</div>
          <h1 id="review-heading">Turn execution into evidence.</h1>
          <p>Capture what moved, what comes next, and what needs help—without inferring execution state.</p>
        </div>
        <label className="review-date">
          <span>Review date</span>
          <input
            max={today}
            onChange={(event) => {
              setDate(event.target.value)
              checkinIntent.current = null
              entryIntentKey.current = null
            }}
            type="date"
            value={date}
          />
        </label>
      </header>

      {reviewQuery.isPending ? <LoadingBlock label="Opening the review…" /> : reviewQuery.isError || !review ? (
        <ErrorState message={getErrorMessage(reviewQuery.error)} onRetry={() => void reviewQuery.refetch()} />
      ) : (
        <>
          <div className="review-summary" aria-label="Daily review summary">
            <div><span>Check-in</span><strong>{review.day.start_time ?? 'Not yet'}</strong><Button disabled={checkinMutation.isPending} onClick={startCheckin} variant="ghost">{review.day.start_time ? 'Update time' : checkinMutation.isError ? 'Retry check-in' : 'Check in now'}</Button></div>
            <div><span>Entries today</span><strong>{review.day.entries.length}</strong><small>{review.day.entries.reduce((count, entry) => count + entry.done.length, 0)} done items</small></div>
            <div><span>7-day tasks</span><strong>{review.weekly.projects.length}</strong><small>{review.weekly.range.start} → {review.weekly.range.end}</small></div>
            <div className={review.weekly.projects.some((project) => project.blockers.length) ? 'metric-attention' : ''}><span>Open signals</span><strong>{review.weekly.projects.reduce((count, project) => count + project.blockers.length, 0)}</strong><small>reported blockers</small></div>
          </div>

          <div className="review-layout">
            <form className="review-entry-card" onSubmit={submitEntry}>
              <header><span>New evidence</span><strong>{date}</strong></header>
              <label><span>Task</span><select disabled={entryMutation.isPending} onChange={(event) => { entryIntentKey.current = null; setTaskId(event.target.value) }} value={taskId}>{availableTasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
              {selectedTask ? <button className="review-task-link" onClick={() => onOpenTask(selectedTask.id)} type="button">Open {selectedTask.id} planning detail</button> : null}
              <label><span>Done <small>one item per line</small></span><textarea disabled={entryMutation.isPending} onChange={(event) => changeDraft(setDone)(event.target.value)} rows={4} value={done} /></label>
              <label><span>Next <small>one item per line</small></span><textarea disabled={entryMutation.isPending} onChange={(event) => changeDraft(setNext)(event.target.value)} rows={4} value={next} /></label>
              <label><span>Blockers <small>one item per line</small></span><textarea disabled={entryMutation.isPending} onChange={(event) => changeDraft(setBlockers)(event.target.value)} rows={3} value={blockers} /></label>
              <Button disabled={entryMutation.isPending || !taskId || ![done, next, blockers].some((value) => value.trim())} type="submit" variant="primary">{entryMutation.isPending ? 'Saving…' : entryMutation.isError ? 'Retry unchanged entry' : 'Add review entry'}</Button>
              {entryMutation.error ? <p className="form-error" role="alert">{getErrorMessage(entryMutation.error)}</p> : null}
            </form>

            <section className="review-day-card" aria-labelledby="review-day-heading">
              <header><span>Day record</span><strong id="review-day-heading">{review.day.entries.length ? `${review.day.entries.length} entries` : 'No entries yet'}</strong></header>
              {review.day.entries.length ? <div className="review-entry-list">{review.day.entries.map((entry, index) => (
                <article key={`${entry.task_id}-${index}`}>
                  <button onClick={() => onOpenTask(entry.task_id)} type="button"><strong>{entry.task_id}</strong><span>{entry.task}</span></button>
                  {entry.session_id && entry.duration_seconds !== undefined ? <small className="review-entry-duration">{formatDuration(entry.duration_seconds)} focused · {entry.session_id}</small> : null}
                  {(['done', 'next', 'blockers'] as const).map((field) => entry[field].length ? <div className={`review-facts review-facts--${field}`} key={field}><span>{field}</span><ul>{entry[field].map((item) => <li key={item}>{item}</li>)}</ul></div> : null)}
                </article>
              ))}</div> : <p className="review-empty">Record a concrete Done, Next, or Blocker item for a Task.</p>}
            </section>
          </div>

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
          {onOpenCapture ? <LeadershipSignalsPanel captures={captures} onNotice={onNotice} onOpenCapture={onOpenCapture} /> : null}
        </>
      )}
    </section>
  )
}
