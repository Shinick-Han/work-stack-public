import { z } from 'zod'
import { captureSchema } from '../domain/schemas'
import type { Capture } from '../domain/types'
import { captureRevisionSchema, captureUndoReceiptIdSchema } from './captureLinks'
import { ApiError, mutateData, type MutateReceipt } from './transport'

/**
 * The one explicit inverse of ONE recorded Capture unlink, exactly as
 * `R33-CAPTURE-UNDO-CONTRACT.md` publishes it. One POST per explicit intent.
 *
 * It reuses the sibling module's transport, revision admission and receipt-id grammar,
 * and nothing else. In particular the copy below is this route's own: an Undo that was
 * refused did not restore a link, which is a different sentence from a removal that did
 * not happen, and reusing the unlink table would put the wrong one on screen.
 *
 * The boundaries are the contract's own:
 *
 * - **The body is the two published fields and nothing else.** `receipt_id` names the
 *   recorded removal to invert; `revision` is the POST-unlink Capture revision that
 *   removal answered with. There is no prior status, task id, source or snapshot here:
 *   the owner reads the original Task from its own receipt.
 * - **The revision is the one the confirmed removal returned**, never a re-read one. A
 *   Capture that has moved on is a `409 revision_conflict` that writes nothing, and this
 *   module never fetches a newer revision to retry with.
 * - **The caller owns the idempotency key**, and it is a NEW key, independent of the
 *   unlink it inverts. After an unknown outcome the same intent is retried under that
 *   same key; a fresh key would be a second Undo intent.
 * - **One explicit intent is exactly one POST.** This route opts into the transport's
 *   `singleAttempt`, so neither a failed fetch nor a rotated CSRF nonce resends it. The
 *   reader is told the outcome is unknown and decides; a client that retried on its own
 *   would settle an unknown state without ever showing it.
 * - **A 200 must prove the restoration that was asked for.** The returned projection has
 *   to be the requested Capture, at the post-unlink revision plus one, carrying the
 *   intended Task again. Any other schema-valid Capture is ambiguous, not a success.
 * - **Ambiguity is a first-class outcome.** A failed fetch, an unreadable 2xx body, an
 *   unpublished success status or a projection that does not parse all mean the restore
 *   may have happened. They are `ambiguous`, never a refusal, and never a restoration.
 * - **No backend prose reaches the screen.** An unrecognised code falls back to one
 *   fixed sentence.
 */

export const CAPTURE_UNDO_UNLINK_METHOD = 'POST' as const

export function captureUndoUnlinkPath(captureId: string): string {
  return `/api/v1/captures/${encodeURIComponent(captureId)}/undo-unlink`
}

/** Exactly `{receipt_id, revision}`. A stray key is a request this module refuses to send. */
export const captureUndoUnlinkRequestSchema = z
  .object({ receipt_id: captureUndoReceiptIdSchema, revision: captureRevisionSchema })
  .strict()

export interface CaptureUndoUnlinkInput {
  captureId: string
  /** The canonical receipt id the confirmed removal returned. */
  receiptId: string
  /** That same removal's post-unlink Capture revision. */
  revision: number
  /**
   * The Task whose link this Undo must put back. It is a RECOGNITION input only: the
   * published body is `{receipt_id, revision}`, and the owner reads the Task from its own
   * receipt. It never reaches the wire; it decides whether the answer proves the restore.
   */
  taskId: string
  /** A NEW key, stable for this one Undo intent: an ambiguous retry MUST reuse it. */
  idempotencyKey: string
}

export interface CaptureUndoUnlinkOutcome {
  capture: Capture
}

/** Client-side codes, kept in the same closed table as the contract's server codes. */
export const UNDO_UNLINK_UNREADABLE = 'capture_unlink_undo_unreadable'
export const UNDO_UNLINK_NOT_AVAILABLE = 'capture_unlink_undo_not_available'
export const UNDO_UNLINK_REFUSED = 'capture_unlink_undo_refused'

export class CaptureUndoUnlinkError extends Error {
  readonly code: string
  readonly cause: unknown

  constructor(code: string, cause?: unknown) {
    super(describeCaptureUndoUnlinkFailure(code))
    this.name = 'CaptureUndoUnlinkError'
    this.code = code
    this.cause = cause
  }
}

/**
 * Every sentence this surface can put on screen.
 *
 * Null-prototype for the same reason the unlink table has one: a backend code of
 * `constructor`, `toString` or `__proto__` must not resolve through `Object.prototype`
 * into an inherited function instead of the one fixed fallback sentence.
 */
export const CAPTURE_UNDO_UNLINK_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    revision_conflict:
      'This capture changed after the link was removed, so it was not restored. Refresh the Task to see where it stands.',
    capture_unlink_undo_conflict:
      'This capture no longer matches the removal that was undone, so the task link was not restored.',
    not_found: 'That removal can no longer be undone, so the task link was not restored.',
    idempotency_conflict:
      'A different request already used this attempt, so the task link was not restored.',
    invalid_body: 'The server refused this request, so the task link was not restored.',
    invalid_request: 'The server refused this request, so the task link was not restored.',
    [UNDO_UNLINK_NOT_AVAILABLE]: 'This removal cannot be undone.',
    [UNDO_UNLINK_UNREADABLE]:
      'Work Stack could not confirm whether the task link was restored. Try the same request again to settle it without repeating the change.',
  }),
)

export const CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE =
  'The task link could not be restored, and nothing was changed.'

/** Own properties only: no backend code can reach an inherited `Object.prototype` member. */
function hasClosedMessage(code: string): boolean {
  return Object.hasOwn(CAPTURE_UNDO_UNLINK_MESSAGES, code)
}

