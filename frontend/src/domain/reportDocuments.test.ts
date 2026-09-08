import { describe, expect, test } from 'vitest'
import {
  MAX_REPORT_MARKDOWN_CHARS,
  REPORT_LIST_FILTERS,
  REPORT_LIST_PAGE_SIZE,
  REPORT_STORED_STATES,
  reportArchiveDataSchema,
  reportCreateDataSchema,
  reportFinalizeDataSchema,
  reportListDataSchema,
  reportListFilterSchema,
  reportReadDataSchema,
  reportRestoreDataSchema,
  reportReviseDataSchema,
  reportStoredStateSchema,
} from './reportDocuments'

const WORKSPACE = '0f50a123-3da8-4c82-8f16-8ee1a57260c4'
const OTHER_WORKSPACE = '1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d'
const REPORT_UID = '00000001-abcd-4000-8000-000000000000'
const DIGEST = `sha256:${'0'.repeat(64)}`
const NOW = '2026-09-06T12:00:00Z'
const LATER = '2026-09-06T13:00:00Z'
const GENERATED = '2026-09-06T11:59:00Z'
const MARKDOWN = '# body <script>alert(1)</script> | a | b |'
const CURSOR = `A${'b'.repeat(63)}`

function period() {
  return { kind: 'day' as const, date: '2026-09-06' }
}

function content(overrides: Record<string, unknown> = {}) {
  return {
    content_revision: 1,
    document_revision: 1,
    markdown: MARKDOWN,
    authored_at: NOW,
    note: null,
    ...overrides,
  }
}

