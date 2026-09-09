import { z } from 'zod'
import { captureSchema } from '../domain/schemas'
import type { Capture } from '../domain/types'
import { ApiError, mutateData, type MutateReceipt } from './transport'

/**
 * The one explicit inverse of Capture linking, exactly as
 * `R22-CAPTURE-UNLINK-CONTRACT.md` publishes it. One POST per explicit intent.
 *
 * Deliberate boundaries, all of them the contract's own:
 *
 * - **The body is the two published fields and nothing else.** `task_id` names the link
 *   to drop; `revision` is the Capture revision the reader is looking at. There is no
 *   field here for a status, a link list or a Task revision: the owner derives every
 *   other effect from its own stored record.
 * - **The displayed revision is sent, never a freshly fetched one.** A stale view is a
 *   `409 revision_conflict` that writes nothing, which is the entire point of the CAS.
 *   This module never re-reads the Capture to retry with a newer revision.
 * - **The caller owns the idempotency key.** It is a required argument, because after an
 *   ambiguous outcome the same intent must be retried under the same key — a fresh key
 *   would be a second intent, and the server would be right to apply it twice.
 * - **Ambiguity is a first-class outcome.** A network failure, an unreadable success
 *   body, an unpublished success status or a body that does not parse all mean the write
 *   may have happened. They are classified `ambiguous`, never as a refusal, and never as
 *   a removal.
 * - **No backend prose reaches the screen.** Callers render
 *   `describeCaptureUnlinkFailure`, a closed table keyed by the codes this route can
 *   answer with. An unrecognised code falls back to one fixed sentence.
 *
 * The wire literals below are restated from the contract rather than imported from the
 * Python package: the frontend cannot depend on it.
 */

export const CAPTURE_UNLINK_METHOD = 'POST' as const

export function captureUnlinkPath(captureId: string): string {
  return `/api/v1/captures/${encodeURIComponent(captureId)}/unlink`
}

/** A Capture revision is a nonnegative safe integer. `z.number()` refuses a boolean. */
export const captureRevisionSchema = z
  .number()
  .int()
  .nonnegative()
  .max(Number.MAX_SAFE_INTEGER)

/** Exactly `{task_id, revision}`. A stray key is a request this module refuses to send. */
export const captureUnlinkRequestSchema = z
  .object({ task_id: z.string().min(1), revision: captureRevisionSchema })
  .strict()

/**
 * `meta.duplicate` is read; other meta keys are left alone. The existing idempotency
 * receipt persistence is allowed to add its own, and refusing those would turn a
 * published success into a false ambiguity.
 */
export const captureUnlinkMetaSchema = z.object({ duplicate: z.boolean() })

/**
 * R33: the OPTIONAL Undo hint the owner adds to a mutating unlink, in its canonical
 * lowercase 8-4-4-4-12 form.
 *
 * It is read entirely separately from the required legacy metadata above, and the
 * separation is the point: an older owner returns no hint at all, and a malformed one is
 * a hint this client cannot use. Neither may turn a removal the server already confirmed
 * into an unknown outcome, so a failed read here yields `undefined` and nothing else.
 */
export const captureUndoReceiptIdSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)

function readUndoReceiptId(meta: unknown): string | undefined {
  if (typeof meta !== 'object' || meta === null || Array.isArray(meta)) return undefined
  if (!Object.hasOwn(meta, 'undo_receipt_id')) return undefined
  const parsed = captureUndoReceiptIdSchema.safeParse((meta as Record<string, unknown>).undo_receipt_id)
  return parsed.success ? parsed.data : undefined
}

export interface CaptureUnlinkInput {
  captureId: string
  taskId: string
  /** The revision currently displayed on the card, not a re-read one. */
  revision: number
  /** Stable for one intent: an ambiguous retry MUST reuse it. */
  idempotencyKey: string
}

export interface CaptureUnlinkOutcome {
  capture: Capture
  /** True when the link was already absent at the correct revision: nothing changed. */
  duplicate: boolean
  /**
   * R33: the owner's canonical Undo receipt hint, when it returned a readable one.
   *
   * Absent for a legacy owner, and reported as-read for a duplicate: whether a hint may
   * be turned into an offer is a decision for the feature that also knows what the reader
   * was shown, not for this wire reader.
   */
  undoReceiptId?: string
}

/** Client-side codes, kept in the same closed table as the contract's server codes. */
export const UNLINK_UNREADABLE = 'capture_unlink_unreadable'
export const UNLINK_NOT_AVAILABLE = 'capture_unlink_not_available'

export class CaptureUnlinkError extends Error {
  readonly code: string
  readonly cause: unknown

  constructor(code: string, cause?: unknown) {
    super(describeCaptureUnlinkFailure(code))
    this.name = 'CaptureUnlinkError'
    this.code = code
    this.cause = cause
  }
}

/**
 * Every sentence this surface can put on screen.
 *
 * The table has a null prototype on purpose: on an ordinary object literal a backend
 * code of `constructor`, `toString` or `__proto__` resolves through `Object.prototype`,
 * so both a membership test and a lookup hand back an inherited function instead of the
 * one fixed fallback sentence. The helpers below additionally test own properties only.
 */
