import { z } from 'zod'
import { ApiError, UnreadableSuccessError, getCsrfToken, mutateData, type MutateReceipt } from './transport'
import {
  MAX_EVIDENCE_ITEMS,
  OPAQUE_REF_PATTERN,
  canonicalUuidSchema,
} from '../domain/schemaKnowledgeCapture'
import type { RetrievalSourceType } from '../domain/types'

/**
 * The one owner verification route, exactly as `R21-VERIFICATION-WIRING-CONTRACT.md`
 * publishes it. One POST per explicit click and nothing else.
 *
 * Deliberate boundaries, all of them the contract's own:
 *
 * - **The browser asserts nothing.** The body carries three fields — the workspace UID,
 *   the capture identifier and the revision the reader is looking at. There is no field
 *   here for a document ref, an expected version, a corpus, a connection alias, a verifier
 *   command or a config path: the owner derives every one of those from stored evidence.
 * - **No `Idempotency-Key`, no response cache.** A check is an observation at a timestamp,
 *   not a write, so there is nothing to replay. `singleAttempt` gives up the transport's
 *   network retry and its 403 resend, because a second POST would be a second child
 *   process, not a retry of the first.
 * - **A result is never authority.** `verifyCaptureSources` re-checks the returned binding
 *   and the returned evidence list against the exact capture and evidence order the caller
 *   passed in. A well-formed answer about some other capture, some other revision or some
 *   other document is a refusal, not a display.
 * - **No backend prose reaches the screen.** Callers render
 *   `describeKnowledgeVerificationError`, a closed table keyed by the contract's own error
 *   codes. An unrecognised code falls back to one fixed sentence.
 *
 * The wire literals below are restated from the contract rather than imported from the
 * Python package: the frontend cannot depend on it, and a closed schema that mirrors a
 * published contract is the right place for the contract's own spelling.
 */

export const KNOWLEDGE_CAPTURE_VERIFY_PATH = '/api/v1/knowledge/captures/verify'
export const KNOWLEDGE_VERIFICATION_SCHEMA_ID = 'workstack.knowledge-verification.v1'
export const KNOWLEDGE_VERIFICATION_OUTCOME = 'verification_ready'

/** `R20-VERIFICATION-CONTRACT.md`: the stored capture identifier grammar. */
export const CAPTURE_ID_PATTERN = /^C-\d{4,}$/
/**
 * RFC3339 with an explicit offset. The server clock is the only observation clock.
 * The groups feed the calendar check below; the accepted shape is unchanged.
 */
export const RFC3339_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-](\d{2}):(\d{2}))$/
/** A capture revision is a nonnegative safe integer, and drawers read small ones. */
export const MAX_CAPTURE_REVISION = Number.MAX_SAFE_INTEGER

/** The closed status vocabulary. Nothing outside this list can render. */
export const VERIFICATION_STATUSES = [
  'current',
  'stale',
  'missing',
  'unavailable',
  'denied',
  'refused',
  'revoked',
  'unverifiable',
] as const

export type VerificationStatus = (typeof VERIFICATION_STATUSES)[number]

/**
 * The only legal status/code pairs. A verifier that reports `current/root_unavailable`
 * or invents a code is refused rather than shown, so a coarse public code can never be
 * widened by the wire.
 */
export const VERIFICATION_STATUS_CODES: Readonly<Record<VerificationStatus, readonly string[]>> = {
  current: ['hash_matched'],
  stale: ['hash_differs'],
  missing: ['file_absent'],
  unavailable: ['root_unavailable'],
  denied: ['access_denied'],
  refused: ['source_refused'],
  revoked: ['mapping_revoked'],
  unverifiable: [
    'no_expected_version',
    'no_origin_verifier',
    'unsupported_source_type',
    'verification_unavailable',
  ],
}

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] as const

/** Gregorian, as RFC3339 requires: every fourth year except centuries not divisible by 400. */
function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
}

function isCalendarDate(year: number, month: number, day: number): boolean {
  if (year < 1 || month < 1 || month > 12) return false
  const days = month === 2 && isLeapYear(year) ? 29 : DAYS_IN_MONTH[month - 1]
  return day >= 1 && day <= days
}

