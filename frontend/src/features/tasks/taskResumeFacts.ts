/**
 * The one recorded checkpoint a Task resumes from.
 *
 * Resume answers a single question — what did I say I would do next? — so this
 * module picks exactly ONE record and reports it whole. It is pure and total:
 * every audit shape resolves to a stated snapshot, and nothing here fetches,
 * writes, invalidates or reformats a fact.
 *
 * Three rules carry the honesty of the whole surface.
 *
 * ONE RECORD. Done, Next and Blockers always come from the same checkpoint. An
 * older nonempty blocker is never carried over a newer empty one: the newer
 * record's silence is the author's own statement that nothing is in the way.
 *
 * THE LATEST IS THE LATEST. If the newest active record cannot be read, that is
 * reported as an unreadable latest — an older readable entry is never quietly
 * promoted into its place, because a reader acting on a stale next step is worse
 * off than one told the record is broken.
 *
 * THIS TASK ONLY. Selection is bound to the workspace and Task the caller asked
 * for, so a drawer that has already navigated to another Task cannot show the
 * previous one's plan. A legacy row predating checkpoint identity has no Task in
 * its locator at all; it is still offered, but its weaker binding is declared
 * rather than hidden.
 */

import {
  summarizeCheckpointEntry,
  type CheckpointEntrySummary,
  type CheckpointExtra,
} from '../../domain/checkpointEntrySummary'
import type { CheckpointAudit, CheckpointAuditEntry } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'

/** Where the record's Task binding came from. Never inferred silently. */
export type TaskResumeBinding =
  /** The audit locator names the Task. Server-validated and authoritative. */
  | 'locator'
  /**
   * A legacy row carries no Task locator, so the only claim available is the
   * one inside the opaque payload. Weaker, and always shown as such.
   */
  | 'entry-payload'

export type TaskResumeStatus =
  /** The workspace audit is in flight. Nothing may be presented as a fact. */
  | 'loading'
  /** The audit read failed. `errorMessage` carries the reason verbatim. */
  | 'error'
  /** The audit was read and holds no active record for this Task. */
  | 'empty'
  /** A latest active record exists, but nothing human-facing could be read. */
  | 'unreadable'
  /** The latest record was read, but part of it could not be presented. */
  | 'partial'
  /** The latest record was read and rendered in full. */
  | 'ready'

/** Identity of the selected record. Every field is stored, never derived. */
export interface TaskResumeProvenance {
  workspaceUid: string
  /** The requested Task. This snapshot never speaks for another one. */
  taskId: string
  /** Null on a legacy row recorded before checkpoint identity existed. */
  checkpointId: string | null
  /** Content digest of the recorded entry; null on a legacy row. */
  entryDigest: string | null
  /** The recorded calendar day, verbatim. No time of day is invented. */
  date: string
  /** Slot within that day, and the tie-break that orders same-day records. */
  ordinal: number
  /** Transition revision; 0 while the record has never been superseded. */
  revision: number
  /** Recording origin as stored, such as `agent-cli-v1`. */
  origin: string | null
  binding: TaskResumeBinding
  /** What the opaque payload itself claimed, so a disagreement stays visible. */
  recordedTaskId: string | null
  recordedTaskTitle: string | null
}

export interface TaskResumeFacts {
  status: TaskResumeStatus
  workspaceUid: string
  taskId: string
  /** Null unless a record was selected. */
  provenance: TaskResumeProvenance | null
  /** All three come from ONE record. They are never blended across records. */
  done: readonly string[]
  next: readonly string[]
  blockers: readonly string[]
  /** What the summary could not render, kept discoverable rather than dropped. */
  extras: readonly CheckpointExtra[]
  /** The summarizer's own stated reason, present exactly when `unreadable`. */
  unreadableReason: string | null
  /** The query failure, verbatim, present exactly when `error`. */
  errorMessage: string | null
  /** Active records for this Task, the selected one included. */
  activeRecordCount: number
  /** Superseded records stay in history; the resume snapshot excludes them. */
  supersededRecordCount: number
  /** Changes exactly when this Task's snapshot identity or content changes. */
  version: string
}

