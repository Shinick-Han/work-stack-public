import { z } from 'zod'

export const REPORT_LIST_PAGE_SIZE = 50
export const MAX_REPORT_MARKDOWN_CHARS = 100_000
export const MAX_REPORT_CONTENT_REVISIONS = 20
export const MAX_REPORT_DOCUMENT_REVISION = 9_007_199_254_740_991
export const MAX_REPORT_NOTE_CHARS = 240
export const MAX_REPORT_NOTE_BYTES = 1_024
export const REPORT_TEMPLATE_DAILY_V1 = 'daily-v1' as const
export const REPORT_LIST_FILTERS = ['active', 'archived', 'all'] as const
export const REPORT_STORED_STATES = ['draft', 'finalized', 'archived'] as const
export const REPORT_IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/
export const COMMIT_UNKNOWN_REPORT =
  'The report mutation may have committed. Retry the same request unchanged to verify it without duplication.'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const WORKSPACE_UID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const REPORT_UID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const SOURCE_DIGEST = /^sha256:[0-9a-f]{64}$/
const MOMENT = /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/
const CURSOR = /^[A-Za-z0-9_-]+$/
const FILTER_STATES = {
  active: new Set(['draft', 'finalized']),
  archived: new Set(['archived']),
  all: new Set(['draft', 'finalized', 'archived']),
} as const

export type ReportListFilter = (typeof REPORT_LIST_FILTERS)[number]
export type ReportStoredState = (typeof REPORT_STORED_STATES)[number]

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

function encodesAsUtf8(value: string): boolean {
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value)
}

function codePoints(value: string): number {
  return [...value].length
}

function fail(context: z.RefinementCtx, message: string, path: (string | number)[] = []) {
  context.addIssue({ code: 'custom', message, path })
}

export const reportWorkspaceUidSchema = z.string()
  .regex(WORKSPACE_UID, 'not a canonical RFC variant UUID')
  .refine((value) => value !== NIL_UUID, 'the nil UUID is not a workspace')

export const reportUidSchema = z.string().regex(REPORT_UID, 'not a canonical report UUIDv4')

export const reportIdempotencyKeySchema = z.string().regex(
  REPORT_IDEMPOTENCY_KEY,
  'idempotency key must match [A-Za-z0-9._:-]{8,128}',
)

export const reportListFilterSchema = z.enum(REPORT_LIST_FILTERS)
export const reportStoredStateSchema = z.enum(REPORT_STORED_STATES)

const sourceDigestSchema = z.string().regex(SOURCE_DIGEST, 'source_digest must be sha256:<64 lowercase hex>')
const momentSchema = z.string().regex(MOMENT, 'not a canonical UTC second stamp').refine((value) => {
  return isRealDate(value.slice(0, 10))
}, 'stamp is not a real calendar instant')
const instantSchema = z.string().regex(INSTANT, 'not an RFC 3339 instant').refine((value) => {
  return isRealDate(value.slice(0, 10))
}, 'instant is not a real calendar day')
const markdownSchema = z.string()
  .refine(encodesAsUtf8, 'markdown is not valid UTF-8')
  .refine((value) => codePoints(value) <= MAX_REPORT_MARKDOWN_CHARS, 'markdown exceeds the code-point bound')
const noteSchema = z.string()
  .refine(encodesAsUtf8, 'note is not valid UTF-8')
  .refine((value) => codePoints(value) <= MAX_REPORT_NOTE_CHARS, 'note exceeds the code-point bound')
  .refine((value) => new TextEncoder().encode(value).length <= MAX_REPORT_NOTE_BYTES, 'note exceeds the UTF-8 byte bound')
const nullableNoteSchema = z.union([noteSchema, z.null()])
const revisionSchema = z.number().int().min(1).max(MAX_REPORT_DOCUMENT_REVISION)
const contentRevisionSchema = z.number().int().min(1).max(MAX_REPORT_CONTENT_REVISIONS)

