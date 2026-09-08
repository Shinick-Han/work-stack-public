import { z } from 'zod'

/**
 * D5 transition schemas. These are deliberately separate from the unchanged
 * worklogEntrySchema: known metadata is strict, so unknown fields refuse rather
 * than being coerced or dropped.
 */
const CHECKPOINT_ID = /^CP-[0-9a-f]{64}$/
const ENTRY_DIGEST = /^sha256:[0-9a-f]{64}$/
const TASK_ID = /^T-[0-9]{4,}$/
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
/**
 * Canonical lowercase with the RFC variant 10xx. The frozen domain places no
 * restriction on the version nibble, so 0, 9, a and f are all legitimate.
 */
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const workspaceUidSchema = z.string()
  .regex(UUID_SHAPE, 'not a canonical RFC variant UUID')
  .refine((value) => value !== NIL_UUID, 'the nil UUID is not a workspace')
const SUPERSEDE_REASONS = ['incorrect', 'duplicate', 'obsolete'] as const
const RESTORE_REASONS = ['restore'] as const
const REASON_CODES = [...SUPERSEDE_REASONS, ...RESTORE_REASONS] as const

/**
 * The Python str.strip() whitespace set: str.isspace() holds for the ASCII
 * controls U+001C..U+001F and U+0085 as well as the Unicode space classes.
 * JavaScript trim() differs at both ends, so it is deliberately not used.
 */
const PY_SPACE = '\t\n\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000'
const PY_STRIPPED = new RegExp(`^[^${PY_SPACE}]([\\s\\S]*[^${PY_SPACE}])?$`)

/** Strict UTF-8: an unpaired surrogate cannot be encoded at all. */
function encodesAsUtf8(value: string): boolean {
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value)
}

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

const knownDateSchema = z.string().refine(isRealDate, 'not a real calendar date')

/**
 * A persisted explanation already passed the Python strip, the codepoint bound
 * and the UTF-8 bound. The UI validates exactly that and never renormalizes:
 * NFC and NFD are both retained, and U+FEFF is not whitespace to Python.
 */
const persistedExplanationSchema = z.string()
  .refine((value) => PY_STRIPPED.test(value), 'explanation is not Python-stripped')
  .refine((value) => {
    const points = [...value].length
    return points >= 1 && points <= 240
  }, 'explanation is outside 1..240 codepoints')
  .refine(encodesAsUtf8, 'explanation is not valid UTF-8')
  .refine(
    (value) => new TextEncoder().encode(value).length <= 1024,
    'explanation exceeds the UTF-8 byte bound',
  )

const transitionReasonSchema = z.object({
  code: z.enum(REASON_CODES),
  explanation: persistedExplanationSchema,
}).strict()

export const checkpointTransitionEventSchema = z.object({
  type: z.enum(['worklog.superseded', 'worklog.restored']),
  workspace_uid: workspaceUidSchema,
  task_id: z.string().regex(TASK_ID),
  checkpoint_id: z.string().regex(CHECKPOINT_ID),
  date: knownDateSchema,
  ordinal: z.number().int().nonnegative(),
  entry_digest: z.string().regex(ENTRY_DIGEST),
  state: z.enum(['superseded', 'active']),
  revision: z.number().int().positive(),
  reason: transitionReasonSchema,
  origin: z.literal('agent-cli-v1').nullable(),
}).strict().superRefine((event, context) => {
  const fail = (message: string) => context.addIssue({ code: 'custom', message })
  if ((event.type === 'worklog.superseded') !== (event.state === 'superseded')) {
    fail('transition type and resulting state disagree')
  }
  // A supersede cannot carry a restore reason, nor a restore a supersede one.
  const allowed: readonly string[] = event.state === 'superseded'
    ? SUPERSEDE_REASONS
    : RESTORE_REASONS
  if (!allowed.includes(event.reason.code)) fail('reason code does not match the resulting state')
  // Durable parity: odd revisions supersede, even revisions restore.
  const expected = event.revision % 2 === 1 ? 'superseded' : 'active'
  if (event.state !== expected) fail('state and revision parity disagree')
})

/** The recorded fact is known metadata, not opaque: exactly eight fields. */
const checkpointRecordedFactSchema = z.object({
  type: z.literal('worklog.recorded'),
  workspace_uid: workspaceUidSchema,
  task_id: z.string().regex(TASK_ID),
  checkpoint_id: z.string().regex(CHECKPOINT_ID),
  date: knownDateSchema,
  ordinal: z.number().int().nonnegative(),
  entry_digest: z.string().regex(ENTRY_DIGEST),
  origin: z.literal('agent-cli-v1').nullable(),
}).strict()

