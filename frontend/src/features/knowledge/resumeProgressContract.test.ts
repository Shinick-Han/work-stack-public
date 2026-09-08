import { expect, test } from 'vitest'

import {
  checkpointAttribution,
  checkpointProvenanceLabel,
  progressOmissionAcknowledgement,
  resumeProgressChanged,
  resumeProgressKey,
  resumeProgressSnapshot,
  UNBOUND_RESUME_PROGRESS,
  type ResumeProgressFacts,
  type ResumeProgressSnapshot,
} from './resumeProgressContract'

const snapshot: ResumeProgressSnapshot = {
  checkpointId: 'CP-2026-09-07-1',
  recordedDate: '2026-09-07',
  ordinal: 1,
  revision: 4,
  digest: 'digest-one',
  done: ['Split the adapter out of the panel.'],
  next: ['Wire the brief into the drawer.'],
  blockers: [],
}

const ready: ResumeProgressFacts = { status: 'ready', snapshot }

test('each settled state has its own identity, so none of them reads as another', () => {
  const keys = [
    resumeProgressKey(ready),
    resumeProgressKey({ status: 'none' }),
    resumeProgressKey({ status: 'unavailable', reason: 'The audit could not be read.' }),
    resumeProgressKey({ status: 'partial', snapshot, missing: ['blockers'] }),
    resumeProgressKey({ status: 'loading' }),
  ]
  expect(new Set(keys).size).toBe(keys.length)
  expect(resumeProgressKey(ready)).toBe('record:digest-one')
})

test('a checkpoint-only edit changes the key even when the Task revision does not', () => {
  const edited: ResumeProgressFacts = {
    status: 'ready',
    snapshot: { ...snapshot, digest: 'digest-two', next: ['Ship the installer.'] },
  }
  expect(resumeProgressChanged(resumeProgressKey(ready), edited)).toBe(true)
  expect(resumeProgressChanged(resumeProgressKey(ready), ready)).toBe(false)
})

test('a partial record that loses another field is a different record to freeze', () => {
  const one = resumeProgressKey({ status: 'partial', snapshot, missing: ['blockers'] })
  const two = resumeProgressKey({ status: 'partial', snapshot, missing: ['blockers', 'done'] })
  expect(one).not.toBe(two)
  expect(resumeProgressKey({ status: 'partial', snapshot, missing: ['done', 'blockers'] })).toBe(two)
})

test('a refetch in flight does not invalidate an already prepared brief', () => {
  expect(resumeProgressChanged(resumeProgressKey(ready), { status: 'loading' })).toBe(false)
})

test('an unbound view is unavailable, never an empty or clear record', () => {
  expect(UNBOUND_RESUME_PROGRESS.status).toBe('unavailable')
  expect(resumeProgressSnapshot(UNBOUND_RESUME_PROGRESS)).toBeNull()
  expect(resumeProgressSnapshot({ status: 'none' })).toBeNull()
  expect(resumeProgressSnapshot(ready)).toBe(snapshot)
  expect(resumeProgressChanged(resumeProgressKey(UNBOUND_RESUME_PROGRESS), { status: 'none' })).toBe(true)
})

test('checkpoint attribution repeats the recorded date and invents no time of day', () => {
  expect(checkpointAttribution(snapshot)).toBe('From the latest checkpoint · 2026-09-07')
})

test('a legacy record is named by its recorded slot rather than a fabricated identifier', () => {
  expect(checkpointProvenanceLabel(snapshot)).toBe('Checkpoint CP-2026-09-07-1')
  expect(checkpointProvenanceLabel({ ...snapshot, checkpointId: null }))
    .toBe('Legacy record · 2026-09-07 #1')
  expect(checkpointProvenanceLabel({ ...snapshot, checkpointId: null, ordinal: null }))
    .toBe('Legacy record · 2026-09-07')
})

test('only omitted or partly unreadable progress asks the user to decide', () => {
  expect(progressOmissionAcknowledgement(ready)).toBeNull()
  expect(progressOmissionAcknowledgement({ status: 'none' })).toBeNull()
  expect(progressOmissionAcknowledgement({ status: 'loading' })).toBeNull()
  expect(progressOmissionAcknowledgement(UNBOUND_RESUME_PROGRESS))
    .toBe('Prepare this brief without a recorded progress snapshot.')
  expect(progressOmissionAcknowledgement({ status: 'partial', snapshot, missing: ['blockers'] }))
    .toBe('Include the partially readable checkpoint and state what could not be read.')
})
