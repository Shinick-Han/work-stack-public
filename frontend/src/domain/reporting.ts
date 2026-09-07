import { z } from 'zod'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const SOURCE_DIGEST = /^sha256:[0-9a-f]{64}$/

/** Matches workstack.reporting: 262144-byte projection input, 100000-char markdown. */
export const MAX_DAILY_PREVIEW_RESPONSE_BYTES = 262144
export const MAX_DAILY_PREVIEW_MARKDOWN_CHARS = 100_000
export const MAX_DAILY_PREVIEW_DAY_ENTRIES = 200
const MAX_UNKNOWN_FIELDS = 32_768
const MAX_PROJECTION_STRING = MAX_DAILY_PREVIEW_RESPONSE_BYTES
const PREVIEW_KEYS = [
  'absence',
  'generated_at',
  'markdown',
  'period',
  'provenance',
  'template',
] as const

const workspaceUidSchema = z.string()
  .regex(UUID_SHAPE, 'not a canonical RFC variant UUID')
  .refine((value) => value !== NIL_UUID, 'the nil UUID is not a workspace')

/**
 * A real calendar day for years 0001..9999. Date.UTC maps 0..99 onto
 * 1900..1999, so the year is assigned explicitly instead.
 */
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

const dailyDateSchema = z.string().refine(isRealDate, 'not a real calendar date')
const projectionString = z.string().max(MAX_PROJECTION_STRING)

const dailyReportSourceSchema = z.object({
  kind: z.literal('review.day.entry'),
  date: dailyDateSchema,
  index: z.number().int().min(0).max(MAX_DAILY_PREVIEW_DAY_ENTRIES - 1),
  task_id: projectionString.nullable(),
  unknown_fields: z.array(projectionString).max(MAX_UNKNOWN_FIELDS),
}).strict()

const weeklyRangeSchema = z.object({
  start: dailyDateSchema,
  end: dailyDateSchema,
  days: z.number().int().min(1).max(31),
}).strict()

function utf8JsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
}

export const dailyReportPreviewPayloadSchema = z.object({
  workspace_uid: workspaceUidSchema,
  source_digest: z.string().regex(SOURCE_DIGEST, 'source_digest must be sha256:<64 lowercase hex>'),
  preview: z.object({
    template: z.literal('daily-v1'),
    period: z.object({
      kind: z.literal('day'),
      date: dailyDateSchema,
    }).strict(),
    generated_at: z.string().datetime({ offset: true }),
    absence: z.union([z.null(), z.literal('no records')]),
    provenance: z.object({
      date: dailyDateSchema,
      task_ids: z.array(projectionString).max(MAX_DAILY_PREVIEW_DAY_ENTRIES),
      sources: z.array(dailyReportSourceSchema).max(MAX_DAILY_PREVIEW_DAY_ENTRIES),
      weekly_range: weeklyRangeSchema.nullable(),
      ignored_keys: z.array(z.enum(PREVIEW_KEYS)).max(PREVIEW_KEYS.length),
    }).strict(),
    markdown: z.string().max(MAX_DAILY_PREVIEW_MARKDOWN_CHARS),
  }).strict(),
}).strict().superRefine((value, ctx) => {
  if (value.preview.period.date !== value.preview.provenance.date) {
    ctx.addIssue({
      code: 'custom',
      message: 'preview period and provenance dates differ',
      path: ['preview', 'provenance', 'date'],
    })
  }
  if (utf8JsonBytes(value) > MAX_DAILY_PREVIEW_RESPONSE_BYTES) {
    ctx.addIssue({
      code: 'custom',
      message: 'daily preview response exceeds the 262144-byte ceiling',
      path: [],
    })
  }
})

export type DailyReportPreviewResponse = z.infer<typeof dailyReportPreviewPayloadSchema>
export type DailyReportPreviewDocument = DailyReportPreviewResponse['preview']

export function dailyReportPreviewResponseSchema(
  expected: { date: string; workspace_uid: string },
) {
  return dailyReportPreviewPayloadSchema.superRefine((value, ctx) => {
    if (value.workspace_uid !== expected.workspace_uid) {
      ctx.addIssue({
        code: 'custom',
        message: 'workspace_uid does not match the selected workspace',
        path: ['workspace_uid'],
      })
    }
    if (value.preview.period.date !== expected.date) {
      ctx.addIssue({
        code: 'custom',
        message: 'period date does not match the requested date',
        path: ['preview', 'period', 'date'],
      })
    }
  })
}
