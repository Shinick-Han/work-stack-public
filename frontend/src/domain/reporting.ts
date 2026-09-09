import { z } from 'zod'
import { CAPTURE_STATUSES } from './types'

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

/** Frozen R44 catalogue bounds: 32 rows, existing admitted Capture display title. */
export const MAX_DAILY_PREVIEW_CONTEXT_ITEMS = 32
export const MAX_DAILY_PREVIEW_CONTEXT_TITLE_CHARS = 500
const CAPTURE_ID = /^C-[0-9]{4,}$/
const TASK_ID = /^T-[0-9]{4,}$/

const contextCatalogItemSchema = z.object({
  capture_id: z.string().regex(CAPTURE_ID, 'capture_id must be an existing C-<n> Capture id'),
  capture_revision: z.number().int().nonnegative(),
  title: z.string().min(1).max(MAX_DAILY_PREVIEW_CONTEXT_TITLE_CHARS),
  linked_task_ids: z.array(z.string().regex(TASK_ID, 'linked_task_ids must be existing T-<n> Task ids'))
    .min(1)
    .max(MAX_DAILY_PREVIEW_DAY_ENTRIES),
  status: z.enum(CAPTURE_STATUSES),
}).strict()

/** Shared with the weekly preview decoder (R45); the closed R44 catalogue shape. */
export const contextCatalogSchema = z.object({
  captured_at: z.string().datetime({ offset: true }),
  items: z.array(contextCatalogItemSchema).max(MAX_DAILY_PREVIEW_CONTEXT_ITEMS),
  omitted_count: z.number().int().nonnegative(),
}).strict()

export type DailyReportContextCatalog = z.infer<typeof contextCatalogSchema>
export type DailyReportContextItem = z.infer<typeof contextCatalogItemSchema>

/**
 * Natural order for a zero-padded `<prefix>-<digits>` wire id: fewer
 * significant digits sort first, so C-00010 follows C-0009 the way the
 * backend enumerates Captures.
 */
function naturalIdOrder(left: string, right: string): number {
  const leftDigits = left.replace(/^[A-Z]+-0*/, '')
  const rightDigits = right.replace(/^[A-Z]+-0*/, '')
  if (leftDigits.length !== rightDigits.length) return leftDigits.length - rightDigits.length
  if (leftDigits === rightDigits) return 0
  return leftDigits < rightDigits ? -1 : 1
}

function isNaturalAscending(ids: readonly string[]): boolean {
  return ids.every((id, index) => index === 0 || naturalIdOrder(ids[index - 1], id) < 0)
}

function refuseContext(ctx: z.RefinementCtx, message: string, path: (string | number)[]) {
  ctx.addIssue({ code: 'custom', message, path: ['context_catalog', ...path] })
}

/**
 * The catalogue is a snapshot of the SAME preview generation, so it may only
 * name that preview's provenance tasks and must arrive already deduplicated,
 * naturally ordered and truthfully truncated. A malformed present catalogue is
 * refused here rather than laundered into a partially trusted panel.
 *
 * The preview argument is structural, so the weekly preview reuses this exact
 * predicate against its own generated_at and provenance.task_ids.
 */
export function refineContextCatalog(
  catalog: DailyReportContextCatalog,
  preview: { generated_at: string; provenance: { task_ids: string[] } },
  ctx: z.RefinementCtx,
) {
  if (catalog.captured_at !== preview.generated_at) {
    refuseContext(ctx, 'context_catalog captured_at is not the preview generation time', ['captured_at'])
  }
  const dayTaskIds = new Set(preview.provenance.task_ids)
  if (!isNaturalAscending(catalog.items.map((item) => item.capture_id))) {
    refuseContext(ctx, 'context_catalog items are not in unique natural Capture id order', ['items'])
  }
  catalog.items.forEach((item, index) => {
    if (!isNaturalAscending(item.linked_task_ids)) {
      refuseContext(ctx, 'linked_task_ids are not unique and natural-sorted', ['items', index, 'linked_task_ids'])
    }
    if (item.linked_task_ids.some((taskId) => !dayTaskIds.has(taskId))) {
      refuseContext(ctx, 'linked_task_ids must intersect this preview\'s provenance tasks', ['items', index, 'linked_task_ids'])
    }
  })
  if (catalog.omitted_count > 0 && catalog.items.length !== MAX_DAILY_PREVIEW_CONTEXT_ITEMS) {
    refuseContext(ctx, 'omitted_count is only possible once the item bound is reached', ['omitted_count'])
  }
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
  // Absent on servers before R44; the daily preview stays fully usable without it.
  context_catalog: contextCatalogSchema.optional(),
}).strict().superRefine((value, ctx) => {
  if (value.context_catalog) refineContextCatalog(value.context_catalog, value.preview, ctx)
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
