import { expect, test } from 'vitest'
import { ApiError } from '../../api/transport'
import {
  KnowledgeObservationError,
  OBSERVATION_HISTORY_UNSUPPORTED,
  type SavedObservation,
} from '../../api/knowledgeObservation'
import {
  applyHistoryLoadFailure,
  applyHistoryLoadStart,
  applyHistoryLoadSuccess,
  applyHistoryRecordFailure,
  applyHistoryRecordStart,
  applyHistoryRecordSuccess,
  idleHistoryView,
  toSavedCheck,
} from './sourceCheckHistoryState'

const UNCHANGED: SavedObservation = {
  accepted_at: '2026-09-08T10:15:04Z',
  checked_at: '2026-09-08T10:15:02Z',
  binding_state: 'unchanged',
  result: {
    schema: 'workstack.knowledge-verification.v1',
    verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    checked_at: '2026-09-08T10:15:02Z',
    evidence: [{
      document_ref: 'od-page-7f3ba1d34f50c884600112ab',
      source_type: 'notion.page',
      expected_source_version: 'od-version-14',
      observed_source_version: 'od-version-14',
      status: 'current',
      code: 'hash_matched',
    }],
  },
}

const CHANGED: SavedObservation = {
  accepted_at: '2026-09-08T10:15:04Z',
  checked_at: '2026-09-08T10:15:02Z',
  binding_state: 'changed',
  result: null,
}

test('a changed observation keeps the time and drops the evidence overlay', () => {
  expect(toSavedCheck(null)).toBeNull()
  expect(toSavedCheck(UNCHANGED)).toEqual({
    checkedAt: '2026-09-08T10:15:02Z',
    bindingState: 'unchanged',
    entries: UNCHANGED.result?.evidence,
  })
  expect(toSavedCheck(CHANGED)).toEqual({
    checkedAt: '2026-09-08T10:15:02Z',
    bindingState: 'changed',
    entries: null,
  })
})

test('a history read selects legacy only for the unsupported owner, never for other failures', () => {
  const idle = idleHistoryView('ws|C-0001|0', true)
  expect(idle.mode).toBe('loading')
  expect(idleHistoryView('ws|C-0001|0', false).mode).toBe('disabled')

  const loading = applyHistoryLoadStart(idle)
  expect(loading.loading).toBe(true)
  expect(loading.error).toBeNull()

  const ready = applyHistoryLoadSuccess(loading, UNCHANGED)
  expect(ready.mode).toBe('supported')
  expect(ready.loading).toBe(false)
  expect(ready.saved?.entries).toHaveLength(1)
  expect(ready.needsReload).toBe(false)

  const empty = applyHistoryLoadSuccess(loading, null)
  expect(empty.mode).toBe('supported')
  expect(empty.saved).toBeNull()

  const legacy = applyHistoryLoadFailure(loading, new KnowledgeObservationError(OBSERVATION_HISTORY_UNSUPPORTED))
  expect(legacy.mode).toBe('legacy')
  expect(legacy.error).toBeNull()
  expect(legacy.saved).toBeNull()

  const unavailable = applyHistoryLoadFailure(loading, new TypeError('Failed to fetch'))
  expect(unavailable.mode).toBe('unavailable')
  expect(unavailable.error).toBe('Saved source checks could not be loaded from this server.')
  expect(unavailable.saved).toBeNull()
})

test('a failed record clears the rows and waits for an explicit reload', () => {
  const supported = applyHistoryLoadSuccess(idleHistoryView('ws|C-0001|0', true), UNCHANGED)
  const pending = applyHistoryRecordStart(supported)
  expect(pending.pending).toBe(true)
  expect(pending.saved).toBeNull()
  expect(pending.error).toBeNull()

  const failed = applyHistoryRecordFailure(
    pending,
    new ApiError(503, 'observation_save_unknown', 'OSError 28 on /srv/store.db'),
  )
  expect(failed.pending).toBe(false)
  expect(failed.saved).toBeNull()
  expect(failed.needsReload).toBe(true)
  expect(failed.error).toContain('could not be confirmed')
  expect(failed.error).not.toContain('OSError')

  const recovered = applyHistoryLoadSuccess(applyHistoryLoadStart(failed), UNCHANGED)
  expect(recovered.needsReload).toBe(false)
  expect(recovered.error).toBeNull()
  expect(recovered.saved?.entries).toHaveLength(1)

  const recorded = applyHistoryRecordSuccess(applyHistoryRecordStart(recovered), UNCHANGED)
  expect(recorded.pending).toBe(false)
  expect(recorded.needsReload).toBe(false)
  expect(recorded.saved?.bindingState).toBe('unchanged')
})