/** The audit read, as the caller's query reports it. */
export interface TaskResumeAuditState {
  audit: CheckpointAudit | undefined
  isPending: boolean
  error: unknown
}

/**
 * The shared workspace audit key. Daily Review and the checkpoint notices use
 * this same literal, so a Task drawer and the handoff panel read one cache
 * entry and one in-flight request rather than opening a second one.
 */
export function taskResumeAuditQueryKey(workspaceUid: string): readonly [string, string] {
  return ['checkpoint-audit', workspaceUid]
}

/**
 * The Task binding, or null when this entry belongs to another Task.
 *
 * A recorded row names its Task in the locator and that is the end of it. Only
 * a legacy row — no checkpoint identity, no recorded fact, no Task locator —
 * falls back to the claim inside its own payload, which is the only place that
 * era stored the Task at all.
 */
function taskBinding(entry: CheckpointAuditEntry, taskId: string): TaskResumeBinding | null {
  if (entry.locator.task_id !== null) {
    return entry.locator.task_id === taskId ? 'locator' : null
  }
  return summarizeCheckpointEntry(entry.entry).taskId === taskId ? 'entry-payload' : null
}

interface BoundEntry {
  entry: CheckpointAuditEntry
  binding: TaskResumeBinding
}

interface TaskRecords {
  latestActive: BoundEntry | null
  activeCount: number
  supersededCount: number
}

/** Later means a later recorded day, then a later slot within that day. */
function isLater(candidate: CheckpointAuditEntry, incumbent: CheckpointAuditEntry): boolean {
  const { date, ordinal } = candidate.locator
  // ISO-8601 dates sort correctly as text, and the schema already proved these
  // are real calendar days. Checkpoint ids are never parsed for order.
  if (date !== incumbent.locator.date) return date > incumbent.locator.date
  return ordinal > incumbent.locator.ordinal
}

/**
 * One pass over the workspace audit. The workspace filter is applied per entry
 * rather than trusting the envelope, so a cache entry that somehow belongs to
 * another workspace contributes nothing instead of leaking into this Task.
 */
function collectTaskRecords(
  audit: CheckpointAudit,
  workspaceUid: string,
  taskId: string,
): TaskRecords {
  let latestActive: BoundEntry | null = null
  let activeCount = 0
  let supersededCount = 0
  for (const entry of audit.entries) {
    if (entry.locator.workspace_uid !== workspaceUid) continue
    const binding = taskBinding(entry, taskId)
    if (binding === null) continue
    if (entry.state === 'superseded') {
      supersededCount += 1
      continue
    }
    activeCount += 1
    if (latestActive === null || isLater(entry, latestActive.entry)) {
      latestActive = { entry, binding }
    }
  }
  return { latestActive, activeCount, supersededCount }
}

function provenanceOf(
  bound: BoundEntry,
  summary: CheckpointEntrySummary,
  workspaceUid: string,
  taskId: string,
): TaskResumeProvenance {
  const { entry } = bound
  return {
    workspaceUid,
    taskId,
    checkpointId: entry.checkpoint_id,
    entryDigest: entry.locator.entry_digest,
    date: entry.locator.date,
    ordinal: entry.locator.ordinal,
    revision: entry.revision,
    origin: entry.recorded?.origin ?? null,
    binding: bound.binding,
    recordedTaskId: summary.taskId,
    recordedTaskTitle: summary.taskTitle,
  }
}

/**
 * The freshness identity a prepared brief is frozen against.
 *
 * It is built only from THIS Task's selected record, so another Task's activity
 * cannot spuriously stale a brief, while a new checkpoint on this Task changes
 * the identity even though the Task's own revision did not move.
 */