function isClockTime(
  hour: number,
  minute: number,
  second: number,
  offsetHour: number,
  offsetMinute: number,
): boolean {
  // No leap second is admitted. This reads one owner instance's own observation clock,
  // and second 60 on that clock is a malformed timestamp, not a real UTC insertion.
  if (hour > 23 || minute > 59 || second > 59) return false
  // `Z` leaves the offset groups unmatched, so there is nothing further to bound.
  if (Number.isNaN(offsetHour)) return true
  return offsetHour <= 23 && offsetMinute <= 59
}

/**
 * Calendar-valid RFC3339, not merely RFC3339-shaped.
 *
 * `Date.parse` is not a validator on this boundary: it accepts `2026-02-31T10:15:02Z`,
 * reports a finite number and silently normalises it to March 3, so a server that sent a
 * date the calendar does not have would still be rendered as an authoritative
 * `Checked <time>` observation. Every field is therefore range-checked itself — month,
 * day against the actual month length under the Gregorian leap rule, the time components
 * and the offset bounds — and only then is `Date.parse` consulted as a final guard.
 */
export function isCalendarRfc3339(value: string): boolean {
  const match = RFC3339_PATTERN.exec(value)
  if (!match) return false
  const [, year, month, day, hour, minute, second, offsetHour, offsetMinute] = match.map(Number)
  return isCalendarDate(year, month, day)
    && isClockTime(hour, minute, second, offsetHour, offsetMinute)
    && Number.isFinite(Date.parse(value))
}

const opaqueRef = z.string().min(8).max(256).regex(OPAQUE_REF_PATTERN)
const sourceType = z.enum(['notion.page', 'nas.file', 'knowledge.answer'])
const captureId = z.string().regex(CAPTURE_ID_PATTERN)
const captureRevision = z.number().int().min(0).max(MAX_CAPTURE_REVISION)
const observedAt = z.string().max(64).refine(isCalendarRfc3339, 'invalid_timestamp')

function refineEntryPairs(
  entry: { status: string; code: string; expected_source_version: string | null; observed_source_version: string | null },
  context: z.RefinementCtx,
) {
  const codes = VERIFICATION_STATUS_CODES[entry.status as VerificationStatus]
  if (!codes.includes(entry.code)) {
    context.addIssue({ code: 'custom', path: ['code'], message: 'Code is not paired with this status.' })
    return
  }
  const { expected_source_version: expected, observed_source_version: observed, status } = entry
  if (status === 'current' || status === 'stale') {
    if (expected === null || observed === null) {
      context.addIssue({ code: 'custom', path: ['observed_source_version'], message: 'A compared status needs both versions.' })
      return
    }
    // `current` is the only claim on this surface that asserts anything about the
    // live source, so it survives only when the two versions are literally equal.
    if ((expected === observed) !== (status === 'current')) {
      context.addIssue({ code: 'custom', path: ['status'], message: 'Status contradicts the compared versions.' })
    }
    return
  }
  if (observed !== null) {
    context.addIssue({ code: 'custom', path: ['observed_source_version'], message: 'An uncompared status reports no observed version.' })
  }
}

export const verificationEntrySchema = z.object({
  document_ref: opaqueRef,
  source_type: sourceType,
  expected_source_version: opaqueRef.nullable(),
  observed_source_version: opaqueRef.nullable(),
  status: z.enum(VERIFICATION_STATUSES),
  code: z.string().min(1).max(64),
}).strict().superRefine(refineEntryPairs)

export const verificationBindingSchema = z.object({
  workspace_uid: canonicalUuidSchema,
  capture_id: captureId,
  capture_revision: captureRevision,
}).strict()

export const verificationResultSchema = z.object({
  schema: z.literal(KNOWLEDGE_VERIFICATION_SCHEMA_ID),
  verification_id: canonicalUuidSchema,
  checked_at: observedAt,
  evidence: z.array(verificationEntrySchema).min(1).max(MAX_EVIDENCE_ITEMS),
}).strict()

export const verificationDataSchema = z.object({
  binding: verificationBindingSchema,
  result: verificationResultSchema,
}).strict()

export const verificationMetaSchema = z.object({
  outcome: z.literal(KNOWLEDGE_VERIFICATION_OUTCOME),
}).strict()

