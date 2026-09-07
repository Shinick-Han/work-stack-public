import { describe, expect, test } from 'vitest'

import {
  INITIAL_DRAFT_STATE,
  baseFromSource,
  coordinateKey,
  isDirty,
  isRegeneratedUnchanged,
  isStaleSource,
  reportDraftReducer,
  type DailyReportDraftSource,
  type DraftSessionState,
} from './reportDraftEditorModel'
import { REPORT_DRAFT_TEMPLATE } from './reportDraftStorage'

/** Synthetic fixtures only: no authority, network, clock or personal value. */

const DIGEST = 'sha256:' + 'a'.repeat(64)
const OTHER_DIGEST = 'sha256:' + 'b'.repeat(64)
const BASE = {
  sourceDigest: DIGEST,
  baseGeneratedAt: '2026-09-06T09:00:00Z',
  baseMarkdown: '# Generated\n',
}

function source(overrides: Partial<DailyReportDraftSource> = {}): DailyReportDraftSource {
  return {
    generatedAt: '2026-09-06T09:00:00Z',
    markdown: '# Generated\n',
    sourceDigest: DIGEST,
    ...overrides,
  }
}

function loaded(overrides: Partial<DraftSessionState> = {}): DraftSessionState {
  return {
    ...reportDraftReducer(INITIAL_DRAFT_STATE, {
      type: 'loaded',
      base: BASE,
      localRevision: 1,
      markdown: 'saved text',
    }),
    ...overrides,
  }
}

describe('session lifecycle releases the previous operation', () => {
  test('starting a session clears busy, prompts, notices and retained state', () => {
    const stuck = loaded({
      busy: true,
      confirmation: { kind: 'delete', revision: 1 },
      notice: 'old failure',
      retainedUnsaved: true,
      status: 'old status',
    })
    const next = reportDraftReducer(stuck, { type: 'session-start' })
    // Without this the new coordinate opens permanently disabled and uncloseable.
    expect(next.busy).toBe(false)
    expect(next.confirmation).toBeNull()
    expect(next.notice).toBeNull()
    expect(next.retainedUnsaved).toBe(false)
    expect(next.status).toBeNull()
    expect(next.loading).toBe(true)
  })
})

describe('what counts as unsaved', () => {
  test('a save baselines the written snapshot, not what is on screen now', () => {
    const typing = { ...loaded(), text: 'saved text plus more' }
    const after = reportDraftReducer(typing, {
      type: 'saved',
      localRevision: 2,
      markdown: 'saved text',
    })
    // The newer keystrokes are not in the saved copy, so the editor stays dirty.
    expect(after.baseline).toBe('saved text')
    expect(after.text).toBe('saved text plus more')
    expect(isDirty(after)).toBe(true)
  })

  test('a delete marks the retained text unsaved even when it equals the baseline', () => {
    const clean = loaded()
    expect(isDirty(clean)).toBe(false)
    const after = reportDraftReducer(clean, { type: 'deleted' })
    // Text equality alone cannot say "this used to be saved and no longer is".
    expect(after.text).toBe('saved text')
    expect(after.localRevision).toBeNull()
    expect(isDirty(after)).toBe(true)
  })

  test('a failed operation keeps the text and releases busy', () => {
    const busy = reportDraftReducer(loaded(), { type: 'begin' })
    expect(busy.busy).toBe(true)
    const after = reportDraftReducer(busy, { type: 'failed', notice: 'nope' })
    expect(after.busy).toBe(false)
    expect(after.text).toBe('saved text')
    expect(after.notice).toBe('nope')
  })
})

describe('source identity', () => {
  test('a different digest is stale', () => {
    expect(isStaleSource(BASE, source({ sourceDigest: OTHER_DIGEST }))).toBe(true)
  })

  test('the same facts generated later are NOT stale', () => {
    const later = source({ generatedAt: '2026-09-06T18:00:00Z' })
    // Regeneration moves the instant, not the report.
    expect(isStaleSource(BASE, later)).toBe(false)
    expect(isRegeneratedUnchanged(BASE, later)).toBe(true)
  })

  test('an identical source is neither stale nor regenerated', () => {
    expect(isStaleSource(BASE, source())).toBe(false)
    expect(isRegeneratedUnchanged(BASE, source())).toBe(false)
  })

  test('no base means nothing to compare', () => {
    expect(isStaleSource(null, source({ sourceDigest: OTHER_DIGEST }))).toBe(false)
    expect(isRegeneratedUnchanged(null, source())).toBe(false)
  })

  test('a base built from a source mirrors it exactly', () => {
    expect(baseFromSource(source())).toEqual(BASE)
  })
})

describe('coordinate identity', () => {
  test('every field takes part in the key', () => {
    const coordinate = {
      workspaceUid: '11111111-1111-4111-8111-111111111111',
      date: '2026-09-06',
      template: REPORT_DRAFT_TEMPLATE,
    }
    expect(coordinateKey(coordinate)).toBe(
      '11111111-1111-4111-8111-111111111111|2026-09-06|daily-v1',
    )
    expect(coordinateKey({ ...coordinate, date: '2026-09-07' })).not.toBe(
      coordinateKey(coordinate),
    )
  })
})
