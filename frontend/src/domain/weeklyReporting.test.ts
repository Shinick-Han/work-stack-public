import { describe, expect, test } from 'vitest'
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
