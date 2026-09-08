import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Button, Pill } from '../../components/Primitives'
import type { Objective } from '../../domain/types'
import { formatDate, getObjectiveTitle, priorityLabels } from '../../utils/format'
import type { KnowledgeTaskRef } from '../knowledge/knowledgeTypes'
import { LinkedReferenceSummary } from '../knowledge/LinkedReferenceSummary'
import type { TaskResumeFactsResult } from './useTaskResumeFacts'
import {
  checkpointAttribution,
  LOADING_PROGRESS_COPY,
  NO_BLOCKERS_COPY,
  NO_DONE_COPY,
  NO_NEXT_COPY,
  NO_PROGRESS_COPY,
  PARTIAL_RECORD_HEADLINE,
  PARTIAL_RECORD_PRESERVED,
  previewResumeItems,
  recentProgressStateCopy,
  recordedTaskDispute,
  recordedTaskMismatchCopy,
  unpresentedFieldsCopy,
} from './taskResumeProgressAdapter'
import './TaskResumePanel.css'

export const VIEW_ALL_CONTEXT_ID = 'task-resume-view-all'

export interface TaskResumeSavedTask extends KnowledgeTaskRef {
  detail: string
  due: string | null
  priority: 'P0' | 'P1' | 'P2' | 'P3'
  scheduled?: string | null
}

export interface TaskResumePanelProps {
  contextCount: number
  facts: TaskResumeFactsResult
  objectives: Objective[]
  onOpenContext: () => void
  onOpenObjective?: (objectiveId: string) => void
  progressLocked: boolean
  savedTask: TaskResumeSavedTask
  workspaceUid: string
}

function TaskResumeTitle({ title }: { title: string }) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(title.length > 72 || title.includes('\n'))

  useLayoutEffect(() => {
    const heading = headingRef.current
    if (!heading || expanded) return
    const long = title.length > 72 || title.includes('\n')
    setOverflows(long || heading.scrollHeight > heading.clientHeight + 1)
  }, [expanded, title])

  const toggle = overflows || expanded
  return (
    <div className="task-resume-title">
      <h2 className={expanded ? undefined : 'is-clamped'} ref={headingRef}>{title}</h2>
      {toggle ? (
        <button
          aria-expanded={expanded}
          className="task-resume-title__toggle"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >{expanded ? 'Show less' : 'Show full title'}</button>
      ) : null}
    </div>
  )
}

function ResumeChips({
  navigationLocked,
  objectives,
  onOpenObjective,
  priority,
}: {
  navigationLocked: boolean
  objectives: Objective[]
  onOpenObjective?: (objectiveId: string) => void
  priority: TaskResumeSavedTask['priority']
}) {
  return (
    <div className="drawer-chips">
      <Pill tone={priority.toLowerCase()}>{priority} · {priorityLabels[priority]}</Pill>
      {objectives.map((objective) => onOpenObjective ? (
        <button
          aria-label={`Open objective ${objective.id}`}
          className="objective-pill-link"
          disabled={navigationLocked}
          key={objective.id}
          onClick={() => onOpenObjective(objective.id)}
          title={getObjectiveTitle(objective)}
          type="button"
        ><Pill tone="accent">{objective.id}</Pill></button>
      ) : <Pill key={objective.id} tone="accent">{objective.id}</Pill>)}
    </div>
  )
}

function FactList({ empty, items, label }: { empty: string; items: readonly string[]; label: string }) {
  const [expanded, setExpanded] = useState(false)
  if (!items.length) return <p>{empty}</p>
  const preview = previewResumeItems(items)
  const shown = expanded ? items : preview.shown
  return (
    <>
      <ol aria-label={label}>
        {shown.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}
      </ol>
      {preview.hidden ? (
        <button
          aria-expanded={expanded}
          className="task-resume-title__toggle"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >{expanded ? 'Show fewer' : `Show all ${items.length}`}</button>
      ) : null}
    </>
  )
}