function versionOf(
  status: TaskResumeStatus,
  workspaceUid: string,
  taskId: string,
  provenance: TaskResumeProvenance | null,
): string {
  const binding = `v1:${status}:${workspaceUid}:${taskId}`
  if (provenance === null) return binding
  const identity = provenance.checkpointId ?? `legacy@${provenance.date}#${provenance.ordinal}`
  const digest = provenance.entryDigest ?? 'no-digest'
  return `${binding}:${identity}:${digest}:r${provenance.revision}`
}

interface FactsDraft {
  status: TaskResumeStatus
  provenance?: TaskResumeProvenance | null
  done?: readonly string[]
  next?: readonly string[]
  blockers?: readonly string[]
  extras?: readonly CheckpointExtra[]
  unreadableReason?: string | null
  errorMessage?: string | null
  activeRecordCount?: number
  supersededRecordCount?: number
}

function facts(workspaceUid: string, taskId: string, draft: FactsDraft): TaskResumeFacts {
  const provenance = draft.provenance ?? null
  return {
    status: draft.status,
    workspaceUid,
    taskId,
    provenance,
    done: draft.done ?? [],
    next: draft.next ?? [],
    blockers: draft.blockers ?? [],
    extras: draft.extras ?? [],
    unreadableReason: draft.unreadableReason ?? null,
    errorMessage: draft.errorMessage ?? null,
    activeRecordCount: draft.activeRecordCount ?? 0,
    supersededRecordCount: draft.supersededRecordCount ?? 0,
    version: versionOf(draft.status, workspaceUid, taskId, provenance),
  }
}

/**
 * Partial means read but not wholly presentable: either the summary left values
 * it could not render, or the payload names a different Task than the locator
 * it was filed under. Both are worth a reader's attention before they act.
 */
function readableStatus(
  summary: CheckpointEntrySummary,
  binding: TaskResumeBinding,
  taskId: string,
): TaskResumeStatus {
  const disputed = binding === 'locator'
    && summary.taskId !== null
    && summary.taskId !== taskId
  return summary.extras.length > 0 || disputed ? 'partial' : 'ready'
}

function selectFromRecords(
  workspaceUid: string,
  taskId: string,
  records: TaskRecords,
): TaskResumeFacts {
  const counts = {
    activeRecordCount: records.activeCount,
    supersededRecordCount: records.supersededCount,
  }
  const bound = records.latestActive
  if (bound === null) return facts(workspaceUid, taskId, { status: 'empty', ...counts })

  const summary = summarizeCheckpointEntry(bound.entry.entry)
  const provenance = provenanceOf(bound, summary, workspaceUid, taskId)
  if (!summary.readable) {
    // The newest record stays the newest record. History is still reachable
    // through the audit; an older entry is never relabelled as the latest.
    return facts(workspaceUid, taskId, {
      ...counts,
      status: 'unreadable',
      provenance,
      extras: summary.extras,
      unreadableReason: summary.fallback,
    })
  }
  return facts(workspaceUid, taskId, {
    ...counts,
    status: readableStatus(summary, bound.binding, taskId),
    provenance,
    done: summary.done,
    next: summary.next,
    blockers: summary.blockers,
    extras: summary.extras,
  })
}

/**
 * The resume snapshot for one workspace and one saved Task.
 *
 * Deterministic: the same audit and binding always produce the same facts and
 * the same `version`. An absent audit is not an empty one — a read still in
 * flight says `loading`, a failed read says `error`, and only a read that
 * genuinely returned no active record for this Task says `empty`.
 */
export function selectTaskResumeFacts(
  workspaceUid: string,
  taskId: string,
  state: TaskResumeAuditState,
): TaskResumeFacts {
  if (state.error !== null && state.error !== undefined) {
    return facts(workspaceUid, taskId, {
      status: 'error',
      errorMessage: getErrorMessage(state.error),
    })
  }
  if (state.audit === undefined) {
    // No audit and nothing in flight is an idle read, not a pending one: there
    // are no records to report and none are coming.
    return facts(workspaceUid, taskId, { status: state.isPending ? 'loading' : 'empty' })
  }
  return selectFromRecords(
    workspaceUid,
    taskId,
    collectTaskRecords(state.audit, workspaceUid, taskId),
  )
}
