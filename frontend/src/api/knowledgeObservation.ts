import { z } from 'zod'
import {
  ApiError,
  UnreadableSuccessError,
  assertOk,
  fetchWithNetworkRetry,
  getCsrfToken,
  mutateData,
  type MutateReceipt,
} from './transport'
import { MAX_EVIDENCE_ITEMS, canonicalUuidSchema } from '../domain/schemaKnowledgeCapture'
import {
  CAPTURE_ID_PATTERN,
  KnowledgeVerificationError,
  MAX_CAPTURE_REVISION,
  VERIFICATION_NOT_CHECKABLE,
  bindingMatchesIdentity,
  classifyKnowledgeVerificationFailure,
  describeKnowledgeVerificationError,
  evidenceMatchesExpected,
  isCalendarRfc3339,
  verificationBindingSchema,
  verificationResultSchema,
  type CaptureIdentity,
  type ExpectedEvidence,
} from './knowledgeVerification'

/**
 * The two saved-source-check routes, exactly as `R27-HTTP-UI-CONTRACT.md` sections D and
 * E publish them. One historical GET per opened view, one POST per explicit click.
 *
 * What this module is careful about, all of it the contract's own:
 *
 * - **A saved observation is history, never a current claim.** The wire says when a check
 *   was accepted and what it saw *then*. `binding_state` is the owner's own verdict on
 *   whether the capture it was taken against still agrees; `changed` carries no result at
 *   all, so there is nothing here that could overlay stale evidence on a moved binding.
 * - **The GET is a read and only a read.** It sends the same three identity fields as
 *   query parameters, on the exact canonical path, and it never reaches a provider. It is
 *   the one network call this surface makes without a click.
 * - **The POST is one child process.** `singleAttempt` gives up the transport's network
 *   retry and its 403 resend for the same reason `verifyCaptureSources` does: a second
 *   POST would be a second source check, not a retry of the first. Nothing in this module
 *   falls back to another route, and nothing retries after a failure.
 * - **A well-formed answer about something else is a refusal.** Both calls re-check the
 *   returned binding against the capture the reader is looking at, and an `unchanged`
 *   observation additionally has to line up positionally with the evidence rows currently
 *   rendered — same length, same order, same document ref, type and expected version.
 * - **No backend prose, no wire values on screen.** Callers render
 *   `describeKnowledgeObservationError`, a closed table with a null prototype, and fall
 *   through to R21's existing closed sentences for the error codes both routes share.
 *
 * The wire literals are restated from the contract rather than imported from the Python
 * package, for the same reason `knowledgeVerification` restates R21's.
 */

export const KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH = '/api/v1/knowledge/captures/record-check'
export const KNOWLEDGE_CAPTURE_OBSERVATION_PATH = '/api/v1/knowledge/captures/observation'
export const OBSERVATION_RECORDED_OUTCOME = 'observation_recorded'
export const OBSERVATION_READY_OUTCOME = 'observation_ready'

/** The owner's two verdicts about the capture the saved check was taken against. */
export const OBSERVATION_BINDING_STATES = ['unchanged', 'changed'] as const
export type ObservationBindingState = (typeof OBSERVATION_BINDING_STATES)[number]

const observedAt = z.string().max(64).refine(isCalendarRfc3339, 'invalid_timestamp')

/**
 * One retained observation.
 *
 * The two invariants below are the contract's, and they are checked here rather than
 * trusted: `unchanged` must carry the original result and must agree with it about when
 * the check happened, and `changed` must carry no result at all. A body that says
 * `changed` while handing over evidence would otherwise let a revoked or re-pointed
 * capture be decorated with the statuses of the source it used to have.
 */
export const savedObservationSchema = z.object({
  accepted_at: observedAt,
  checked_at: observedAt,
  binding_state: z.enum(OBSERVATION_BINDING_STATES),
  result: verificationResultSchema.nullable(),
}).strict().superRefine((observation, context) => {
  if (observation.binding_state === 'unchanged') {
    if (observation.result === null) {
      context.addIssue({ code: 'custom', path: ['result'], message: 'An unchanged observation keeps its result.' })
      return
    }
    if (observation.result.checked_at !== observation.checked_at) {
      context.addIssue({ code: 'custom', path: ['checked_at'], message: 'The observation disagrees with its own result.' })
    }
    return
  }
  if (observation.result !== null) {
    context.addIssue({ code: 'custom', path: ['result'], message: 'A changed binding carries no evidence.' })
  }
})

export const observationDataSchema = z.object({
  binding: verificationBindingSchema,
  observation: savedObservationSchema.nullable(),
}).strict()

export const observationReadyMetaSchema = z.object({
  outcome: z.literal(OBSERVATION_READY_OUTCOME),
}).strict()

