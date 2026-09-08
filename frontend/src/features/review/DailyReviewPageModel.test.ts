import { expect, test } from 'vitest'
import { task } from '../../test/fixtures'
import {
  REVIEW_COPY,
  boundTargetLabel,
  buildReviewEntryInput,
  canDiscardOrdinaryDraft,
  draftHasContent,
  formatDuration,
  frozenAttemptStranded,
  progressNavigationLocked,
  resolveInitialTaskId,
  saveButtonLabel,
  sortReviewTasks,
  strandedIdentityLabel,
  type FrozenEntryAttempt,
} from './DailyReviewPageModel'

const other = {
  ...task,
  id: 'T-0033',
  uid: '33333333-3333-4333-8333-333333333333',
  title: 'Host-side search worker isolation',
}

test('resolveInitialTaskId never silently picks the first Task', () => {
  expect(resolveInitialTaskId(undefined, [task, other])).toEqual({ taskId: '', invalid: false })
  expect(resolveInitialTaskId(null, [task, other])).toEqual({ taskId: '', invalid: false })
  expect(resolveInitialTaskId('', [task, other])).toEqual({ taskId: '', invalid: false })
})

test('resolveInitialTaskId binds a workspace Task and flags an unknown id', () => {
  expect(resolveInitialTaskId('T-0033', [task, other])).toEqual({ taskId: 'T-0033', invalid: false })
  expect(resolveInitialTaskId('T-9999', [task, other])).toEqual({ taskId: '', invalid: true })
})

test('sortReviewTasks keeps done and dropped after open work without inventing an id order default', () => {
  const done = { ...task, id: 'T-0000', uid: '00000000-0000-4000-8000-000000000000', status: 'done' as const }
  expect(sortReviewTasks([done, other, task]).map((item) => item.id)).toEqual(['T-0001', 'T-0033', 'T-0000'])
})

test('draft and save labels stay explicit', () => {
  expect(draftHasContent('', '  ', '')).toBe(false)
  expect(draftHasContent('shipped', '', '')).toBe(true)
  expect(boundTargetLabel('T-0033')).toBe('Recording progress for T-0033')
  expect(saveButtonLabel(true, false, true)).toBe(REVIEW_COPY.saving)
  expect(saveButtonLabel(false, true, true)).toBe(REVIEW_COPY.retrySame)
  expect(saveButtonLabel(false, false, true)).toBe(REVIEW_COPY.retryUnchanged)
  expect(saveButtonLabel(false, false, false)).toBe(REVIEW_COPY.save)
  expect(canDiscardOrdinaryDraft(true, false, false)).toBe(true)
  expect(canDiscardOrdinaryDraft(true, true, false)).toBe(false)
  expect(canDiscardOrdinaryDraft(true, false, true)).toBe(false)
  expect(progressNavigationLocked(false, false, false)).toBe(false)
  expect(progressNavigationLocked(true, false, false)).toBe(true)
  expect(progressNavigationLocked(false, true, false)).toBe(true)
  expect(progressNavigationLocked(false, false, true)).toBe(true)
})

test('a frozen attempt strands on its own owner and keeps its own identity', () => {
  const attempt: FrozenEntryAttempt = {
    owner: 'ws-a|2026-08-30|T-0001#0',
    workspaceId: 'ws-a',
    input: { date: '2026-08-30', task_id: 'T-0001', done: ['shipped'], next: [], blockers: [] },
    key: 'workstack:entry-1',
  }
  expect(frozenAttemptStranded(null, 'ws-a|2026-08-30|T-0001#0')).toBe(false)
  expect(frozenAttemptStranded(attempt, 'ws-a|2026-08-30|T-0001#0')).toBe(false)
  expect(frozenAttemptStranded(attempt, 'ws-b|2026-08-30|T-0001#1')).toBe(true)
  expect(strandedIdentityLabel(attempt.input.task_id, attempt.input.date, attempt.workspaceId))
    .toBe('T-0001 · 2026-08-30 · workspace ws-a')
  expect(saveButtonLabel(false, true, true, true)).toBe(REVIEW_COPY.retryUnavailable)
  expect(saveButtonLabel(true, true, true, true)).toBe(REVIEW_COPY.saving)
  expect(saveButtonLabel(false, true, true, false)).toBe(REVIEW_COPY.retrySame)
  // Releasing a stranded attempt must not read as a failure or a cancellation.
  expect(REVIEW_COPY.releaseStrandedNote).toContain('does not cancel')
  expect(REVIEW_COPY.releaseStrandedNote).toContain('does not mean the write failed')
})

test('buildReviewEntryInput splits lines and refuses an empty write', () => {
  expect(buildReviewEntryInput('2026-08-30', 'T-0001', '', '', '')).toBeNull()
  expect(buildReviewEntryInput('2026-08-30', 'T-0001', 'one\n\ntwo', 'next', '')).toEqual({
    date: '2026-08-30',
    task_id: 'T-0001',
    done: ['one', 'two'],
    next: ['next'],
    blockers: [],
  })
  expect(formatDuration(3900)).toBe('1h 5m')
})
