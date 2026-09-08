import { describe, expect, test } from 'vitest'
import {
  MAX_REPORT_MARKDOWN_CHARS,
  MAX_REPORT_NOTE_CHARS,
} from '../../domain/reportDocuments'
import {
  VALIDATION_MARKDOWN_LIMIT,
  VALIDATION_NOTE_LIMIT,
  canRetry,
  canSave,
  codePointCount,
  initialRevisionState,
  isDirty,
  isStaleOwner,
  noteForRequest,
  pinFromDocument,
  reportRevisionReducer,
  validateRevisionFields,
  workspaceAdmitted,
  type RevisionPin,
} from './reportRevisionModel'

const WORKSPACE = '0f50a123-3da8-4c82-8f16-8ee1a57260c4'
const REPORT_UID = '00000001-abcd-4000-8000-000000000000'
const OTHER = '00000002-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const MARKDOWN = '# body'

function document(state: 'draft' | 'finalized' | 'archived' = 'draft') {
  return {
    uid: REPORT_UID,
    template: 'daily-v1' as const,
    period: { kind: 'day' as const, date: '2026-09-06' },
    state,
    revision: 4,
    content_revision: 2,
    source_digest: DIGEST,
    archived_from_state: state === 'archived' ? 'finalized' as const : null,
    created_at: NOW,
    updated_at: NOW,
    source_stale: true,
    revisions: [
      {
        content_revision: 1,
        document_revision: 1,
        markdown: 'first',
        authored_at: NOW,
        note: null,
      },
      {
        content_revision: 2,
        document_revision: 4,
        markdown: MARKDOWN,
        authored_at: NOW,
        note: 'kept',
      },
    ],
  }
}

function pin(overrides: Partial<RevisionPin> = {}): RevisionPin {
  return {
    ...pinFromDocument(document(), WORKSPACE),
    ...overrides,
  }
}

function loaded(text = MARKDOWN) {
  return initialRevisionState(pin())
}

describe('character bounds follow Python code points, not UTF-16 units', () => {
  test('emoji at the markdown cap is accepted and one more is not', () => {
    const emoji = '😀'
    expect(emoji.length).toBe(2)
    expect(codePointCount(emoji)).toBe(1)
    const exact = emoji.repeat(MAX_REPORT_MARKDOWN_CHARS)
    expect(validateRevisionFields(exact, '')).toBeNull()
    expect(validateRevisionFields(exact + 'x', '')).toBe(VALIDATION_MARKDOWN_LIMIT)
  })

  test('a note at 240 code points is accepted and one more is not', () => {
    const exact = '한'.repeat(MAX_REPORT_NOTE_CHARS)
    expect(exact.length).toBe(MAX_REPORT_NOTE_CHARS)
    expect(validateRevisionFields('ok', exact)).toBeNull()
    expect(validateRevisionFields('ok', exact + '한')).toBe(VALIDATION_NOTE_LIMIT)
  })

  test('blank notes become null before the write', () => {
    expect(noteForRequest('')).toBeNull()
    expect(noteForRequest('   ')).toBeNull()
    expect(noteForRequest('  keep  ')).toBe('  keep  ')
  })
})

describe('revision reducer', () => {
  test('a successful save baselines the written snapshot and keeps newer text', () => {
    const typing = reportRevisionReducer(loaded(), { type: 'edit-text', text: 'first extra' })
    const started = reportRevisionReducer(typing, {
      type: 'save-start',
      frozen: {
        key: 'workstack:key-1',
        workspaceUid: WORKSPACE,
        reportUid: REPORT_UID,
        expectedRevision: 4,
        markdown: 'first extra',
        note: null,
      },
    })
    const during = reportRevisionReducer(started, { type: 'edit-text', text: 'first extra more' })
    const saved = reportRevisionReducer(during, {
      type: 'save-done',
      revision: 5,
      markdown: 'first extra',
      note: null,
      reopened: true,
      sourceStale: true,
      contentRevision: 3,
    })
    expect(saved.text).toBe('first extra more')
    expect(saved.markdownBaseline).toBe('first extra')
    expect(isDirty(saved)).toBe(true)
    expect(saved.pin.expectedRevision).toBe(5)
    expect(saved.pin.openedState).toBe('draft')
    expect(saved.status).toMatch(/finalized report is now a draft/)
  })

  test('commit-unknown keeps the frozen payload and refuses a new save', () => {
    const frozen = {
      key: 'workstack:key-1',
      workspaceUid: WORKSPACE,
      reportUid: REPORT_UID,
      expectedRevision: 4,
      markdown: 'held',
      note: null,
    }
    const started = reportRevisionReducer(
      reportRevisionReducer(loaded(), { type: 'edit-text', text: 'held' }),
      { type: 'save-start', frozen },
    )
    const unknown = reportRevisionReducer(started, { type: 'save-unknown', message: 'maybe' })
    expect(unknown.frozen).toEqual(frozen)
    expect(unknown.unknown).toBe(true)
    expect(canSave(unknown, false, true)).toBe(false)
    expect(canRetry(unknown, false, true)).toBe(true)
    const typed = reportRevisionReducer(unknown, { type: 'edit-text', text: 'newer' })
    expect(typed.frozen?.markdown).toBe('held')
    expect(typed.text).toBe('newer')
    expect(canSave(typed, false, true)).toBe(false)
  })

  test('a CAS refusal drops the frozen key and keeps the buffer', () => {
    const started = reportRevisionReducer(
      reportRevisionReducer(loaded(), { type: 'edit-text', text: 'mine' }),
      {
        type: 'save-start',
        frozen: {
          key: 'workstack:key-1',
          workspaceUid: WORKSPACE,
          reportUid: REPORT_UID,
          expectedRevision: 4,
          markdown: 'mine',
          note: null,
        },
      },
    )
    const refused = reportRevisionReducer(started, { type: 'save-refused', message: 'conflict' })
    expect(refused.text).toBe('mine')
    expect(refused.frozen).toBeNull()
    expect(refused.unknown).toBe(false)
    expect(refused.pin.expectedRevision).toBe(4)
  })
})

describe('ownership and admission', () => {
  test('a different selected report is a stale owner and cannot save or export', () => {
    expect(isStaleOwner(pin(), WORKSPACE, OTHER)).toBe(true)
    expect(isStaleOwner(pin(), WORKSPACE, REPORT_UID)).toBe(false)
    expect(isStaleOwner(pin(), WORKSPACE, null)).toBe(true)
    const dirty = reportRevisionReducer(loaded(), { type: 'edit-text', text: 'edited' })
    expect(canSave(dirty, true, true)).toBe(false)
  })

  test('archived reports and unadmitted workspaces cannot save', () => {
    const archived = initialRevisionState(pinFromDocument(document('archived'), WORKSPACE))
    const edited = reportRevisionReducer(archived, { type: 'edit-text', text: 'nope' })
    expect(canSave(edited, false, true)).toBe(false)
    expect(workspaceAdmitted('22222222-2222-2222-2222-222222222222')).toBe(false)
    expect(workspaceAdmitted(WORKSPACE)).toBe(true)
  })
})