export const observationRecordedMetaSchema = z.object({
  outcome: z.literal(OBSERVATION_RECORDED_OUTCOME),
}).strict()

/** Whole published bodies, top level included — see `strictEnvelope` in the transport. */
export const observationReadyEnvelopeSchema = z.object({
  data: observationDataSchema,
  meta: observationReadyMetaSchema,
}).strict()

export const observationRecordedEnvelopeSchema = z.object({
  data: observationDataSchema,
  meta: observationRecordedMetaSchema,
}).strict()

export type SavedObservation = z.infer<typeof savedObservationSchema>
export type ObservationOutcome = z.infer<typeof observationDataSchema>

/** A POST that succeeded has an observation, and it is always an `unchanged` one. */
export interface RecordedObservation {
  binding: ObservationOutcome['binding']
  observation: SavedObservation & { binding_state: 'unchanged' }
}

export interface CaptureObservationInput extends CaptureIdentity {
  expectedEvidence: readonly ExpectedEvidence[]
}

/** A refusal this module raised itself. The code is one of the closed codes below. */
export class KnowledgeObservationError extends Error {
  readonly code: string

  constructor(code: string) {
    super(describeKnowledgeObservationError(code))
    this.name = 'KnowledgeObservationError'
    this.code = code
  }
}

/** Client-side codes, kept in the same closed table as the contract's server codes. */
export const OBSERVATION_UNREADABLE = 'observation_unreadable'
export const OBSERVATION_BINDING_MISMATCH = 'observation_binding_mismatch'
export const OBSERVATION_HISTORY_UNAVAILABLE = 'observation_history_unavailable'
/**
 * The one old-server diagnosis. It is raised for a 404 whose error code is exactly
 * `not_found` — the answer an owner that never published this route gives — and for
 * nothing else. An `unknown_capture` 404, a 503 or an unreadable body says something
 * about this capture or this moment, not about the server's age, so none of them may
 * quietly send the surface back to the old readonly route.
 */
export const OBSERVATION_HISTORY_UNSUPPORTED = 'observation_history_unsupported'

/**
 * Every sentence this surface can put on screen for a saved check. Null prototype and
 * own-property lookups, for the reason `KNOWLEDGE_VERIFICATION_MESSAGES` gives: a backend
 * code of `constructor` or `toString` must reach a fixed sentence, not an inherited
 * function.
 *
 * `observation_save_unknown` is the delicate one. The owner reports it when a check ran
 * and the save could not be confirmed, which is not the same as knowing nothing was
 * written — so the sentence says the save is unconfirmed and points at the reload, and it
 * never tells the reader the check was discarded.
 */
export const KNOWLEDGE_OBSERVATION_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    observation_save_unknown:
      'The source check ran, but saving it could not be confirmed. Reload the saved check to see what was kept.',
    observation_read_unavailable: 'The saved source check could not be read.',
    [OBSERVATION_UNREADABLE]: 'The saved source check could not be read, so nothing was shown.',
    [OBSERVATION_BINDING_MISMATCH]: 'The saved check did not match this capture, so nothing was shown.',
    [OBSERVATION_HISTORY_UNAVAILABLE]: 'Saved source checks could not be loaded from this server.',
    [OBSERVATION_HISTORY_UNSUPPORTED]: 'Saved checks are unavailable on this server.',
  }),
)

function hasClosedMessage(code: string): boolean {
  return Object.hasOwn(KNOWLEDGE_OBSERVATION_MESSAGES, code)
}

/**
 * This table first, then R21's. Both routes share the owner's released verification error
 * codes — `unknown_capture`, `verification_busy`, `capture_revision_changed` and the rest
 * — and those already have reviewed sentences; re-spelling them here would be two copies
 * of the same copy. An unknown code still lands on R21's one fixed fallback.
 */
export function describeKnowledgeObservationError(code: string): string {
  const message = hasClosedMessage(code) ? KNOWLEDGE_OBSERVATION_MESSAGES[code] : undefined
  return typeof message === 'string' ? message : describeKnowledgeVerificationError(code)
}

/** Maps any thrown value onto the closed copy above. Backend prose never survives this. */
export function classifyKnowledgeObservationFailure(error: unknown): { code: string; message: string } {
  if (error instanceof KnowledgeObservationError) return { code: error.code, message: error.message }
  if (error instanceof KnowledgeVerificationError) return { code: error.code, message: error.message }
  if (error instanceof ApiError && hasClosedMessage(error.code)) {
    return { code: error.code, message: describeKnowledgeObservationError(error.code) }
  }
  // A malformed success body is this surface's own refusal, not R21's transient one.
  if (error instanceof UnreadableSuccessError || error instanceof z.ZodError) {
    return { code: OBSERVATION_UNREADABLE, message: describeKnowledgeObservationError(OBSERVATION_UNREADABLE) }
  }
  return classifyKnowledgeVerificationFailure(error)
}