function listItem(overrides: Record<string, unknown> = {}) {
  return {
    uid: REPORT_UID,
    template: 'daily-v1',
    period: period(),
    state: 'draft',
    revision: 1,
    content_revision: 1,
    source_digest: DIGEST,
    archived_from_state: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

type CreateExpectation = Parameters<typeof reportCreateDataSchema>[0]

function createExpectation(overrides: Partial<CreateExpectation> = {}): CreateExpectation {
  return {
    workspace_uid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    source_digest: DIGEST,
    source_generated_at: GENERATED,
    markdown: MARKDOWN,
    ...overrides,
  }
}

function summary(overrides: Record<string, unknown> = {}) {
  return {
    uid: REPORT_UID,
    workspace_uid: WORKSPACE,
    template: 'daily-v1',
    period: period(),
    source_digest: DIGEST,
    source_generated_at: GENERATED,
    state: 'draft',
    revision: 1,
    archived_from_state: null,
    archived_at: null,
    archive_note: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

describe('report list and stored-state vocabularies', () => {
  test('keeps GET filter aliases distinct from stored document states', () => {
    expect([...REPORT_LIST_FILTERS]).toEqual(['active', 'archived', 'all'])
    expect([...REPORT_STORED_STATES]).toEqual(['draft', 'finalized', 'archived'])
    expect(reportListFilterSchema.safeParse('draft').success).toBe(false)
    expect(reportStoredStateSchema.safeParse('active').success).toBe(false)
    expect(reportStoredStateSchema.safeParse('all').success).toBe(false)
  })
})

describe('report list envelope', () => {
  const schema = reportListDataSchema({ workspace_uid: WORKSPACE, state: 'active' })

  test('accepts the query projection without markdown or source_stale', () => {
    const page = {
      workspace_uid: WORKSPACE,
      reports: [listItem()],
      omitted_count: 0,
      cursor: null,
    }
    expect(schema.parse(page)).toEqual(page)
    expect(page.reports[0]).not.toHaveProperty('markdown')
    expect(page.reports[0]).not.toHaveProperty('source_stale')
    expect(page.reports[0]).not.toHaveProperty('revisions')
  })

  test('refuses a stored-state alias, a workspace mismatch, and extra keys', () => {
    expect(() => schema.parse({
      workspace_uid: WORKSPACE,
      reports: [listItem({ state: 'active' })],
      omitted_count: 0,
      cursor: null,
    })).toThrow()
    expect(() => schema.parse({
      workspace_uid: OTHER_WORKSPACE,
      reports: [listItem()],
      omitted_count: 0,
      cursor: null,
    })).toThrow()
    expect(() => schema.parse({
      workspace_uid: WORKSPACE,
      reports: [listItem()],
      omitted_count: 0,
      cursor: null,
      extra: true,
    })).toThrow()
  })

  test('refuses an archived row on the active filter and a draft on archived', () => {
    expect(() => schema.parse({
      workspace_uid: WORKSPACE,
      reports: [listItem({ state: 'archived', archived_from_state: 'draft' })],
      omitted_count: 0,
      cursor: null,
    })).toThrow()
    expect(() => reportListDataSchema({ workspace_uid: WORKSPACE, state: 'archived' }).parse({
      workspace_uid: WORKSPACE,
      reports: [listItem()],
      omitted_count: 0,
      cursor: null,
    })).toThrow()
  })

  test('pairs omitted_count with a full page and a cursor', () => {
    const unique = Array.from({ length: REPORT_LIST_PAGE_SIZE }, (_, index) => ({
      ...listItem(),
      uid: `${index.toString(16).padStart(8, '0')}-abcd-4000-8000-000000000000`,
    }))
    expect(reportListDataSchema({ workspace_uid: WORKSPACE, state: 'active' }).parse({
      workspace_uid: WORKSPACE,
      reports: unique,
      omitted_count: 3,
      cursor: CURSOR,
    }).omitted_count).toBe(3)
    expect(() => schema.parse({
      workspace_uid: WORKSPACE,
      reports: [listItem()],
      omitted_count: 3,
      cursor: CURSOR,
    })).toThrow()
    expect(() => schema.parse({
      workspace_uid: WORKSPACE,
      reports: [listItem()],
      omitted_count: 0,
      cursor: CURSOR,
    })).toThrow()
  })
})

describe('report read document', () => {
  const schema = reportReadDataSchema({ report_uid: REPORT_UID })

  test('accepts full history, source_stale, and authored markdown verbatim', () => {
    const document = {
      ...listItem({ content_revision: 2, revision: 3 }),
      revisions: [
        content(),
        content({ content_revision: 2, document_revision: 3, markdown: MARKDOWN }),
      ],
      source_stale: true,
    }
    expect(schema.parse(document)).toEqual(document)
    expect(schema.parse(document).revisions[1].markdown).toBe(MARKDOWN)
  })

  test('refuses uid mismatch, missing source_stale, and a broken content history', () => {
    const document = {
      ...listItem(),
      revisions: [content()],
      source_stale: false,
    }
    expect(() => reportReadDataSchema({ report_uid: '00000002-abcd-4000-8000-000000000000' }).parse(document)).toThrow()
    const { source_stale: _stale, ...missing } = document
    expect(() => schema.parse(missing)).toThrow()
    expect(() => schema.parse({
      ...document,
      revisions: [content({ content_revision: 2 })],
    })).toThrow()
    expect(() => schema.parse({
      ...listItem({ content_revision: 2 }),
      revisions: [content()],
      source_stale: false,
    })).toThrow()
  })
})

describe('report mutation payloads', () => {
  test('create, revise, finalize, archive, and restore carry the service key sets', () => {
    const created = {
      ...summary(),
      content_entry: content({ markdown: MARKDOWN }),
      source_stale: false,
    }
    expect(reportCreateDataSchema(createExpectation()).parse(created)).toEqual(created)

    const revised = {
      ...summary({ revision: 2, updated_at: LATER }),
      content_entry: content({
        content_revision: 2,
        document_revision: 2,
        markdown: '# second',
        authored_at: LATER,
        note: null,
      }),
      reopened: false,
      source_stale: true,
    }
    expect(reportReviseDataSchema({
      workspace_uid: WORKSPACE,
      uid: REPORT_UID,
      expected_revision: 1,
      markdown: '# second',
      note: null,
    }).parse(revised)).toEqual(revised)

    const finalized = { ...summary({ state: 'finalized', revision: 2, updated_at: LATER }), source_stale: false }
    expect(reportFinalizeDataSchema({ workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1 }).parse(finalized)).toEqual(finalized)

    const archived = summary({
      state: 'archived',
      revision: 2,
      updated_at: LATER,
      archived_from_state: 'draft',
      archived_at: LATER,
      archive_note: null,
    })
    expect(reportArchiveDataSchema({ workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1, note: null }).parse(archived)).toEqual(archived)

    const restored = summary({ state: 'draft', revision: 2, updated_at: LATER })
    expect(reportRestoreDataSchema({ workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1 }).parse(restored)).toEqual(restored)
  })

  test('refuses invented mutation fields, filter aliases, and bound mismatches', () => {
    expect(() => reportCreateDataSchema(createExpectation()).parse({
      ...summary(),
      content_entry: content({ markdown: MARKDOWN }),
      source_stale: false,
      revisions: [content()],
    })).toThrow()
    expect(() => reportArchiveDataSchema({ workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1, note: null }).parse({
      ...summary({ state: 'archived', archived_from_state: 'draft', archived_at: LATER }),
      source_stale: false,
    })).toThrow()
    expect(() => reportCreateDataSchema(createExpectation()).parse({
      ...summary({ workspace_uid: OTHER_WORKSPACE }),
      content_entry: content({ markdown: MARKDOWN }),
      source_stale: false,
    })).toThrow()
    expect(() => reportCreateDataSchema(createExpectation()).parse({
      ...summary({ state: 'active' }),
      content_entry: content({ markdown: MARKDOWN }),
      source_stale: false,
    })).toThrow()
  })

  test('admits exactly one revision step past the asserted revision on every transition', () => {
    const revised = {
      ...summary({ revision: 4, updated_at: LATER }),
      content_entry: content({
        content_revision: 2,
        document_revision: 4,
        markdown: '# second',
        authored_at: LATER,
        note: null,
      }),
      reopened: true,
      source_stale: false,
    }
    const revise = (expected_revision: number) => reportReviseDataSchema({
      workspace_uid: WORKSPACE,
      uid: REPORT_UID,
      expected_revision,
      markdown: '# second',
      note: null,
    })
    expect(revise(3).parse(revised)).toEqual(revised)
    expect(() => revise(2).parse(revised)).toThrow()
    expect(() => revise(4).parse(revised)).toThrow()

    const finalized = { ...summary({ state: 'finalized', revision: 777, updated_at: LATER }), source_stale: false }
    expect(() => reportFinalizeDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1,
    }).parse(finalized)).toThrow()
    expect(reportFinalizeDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 776,
    }).parse(finalized)).toEqual(finalized)

    const archived = summary({
      state: 'archived',
      revision: 777,
      updated_at: LATER,
      archived_from_state: 'finalized',
      archived_at: LATER,
      archive_note: 'shelved',
    })
    expect(() => reportArchiveDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1, note: 'shelved',
    }).parse(archived)).toThrow()
    expect(reportArchiveDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 776, note: 'shelved',
    }).parse(archived)).toEqual(archived)

    const restored = summary({ state: 'finalized', revision: 777, updated_at: LATER })
    expect(() => reportRestoreDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 1,
    }).parse(restored)).toThrow()
    expect(reportRestoreDataSchema({
      workspace_uid: WORKSPACE, uid: REPORT_UID, expected_revision: 776,
    }).parse(restored)).toEqual(restored)
  })

  test('refuses a create answered for another template, period, digest, or source stamp', () => {
    const created = { ...summary(), content_entry: content({ markdown: MARKDOWN }), source_stale: false }
    expect(reportCreateDataSchema(createExpectation()).parse(created)).toEqual(created)
    expect(() => reportCreateDataSchema(createExpectation({
      period: { kind: 'day', date: '2026-09-07' },
    })).parse(created)).toThrow()
    expect(() => reportCreateDataSchema(createExpectation({
      source_digest: `sha256:${'1'.repeat(64)}`,
    })).parse(created)).toThrow()
    expect(() => reportCreateDataSchema(createExpectation({
      source_generated_at: '2026-09-06T10:00:00Z',
    })).parse(created)).toThrow()
    expect(() => reportCreateDataSchema(createExpectation({
      template: 'weekly-v1',
    })).parse(created)).toThrow()
  })

  test('preserves a 100000-character markdown body and refuses one extra code point', () => {
    const body = 'm'.repeat(MAX_REPORT_MARKDOWN_CHARS)
    const created = {
      ...summary(),
      content_entry: content({ markdown: body }),
      source_stale: false,
    }
    expect(reportCreateDataSchema(createExpectation({ markdown: body })).parse(created)
      .content_entry.markdown).toHaveLength(MAX_REPORT_MARKDOWN_CHARS)
    expect(() => reportCreateDataSchema(createExpectation({ markdown: `${body}x` })).parse({
      ...created,
      content_entry: content({ markdown: `${body}x` }),
    })).toThrow()
  })
})