export const CAPTURE_UNLINK_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    revision_conflict:
      'This capture changed since it was shown, so nothing was removed. Refresh the Task and try again.',
    not_found: 'This capture or Task is no longer available, so nothing was removed.',
    idempotency_conflict:
      'A different request already used this attempt, so nothing was removed. Refresh the Task and try again.',
    invalid_body: 'The server refused this request, so nothing was removed.',
    invalid_request: 'The server refused this request, so nothing was removed.',
    [UNLINK_NOT_AVAILABLE]: 'This card cannot be removed as a task link.',
    [UNLINK_UNREADABLE]:
      'Work Stack could not confirm whether the task link was removed. Try the same request again to settle it without repeating the change.',
  }),
)

export const CAPTURE_UNLINK_FALLBACK_MESSAGE =
  'The task link could not be removed, and nothing was changed.'

/** Own properties only: no backend code can reach an inherited `Object.prototype` member. */
function hasClosedMessage(code: string): boolean {
  return Object.hasOwn(CAPTURE_UNLINK_MESSAGES, code)
}

export function describeCaptureUnlinkFailure(code: string): string {
  const message = hasClosedMessage(code) ? CAPTURE_UNLINK_MESSAGES[code] : undefined
  // Whatever a lookup produced, only one of this module's own literal sentences returns.
  return typeof message === 'string' ? message : CAPTURE_UNLINK_FALLBACK_MESSAGE
}

/**
 * `refused` and `conflict` are determinate: the server answered, and it wrote nothing.
 * `ambiguous` is the unknown outcome — the same intent may be retried under its own key,
 * and nothing may be shown as removed.
 */
export type CaptureUnlinkFailureKind = 'conflict' | 'refused' | 'ambiguous'

export interface CaptureUnlinkFailure {
  kind: CaptureUnlinkFailureKind
  code: string
  message: string
}

function failure(kind: CaptureUnlinkFailureKind, code: string): CaptureUnlinkFailure {
  return { kind, code, message: describeCaptureUnlinkFailure(code) }
}

/**
 * Maps any thrown value onto the closed copy table and one of the three outcomes.
 *
 * A 5xx is ambiguous, not a refusal: the taxonomy that produces this route's determinate
 * answers ends at 409, so a server-error status is an unhandled path whose write state
 * this client cannot claim to know.
 */
export function classifyCaptureUnlinkFailure(error: unknown): CaptureUnlinkFailure {
  if (error instanceof CaptureUnlinkError) {
    return failure(error.code === UNLINK_UNREADABLE ? 'ambiguous' : 'refused', error.code)
  }
  if (error instanceof ApiError) {
    if (error.status >= 500) return failure('ambiguous', UNLINK_UNREADABLE)
    if (error.code === 'revision_conflict') return failure('conflict', 'revision_conflict')
    return failure('refused', hasClosedMessage(error.code) ? error.code : 'capture_unlink_refused')
  }
  return failure('ambiguous', UNLINK_UNREADABLE)
}

/**
 * POST one explicit link removal.
 *
 * The caller is expected to have taken its own synchronous lock before awaiting this:
 * the CSRF preflight inside the shared transport is a real await, and two clicks that
 * both reached it would be two POSTs. The transport's own network retry and its 403
 * resend both replay the identical body under the identical `Idempotency-Key`, which is
 * what makes them a retry of this intent rather than a new one.
 */
export async function unlinkCaptureTask(input: CaptureUnlinkInput): Promise<CaptureUnlinkOutcome> {
  if (!input.captureId || !input.idempotencyKey) throw new CaptureUnlinkError(UNLINK_NOT_AVAILABLE)
  const parsed = captureUnlinkRequestSchema.safeParse({
    task_id: input.taskId,
    revision: input.revision,
  })
  // A malformed intent never reaches the network, and never counts as ambiguous:
  // no request was made, so nothing can have been written.
  if (!parsed.success) throw new CaptureUnlinkError(UNLINK_NOT_AVAILABLE, parsed.error)

  const receipt: MutateReceipt = {}
  let capture: Capture
  try {
    capture = await mutateData(
      captureUnlinkPath(input.captureId),
      CAPTURE_UNLINK_METHOD,
      parsed.data,
      captureSchema,
      input.idempotencyKey,
      true,
      { receipt },
    )
  } catch (error) {
    // A determinate server refusal keeps its own code. Everything else — a failed
    // fetch, an unreadable 2xx body, a projection that does not parse — happened
    // around a request that may well have committed.
    if (error instanceof ApiError) throw error
    throw new CaptureUnlinkError(UNLINK_UNREADABLE, error)
  }
  // R22 publishes one success status and one only. The shared transport accepts every
  // `Response.ok` status, so the route checks its own; a 201/202 means something other
  // than "this removal is settled".
  if (receipt.status !== 200) throw new CaptureUnlinkError(UNLINK_UNREADABLE)
  const meta = captureUnlinkMetaSchema.safeParse(receipt.meta ?? {})
  if (!meta.success) throw new CaptureUnlinkError(UNLINK_UNREADABLE, meta.error)
  return {
    capture,
    duplicate: meta.data.duplicate,
    undoReceiptId: readUndoReceiptId(receipt.meta),
  }
}
