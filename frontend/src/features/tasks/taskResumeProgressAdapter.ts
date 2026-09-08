/**
 * Maps UI-B resume facts onto the UI-D knowledge progress prop.
 *
 * UI-C owns this adapter so knowledge does not import the Task feature. It does
 * not select checkpoints, fetch audits, or invent a facts shim. Arrays are kept
 * intact; digest is the facts version so a checkpoint-only change is visible.
 */

import {
  NO_BLOCKERS_COPY,
  NO_NEXT_COPY,
  NO_PROGRESS_COPY,
  UNBOUND_RESUME_PROGRESS,
  type ResumeProgressFacts,
  type ResumeProgressSnapshot,
} from '../knowledge/resumeProgressContract'
import type { TaskResumeFacts, TaskResumeProvenance, TaskResumeStatus } from './taskResumeFacts'

export type { ResumeProgressFacts, ResumeProgressSnapshot }

/**
 * Resume and the brief say the same thing about the same record, so the wording
 * has one home: the knowledge contract both sides already speak. Re-exporting it
 * here keeps the Task feature's imports local without giving the copy a second
 * definition that could drift.
 */
export { NO_BLOCKERS_COPY, NO_NEXT_COPY, NO_PROGRESS_COPY }

/**
 * The contract states this wording once, on the unbound value itself. Narrowing
 * is only a type artifact of that constant being declared as the whole union —
 * `UNBOUND_RESUME_PROGRESS` is the unavailable branch by construction, and
 * `adapter shares the knowledge contract's copy` asserts it stays so.
 */
export const UNAVAILABLE_PROGRESS_COPY =
  UNBOUND_RESUME_PROGRESS.status === 'unavailable' ? UNBOUND_RESUME_PROGRESS.reason : ''

export const RECORDED_TASK_IDENTITY_MISSING = 'recorded task identity'

/** Read, but not wholly presentable. Never dressed up as an ordinary record. */
export const PARTIAL_RECORD_HEADLINE = 'This record could not be presented in full.'
/** The recorded text is still the author's, so it is shown, not withheld. */
export const PARTIAL_RECORD_PRESERVED = 'The recorded text below is shown exactly as it was saved.'
export const NO_DONE_COPY = 'No done items recorded in this checkpoint.'
export const LOADING_PROGRESS_COPY = 'Loading recorded progress…'
export const ERROR_PROGRESS_COPY = 'Recorded progress could not be read.'
export const UNREADABLE_DONE_COPY = 'Recent progress could not be read from this checkpoint.'

const LIST_PREVIEW = 4

export function joinResumeField(items: readonly string[]): string | null {
  return items.length ? items.join('\n') : null
}

export function previewResumeItems(items: readonly string[]): {
  shown: readonly string[]
  hidden: number
} {
  if (items.length <= LIST_PREVIEW) return { shown: items, hidden: 0 }
  return { shown: items.slice(0, LIST_PREVIEW), hidden: items.length - LIST_PREVIEW }
}

export function checkpointAttribution(date: string): string {
  return `From the latest checkpoint · ${date}`
}

/**
 * The recorded payload's own Task claim, when it contradicts the locator this
 * record was filed under. Null whenever the two agree or the payload is silent —
 * a record that never named a Task disputes nothing.
 */
export function recordedTaskDispute(
  provenance: TaskResumeProvenance | null,
  taskId: string,
): TaskResumeProvenance | null {
  if (!provenance?.recordedTaskId) return null
  return provenance.recordedTaskId === taskId ? null : provenance
}

/** Names both sides of the disagreement with the values actually stored. */
export function recordedTaskMismatchCopy(
  provenance: TaskResumeProvenance,
  taskId: string,
): string {
  const title = provenance.recordedTaskTitle
  const recorded = title ? `${provenance.recordedTaskId} · ${title}` : String(provenance.recordedTaskId)
  return `The recorded entry names Task ${recorded}, not ${taskId}.`
}

/**
 * The field labels the summary could not render. They are named rather than
 * described, so a reader can find each one verbatim under record details.
 */
export function unpresentedFieldsCopy(labels: readonly string[]): string {
  return `Kept under Record details and history, not shown above: ${labels.join(', ')}.`
}

/** What "Recent progress" says when there is no readable done list to list. */
export function recentProgressStateCopy(status: TaskResumeStatus): string {
  if (status === 'loading') return LOADING_PROGRESS_COPY
  if (status === 'empty') return NO_PROGRESS_COPY
  if (status === 'error') return ERROR_PROGRESS_COPY
  return UNREADABLE_DONE_COPY
}

function snapshotFrom(facts: TaskResumeFacts): ResumeProgressSnapshot | null {
  const provenance = facts.provenance
  if (!provenance) return null
  return {
    checkpointId: provenance.checkpointId,
    recordedDate: provenance.date,
    ordinal: provenance.ordinal,
    revision: provenance.revision,
    digest: facts.version,
    done: facts.done,
    next: facts.next,
    blockers: facts.blockers,
  }
}

function missingScope(facts: TaskResumeFacts): string[] {
  const missing = facts.extras.map((extra) => extra.label)
  const provenance = facts.provenance
  if (recordedTaskDispute(provenance, facts.taskId)) {
    missing.push(RECORDED_TASK_IDENTITY_MISSING)
  }
  // No fallback label: `partial` requires extras or a dispute, and both branches
  // above populate `missing`. Inventing one here would name something missing
  // that nothing knows to be missing.
  return [...new Set(missing)]
}

export function toResumeProgressFacts(facts: TaskResumeFacts): ResumeProgressFacts {
  if (facts.status === 'loading') return { status: 'loading' }
  if (facts.status === 'empty') return { status: 'none' }
  if (facts.status === 'error') {
    return { status: 'unavailable', reason: facts.errorMessage ?? UNAVAILABLE_PROGRESS_COPY }
  }
  if (facts.status === 'unreadable') {
    return { status: 'unavailable', reason: facts.unreadableReason ?? UNAVAILABLE_PROGRESS_COPY }
  }
  const snapshot = snapshotFrom(facts)
  if (!snapshot) return { status: 'unavailable', reason: UNAVAILABLE_PROGRESS_COPY }
  if (facts.status === 'partial') {
    return { status: 'partial', snapshot, missing: missingScope(facts) }
  }
  return { status: 'ready', snapshot }
}
