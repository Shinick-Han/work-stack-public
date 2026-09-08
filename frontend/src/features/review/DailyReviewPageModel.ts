import type { Task } from '../../domain/types'

/** Product-owned Review copy. User-authored Task titles stay untouched. */
export const REVIEW_COPY = {
  heading: 'Daily Review',
  lead: 'Capture what moved, what comes next, and what needs help—without inferring execution state.',
  selectTask: 'Select a task',
  missingTarget: 'Select a task before recording progress. Review does not choose a Task automatically.',
  invalidTarget: 'That task is not in this workspace. Select a task before recording progress.',
  changed: 'What changed?',
  next: 'What comes next?',
  blockers: 'Add blockers',
  fieldHint: 'one item per line',
  save: 'Save progress',
  saving: 'Saving…',
  retryUnchanged: 'Retry unchanged entry',
  retrySame: 'Retry the same request',
  recent: 'Recent progress for this task',
  details: 'Record details and history',
  guardTitle: 'Keep unsaved progress?',
  guardBody: 'Changing the date or task would attach this draft to a different target. Keep the current draft, or discard it and continue.',
  keepDraft: 'Keep draft',
  discard: 'Discard draft',
  discardDraft: 'Discard draft and continue',
  frozenGuardTitle: 'This save is unresolved',
  frozenGuardBody: 'The progress write may or may not have committed. Retry the same request. The Task and date cannot change until this request is resolved.',
  strandedGuardBody: 'This progress write belongs to a previous workspace and cannot be retried here. Acknowledge and release it in the progress form to continue.',
  keepFrozen: 'Keep this request',
  saved: 'Daily review entry added',
  strandedHeading: 'Unresolved progress write from a previous workspace',
  strandedExplanation: 'The result of this request never arrived, and the workspace it was sent for has since been replaced. It may or may not have been recorded there. It cannot be retried here, because a retry would send it to the workspace now open.',
  strandedPayload: 'The request that is still unresolved',
  retryUnavailable: 'Retry unavailable in this workspace',
  releaseStranded: 'Acknowledge and release',
  releaseStrandedNote: 'Releasing only stops this page from holding the request. It does not cancel the request and does not mean the write failed. Confirm the outcome in the workspace it was sent for.',
} as const

export type DraftGuardKind = 'date' | 'task'

export interface DraftGuard {
  kind: DraftGuardKind
  value: string
}

export interface FrozenEntryAttempt {
  owner: string
  workspaceId: string
  input: {
    date: string
    task_id: string
    done: string[]
    next: string[]
    blockers: string[]
  }
  key: string
}

export function splitItems(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

export function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  if (hours && minutes) return `${hours}h ${minutes}m`
  if (hours) return `${hours}h`
  if (minutes) return `${minutes}m`
  return `${totalSeconds}s`
}

export function localTime(): string {
  const now = new Date()
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
}

export function draftHasContent(done: string, next: string, blockers: string): boolean {
  return [done, next, blockers].some((value) => value.trim())
}

export function boundTargetLabel(taskId: string): string {
  return `Recording progress for ${taskId}`
}

export function sortReviewTasks(tasks: readonly Task[]): Task[] {
  return [...tasks].sort((left, right) => (
    left.status === 'done' || left.status === 'dropped' ? 1 : -1
  ) - (right.status === 'done' || right.status === 'dropped' ? 1 : -1) || left.id.localeCompare(right.id))
}

export function resolveInitialTaskId(
  initialTaskId: string | null | undefined,
  tasks: readonly Task[],
): { taskId: string; invalid: boolean } {
  if (initialTaskId == null || initialTaskId === '') {
    return { taskId: '', invalid: false }
  }
  if (tasks.some((task) => task.id === initialTaskId)) {
    return { taskId: initialTaskId, invalid: false }
  }
  return { taskId: '', invalid: true }
}

export function saveButtonLabel(
  pending: boolean,
  unknownRetry: boolean,
  failed: boolean,
  stranded = false,
): string {
  if (pending) return REVIEW_COPY.saving
  // A stranded attempt is never offered as a retry: the only remaining action
  // is an explicit acknowledge-and-release, so the label must not promise one.
  if (stranded) return REVIEW_COPY.retryUnavailable
  if (unknownRetry) return REVIEW_COPY.retrySame
  if (failed) return REVIEW_COPY.retryUnchanged
  return REVIEW_COPY.save
}

/**
 * True once a frozen commit_unknown attempt outlives the owner it was minted
 * for — in practice a workspace replacement. The attempt stays inspectable and
 * keeps its own identity; it is never re-POSTed to the new owner.
 */
export function frozenAttemptStranded(
  attempt: FrozenEntryAttempt | null,
  owner: string,
): boolean {
  return attempt !== null && attempt.owner !== owner
}

/** Old Task, date and workspace of a stranded attempt, stated verbatim. */
export function strandedIdentityLabel(
  taskId: string,
  date: string,
  workspaceId: string,
): string {
  return `${taskId} · ${date} · workspace ${workspaceId}`
}

export function buildReviewEntryInput(
  date: string,
  taskId: string,
  done: string,
  next: string,
  blockers: string,
): FrozenEntryAttempt['input'] | null {
  const input = {
    date,
    task_id: taskId,
    done: splitItems(done),
    next: splitItems(next),
    blockers: splitItems(blockers),
  }
  if (!input.done.length && !input.next.length && !input.blockers.length) return null
  return input
}

export function canDiscardOrdinaryDraft(dirty: boolean, frozen: boolean, pending: boolean): boolean {
  return dirty && !frozen && !pending
}

/**
 * The two safe ways out of a held draft. Both drop the ordinary refusal along
 * with the draft it named; neither ever resubmits, and neither touches a frozen
 * attempt that is still retryable by the owner it was minted for.
 */
export function draftResolutionActions({
  clearDraft,
  frozen,
  pending,
  reset,
  resetText,
  stranded,
}: {
  clearDraft: () => void
  frozen: boolean
  pending: boolean
  reset: () => void
  resetText: () => void
  stranded: boolean
}) {
  const discardText = () => { resetText(); reset() }
  return {
    discardText,
    discardOrdinaryDraft: () => {
      if (frozen || pending) return
      discardText()
    },
    // Acknowledge-and-release asserts nothing about the previous write: it only
    // stops this page from holding it, which releases the navigation lock.
    releaseStrandedAttempt: () => {
      if (!stranded || pending) return
      clearDraft()
      reset()
    },
  }
}

export function progressNavigationLocked(dirty: boolean, pending: boolean, frozen: boolean): boolean {
  return dirty || pending || frozen
}