function ResumeStatus({ children, facts }: { children?: ReactNode; facts: TaskResumeFactsResult }) {
  if (facts.status === 'loading') {
    return (
      <div aria-busy="true" aria-live="polite" className="task-resume-skeleton">
        <span /><span /><span />
        <span className="sr-only">{LOADING_PROGRESS_COPY}</span>
      </div>
    )
  }
  if (facts.status === 'error') {
    return (
      <div className="inline-error" role="alert">
        <span>{facts.errorMessage ?? 'Recorded progress could not be read.'}</span>
        <Button onClick={facts.retry} variant="ghost">Try again</Button>
      </div>
    )
  }
  if (facts.status === 'empty') return <p>{NO_PROGRESS_COPY}</p>
  if (facts.status === 'unreadable') {
    return (
      <div className="inline-error" role="alert">
        <span>{facts.unreadableReason ?? 'The latest checkpoint could not be read.'}</span>
      </div>
    )
  }
  return <>{children}</>
}

/** Read far enough to have Done/Next/Blockers to show, whole or not. */
function isSettled(facts: TaskResumeFactsResult) {
  return facts.status === 'ready' || facts.status === 'partial'
}

/**
 * Says what could not be presented, before the reader acts on what could.
 *
 * A `partial` record is readable but disputed or incomplete, and rendering it
 * exactly like an ordinary one is what would make this screen lie. Everything
 * here is a stored value or a label the summariser produced — the notice names
 * the disagreement and the fields it could not lay out, and never guesses why.
 */
function ResumePartialNotice({ facts }: { facts: TaskResumeFactsResult }) {
  if (facts.status !== 'partial') return null
  const disputed = recordedTaskDispute(facts.provenance, facts.taskId)
  const labels = facts.extras.map((extra) => extra.label)
  return (
    <div className="task-resume-notice" role="status">
      <p className="task-resume-notice__headline">{PARTIAL_RECORD_HEADLINE}</p>
      {disputed ? <p>{recordedTaskMismatchCopy(disputed, facts.taskId)}</p> : null}
      {labels.length ? <p>{unpresentedFieldsCopy(labels)}</p> : null}
      <p className="task-resume__meta">{PARTIAL_RECORD_PRESERVED}</p>
    </div>
  )
}

function ResumeNextBlockers({ facts }: { facts: TaskResumeFactsResult }) {
  const date = facts.provenance?.date
  const settled = isSettled(facts)
  return (
    <section className="task-resume-section">
      <ResumePartialNotice facts={facts} />
      <h3>Next step</h3>
      {date ? <p className="task-resume__meta">{checkpointAttribution(date)}</p> : null}
      {settled ? <FactList empty={NO_NEXT_COPY} items={facts.next} label="Next step" /> : <ResumeStatus facts={facts} />}
      {settled ? (
        <>
          <h3>Blockers</h3>
          <FactList empty={NO_BLOCKERS_COPY} items={facts.blockers} label="Blockers" />
        </>
      ) : null}
    </section>
  )
}

/** Whether any record identity or count exists to disclose. */
function hasRecordDetails(facts: TaskResumeFactsResult) {
  return Boolean(facts.provenance)
    || facts.extras.length > 0
    || facts.activeRecordCount > 0
    || facts.supersededRecordCount > 0
}

function ResumeHistory({ facts }: { facts: TaskResumeFactsResult }) {
  const provenance = facts.provenance
  if (!hasRecordDetails(facts)) return null
  return (
    <details className="task-resume-details">
      <summary>Record details and history</summary>
      <div className="task-resume-history">
        {provenance ? (
          <p className="task-resume__meta">
            {provenance.checkpointId ?? `legacy@${provenance.date}#${provenance.ordinal}`}
            {` · ordinal ${provenance.ordinal} · revision ${provenance.revision}`}
            {provenance.binding === 'entry-payload' ? ' · bound from the recorded entry' : ''}
            {provenance.origin ? ` · ${provenance.origin}` : ''}
          </p>
        ) : null}
        <p className="task-resume__meta">
          {facts.activeRecordCount} active
          {facts.supersededRecordCount ? ` · ${facts.supersededRecordCount} superseded` : ''}
        </p>
        {facts.extras.length ? (
          <pre>{facts.extras.map((extra) => `${extra.label}: ${extra.value}`).join('\n')}</pre>
        ) : null}
      </div>
    </details>
  )
}

