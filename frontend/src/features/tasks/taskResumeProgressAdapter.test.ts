import { expect, test } from 'vitest'
import {
  ERROR_PROGRESS_COPY,
  joinResumeField,
  LOADING_PROGRESS_COPY,
  NO_BLOCKERS_COPY,
  NO_NEXT_COPY,
  NO_PROGRESS_COPY,
  previewResumeItems,
  RECORDED_TASK_IDENTITY_MISSING,
  recentProgressStateCopy,
  recordedTaskDispute,
  recordedTaskMismatchCopy,
  toResumeProgressFacts,
  UNAVAILABLE_PROGRESS_COPY,
  unpresentedFieldsCopy,
  UNREADABLE_DONE_COPY,
} from './taskResumeProgressAdapter'
import * as contract from '../knowledge/resumeProgressContract'
import type { TaskResumeFacts } from './taskResumeFacts'

function facts(overrides: Partial<TaskResumeFacts>): TaskResumeFacts {
  return {
    status: 'empty',
    workspaceUid: 'workspace-1',
    taskId: 'T-0001',
    provenance: null,
    done: [],
    next: [],
    blockers: [],
    extras: [],
    unreadableReason: null,
    errorMessage: null,
    activeRecordCount: 0,
    supersededRecordCount: 0,
    version: 'v1:empty:workspace-1:T-0001',
    ...overrides,
  }
}

const readyProvenance = {
  workspaceUid: 'workspace-1',
  taskId: 'T-0001',
  checkpointId: 'CP-1',
  entryDigest: 'digest-1',
  date: '2026-09-07',
  ordinal: 2,
  revision: 0,
  origin: 'agent-cli-v1',
  binding: 'locator' as const,
  recordedTaskId: 'T-0001',
  recordedTaskTitle: 'Gate',
}

test('joinResumeField preserves original bullets and treats empty as null', () => {
  expect(joinResumeField([])).toBeNull()
  expect(joinResumeField(['Keep the Korean 제목', 'Second'])).toBe('Keep the Korean 제목\nSecond')
})

test('previewResumeItems keeps a bounded preview without dropping the full list', () => {
  expect(previewResumeItems(['a', 'b'])).toEqual({ shown: ['a', 'b'], hidden: 0 })
  expect(previewResumeItems(['a', 'b', 'c', 'd', 'e']).hidden).toBe(1)
  expect(previewResumeItems(['a', 'b', 'c', 'd', 'e']).shown).toEqual(['a', 'b', 'c', 'd'])
})

test('adapter maps each UI-B status onto the UI-D progress contract without blending records', () => {
  expect(toResumeProgressFacts(facts({ status: 'loading' }))).toEqual({ status: 'loading' })
  expect(toResumeProgressFacts(facts({ status: 'empty' }))).toEqual({ status: 'none' })
  expect(toResumeProgressFacts(facts({ status: 'error', errorMessage: 'audit failed' }))).toEqual({
    status: 'unavailable',
    reason: 'audit failed',
  })
  expect(toResumeProgressFacts(facts({ status: 'unreadable', unreadableReason: 'No readable summary for this entry' }))).toEqual({
    status: 'unavailable',
    reason: 'No readable summary for this entry',
  })
  expect(toResumeProgressFacts(facts({ status: 'ready' }))).toEqual({
    status: 'unavailable',
    reason: UNAVAILABLE_PROGRESS_COPY,
  })
})

test('ready snapshots keep arrays lossless and key digest on facts.version', () => {
  const version = 'v1:ready:workspace-1:T-0001:CP-1:digest-1:r0'
  const ready = toResumeProgressFacts(facts({
    status: 'ready',
    provenance: readyProvenance,
    done: ['Shipped the drawer shell', 'Kept the Korean 제목'],
    next: ['Wire Review'],
    blockers: [],
    version,
  }))
  expect(ready).toEqual({
    status: 'ready',
    snapshot: {
      checkpointId: 'CP-1',
      recordedDate: '2026-09-07',
      ordinal: 2,
      revision: 0,
      digest: version,
      done: ['Shipped the drawer shell', 'Kept the Korean 제목'],
      next: ['Wire Review'],
      blockers: [],
    },
  })
})

