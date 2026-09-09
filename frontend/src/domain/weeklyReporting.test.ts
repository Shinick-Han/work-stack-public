import { describe, expect, test } from 'vitest'
import { MAX_DAILY_PREVIEW_CONTEXT_ITEMS } from './reporting'
import {
  MAX_WEEKLY_PREVIEW_ITEMS,
  MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS,
  shiftIsoDate,
  weekDates,
  weeklyReportPreviewPayloadSchema,
  weeklyReportPreviewResponseSchema,
} from './weeklyReporting'

const UID = '22222222-2222-4222-8222-222222222222'
const END = '2026-08-30'
const START = '2026-08-24'
const WEEK = [
  '2026-08-24',
  '2026-08-25',
  '2026-08-26',
  '2026-08-27',
  '2026-08-28',
  '2026-08-29',
  '2026-08-30',
]
const DIGEST = `sha256:${'a'.repeat(64)}`

function payload(overrides: Record<string, unknown> = {}) {
  return {
    workspace_uid: UID,
    source_digest: DIGEST,
    preview: {
      template: 'weekly-v1',
      period: { kind: 'week', start: START, end: END, days: 7 },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        range: { start: START, end: END, days: 7 },
        task_ids: [],
        sources: [],
        coverage: { record_dates: [], no_record_dates: WEEK },
        ignored_keys: [],
      },
      markdown: '# Weekly review 2026-08-24 → 2026-08-30\n',
    },
    ...overrides,
  }
}

function recordedPreview(overrides: Record<string, unknown> = {}) {
  return payload({
    preview: {
      template: 'weekly-v1',
      period: { kind: 'week', start: START, end: END, days: 7 },
      generated_at: '2026-09-06T01:02:03Z',
      absence: null,
      provenance: {
        range: { start: START, end: END, days: 7 },
        task_ids: ['T-0001'],
        sources: [{
          kind: 'review.weekly.project',
          task_id: 'T-0001',
          dates: ['2026-08-30'],
        }],
        coverage: {
          record_dates: ['2026-08-30'],
          no_record_dates: WEEK.filter((day) => day !== '2026-08-30'),
        },
        ignored_keys: ['extra'],
      },
      markdown: '# Weekly review 2026-08-24 → 2026-08-30\n\n## Projects\n',
    },
    ...overrides,
  })
}

describe('weekly report preview wire schema', () => {
  test('accepts an empty week and a recorded weekly-v1 core preview', () => {
    const empty = payload()
    expect(weeklyReportPreviewPayloadSchema.parse(empty)).toEqual(empty)
    expect(
      weeklyReportPreviewResponseSchema({ end_date: END, workspace_uid: UID }).parse(empty),
    ).toEqual(empty)
    const recorded = recordedPreview()
    expect(weeklyReportPreviewPayloadSchema.parse(recorded)).toEqual(recorded)
  })

  test('computes the inclusive seven-day window from the end date', () => {
    expect(shiftIsoDate(END, -6)).toBe(START)
    expect(weekDates(START)).toEqual(WEEK)
  })

  test('refuses uid, template, period end, and range mismatches', () => {
    const body = payload()
    expect(() => weeklyReportPreviewResponseSchema({
      end_date: END,
      workspace_uid: '00000000-0000-4000-8000-0000000000b0',
    }).parse(body)).toThrow()
    expect(() => weeklyReportPreviewResponseSchema({
      end_date: '2026-08-29',
      workspace_uid: UID,
    }).parse(body)).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: { ...(payload().preview as object), template: 'daily-v1' },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        period: { kind: 'week', start: '2026-08-23', end: END, days: 7 },
      },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        provenance: {
          ...(payload().preview as { provenance: object }).provenance,
          range: { start: START, end: '2026-08-29', days: 7 },
        },
      },
    }))).toThrow()
  })

  test('refuses coverage that does not partition the seven days', () => {
    const provenance = (payload().preview as { provenance: Record<string, unknown> }).provenance
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        provenance: {
          ...provenance,
          coverage: { record_dates: ['2026-08-30'], no_record_dates: WEEK },
        },
      },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        provenance: {
          ...provenance,
          coverage: { record_dates: [], no_record_dates: WEEK.slice(1) },
        },
      },
    }))).toThrow()
  })

  test('refuses malformed digest, extra keys, invented objectives, and mismatched sources', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({ source_digest: 'a'.repeat(64) }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({ extra: true }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: { ...(payload().preview as object), objectives: [] },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(recordedPreview({
      preview: {
        ...(recordedPreview().preview as object),
        provenance: {
          ...(recordedPreview().preview as { provenance: object }).provenance,
          task_ids: ['T-0002'],
        },
      },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: { ...(payload().preview as object), absence: null },
    }))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(recordedPreview({
      preview: { ...(recordedPreview().preview as object), absence: 'no records' },
    }))).toThrow()
  })

  test('refuses oversized markdown and more than 200 weekly sources', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        markdown: 'm'.repeat(MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS + 1),
      },
    }))).toThrow()
    const sources = Array.from({ length: MAX_WEEKLY_PREVIEW_ITEMS + 1 }, (_, index) => ({
      kind: 'review.weekly.project',
      task_id: `T-${String(index).padStart(4, '0')}`,
      dates: ['2026-08-30'],
    }))
    expect(() => weeklyReportPreviewPayloadSchema.parse(recordedPreview({
      preview: {
        ...(recordedPreview().preview as object),
        provenance: {
          range: { start: START, end: END, days: 7 },
          task_ids: sources.map((source) => source.task_id),
          sources,
          coverage: {
            record_dates: ['2026-08-30'],
            no_record_dates: WEEK.filter((day) => day !== '2026-08-30'),
          },
          ignored_keys: [],
        },
      },
    }))).toThrow()
  })

  test('accepts markdown at the 100000-character bound', () => {
    const body = payload({
      preview: {
        ...(payload().preview as object),
        markdown: 'm'.repeat(MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS),
      },
    })
    expect(weeklyReportPreviewPayloadSchema.parse(body).preview.markdown).toHaveLength(
      MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS,
    )
  })
})