export const reportPeriodSchema = z.object({
  kind: z.literal('day'),
  date: z.string().refine(isRealDate, 'not a real calendar date'),
}).strict()

export const reportContentEntrySchema = z.object({
  content_revision: contentRevisionSchema,
  document_revision: revisionSchema,
  markdown: markdownSchema,
  authored_at: momentSchema,
  note: nullableNoteSchema,
}).strict()

const reportListItemObject = z.object({
  uid: reportUidSchema,
  template: z.literal(REPORT_TEMPLATE_DAILY_V1),
  period: reportPeriodSchema,
  state: reportStoredStateSchema,
  revision: revisionSchema,
  content_revision: contentRevisionSchema,
  source_digest: sourceDigestSchema,
  archived_from_state: z.union([z.enum(['draft', 'finalized']), z.null()]),
  created_at: momentSchema,
  updated_at: momentSchema,
}).strict()

function checkListItem(item: z.infer<typeof reportListItemObject>, allowed: ReadonlySet<string>, context: z.RefinementCtx) {
  if (!allowed.has(item.state)) fail(context, 'list item state is outside the requested filter', ['state'])
  if ((item.state === 'archived') !== (item.archived_from_state !== null)) {
    fail(context, 'archived_from_state does not match stored state', ['archived_from_state'])
  }
  if (item.updated_at < item.created_at) fail(context, 'updated_at precedes created_at', ['updated_at'])
}

function reportListItemSchema(allowed: ReadonlySet<string>) {
  return reportListItemObject.superRefine((item, context) => {
    checkListItem(item, allowed, context)
  })
}

const listCursorSchema = z.string().min(64).max(256).regex(CURSOR, 'cursor is not a canonical opaque token')

export function reportListDataSchema(expected: { workspace_uid: string; state: ReportListFilter }) {
  const allowed = FILTER_STATES[expected.state]
  return z.object({
    workspace_uid: reportWorkspaceUidSchema,
    reports: z.array(reportListItemSchema(allowed)).max(REPORT_LIST_PAGE_SIZE),
    omitted_count: z.number().int().nonnegative(),
    cursor: z.union([listCursorSchema, z.null()]),
  }).strict().superRefine((page, context) => {
    if (page.workspace_uid !== expected.workspace_uid) {
      fail(context, 'workspace_uid does not match the requested workspace', ['workspace_uid'])
    }
    if (page.omitted_count === 0 && page.cursor !== null) {
      fail(context, 'a complete page cannot carry a continuation cursor', ['cursor'])
    }
    if (page.omitted_count > 0 && (page.reports.length !== REPORT_LIST_PAGE_SIZE || page.cursor === null)) {
      fail(context, 'a truncated page must be full and carry a cursor', ['cursor'])
    }
  })
}

function checkHistory(entries: z.infer<typeof reportContentEntrySchema>[], revision: number, context: z.RefinementCtx) {
  let previous = 0
  entries.forEach((entry, index) => {
    if (entry.content_revision !== index + 1) {
      fail(context, 'content_revision values are not contiguous', ['revisions', index, 'content_revision'])
    }
    if (!(previous < entry.document_revision && entry.document_revision <= revision)) {
      fail(context, 'document_revision is not a strictly ascending CAS bound', ['revisions', index, 'document_revision'])
    }
    previous = entry.document_revision
  })
}

export function reportReadDataSchema(expected: { report_uid: string }) {
  return reportListItemObject.extend({
    revisions: z.array(reportContentEntrySchema).min(1).max(MAX_REPORT_CONTENT_REVISIONS),
    source_stale: z.boolean(),
  }).strict().superRefine((document, context) => {
    checkListItem(document, FILTER_STATES.all, context)
    if (document.uid !== expected.report_uid) {
      fail(context, 'uid does not match the requested report', ['uid'])
    }
    checkHistory(document.revisions, document.revision, context)
    const latest = document.revisions.at(-1)
    if (latest && latest.content_revision !== document.content_revision) {
      fail(context, 'content_revision does not match the latest authored body', ['content_revision'])
    }
  })
}