/**
 * The whole published success body, top level included.
 *
 * The shared transport envelope reads `data` and `meta` and drops anything else, so an
 * unpublished top-level key is already gone by the time this module sees the data. R21
 * publishes exactly these two keys, so this schema is handed to the transport's opt-in
 * `strictEnvelope` and validates the original payload while the extra key is still there.
 */
export const verificationEnvelopeSchema = z.object({
  data: verificationDataSchema,
  meta: verificationMetaSchema,
}).strict()

export type VerificationEvidenceEntry = z.infer<typeof verificationEntrySchema>
export type VerificationBinding = z.infer<typeof verificationBindingSchema>
export type VerificationResult = z.infer<typeof verificationResultSchema>
export type VerificationOutcome = z.infer<typeof verificationDataSchema>

/**
 * What the reader is currently looking at, in the order the capture stores it.
 * Duplicated document refs are ordinary — distinct chunks share a document — so the
 * comparison below is positional and never a set membership test.
 */
export interface ExpectedEvidence {
  document_ref: string
  source_type: RetrievalSourceType
  expected_source_version: string | null
}

/**
 * The three fields that name one capture as the reader is looking at it. Split out of
 * `VerifyCaptureSourcesInput` so the saved-observation routes can be checked against the
 * same identity by the same two comparisons below, rather than by a second copy of them.
 */
export interface CaptureIdentity {
  workspaceUid: string
  captureId: string
  captureRevision: number
}

export interface VerifyCaptureSourcesInput extends CaptureIdentity {
  expectedEvidence: readonly ExpectedEvidence[]
}

/** A refusal this module raised itself. The code is one of the closed codes below. */
export class KnowledgeVerificationError extends Error {
  readonly code: string

  constructor(code: string) {
    super(describeKnowledgeVerificationError(code))
    this.name = 'KnowledgeVerificationError'
    this.code = code
  }
}

/** Client-side codes, kept in the same closed table as the contract's server codes. */
export const VERIFICATION_UNREADABLE = 'verification_unreadable'
export const VERIFICATION_NOT_CHECKABLE = 'verification_not_checkable'
export const VERIFICATION_BINDING_MISMATCH = 'verification_binding_mismatch'

/**
 * Every sentence the verification surface can put on screen. The server's error
 * envelope carries a message field; this surface never renders it, because the copy a
 * reader sees about their own sources must not be attacker- or subprocess-authored.
 *
 * The table has a null prototype on purpose. On an ordinary object literal a backend
 * code of `constructor`, `toString` or `__proto__` is answered by `Object.prototype`, so
 * both a `code in messages` membership test and a `messages[code]` lookup succeed and
 * hand back an inherited function instead of the one fixed fallback sentence. With no
 * prototype there is nothing to inherit, and the two helpers below additionally test own
 * properties only.
 */
export const KNOWLEDGE_VERIFICATION_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    knowledge_verifier_unavailable: 'No source verifier is set up for this workspace, so nothing was checked.',
    verification_busy: 'Another source check is already running. Try again once it finishes.',
    verification_cleanup_unsettled: 'An earlier source check did not finish cleanly. Restart Work Stack before checking again.',
    verification_binding_mismatch: 'The answer did not match this capture, so nothing was accepted.',
    capture_revision_changed: 'This capture changed while the check ran. Reopen it and check again.',
    unknown_capture: 'This capture is no longer available to check.',
    capture_not_verifiable: 'This capture has no stored evidence that can be checked.',
    verification_authority_changed: 'Access to this source changed. A new search is needed before it can be checked.',
    verification_result_refused: 'The verifier returned an answer that could not be accepted.',
    driver_not_started: 'The source verifier could not be started.',
    driver_outcome_unknown: 'The source check ended without a usable answer.',
    invalid_request: 'The source check was rejected as invalid.',
    [VERIFICATION_UNREADABLE]: 'The source check answer could not be read, so nothing was checked.',
    [VERIFICATION_NOT_CHECKABLE]: 'This capture has no stored evidence that can be checked.',
  }),
)

export const KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE = 'The source check could not be completed.'

/** Own properties only: no backend code can reach an inherited `Object.prototype` member. */
function hasClosedMessage(code: string): boolean {
  return Object.hasOwn(KNOWLEDGE_VERIFICATION_MESSAGES, code)
}