const GENERATED_AT = '2026-09-06T01:02:03Z'

/**
 * A recorded week whose provenance names `taskIds`, optionally carrying the
 * R45 catalogue sibling. Sources mirror the ids so the existing weekly
 * provenance rules stay satisfied and only the catalogue is under test.
 */
function weeklyCatalogPayload(
  catalog: Record<string, unknown> | undefined,
  taskIds: string[] = ['T-0001', 'T-0002'],
) {
  const base = recordedPreview().preview as Record<string, unknown>
  const provenance = base.provenance as Record<string, unknown>
  const body: Record<string, unknown> = {
    ...recordedPreview(),
    preview: {
      ...base,
      provenance: {
        ...provenance,
        task_ids: taskIds,
        sources: taskIds.map((taskId) => ({
          kind: 'review.weekly.project',
          task_id: taskId,
          dates: ['2026-08-30'],
        })),
      },
    },
  }
  if (catalog) body.context_catalog = catalog
  return body
}

/** An empty week (absence 'no records', no provenance tasks) plus a catalogue. */
function emptyWeekCatalogPayload(catalog: Record<string, unknown>) {
  return { ...payload(), context_catalog: catalog }
}

function catalogItem(overrides: Record<string, unknown> = {}) {
  return {
    capture_id: 'C-0001',
    capture_revision: 1,
    title: 'Synthetic context',
    linked_task_ids: ['T-0001'],
    status: 'linked',
    ...overrides,
  }
}

function readyCatalog(overrides: Record<string, unknown> = {}) {
  return {
    captured_at: GENERATED_AT,
    items: [catalogItem()],
    omitted_count: 0,
    ...overrides,
  }
}

function filledItems(count: number) {
  return Array.from({ length: count }, (_, index) => catalogItem({
    capture_id: `C-${String(index + 1).padStart(4, '0')}`,
  }))
}

