import { lazy, Suspense, useMemo, useState } from 'react'
import { Icon } from '../../components/Icon'
import { Button, EmptyState, ErrorState, IconButton, LoadingBlock, Pill } from '../../components/Primitives'
import type { TaskStatus, WorkSessionEntryInput, WorkSessionProjection, WorkspaceProjection } from '../../domain/types'
import { formatDate, getErrorMessage } from '../../utils/format'
import { buildFocusProjection, type FocusReason } from './focusModel'
import { WorkSessionPanel } from './WorkSessionPanel'

const AgendaPanel = lazy(() => import('./AgendaPanel').then((module) => ({ default: module.AgendaPanel })))

interface FocusPageProps {
  workspace: WorkspaceProjection
  today: string
  isRefreshing: boolean
  onRefresh: () => void
  onCreateTask: () => void
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onSelectTask: (taskId: string) => void
  workSessions?: WorkSessionProjection
  isWorkSessionPending?: boolean
  onStartWorkSession?: (taskId: string) => Promise<void>
  onTransitionWorkSession?: (sessionId: string, action: 'pause' | 'resume' | 'stop') => Promise<void>
  onRecordWorkSession?: (sessionId: string, input: WorkSessionEntryInput) => Promise<void>
  workSessionError?: string
  onNotice?: (message: string, tone?: 'success' | 'error') => void
}

const reasonTones: Record<FocusReason['kind'], string> = {
  invalid_due: 'warning',
  overdue: 'overdue',
  due_today: 'due-today',
  due_soon: 'due-soon',
  in_progress: 'started',
  high_priority: 'neutral',
}

export function FocusPage({
  workspace,
  today,
  isRefreshing,
  onRefresh,
  onCreateTask,
  onChangeTaskStatus,
  onSelectTask,
  workSessions,
  isWorkSessionPending = false,
  onStartWorkSession,
  onTransitionWorkSession,
  onRecordWorkSession,
  workSessionError,
  onNotice,
}: FocusPageProps) {
  const [pendingTaskIds, setPendingTaskIds] = useState<Set<string>>(() => new Set())
  const result = useMemo(() => {
    try {
      return { projection: buildFocusProjection(workspace.tasks, today), error: null }
    } catch (error) {
      return { projection: null, error: getErrorMessage(error) }
    }
  }, [today, workspace.tasks])

  const changeStatus = async (taskId: string, status: TaskStatus) => {
    setPendingTaskIds((current) => new Set(current).add(taskId))
    try {
      await onChangeTaskStatus(taskId, status)
    } finally {
      setPendingTaskIds((current) => {
        const next = new Set(current)
        next.delete(taskId)
        return next
      })
    }
  }

  return (
    <section className="focus-page" aria-labelledby="focus-heading">
      <header className="page-heading page-heading--compact">
        <div>
          <div className="eyebrow"><Icon name="target" size={14} /> Focus experiment</div>
          <h1 id="focus-heading">See what deserves attention now.</h1>
          <p>
            <time dateTime={today}>{formatDate(today)}</time>
            {' · '}Focus candidates are an explainable subset of your active tasks.
          </p>
        </div>
        <div className="page-heading__actions">
          <IconButton
            disabled={isRefreshing}
            icon="refresh"
            label="Refresh focus"
            onClick={onRefresh}
          />
          <Button icon="plus" onClick={onCreateTask} variant="primary">New task</Button>
        </div>
      </header>

      {result.error || !result.projection ? (
        <ErrorState message={result.error ?? 'Focus projection is unavailable.'} />
      ) : (
        <>
          {workSessionError ? <p className="work-session-error" role="alert">Work sessions unavailable: {workSessionError}</p> : null}
          {workSessions && onTransitionWorkSession && onRecordWorkSession ? (
            <WorkSessionPanel
              disabled={isWorkSessionPending}
              onRecord={onRecordWorkSession}
              onTransition={onTransitionWorkSession}
              projection={workSessions}
            />
          ) : null}
          <div className="focus-summary" aria-label="Focus summary">
            <div>
              <span>Actionable now</span>
              <strong>{result.projection.actionableCount}</strong>
              <small>Focus candidates with completed dependencies</small>
            </div>
            <div>
              <span>Blocked candidates</span>
              <strong>{result.projection.blockedCount}</strong>
              <small>Attention-worthy tasks waiting on dependencies</small>
            </div>
            <div>
              <span>Active tasks</span>
              <strong>{result.projection.activeCount}</strong>
              <small>Open or in progress across the workspace</small>
            </div>
          </div>

          {onNotice ? <Suspense fallback={<LoadingBlock label="Opening week plan…" />}><AgendaPanel onNotice={onNotice} onSelectTask={onSelectTask} tasks={workspace.tasks} /></Suspense> : null}

          {result.projection.candidates.length ? (
            <div className="focus-list" aria-label="Focus candidates">
              {result.projection.candidates.map(({ task, reasons, blockers }) => (
                <article className={`focus-row${blockers.length ? ' focus-row--blocked' : ''}`} key={task.id}>
                  <div className="focus-row__identity">
                    <span className="focus-row__id">{task.id}</span>
                    <div className="focus-row__title-group">
                      <button
                        aria-label={`Open ${task.id} · ${task.title}`}
                        className="focus-row__title"
                        onClick={() => onSelectTask(task.id)}
                        type="button"
                      >
                        {task.title}
                      </button>
                      {blockers.length ? <small>Waiting on {blockers.map((blocker) => `${blocker.id} · ${blocker.title ?? 'Unavailable dependency'}`).join(', ')}</small> : null}
                    </div>
                  </div>
                  <div className="focus-row__reasons" aria-label={`Reasons to focus on ${task.id}`}>
                    {blockers.map((blocker) => blocker.status ? (
                      <button
                        aria-label={`Open blocker ${blocker.id}`}
                        className="focus-blocker-link"
                        key={`blocker-${blocker.id}`}
                        onClick={() => onSelectTask(blocker.id)}
                        type="button"
                      ><Pill tone="warning">Blocked by {blocker.id}</Pill></button>
                    ) : <Pill key={`blocker-${blocker.id}`} tone="warning">Blocked by {blocker.id}</Pill>)}
                    {reasons.map((reason) => (
                      <Pill
                        key={reason.kind}
                        tone={reason.kind === 'high_priority' ? task.priority.toLowerCase() : reasonTones[reason.kind]}
                      >
                        {reason.label}
                      </Pill>
                    ))}
                  </div>
                  <button
                    aria-label={`Begin work session for ${task.id}`}
                    className="focus-row__session"
                    disabled={
                      blockers.length > 0
                      || isWorkSessionPending
                      || Boolean(workSessions?.current)
                      || !onStartWorkSession
                    }
                    onClick={() => void onStartWorkSession?.(task.id)}
                    type="button"
                  >{workSessions?.current?.task_id === task.id ? 'In focus' : workSessions?.current ? 'Session active' : 'Focus'}</button>
                  <button
                    className="focus-row__action"
                    disabled={blockers.length > 0 || pendingTaskIds.has(task.id)}
                    onClick={() => void changeStatus(task.id, task.status === 'open' ? 'started' : 'done')}
                    type="button"
                  >{blockers.length ? 'Blocked' : pendingTaskIds.has(task.id) ? 'Saving…' : task.status === 'open' ? 'Start' : 'Mark done'}</button>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState
              action={<Button icon="plus" onClick={onCreateTask}>New task</Button>}
              icon="check"
              title="No focus candidates"
            >
              No active task currently has a due-date, progress, or priority reason to appear here.
            </EmptyState>
          )}
        </>
      )}
    </section>
  )
}