export function describeKnowledgeVerificationError(code: string): string {
  const message = hasClosedMessage(code) ? KNOWLEDGE_VERIFICATION_MESSAGES[code] : undefined
  // The `typeof` guard is the last line: whatever a lookup produced, only one of this
  // module's own literal sentences is ever returned.
  return typeof message === 'string' ? message : KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE
}

/** Maps any thrown value onto the closed copy table. Backend prose never survives this. */
export function classifyKnowledgeVerificationFailure(error: unknown): { code: string; message: string } {
  if (error instanceof KnowledgeVerificationError) return { code: error.code, message: error.message }
  const code = error instanceof ApiError && hasClosedMessage(error.code)
    ? error.code
    : null
  if (code) return { code, message: describeKnowledgeVerificationError(code) }
  if (error instanceof ApiError) return { code: error.code, message: KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE }
  if (error instanceof UnreadableSuccessError || error instanceof z.ZodError) {
    return { code: VERIFICATION_UNREADABLE, message: describeKnowledgeVerificationError(VERIFICATION_UNREADABLE) }
  }
  return { code: 'verification_failed', message: KNOWLEDGE_VERIFICATION_FALLBACK_MESSAGE }
}

export function bindingMatchesIdentity(
  binding: VerificationBinding,
  identity: CaptureIdentity,
): boolean {
  return binding.workspace_uid === identity.workspaceUid
    && binding.capture_id === identity.captureId
    && binding.capture_revision === identity.captureRevision
}

/**
 * Positional identity between what the reader is looking at and what came back.
 * Same length, same order, same per-entry document ref, source type and expected
 * version — duplicates included, because position is the only thing that binds an
 * observation to a row.
 */
export function evidenceMatchesExpected(
  entries: readonly VerificationEvidenceEntry[],
  expected: readonly ExpectedEvidence[],
): boolean {
  if (entries.length !== expected.length) return false
  return entries.every((entry, index) => {
    const row = expected[index]
    return entry.document_ref === row.document_ref
      && entry.source_type === row.source_type
      && entry.expected_source_version === row.expected_source_version
  })
}

const requestSchema = z.object({
  workspace_uid: canonicalUuidSchema,
  capture_id: captureId,
  capture_revision: captureRevision,
}).strict()

/**
 * POST one explicit owner check.
 *
 * The caller is expected to have taken its own synchronous lock before awaiting this:
 * the CSRF preflight below is a real await, and two clicks that both reach it would be
 * two child processes on the server, not one retried request.
 */
export async function verifyCaptureSources(
  input: VerifyCaptureSourcesInput,
): Promise<VerificationOutcome> {
  if (input.expectedEvidence.length < 1 || input.expectedEvidence.length > MAX_EVIDENCE_ITEMS) {
    throw new KnowledgeVerificationError(VERIFICATION_NOT_CHECKABLE)
  }
  const body = requestSchema.parse({
    workspace_uid: input.workspaceUid,
    capture_id: input.captureId,
    capture_revision: input.captureRevision,
  })
  // Read-only session refresh so an explicit retry can pick up a rotated CSRF nonce.
  // The check itself stays single-attempt: one POST per click, no automatic resend.
  await getCsrfToken(true)
  const receipt: MutateReceipt = {}
  const data = await mutateData(
    KNOWLEDGE_CAPTURE_VERIFY_PATH,
    'POST',
    body,
    verificationDataSchema,
    undefined,
    false,
    { receipt, singleAttempt: true, strictEnvelope: verificationEnvelopeSchema },
  )
  // R21 publishes one success status and one only. A well-formed body returned as 201 or
  // 202 means something other than "this observation is ready" — the shared transport
  // accepts every `Response.ok` status, so the route checks its own.
  if (receipt.status !== 200) throw new KnowledgeVerificationError(VERIFICATION_UNREADABLE)
  verificationMetaSchema.parse(receipt.meta ?? {})
  if (!bindingMatchesIdentity(data.binding, input)
    || !evidenceMatchesExpected(data.result.evidence, input.expectedEvidence)) {
    throw new KnowledgeVerificationError(VERIFICATION_BINDING_MISMATCH)
  }
  return data
}
