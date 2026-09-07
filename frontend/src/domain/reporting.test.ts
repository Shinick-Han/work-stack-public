import { describe, expect, test } from 'vitest'
import {
  dailyReportPreviewPayloadSchema,
  dailyReportPreviewResponseSchema,
  MAX_DAILY_PREVIEW_DAY_ENTRIES,
  MAX_DAILY_PREVIEW_MARKDOWN_CHARS,
} from './reporting'

const UID = '22222222-2222-4222-8222-222222222222'
const DATE = '2026-08-30'
const DIGEST = `sha256:${'a'.repeat(64)}`

function payload(overrides: Record<string, unknown> = {}) {
  return {
    workspace_uid: UID,
    source_digest: DIGEST,
    preview: {
      template: 'daily-v1',
      period: { kind: 'day', date: DATE },
      generated_at: '2026-09-06T01:02:03Z',
      absence: 'no records',
      provenance: {
        date: DATE,
        task_ids: [],
        sources: [],
        weekly_range: { start: DATE, end: DATE, days: 1 },
        ignored_keys: [],
      },
      markdown: '# Daily review 2026-08-30\n\nNo records.\n',
    },
    ...overrides,
  }
}

describe('daily report preview wire schema', () => {
  test('accepts reporting.py provenance including a one-day weekly_range', () => {
    const body = payload({
      preview: {
        template: 'daily-v1',
        period: { kind: 'day', date: DATE },
        generated_at: '2026-09-06T01:02:03Z',
        absence: null,
        provenance: {
          date: DATE,
          task_ids: ['T-0001'],
          sources: [{
            kind: 'review.day.entry',
            date: DATE,
            index: 0,
            task_id: 'T-0001',
            unknown_fields: ['extra'],
          }],
          weekly_range: { start: DATE, end: DATE, days: 1 },
          ignored_keys: ['markdown', 'template'],
        },
        markdown: '# Daily review 2026-08-30\n',
      },
    })
    expect(dailyReportPreviewPayloadSchema.parse(body)).toEqual(body)
    expect(dailyReportPreviewResponseSchema({ date: DATE, workspace_uid: UID }).parse(body)).toEqual(body)
  })

  test('accepts a null weekly_range, a null source task_id, and seconds Z generated_at', () => {
    const body = payload({
      preview: {
        template: 'daily-v1',
        period: { kind: 'day', date: DATE },
        generated_at: '2026-09-06T01:02:03Z',
        absence: 'no records',
        provenance: {
          date: DATE,
          task_ids: [],
          sources: [{
            kind: 'review.day.entry',
            date: DATE,
            index: 0,
            task_id: null,
            unknown_fields: [],
          }],
          weekly_range: null,
          ignored_keys: [],
        },
        markdown: 'No records.\n',
      },
    })
    expect(dailyReportPreviewPayloadSchema.parse(body).preview.provenance.weekly_range).toBeNull()
    expect(dailyReportPreviewPayloadSchema.parse(body).source_digest).toBe(DIGEST)
  })

  test('refuses uid or date mismatch against the requested contract', () => {
    const body = payload()
    expect(() => dailyReportPreviewResponseSchema({
      date: DATE,
      workspace_uid: '00000000-0000-4000-8000-0000000000b0',
    }).parse(body)).toThrow()
    expect(() => dailyReportPreviewResponseSchema({
      date: '2026-08-29',
      workspace_uid: UID,
    }).parse(body)).toThrow()
  })

  test('refuses extra keys, split period/provenance dates, and non-calendar days', () => {
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({ extra: true }))).toThrow()
    const split = payload()
    const preview = { ...(split.preview as Record<string, unknown>) }
    preview.provenance = { ...(preview.provenance as object), date: '2026-08-29' }
    expect(() => dailyReportPreviewPayloadSchema.parse({ ...split, preview })).toThrow()
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...(payload().preview as object),
        period: { kind: 'day', date: '2026-02-30' },
        provenance: { ...(payload().preview as { provenance: object }).provenance, date: '2026-02-30' },
      },
    }))).toThrow()
  })

  test('requires the canonical sha256: prefix on source_digest', () => {
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({ source_digest: 'a'.repeat(64) }))).toThrow()
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      source_digest: `SHA256:${'a'.repeat(64)}`,
    }))).toThrow()
    const missing = payload()
    delete (missing as { source_digest?: string }).source_digest
    expect(() => dailyReportPreviewPayloadSchema.parse(missing)).toThrow()
  })

  test('accepts a 100000-character markdown payload under the 262144-byte ceiling', () => {
    const body = payload({
      preview: {
        ...(payload().preview as object),
        markdown: 'm'.repeat(MAX_DAILY_PREVIEW_MARKDOWN_CHARS),
      },
    })
    expect(dailyReportPreviewPayloadSchema.parse(body).preview.markdown).toHaveLength(
      MAX_DAILY_PREVIEW_MARKDOWN_CHARS,
    )
  })

  test('refuses oversized markdown, day lists, source index, ignored_keys, and byte ceiling', () => {
    const basePreview = payload().preview as {
      provenance: Record<string, unknown>
      [key: string]: unknown
    }
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: { ...basePreview, markdown: 'm'.repeat(MAX_DAILY_PREVIEW_MARKDOWN_CHARS + 1) },
    }))).toThrow()
    const tooManySources = Array.from({ length: MAX_DAILY_PREVIEW_DAY_ENTRIES + 1 }, (_, index) => ({
      kind: 'review.day.entry',
      date: DATE,
      index: 0,
      task_id: null,
      unknown_fields: [],
    }))
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...basePreview,
        provenance: { ...basePreview.provenance, sources: tooManySources },
      },
    }))).toThrow()
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...basePreview,
        provenance: {
          ...basePreview.provenance,
          sources: [{
            kind: 'review.day.entry',
            date: DATE,
            index: MAX_DAILY_PREVIEW_DAY_ENTRIES,
            task_id: null,
            unknown_fields: [],
          }],
        },
      },
    }))).toThrow()
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...basePreview,
        provenance: {
          ...basePreview.provenance,
          ignored_keys: ['absence', 'generated_at', 'markdown', 'period', 'provenance', 'template', 'markdown'],
        },
      },
    }))).toThrow()
    expect(() => dailyReportPreviewPayloadSchema.parse(payload({
      preview: {
        ...basePreview,
        markdown: 'm'.repeat(100_000),
        provenance: {
          ...basePreview.provenance,
          sources: [{
            kind: 'review.day.entry',
            date: DATE,
            index: 0,
            task_id: null,
            unknown_fields: Array.from({ length: 200 }, () => 'u'.repeat(1_000)),
          }],
        },
      },
    }))).toThrow()
  })
})