const reportSummaryObject = z.object({
  uid: reportUidSchema,
  workspace_uid: reportWorkspaceUidSchema,
  template: z.literal(REPORT_TEMPLATE_DAILY_V1),
  period: reportPeriodSchema,
  source_digest: sourceDigestSchema,
  source_generated_at: instantSchema,
  state: reportStoredStateSchema,
  revision: revisionSchema,
  archived_from_state: z.union([z.enum(['draft', 'finalized']), z.null()]),
  archived_at: z.union([momentSchema, z.null()]),
  archive_note: nullableNoteSchema,
  created_at: momentSchema,
  updated_at: momentSchema,
}).strict()

function checkSummary(report: z.infer<typeof reportSummaryObject>, context: z.RefinementCtx) {
  const archived = report.state === 'archived'
  if (archived !== (report.archived_from_state !== null)) {
    fail(context, 'archived_from_state does not match stored state', ['archived_from_state'])
  }
  if (archived !== (report.archived_at !== null)) {
    fail(context, 'archived_at does not match stored state', ['archived_at'])
  }
  if (!archived && report.archive_note !== null) {
    fail(context, 'archive_note is only set while archived', ['archive_note'])
  }
  if (report.updated_at < report.created_at) fail(context, 'updated_at precedes created_at', ['updated_at'])
}

function bindOwner(report: { workspace_uid: string; uid: string }, expected: { workspace_uid: string; uid?: string }, context: z.RefinementCtx) {
  if (report.workspace_uid !== expected.workspace_uid) {
    fail(context, 'workspace_uid does not match the requested workspace', ['workspace_uid'])
  }
  if (expected.uid !== undefined && report.uid !== expected.uid) {
    fail(context, 'uid does not match the requested report', ['uid'])
  }
}

/**
 * Every transition the service plans writes `expected_revision + 1`: it refuses
 * any other stored revision before it applies the step, and it never skips one.
 * A replay is answered from the receipt recorded for a byte-identical body, so
 * that answer asserted the same expected_revision and carries the same number.
 * The bound therefore holds on both paths without re-reading the document.
 */
function checkRevisionStep(revision: number, expectedRevision: number, context: z.RefinementCtx) {
  if (revision !== expectedRevision + 1) {
    fail(context, 'revision is not exactly one past the submitted expected_revision', ['revision'])
  }
}

function samePeriod(period: ReportPeriod, expected: ReportPeriod): boolean {
  return period.kind === expected.kind && period.date === expected.date
}

export function reportCreateDataSchema(expected: {
  workspace_uid: string
  template: string
  period: ReportPeriod
  source_digest: string
  source_generated_at: string
  markdown: string
}) {
  return reportSummaryObject.extend({
    content_entry: reportContentEntrySchema,
    source_stale: z.literal(false),
  }).strict().superRefine((data, context) => {
    bindOwner(data, expected, context)
    checkSummary(data, context)
    if (data.state !== 'draft') fail(context, 'a create response must be a draft', ['state'])
    if (data.revision !== 1) fail(context, 'a create response starts at revision 1', ['revision'])
    // The service copies these four straight off the admitted body and refuses
    // the create outright when the day's source moved, so a response naming a
    // different template, period or source is answering some other request.
    if (data.template !== expected.template) {
      fail(context, 'template does not match the request', ['template'])
    }
    if (!samePeriod(data.period, expected.period)) {
      fail(context, 'period does not match the request', ['period'])
    }
    if (data.source_digest !== expected.source_digest) {
      fail(context, 'source_digest does not match the request', ['source_digest'])
    }
    if (data.source_generated_at !== expected.source_generated_at) {
      fail(context, 'source_generated_at does not match the request', ['source_generated_at'])
    }
    if (data.content_entry.content_revision !== 1 || data.content_entry.document_revision !== 1) {
      fail(context, 'create content_entry must be the first authored body', ['content_entry'])
    }
    if (data.content_entry.markdown !== expected.markdown) {
      fail(context, 'create content_entry markdown does not match the request', ['content_entry', 'markdown'])
    }
    if (data.content_entry.note !== null) fail(context, 'create content_entry note must be null', ['content_entry', 'note'])
  })
}