export function describeCaptureUndoUnlinkFailure(code: string): string {
  const message = hasClosedMessage(code) ? CAPTURE_UNDO_UNLINK_MESSAGES[code] : undefined
  // Whatever a lookup produced, only one of this module's own literal sentences returns.
  return typeof message === 'string' ? message : CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE
}

/**
 * `refused` and `conflict` are determinate: the server answered, and it restored nothing.
 * `ambiguous` is the unknown outcome — the same intent may be retried under its own key,
 * and nothing may be announced as restored.
 */
export type CaptureUndoUnlinkFailureKind = 'conflict' | 'refused' | 'ambiguous'

export interface CaptureUndoUnlinkFailure {
  kind: CaptureUndoUnlinkFailureKind
  code: string
  message: string
}

function failure(kind: CaptureUndoUnlinkFailureKind, code: string): CaptureUndoUnlinkFailure {
  return { kind, code, message: describeCaptureUndoUnlinkFailure(code) }
}

/**
 * Maps any thrown value onto the closed copy table and one of the three outcomes.
 *
 * A 5xx is ambiguous, not a refusal: the taxonomy that produces this route's determinate
 * answers ends at 409, so a server-error status is an unhandled path whose write state
 * this client cannot claim to know.
 */
export function classifyCaptureUndoUnlinkFailure(error: unknown): CaptureUndoUnlinkFailure {
  if (error instanceof CaptureUndoUnlinkError) {
    return failure(error.code === UNDO_UNLINK_UNREADABLE ? 'ambiguous' : 'refused', error.code)
  }
  if (error instanceof ApiError) {
    if (error.status >= 500) return failure('ambiguous', UNDO_UNLINK_UNREADABLE)
    if (error.code === 'revision_conflict') return failure('conflict', 'revision_conflict')
    return failure('refused', hasClosedMessage(error.code) ? error.code : UNDO_UNLINK_REFUSED)
  }
  return failure('ambiguous', UNDO_UNLINK_UNREADABLE)
}

/**
 * Does this 200 prove the restoration that was requested, or merely parse?
 *
 * Status 200 says the owner answered; it does not say what it answered about. The three
 * clauses are the identity of the intent that was sent:
 *
 * - the projection is the Capture the reader acted on, not another one;
 * - its revision is exactly the post-unlink revision this request carried, plus one — an
 *   older row is a pre-Undo read and a newer one has moved past the write we asked for,
 *   and neither shows this Undo landing;
 * - the intended Task is linked again. A post-image that restored nothing this reader
 *   asked for is not the restoration the surface is about to announce.
 *
 * A projection that fails any clause is ambiguous: the write may have happened, so the
 * caller keeps the same intent and the same key, and claims nothing.
 */
function recognizesUndoRestoration(
  input: Pick<CaptureUndoUnlinkInput, 'captureId' | 'revision' | 'taskId'>,
  capture: Capture,
): boolean {
  if (capture.id !== input.captureId) return false
  const revision = capture.revision
  if (!Number.isSafeInteger(revision) || revision !== input.revision + 1) return false
  return capture.linked_task_ids.includes(input.taskId)
}

/**
 * POST one explicit Undo of one recorded removal.
 *
 * The caller is expected to have taken its own synchronous lock before awaiting this, for
 * the same reason the unlink route documents: the CSRF preflight inside the shared
 * transport is a real await, and two clicks that both reached it would be two POSTs. This
 * route also declines the transport's own network retry and its 403 resend: they replay
 * the identical body under the identical key, which is safe for the owner, but on this
 * surface only the reader's explicit `Try Undo again` may send a second request.
 */
export async function undoCaptureUnlink(
  input: CaptureUndoUnlinkInput,
): Promise<CaptureUndoUnlinkOutcome> {
  if (!input.captureId || !input.idempotencyKey || !input.taskId) {
    throw new CaptureUndoUnlinkError(UNDO_UNLINK_NOT_AVAILABLE)
  }
  const parsed = captureUndoUnlinkRequestSchema.safeParse({
    receipt_id: input.receiptId,
    revision: input.revision,
  })
  // A malformed intent never reaches the network, and never counts as ambiguous:
  // no request was made, so nothing can have been written.
  if (!parsed.success) throw new CaptureUndoUnlinkError(UNDO_UNLINK_NOT_AVAILABLE, parsed.error)

  const receipt: MutateReceipt = {}
  let capture: Capture
  try {
    capture = await mutateData(
      captureUndoUnlinkPath(input.captureId),
      CAPTURE_UNDO_UNLINK_METHOD,
      parsed.data,
      captureSchema,
      input.idempotencyKey,
      true,
      // One explicit intent, one POST: no network replay and no CSRF resend behind the
      // reader's back. The next request, if any, is the one the reader asks for.
      { receipt, singleAttempt: true },
    )
  } catch (error) {
    // A determinate server refusal keeps its own code. Everything else — a failed fetch,
    // an unreadable 2xx body, a projection that does not parse — happened around a
    // request that may well have committed.
    if (error instanceof ApiError) throw error
    throw new CaptureUndoUnlinkError(UNDO_UNLINK_UNREADABLE, error)
  }
  // The contract publishes one success status and one only. The shared transport accepts
  // every `Response.ok` status, so this route checks its own: a 201/202 means something
  // other than "this restoration is settled".
  if (receipt.status !== 200) throw new CaptureUndoUnlinkError(UNDO_UNLINK_UNREADABLE)
  // A 200 that does not carry the requested Capture, at the expected next revision, with
  // the intended Task linked again, has not shown this Undo landing. It is the same
  // unknown outcome as an unreadable body: retryable under this same intent, never a
  // restoration this surface may announce.
  if (!recognizesUndoRestoration(input, capture)) {
    throw new CaptureUndoUnlinkError(UNDO_UNLINK_UNREADABLE)
  }
  return { capture }
}