test('a legacy row keeps a nullable checkpointId and does not invent an identifier', () => {
  const version = 'v1:partial:workspace-1:T-0001:legacy@2026-09-07#2:no-digest:r0'
  const partial = toResumeProgressFacts(facts({
    status: 'partial',
    provenance: {
      ...readyProvenance,
      checkpointId: null,
      entryDigest: null,
      binding: 'entry-payload',
      recordedTaskId: 'T-0099',
    },
    extras: [{ label: 'unknown_field', value: 'x' }],
    next: ['Keep going'],
    version,
  }))
  expect(partial.status).toBe('partial')
  if (partial.status !== 'partial') return
  expect(partial.snapshot.checkpointId).toBeNull()
  expect(partial.snapshot.digest).toBe(version)
  expect(partial.snapshot.next).toEqual(['Keep going'])
  expect(partial.missing).toEqual(['unknown_field', RECORDED_TASK_IDENTITY_MISSING])
})

test('a disputed Task identity is named even when extras are empty', () => {
  const partial = toResumeProgressFacts(facts({
    status: 'partial',
    provenance: { ...readyProvenance, recordedTaskId: 'T-0041' },
    extras: [],
    next: ['Keep the original next step'],
    version: 'v1:partial:workspace-1:T-0001:CP-1:digest-1:r0',
  }))
  expect(partial.status).toBe('partial')
  if (partial.status !== 'partial') return
  expect(partial.missing).toEqual([RECORDED_TASK_IDENTITY_MISSING])
  expect(partial.snapshot.next).toEqual(['Keep the original next step'])
})

test('adapter shares the knowledge contract copy instead of redeclaring it', () => {
  expect(NO_PROGRESS_COPY).toBe(contract.NO_PROGRESS_COPY)
  expect(NO_NEXT_COPY).toBe(contract.NO_NEXT_COPY)
  expect(NO_BLOCKERS_COPY).toBe(contract.NO_BLOCKERS_COPY)
  expect(contract.UNBOUND_RESUME_PROGRESS.status).toBe('unavailable')
  expect(UNAVAILABLE_PROGRESS_COPY).not.toBe('')
  expect(contract.resumeProgressKey(contract.UNBOUND_RESUME_PROGRESS))
    .toBe(`unavailable:${UNAVAILABLE_PROGRESS_COPY}`)
})

test('missing scope names only what is known to be missing', () => {
  const agreeing = toResumeProgressFacts(facts({
    status: 'partial',
    provenance: { ...readyProvenance, recordedTaskId: 'T-0001' },
    extras: [{ label: 'hand_off', value: '{}' }],
    version: 'v1:partial:workspace-1:T-0001:CP-1:digest-1:r0',
  }))
  expect(agreeing.status).toBe('partial')
  if (agreeing.status !== 'partial') return
  expect(agreeing.missing).toEqual(['hand_off'])
  expect(agreeing.missing).not.toContain(RECORDED_TASK_IDENTITY_MISSING)

  const silent = toResumeProgressFacts(facts({
    status: 'partial',
    provenance: { ...readyProvenance, recordedTaskId: null, recordedTaskTitle: null },
    extras: [],
    version: 'v1:partial:workspace-1:T-0001:CP-1:digest-1:r0',
  }))
  expect(silent.status).toBe('partial')
  if (silent.status !== 'partial') return
  // A record that never named a Task disputes nothing, so nothing is claimed.
  expect(silent.missing).toEqual([])
})

test('a dispute is detected from stored values and named with both sides', () => {
  expect(recordedTaskDispute(null, 'T-0001')).toBeNull()
  expect(recordedTaskDispute({ ...readyProvenance, recordedTaskId: null }, 'T-0001')).toBeNull()
  expect(recordedTaskDispute(readyProvenance, 'T-0001')).toBeNull()
  const disputed = recordedTaskDispute({ ...readyProvenance, recordedTaskId: 'T-9999' }, 'T-0001')
  expect(disputed).not.toBeNull()
  if (!disputed) return
  expect(recordedTaskMismatchCopy(disputed, 'T-0001'))
    .toBe('The recorded entry names Task T-9999 · Gate, not T-0001.')
  expect(recordedTaskMismatchCopy({ ...disputed, recordedTaskTitle: null }, 'T-0001'))
    .toBe('The recorded entry names Task T-9999, not T-0001.')
})

test('unpresented fields stay discoverable by their own labels', () => {
  expect(unpresentedFieldsCopy(['hand_off', 'next[2]']))
    .toBe('Kept under Record details and history, not shown above: hand_off, next[2].')
})

test('every unsettled recent-progress state states itself', () => {
  expect(recentProgressStateCopy('loading')).toBe(LOADING_PROGRESS_COPY)
  expect(recentProgressStateCopy('empty')).toBe(NO_PROGRESS_COPY)
  expect(recentProgressStateCopy('error')).toBe(ERROR_PROGRESS_COPY)
  expect(recentProgressStateCopy('unreadable')).toBe(UNREADABLE_DONE_COPY)
})
