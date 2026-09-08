import { describe, expect, test } from 'vitest'
import {
  allowedTransitions,
  applyTransitionToRead,
  canLoadMore,
  INITIAL_SAVED_REPORTS_STATE,
  matchesListFilter,
  savedReportsReducer,
} from './savedReportsModel'
import type { ReportListItem, ReportReadData } from '../../domain/reportDocuments'

const UID = '00000001-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const LATER = '2026-09-06T13:00:00Z'
const CURSOR = `A${'b'.repeat(63)}`

function item(overrides: Partial<ReportListItem> = {}): ReportListItem {
  return {
    uid: UID,
    template: 'daily-v1',
    period: { kind: 'day', date: '2026-09-06' },
    state: 'draft',
    revision: 1,
    content_revision: 2,
    source_digest: DIGEST,
    archived_from_state: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

function document(overrides: Partial<ReportReadData> = {}): ReportReadData {
  return {
    ...item(),
    revisions: [{
      content_revision: 2,
      document_revision: 1,
      markdown: '# kept',
      authored_at: NOW,
      note: null,
    }],
    source_stale: false,
    ...overrides,
  }
}

describe('saved report list filters', () => {
  test('active is draft or finalized, never a stored state named active', () => {
    expect(matchesListFilter('draft', 'active')).toBe(true)
    expect(matchesListFilter('finalized', 'active')).toBe(true)
    expect(matchesListFilter('archived', 'active')).toBe(false)
    expect(matchesListFilter('archived', 'archived')).toBe(true)
    expect(matchesListFilter('draft', 'all')).toBe(true)
  })

  test('changing filter drops the cursor and accumulated page', () => {
    const loaded = savedReportsReducer(INITIAL_SAVED_REPORTS_STATE, {
      type: 'list-page',
      more: false,
      page: { workspace_uid: '0f50a123-3da8-4c82-8f16-8ee1a57260c4', reports: [item()], omitted_count: 12, cursor: CURSOR },
    })
    expect(canLoadMore(loaded)).toBe(true)
    const filtered = savedReportsReducer(loaded, { type: 'filter', filter: 'archived' })
    expect(filtered.items).toEqual([])
    expect(filtered.cursor).toBeNull()
    expect(filtered.omittedCount).toBe(0)
    expect(canLoadMore(filtered)).toBe(false)
  })
})

describe('saved report transitions', () => {
  test('draft can finalize or archive; archived only restores; never delete', () => {
    expect(allowedTransitions('draft')).toEqual(['finalize', 'archive'])
    expect(allowedTransitions('finalized')).toEqual(['archive'])
    expect(allowedTransitions('archived')).toEqual(['restore'])
  })

  test('a confirmed finalize updates status and keeps the authored body', () => {
    const selected = savedReportsReducer(INITIAL_SAVED_REPORTS_STATE, {
      type: 'read-done',
      document: document(),
    })
    const listed = savedReportsReducer(selected, {
      type: 'list-page',
      more: false,
      page: {
        workspace_uid: '0f50a123-3da8-4c82-8f16-8ee1a57260c4',
        reports: [item()],
        omitted_count: 0,
        cursor: null,
      },
    })
    const done = savedReportsReducer(listed, {
      type: 'transition-done',
      data: {
        uid: UID,
        workspace_uid: '0f50a123-3da8-4c82-8f16-8ee1a57260c4',
        template: 'daily-v1',
        period: { kind: 'day', date: '2026-09-06' },
        source_digest: DIGEST,
        source_generated_at: NOW,
        state: 'finalized',
        revision: 2,
        archived_from_state: null,
        archived_at: null,
        archive_note: null,
        created_at: NOW,
        updated_at: LATER,
        source_stale: true,
      },
    })
    expect(done.document?.state).toBe('finalized')
    expect(done.document?.revision).toBe(2)
    expect(done.document?.content_revision).toBe(2)
    expect(done.document?.revisions[0]?.markdown).toBe('# kept')
    expect(done.document?.source_stale).toBe(true)
    expect(done.items[0]?.state).toBe('finalized')
    expect(done.frozen).toBeNull()
  })

  test('an ambiguous result keeps the last confirmed draft and the frozen key', () => {
    const pending = savedReportsReducer({
      ...INITIAL_SAVED_REPORTS_STATE,
      document: document(),
      frozen: { kind: 'finalize', reportUid: UID, expectedRevision: 1, key: 'workstack:same-key' },
      transitionPending: true,
    }, { type: 'transition-unsettled', message: 'maybe' })
    expect(pending.document?.state).toBe('draft')
    expect(pending.frozen?.key).toBe('workstack:same-key')
    expect(pending.needsReconcile).toBe(true)
    expect(pending.transitionPending).toBe(false)
  })

  test('a revision conflict keeps the read body and does not reuse the spent key', () => {
    const refused = savedReportsReducer({
      ...INITIAL_SAVED_REPORTS_STATE,
      document: document(),
      frozen: { kind: 'finalize', reportUid: UID, expectedRevision: 1, key: 'workstack:spent' },
      transitionPending: true,
    }, { type: 'transition-refused', message: 'refresh', needsReconcile: true })
    expect(refused.document?.state).toBe('draft')
    expect(refused.document?.revisions[0]?.markdown).toBe('# kept')
    expect(refused.frozen).toBeNull()
    expect(refused.needsReconcile).toBe(true)
  })
})

test('applyTransitionToRead never invents markdown from a status write', () => {
  const next = applyTransitionToRead(document(), {
    uid: UID,
    workspace_uid: '0f50a123-3da8-4c82-8f16-8ee1a57260c4',
    template: 'daily-v1',
    period: { kind: 'day', date: '2026-09-06' },
    source_digest: DIGEST,
    source_generated_at: NOW,
    state: 'archived',
    revision: 3,
    archived_from_state: 'draft',
    archived_at: LATER,
    archive_note: null,
    created_at: NOW,
    updated_at: LATER,
  })
  expect(next.revisions[0]?.markdown).toBe('# kept')
  expect(next.state).toBe('archived')
})