const identitySchema = z.object({
  workspace_uid: canonicalUuidSchema,
  capture_id: z.string().regex(CAPTURE_ID_PATTERN),
  capture_revision: z.number().int().min(0).max(MAX_CAPTURE_REVISION),
}).strict()

function identityOf(input: CaptureObservationInput) {
  return identitySchema.parse({
    workspace_uid: input.workspaceUid,
    capture_id: input.captureId,
    capture_revision: input.captureRevision,
  })
}

/**
 * The returned observation has to be about the capture on screen, and an `unchanged` one
 * has to be about the rows on screen. Position is the only thing that binds a saved
 * status to a row, so the comparison is positional and duplicates count.
 */
function admitObservation(data: ObservationOutcome, input: CaptureObservationInput): ObservationOutcome {
  if (!bindingMatchesIdentity(data.binding, input)) {
    throw new KnowledgeObservationError(OBSERVATION_BINDING_MISMATCH)
  }
  const { observation } = data
  if (observation !== null && observation.result !== null
    && !evidenceMatchesExpected(observation.result.evidence, input.expectedEvidence)) {
    throw new KnowledgeObservationError(OBSERVATION_BINDING_MISMATCH)
  }
  return data
}

/**
 * GET the saved observation for one capture.
 *
 * The target is built here rather than interpolated: three single-valued keys, ordinary
 * URL encoding, the revision as canonical base-10 digits, no fragment and no extras. The
 * request carries the same-origin session and nothing else — no CSRF preflight, because
 * this is a read, and no `Idempotency-Key`, because there is nothing to replay.
 */
export async function fetchCaptureObservation(
  input: CaptureObservationInput,
): Promise<ObservationOutcome> {
  const identity = identityOf(input)
  const query = new URLSearchParams({
    workspace_uid: identity.workspace_uid,
    capture_id: identity.capture_id,
    capture_revision: String(identity.capture_revision),
  })
  const response = await fetchWithNetworkRetry(`${KNOWLEDGE_CAPTURE_OBSERVATION_PATH}?${query.toString()}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  let payload: unknown
  try {
    payload = await assertOk(response)
  } catch (error) {
    // The single old-server signal. Everything else — including a 404 that names this
    // capture rather than this route — stays the error it is.
    if (error instanceof ApiError && error.status === 404 && error.code === 'not_found') {
      throw new KnowledgeObservationError(OBSERVATION_HISTORY_UNSUPPORTED)
    }
    throw error
  }
  // The contract publishes one success status. A 204 or a 206 with a well-formed body is
  // not "this history is ready", and `Response.ok` alone would have accepted both.
  if (response.status !== 200) throw new KnowledgeObservationError(OBSERVATION_UNREADABLE)
  const envelope = observationReadyEnvelopeSchema.parse(payload)
  return admitObservation(envelope.data, input)
}

/**
 * POST one explicit owner check and keep what it saved.
 *
 * The caller takes its own synchronous lock before awaiting this: the CSRF preflight is a
 * real await, and two clicks that both reached it would be two child processes on the
 * server. There is no retry and no fallback here — a second check is a new check, and it
 * is the reader's to ask for.
 */
export async function recordCaptureSourceCheck(
  input: CaptureObservationInput,
): Promise<RecordedObservation> {
  if (input.expectedEvidence.length < 1 || input.expectedEvidence.length > MAX_EVIDENCE_ITEMS) {
    throw new KnowledgeVerificationError(VERIFICATION_NOT_CHECKABLE)
  }
  const body = identityOf(input)
  // Read-only session refresh so an explicit click picks up a rotated CSRF nonce. The
  // check itself stays single-attempt: one POST per click, no automatic resend.
  await getCsrfToken(true)
  const receipt: MutateReceipt = {}
  const data = await mutateData(
    KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH,
    'POST',
    body,
    observationDataSchema,
    undefined,
    false,
    { receipt, singleAttempt: true, strictEnvelope: observationRecordedEnvelopeSchema },
  )
  if (receipt.status !== 200) throw new KnowledgeObservationError(OBSERVATION_UNREADABLE)
  observationRecordedMetaSchema.parse(receipt.meta ?? {})
  const admitted = admitObservation(data, input)
  const { observation } = admitted
  // A confirmed save answers with the observation it saved, against a binding it has just
  // reconfirmed. `null`, or a `changed` verdict, is not a recorded check.
  if (observation === null || observation.binding_state !== 'unchanged' || observation.result === null) {
    throw new KnowledgeObservationError(OBSERVATION_UNREADABLE)
  }
  return { binding: admitted.binding, observation: { ...observation, binding_state: 'unchanged' } }
}
