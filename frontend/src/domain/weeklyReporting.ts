import { z } from 'zod'
import { contextCatalogSchema, refineContextCatalog } from './reporting'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const SOURCE_DIGEST = /^sha256:[0-9a-f]{64}$/

export const MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS = 100_000
export const MAX_WEEKLY_PREVIEW_ITEMS = 200
export const WEEKLY_COVERAGE_DAYS = 7

const MAX_PROJECTION_STRING = 262_144
const MAX_IGNORED_KEYS = 32

const workspaceUidSchema = z.string()
  .regex(UUID_SHAPE, 'not a canonical RFC variant UUID')
  .refine((value) => value !== NIL_UUID, 'the nil UUID is not a workspace')

function isRealDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  if (year < 1 || month < 1 || month > 12 || day < 1) return false
  const date = new Date(Date.UTC(2000, month - 1, day))
  date.setUTCFullYear(year)
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day
}

const weeklyDateSchema = z.string().refine(isRealDate, 'not a real calendar date')
const projectionString = z.string().max(MAX_PROJECTION_STRING)

export function shiftIsoDate(value: string, days: number): string | null {
  if (!isRealDate(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(Date.UTC(2000, month - 1, day))
  date.setUTCFullYear(year)
  date.setUTCDate(date.getUTCDate() + days)
  const shiftedYear = date.getUTCFullYear()
  if (shiftedYear < 1 || shiftedYear > 9999) return null
  return [
    String(shiftedYear).padStart(4, '0'),
    String(date.getUTCMonth() + 1).padStart(2, '0'),
    String(date.getUTCDate()).padStart(2, '0'),
  ].join('-')
}

export function weekDates(start: string): string[] {
  const days: string[] = []
  for (let offset = 0; offset < WEEKLY_COVERAGE_DAYS; offset += 1) {
    const next = shiftIsoDate(start, offset)
    if (!next) return []
    days.push(next)
  }
  return days
}

function distinctDates(values: string[]): boolean {
  return new Set(values).size === values.length
}

function coveragePartitionsWeek(
  start: string,
  end: string,
  recordDates: string[],
  noRecordDates: string[],
): boolean {
  const expected = weekDates(start)
  if (expected.length !== WEEKLY_COVERAGE_DAYS || expected[6] !== end) return false
  if (!distinctDates(recordDates) || !distinctDates(noRecordDates)) return false
  const records = new Set(recordDates)
  const missing = new Set(noRecordDates)
  if ([...records].some((day) => missing.has(day))) return false
  if (records.size + missing.size !== WEEKLY_COVERAGE_DAYS) return false
  return expected.every((day) => records.has(day) || missing.has(day))
}

function sourceDatesInRange(dates: string[], start: string, end: string): boolean {
  if (!distinctDates(dates) || dates.length === 0) return false
  return dates.every((date) => date >= start && date <= end)
}

const weeklyRangeSchema = z.object({
  start: weeklyDateSchema,
  end: weeklyDateSchema,
  days: z.literal(WEEKLY_COVERAGE_DAYS),
}).strict()

const weeklySourceSchema = z.object({
  kind: z.literal('review.weekly.project'),
  task_id: projectionString,
  dates: z.array(weeklyDateSchema).min(1).max(WEEKLY_COVERAGE_DAYS),
}).strict()

export const weeklyReportPreviewPayloadSchema = z.object({
  workspace_uid: workspaceUidSchema,
  source_digest: z.string().regex(SOURCE_DIGEST, 'source_digest must be sha256:<64 lowercase hex>'),
  preview: z.object({
    template: z.literal('weekly-v1'),
    period: z.object({
      kind: z.literal('week'),
      start: weeklyDateSchema,
      end: weeklyDateSchema,
      days: z.literal(WEEKLY_COVERAGE_DAYS),
    }).strict(),
    generated_at: z.string().datetime({ offset: true }),
    absence: z.union([z.null(), z.literal('no records')]),
    provenance: z.object({
      range: weeklyRangeSchema,
      task_ids: z.array(projectionString).max(MAX_WEEKLY_PREVIEW_ITEMS),
      sources: z.array(weeklySourceSchema).max(MAX_WEEKLY_PREVIEW_ITEMS),
      coverage: z.object({
        record_dates: z.array(weeklyDateSchema).max(WEEKLY_COVERAGE_DAYS),
        no_record_dates: z.array(weeklyDateSchema).max(WEEKLY_COVERAGE_DAYS),
      }).strict(),
      ignored_keys: z.array(projectionString).max(MAX_IGNORED_KEYS),
    }).strict(),
    markdown: z.string().max(MAX_WEEKLY_PREVIEW_MARKDOWN_CHARS),
  }).strict(),
  // Absent on servers before R45; the weekly preview stays fully usable without it.
  context_catalog: contextCatalogSchema.optional(),
}).strict().superRefine((value, ctx) => {
  const { period, provenance, absence } = value.preview
  // Same R44 catalogue predicate, bound to this week's generation and provenance.
  if (value.context_catalog) refineContextCatalog(value.context_catalog, value.preview, ctx)
  const expectedStart = shiftIsoDate(period.end, 1 - WEEKLY_COVERAGE_DAYS)
  if (period.start !== expectedStart) {
    ctx.addIssue({
      code: 'custom',
      message: 'weekly period start must be the inclusive end minus 6 days',
      path: ['preview', 'period', 'start'],
    })
  }
  if (
    provenance.range.start !== period.start
    || provenance.range.end !== period.end
    || provenance.range.days !== period.days
  ) {
    ctx.addIssue({
      code: 'custom',
      message: 'provenance range does not match the weekly period',
      path: ['preview', 'provenance', 'range'],
    })
  }
  if (!coveragePartitionsWeek(
    period.start,
    period.end,
    provenance.coverage.record_dates,
    provenance.coverage.no_record_dates,
  )) {
    ctx.addIssue({
      code: 'custom',
      message: 'coverage must partition the seven-day period',
      path: ['preview', 'provenance', 'coverage'],
    })
  }
  const sourceIds = provenance.sources.map((source) => source.task_id)
  if (
    sourceIds.length !== provenance.task_ids.length
    || sourceIds.some((taskId, index) => taskId !== provenance.task_ids[index])
  ) {
    ctx.addIssue({
      code: 'custom',
      message: 'provenance task_ids must match weekly project sources',
      path: ['preview', 'provenance', 'task_ids'],
    })
  }
  if (provenance.sources.some((source) => (
    !sourceDatesInRange(source.dates, period.start, period.end)
  ))) {
    ctx.addIssue({
      code: 'custom',
      message: 'source dates must be distinct in-range dates',
      path: ['preview', 'provenance', 'sources'],
    })
  }
  if (absence === 'no records' && provenance.sources.length !== 0) {
    ctx.addIssue({
      code: 'custom',
      message: 'no-records absence cannot include weekly projects',
      path: ['preview', 'absence'],
    })
  }
  if (absence === null && provenance.sources.length === 0) {
    ctx.addIssue({
      code: 'custom',
      message: 'a recorded week must include at least one project source',
      path: ['preview', 'provenance', 'sources'],
    })
  }
})

export type WeeklyReportPreviewResponse = z.infer<typeof weeklyReportPreviewPayloadSchema>
export type WeeklyReportPreviewDocument = WeeklyReportPreviewResponse['preview']

export function weeklyReportPreviewResponseSchema(
  expected: { end_date: string; workspace_uid: string },
) {
  return weeklyReportPreviewPayloadSchema.superRefine((value, ctx) => {
    if (value.workspace_uid !== expected.workspace_uid) {
      ctx.addIssue({
        code: 'custom',
        message: 'workspace_uid does not match the selected workspace',
        path: ['workspace_uid'],
      })
    }
    if (value.preview.template !== 'weekly-v1') {
      ctx.addIssue({
        code: 'custom',
        message: 'template is not weekly-v1',
        path: ['preview', 'template'],
      })
    }
    if (value.preview.period.end !== expected.end_date) {
      ctx.addIssue({
        code: 'custom',
        message: 'period end does not match the requested end_date',
        path: ['preview', 'period', 'end'],
      })
    }
  })
}