/**
 * Recent progress, with a state of its own rather than an orphan heading.
 *
 * When the record has not settled there is no done list to print, so the section
 * says which state it is in instead of leaving a heading over nothing — and when
 * there is no record identity to disclose either, the heading does not appear.
 */
function ResumeRecentProgress({ facts }: { facts: TaskResumeFactsResult }) {
  const settled = isSettled(facts)
  if (!settled && !hasRecordDetails(facts)) return null
  return (
    <section className="task-resume-section">
      <h3>Recent progress</h3>
      {settled
        ? <FactList empty={NO_DONE_COPY} items={facts.done} label="Recent progress" />
        : <p>{recentProgressStateCopy(facts.status)}</p>}
      <ResumeHistory facts={facts} />
    </section>
  )
}

function ResumeSecondary({
  detail,
  due,
  facts,
  revision,
  scheduled,
  uid,
}: {
  detail: string
  due: string | null
  facts: TaskResumeFactsResult
  revision: number
  scheduled: string | null | undefined
  uid: string
}) {
  return (
    <>
      <ResumeRecentProgress facts={facts} />
      <section className="task-resume-section">
        <h3>Definition of done</h3>
        <p>{detail.trim() ? detail : 'No definition of done is saved on this task.'}</p>
      </section>
      <details className="task-resume-details">
        <summary>Task details</summary>
        <div className="task-resume-details__body">
          <span><strong>Plan for</strong><span>{scheduled ? formatDate(scheduled, scheduled) : 'Not scheduled'}</span></span>
          <span><strong>Due</strong><span>{due ? formatDate(due, due) : 'No due date'}</span></span>
          <span><strong>Stable UID</strong><code>{uid}</code></span>
          <span><strong>Current version</strong><code>Revision {revision}</code></span>
        </div>
      </details>
    </>
  )
}

export function TaskResumeFooter({
  locked,
  onRecordProgress,
  taskId,
}: {
  locked: boolean
  onRecordProgress?: (taskId: string) => void
  taskId: string
}) {
  return (
    <div className="task-resume-footer">
      <Button
        disabled={locked || !onRecordProgress}
        onClick={() => onRecordProgress?.(taskId)}
        variant="primary"
      >Record progress</Button>
    </div>
  )
}

export function TaskResumePanel({
  contextCount,
  facts,
  objectives,
  onOpenContext,
  onOpenObjective,
  progressLocked,
  savedTask,
  workspaceUid,
}: TaskResumePanelProps) {
  const contextLabel = contextCount ? `View all context (${contextCount})` : 'View all context'
  return (
    <div className="task-resume">
      <TaskResumeTitle title={savedTask.title} />
      <ResumeChips
        navigationLocked={progressLocked}
        objectives={objectives}
        onOpenObjective={onOpenObjective}
        priority={savedTask.priority}
      />
      <ResumeNextBlockers facts={facts} />
      <LinkedReferenceSummary
        onOpenReferences={onOpenContext}
        task={savedTask}
        workspaceUid={workspaceUid}
      />
      <div className="task-resume-actions">
        <Button id={VIEW_ALL_CONTEXT_ID} onClick={onOpenContext} variant="secondary">{contextLabel}</Button>
        <p className="task-resume__note">Copy this brief into your agent session. Nothing is sent automatically.</p>
      </div>
      <ResumeSecondary
        detail={savedTask.detail}
        due={savedTask.due}
        facts={facts}
        revision={savedTask.revision}
        scheduled={savedTask.scheduled}
        uid={savedTask.uid}
      />
    </div>
  )
}