describe('optional weekly preview context catalogue', () => {
  test('a pre-R45 response without the catalogue still decodes and carries no catalogue', () => {
    const body = weeklyCatalogPayload(undefined)
    const parsed = weeklyReportPreviewPayloadSchema.parse(body)
    expect(parsed.context_catalog).toBeUndefined()
    expect('context_catalog' in parsed).toBe(false)
    expect(
      weeklyReportPreviewResponseSchema({ end_date: END, workspace_uid: UID }).parse(body),
    ).toEqual(body)
  })

  test('accepts the frozen wire shape, an empty week, and a truncated catalogue', () => {
    const ready = readyCatalog()
    const parsed = weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(ready))
    expect(parsed.context_catalog).toEqual(ready)
    expect(parsed.preview.markdown).toBe(
      (recordedPreview().preview as { markdown: string }).markdown,
    )
    expect(weeklyReportPreviewPayloadSchema.parse(
      emptyWeekCatalogPayload(readyCatalog({ items: [] })),
    ).context_catalog?.items).toEqual([])
    const truncated = readyCatalog({
      items: filledItems(MAX_DAILY_PREVIEW_CONTEXT_ITEMS),
      omitted_count: 4,
    })
    const full = weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(truncated))
    expect(full.context_catalog?.items).toHaveLength(MAX_DAILY_PREVIEW_CONTEXT_ITEMS)
    expect(full.context_catalog?.omitted_count).toBe(4)
    expect(weeklyReportPreviewResponseSchema({ end_date: END, workspace_uid: UID })
      .parse(weeklyCatalogPayload(ready)).context_catalog).toEqual(ready)
  })

  test('keeps a dismissed Capture honest and refuses an unknown status or invented id', () => {
    expect(weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ status: 'dismissed' })] }),
    )).context_catalog?.items[0].status).toBe('dismissed')
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ status: 'verified' })] }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ capture_id: 'C-1' })] }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ capture_revision: -1 })] }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ title: '' })] }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ linked_task_ids: [] })] }),
    ))).toThrow()
  })

  test('refuses an unknown catalogue or item field instead of laundering it', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ rag_query: 'anything' }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ source_url: 'https://example.invalid' })] }),
    ))).toThrow()
  })

  test('binds the catalogue to this weekly generation and its provenance tasks', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ captured_at: '2026-09-06T01:02:04Z' }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ linked_task_ids: ['T-0003'] })] }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(
      emptyWeekCatalogPayload(readyCatalog()),
    )).toThrow()
    expect(weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ items: [catalogItem({ linked_task_ids: ['T-0001', 'T-0002'] })] }),
    )).context_catalog?.items[0].linked_task_ids).toEqual(['T-0001', 'T-0002'])
  })

  test('requires unique natural order and refuses more than the 32 bound', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [catalogItem({ capture_id: 'C-0002' }), catalogItem({ capture_id: 'C-0001' })],
    })))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [catalogItem(), catalogItem()],
    })))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: filledItems(MAX_DAILY_PREVIEW_CONTEXT_ITEMS + 1),
      omitted_count: 0,
    })))).toThrow()
    expect(weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [
        catalogItem({ capture_id: 'C-0009' }),
        catalogItem({ capture_id: 'C-00010' }),
      ],
    }))).context_catalog?.items.map((item) => item.capture_id)).toEqual(['C-0009', 'C-00010'])
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [
        catalogItem({ capture_id: 'C-00010' }),
        catalogItem({ capture_id: 'C-0009' }),
      ],
    })))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [catalogItem({ linked_task_ids: ['T-0002', 'T-0001'] })],
    })))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: [catalogItem({ linked_task_ids: ['T-0001', 'T-0001'] })],
    })))).toThrow()
  })

  test('admits omitted_count only at the bound it could come from', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(
      readyCatalog({ omitted_count: 1 }),
    ))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(
      emptyWeekCatalogPayload(readyCatalog({ items: [], omitted_count: 2 })),
    )).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: filledItems(MAX_DAILY_PREVIEW_CONTEXT_ITEMS),
      omitted_count: 1.5,
    })))).toThrow()
    expect(() => weeklyReportPreviewPayloadSchema.parse(weeklyCatalogPayload(readyCatalog({
      items: filledItems(MAX_DAILY_PREVIEW_CONTEXT_ITEMS),
      omitted_count: -1,
    })))).toThrow()
  })

  test('leaves the existing weekly core rules in force beside a valid catalogue', () => {
    expect(() => weeklyReportPreviewPayloadSchema.parse({
      ...weeklyCatalogPayload(readyCatalog()),
      source_digest: 'a'.repeat(64),
    })).toThrow()
    expect(() => weeklyReportPreviewResponseSchema({
      end_date: '2026-08-29',
      workspace_uid: UID,
    }).parse(weeklyCatalogPayload(readyCatalog()))).toThrow()
  })
})