const checkpointAuditLocatorSchema = z.object({
  workspace_uid: workspaceUidSchema,
  task_id: z.string().regex(TASK_ID).nullable(),
  date: knownDateSchema,
  ordinal: z.number().int().nonnegative(),
  entry_digest: z.string().regex(ENTRY_DIGEST).nullable(),
}).strict()

type AuditFail = (message: string) => void
type AuditEntryShape = {
  locator: { workspace_uid: string; task_id: string | null; date: string; ordinal: number; entry_digest: string | null }
  checkpoint_id: string | null
  recorded: { workspace_uid: string; checkpoint_id: string; task_id: string; date: string; ordinal: number; entry_digest: string } | null
  state: 'active' | 'superseded'
  revision: number
  transitions: { revision: number; checkpoint_id: string; workspace_uid: string; task_id: string; date: string; ordinal: number; entry_digest: string; state: string }[]
}

/** A legacy row has no checkpoint identity, no recorded fact and no history. */
function checkRecordedBinding(entry: AuditEntryShape, fail: AuditFail): void {
  const { locator, recorded } = entry
  if ((entry.checkpoint_id === null) !== (recorded === null)) {
    fail('checkpoint identity and recorded fact disagree')
  }
  if (entry.checkpoint_id === null && entry.transitions.length > 0) {
    fail('a legacy entry cannot carry transitions')
  }
  if (recorded === null) return
  if (locator.task_id === null) fail('a recorded entry needs its Task locator')
  if (locator.entry_digest === null) fail('a recorded entry needs its digest locator')
  if (recorded.workspace_uid !== locator.workspace_uid) fail('recorded is bound to another workspace')
  if (recorded.checkpoint_id !== entry.checkpoint_id) fail('recorded is bound to another checkpoint')
  if (recorded.task_id !== locator.task_id) fail('recorded is bound to another Task')
  if (recorded.date !== locator.date) fail('recorded is bound to another day')
  if (recorded.ordinal !== locator.ordinal) fail('recorded is bound to another slot')
  if (recorded.entry_digest !== locator.entry_digest) fail('recorded is bound to another entry')
}

/** Contiguous revisions 1..n, each bound to this slot, ending in the state. */
function checkHistory(entry: AuditEntryShape, fail: AuditFail): void {
  const { locator } = entry
  if (entry.transitions.length !== entry.revision) fail('history is incomplete')
  entry.transitions.forEach((transition, index) => {
    if (transition.revision !== index + 1) fail('history revisions are not contiguous')
    if (transition.checkpoint_id !== entry.checkpoint_id) fail('transition is bound to another checkpoint')
    if (transition.workspace_uid !== locator.workspace_uid) fail('transition is bound to another workspace')
    if (transition.task_id !== locator.task_id) fail('transition is bound to another Task')
    if (transition.date !== locator.date) fail('transition is bound to another day')
    if (transition.ordinal !== locator.ordinal) fail('transition is bound to another slot')
    if (transition.entry_digest !== locator.entry_digest) fail('transition is bound to another entry')
  })
  const last = entry.transitions.at(-1)
  if (last && last.state !== entry.state) fail('current state disagrees with the last transition')
  if (!last && entry.state !== 'active') fail('an entry with no history must be active')
}

export const checkpointAuditEntrySchema = z.object({
  locator: checkpointAuditLocatorSchema,
  checkpoint_id: z.string().regex(CHECKPOINT_ID).nullable(),
  // Opaque by contract: preserved by identity, never validated or normalized.
  entry: z.unknown(),
  recorded: checkpointRecordedFactSchema.nullable(),
  state: z.enum(['active', 'superseded']),
  revision: z.number().int().nonnegative(),
  transitions: z.array(checkpointTransitionEventSchema),
}).strict().superRefine((entry, context) => {
  const fail = (message: string) => context.addIssue({ code: 'custom', message })
  checkRecordedBinding(entry, fail)
  checkHistory(entry, fail)
})

export const checkpointAuditSchema = z.object({
  workspace_uid: workspaceUidSchema,
  entries: z.array(checkpointAuditEntrySchema),
}).strict().superRefine((audit, context) => {
  const fail = (message: string) => context.addIssue({ code: 'custom', message })
  const slots = new Set<string>()
  const checkpoints = new Set<string>()
  for (const entry of audit.entries) {
    if (entry.locator.workspace_uid !== audit.workspace_uid) {
      fail('an entry belongs to a foreign workspace')
    }
    // The physical slot is global to the workspace and day, whichever Task
    // happens to occupy it.
    const slot = `${entry.locator.workspace_uid}|${entry.locator.date}|${entry.locator.ordinal}`
    if (slots.has(slot)) fail('two entries occupy the same physical slot')
    slots.add(slot)
    if (entry.checkpoint_id !== null) {
      if (checkpoints.has(entry.checkpoint_id)) fail('a checkpoint identity is duplicated')
      checkpoints.add(entry.checkpoint_id)
    }
  }
})