export function reportReviseDataSchema(expected: {
  workspace_uid: string
  uid: string
  expected_revision: number
  markdown: string
  note: string | null
}) {
  return reportSummaryObject.extend({
    content_entry: reportContentEntrySchema,
    reopened: z.boolean(),
    source_stale: z.boolean(),
  }).strict().superRefine((data, context) => {
    bindOwner(data, expected, context)
    checkSummary(data, context)
    checkRevisionStep(data.revision, expected.expected_revision, context)
    // Revising always lands in draft, whether or not it reopened a finalized
    // report, so `reopened` is free to be either value on a draft response.
    if (data.state !== 'draft') fail(context, 'a revise response must be a draft', ['state'])
    if (data.content_entry.document_revision !== data.revision) {
      fail(context, 'content_entry is not aligned with the document revision', ['content_entry', 'document_revision'])
    }
    if (data.content_entry.markdown !== expected.markdown) {
      fail(context, 'revise content_entry markdown does not match the request', ['content_entry', 'markdown'])
    }
    if (data.content_entry.note !== expected.note) {
      fail(context, 'revise content_entry note does not match the request', ['content_entry', 'note'])
    }
  })
}

export function reportFinalizeDataSchema(expected: {
  workspace_uid: string
  uid: string
  expected_revision: number
}) {
  return reportSummaryObject.extend({
    source_stale: z.boolean(),
  }).strict().superRefine((data, context) => {
    bindOwner(data, expected, context)
    checkSummary(data, context)
    checkRevisionStep(data.revision, expected.expected_revision, context)
    if (data.state !== 'finalized') fail(context, 'a finalize response must be finalized', ['state'])
  })
}

export function reportArchiveDataSchema(expected: {
  workspace_uid: string
  uid: string
  expected_revision: number
  note: string | null
}) {
  return reportSummaryObject.strict().superRefine((data, context) => {
    bindOwner(data, expected, context)
    checkSummary(data, context)
    checkRevisionStep(data.revision, expected.expected_revision, context)
    if (data.state !== 'archived') fail(context, 'an archive response must be archived', ['state'])
    if (data.archive_note !== expected.note) {
      fail(context, 'archive_note does not match the request', ['archive_note'])
    }
  })
}

export function reportRestoreDataSchema(expected: {
  workspace_uid: string
  uid: string
  expected_revision: number
}) {
  return reportSummaryObject.strict().superRefine((data, context) => {
    bindOwner(data, expected, context)
    checkSummary(data, context)
    checkRevisionStep(data.revision, expected.expected_revision, context)
    if (data.state === 'archived') fail(context, 'a restore response cannot remain archived', ['state'])
  })
}

export const reportMutationMetaSchema = z.object({
  replayed: z.boolean(),
}).strict()

export type ReportPeriod = z.infer<typeof reportPeriodSchema>
export type ReportContentEntry = z.infer<typeof reportContentEntrySchema>
export type ReportListItem = z.infer<typeof reportListItemObject>
export type ReportListData = z.infer<ReturnType<typeof reportListDataSchema>>
export type ReportReadData = z.infer<ReturnType<typeof reportReadDataSchema>>
export type ReportCreateData = z.infer<ReturnType<typeof reportCreateDataSchema>>
export type ReportReviseData = z.infer<ReturnType<typeof reportReviseDataSchema>>
export type ReportFinalizeData = z.infer<ReturnType<typeof reportFinalizeDataSchema>>
export type ReportArchiveData = z.infer<ReturnType<typeof reportArchiveDataSchema>>
export type ReportRestoreData = z.infer<ReturnType<typeof reportRestoreDataSchema>>
