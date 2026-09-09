import { z } from 'zod'
import { taskSchema } from '../domain/schemas'
import { TASK_STATUSES, type Task, type TaskStatus } from '../domain/types'
import { ApiError, getData, mutateData, type MutateReceipt } from './transport'

/**
 * The read side and the one Undo of the shipped mutation-notice contract, exactly as the
 * owner publishes it in `workstack/mutation_notice.py` and `workstack/mutation_service.py`.
 *
 * The boundaries this module keeps, all of them the server's own:
 *
 * - **The query is `limit` and `cursor` and nothing else.** The owner refuses an unknown
 *   query key with `400 invalid_query`, so a stray parameter is a bug that must not be
 *   constructible here. `limit` is admitted against the published 1..50 range before it
 *   is sent, and a cursor that does not match the published opaque grammar is dropped
 *   rather than echoed back.
 * - **The notice is the contract's eighteen fields, strictly.** A row carrying an
 *   unpublished key is a different document, not a superset of this one.
 * - **`after_revision` and the status pair are nullable by contract.** A `commit_unknown`
 *   notice has no after-revision, and only `task.status` carries a status pair. Nothing
 *   here invents either, and nothing here invents a timestamp: the contract publishes none.
 * - **The Undo body is exactly `{revision}`.** The revision that is sent is the one the
 *   notice recorded — `after_revision` — because that is what the owner's compare-and-set
 *   requires. It is never re-read from a fresher projection, and a task that has moved on
 *   is a `409 revision_conflict` that writes nothing.
 * - **The caller owns the idempotency key** and one explicit intent is exactly one POST.
 *   This route opts into `singleAttempt`, so neither a failed fetch nor a rotated CSRF
 *   nonce resends it behind the reader's back.
 * - **A 200 must prove the compensation that was asked for.** The returned Task has to be
 *   the entity the notice named, carrying the notice's before-status, at the sent revision
 *   plus one, AND the owner's own receipt has to name this notice: the published envelope
 *   is exactly `{data, meta:{replayed, undone_notice_id}}`. A replay answers with that
 *   same stored body under `replayed: true`, so a settled replay is recognised on exactly
 *   the same evidence as the first attempt. A body missing that receipt, naming another
 *   notice, or carrying a key the contract does not publish, is not this compensation.
 * - **Ambiguity is a first-class outcome.** A failed fetch, an unreadable 2xx body, an
 *   unpublished success status, a projection that does not parse, and any 5xx all mean the
 *   compensation may have committed. They are `ambiguous`, never a refusal, and never an
 *   undo this client may announce.
 * - **No backend prose reaches the screen.** An unrecognised code falls back to one fixed
 *   sentence, and the idempotency key a notice carries is request identity, never display.
 */

export const MUTATION_NOTICE_FORMAT = 'workstack.mutation-notice'
export const MUTATION_NOTICE_SCHEMA_VERSION = 1
export const MUTATION_NOTICES_PATH = '/api/v1/mutation-notices'
export const MUTATION_NOTICE_DEFAULT_LIMIT = 20
export const MUTATION_NOTICE_MAX_LIMIT = 50
export const TASK_STATUS_OPERATION = 'task.status'
export const COMMITTED = 'committed'

/** The owner's `SOURCES`, `COMMIT_STATES`, `ENTITY_KINDS` and `OPERATION_ENTITY` keys. */
export const MUTATION_SOURCES = ['cli', 'gui', 'agent'] as const
export const MUTATION_COMMIT_STATES = [COMMITTED, 'commit_unknown'] as const
export const MUTATION_ENTITY_KINDS = [
  'task', 'subtask', 'objective', 'key_result', 'worklog',
  'capture', 'workspace', 'profile', 'storage',
] as const
export const MUTATION_OPERATIONS = [
  'task.status', 'task.create', 'task.note', 'task.permanent_delete',
  'subtask.status', 'subtask.create', 'objective.create', 'key_result.create',
  'okr.link', 'okr.progress', 'worklog.append', 'capture.ingest',
  'authority.change', 'profile.change', 'storage.migration',
  'storage.backup', 'storage.restore',
] as const

const NIL_UUID = '00000000-0000-0000-0000-000000000000'
/**
 * Any RFC 4122 identifier, not only v4: `notice_id` and `mutation_uid` are uuid5 digests
 * of the workspace and the idempotency key, so pinning the version nibble to `4` the way
 * `taskSchema.uid` does would reject every real notice.
 */
const noticeUuid = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  .refine((value) => value !== NIL_UUID, 'identity must not be nil.')

/**
 * The opaque page cursor, admitted against the owner's published grammar: unpadded
 * base64url, 32 to 160 characters, and never an `E-000001` event id. Admitting it on the
 * way IN is what stops this client sending back something the owner would refuse.
 */
const noticeCursor = z
  .string()
  .min(32)
  .max(160)
  .regex(/^[A-Za-z0-9_-]+$/)
  .refine((value) => !/^E-\d{6,}$/.test(value), 'a page cursor is never an event id.')

const revision = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER)

export const mutationNoticeSchema = z
  .object({
    format: z.literal(MUTATION_NOTICE_FORMAT),
    schema_version: z.literal(MUTATION_NOTICE_SCHEMA_VERSION),
    notice_id: noticeUuid,
    workspace_uid: noticeUuid,
    mutation_uid: noticeUuid,
    entity_kind: z.enum(MUTATION_ENTITY_KINDS),
    entity_uid: noticeUuid,
    operation: z.enum(MUTATION_OPERATIONS),
    before_revision: revision,
    after_revision: revision.nullable(),
    source: z.enum(MUTATION_SOURCES),
    actor: z.string().min(1).max(64),
    // Request identity, admitted so the row parses. It is never displayed.
    idempotency_key: z.string().min(8).max(128),
    commit_state: z.enum(MUTATION_COMMIT_STATES),
    summary: z.string().min(1).max(160),
    undoable: z.boolean(),
    status_before: z.enum(TASK_STATUSES).nullable(),
    status_after: z.enum(TASK_STATUSES).nullable(),
  })
  .strict()

export const mutationNoticePageSchema = z
  .object({
    items: z.array(mutationNoticeSchema).max(MUTATION_NOTICE_MAX_LIMIT),
    next_cursor: noticeCursor.nullable(),
  })
  .strict()

export type MutationNotice = z.infer<typeof mutationNoticeSchema>
export type MutationNoticePage = z.infer<typeof mutationNoticePageSchema>

/** Exactly `{revision}`. A stray key is a request this module refuses to send. */
export const mutationUndoRequestSchema = z.object({ revision }).strict()

/**
 * The owner's own Undo receipt, exactly as `mutation_service.py` writes it: `replayed`
 * is a boolean either way — `False` on the write, `True` on the stored replay — and
 * `undone_notice_id` is the canonical id of the notice that was compensated.
 */
export const mutationUndoMetaSchema = z
  .object({ replayed: z.boolean(), undone_notice_id: noticeUuid })
  .strict()

/**
 * The whole published success envelope, admitted before the shared, non-strict envelope
 * parse can silently drop anything. `data` is the Task, parsed on its own closed schema
 * one step later; what this adds is that `meta` is present and exact, and that no
 * unpublished top-level key rode along with it.
 */
export const mutationUndoEnvelopeSchema = z
  .object({ data: z.unknown(), meta: mutationUndoMetaSchema })
  .strict()

export interface MutationNoticeQuery {
  cursor?: string | null
  limit?: number
}

function admittedLimit(limit: number | undefined): number {
  if (limit === undefined) return MUTATION_NOTICE_DEFAULT_LIMIT
  if (!Number.isSafeInteger(limit)) return MUTATION_NOTICE_DEFAULT_LIMIT
  return Math.min(Math.max(limit, 1), MUTATION_NOTICE_MAX_LIMIT)
}

/**
 * The published query and nothing else. A cursor that fails the owner's own grammar is
 * omitted rather than sent: a page this client cannot name is the first page, not a 400.
 */
export function mutationNoticesPath(query: MutationNoticeQuery = {}): string {
  const params = new URLSearchParams({ limit: String(admittedLimit(query.limit)) })
  const cursor = query.cursor ?? null
  if (cursor !== null && noticeCursor.safeParse(cursor).success) params.set('cursor', cursor)
  return `${MUTATION_NOTICES_PATH}?${params.toString()}`
}

export function listMutationNotices(query: MutationNoticeQuery = {}): Promise<MutationNoticePage> {
  return getData(mutationNoticesPath(query), mutationNoticePageSchema)
}

export function mutationNoticeUndoPath(noticeId: string): string {
  return `${MUTATION_NOTICES_PATH}/${encodeURIComponent(noticeId)}/undo`
}

/** The identity one Undo needs, derived only from the row the reader is looking at. */
export interface MutationUndoTarget {
  noticeId: string
  /** The Task the notice named. Recognition input; it never reaches the wire. */
  entityUid: string
  /** The notice's own `after_revision`: the owner's compare-and-set expects exactly it. */
  revision: number
  /** The status the compensation restores — the notice's `status_before`. */
  restoredStatus: TaskStatus
}

/**
 * Whether this row may be offered as an Undo, and under what identity.
 *
 * `undoable` is the owner's verdict and it leads; the remaining clauses are the identity
 * the request needs, and a row that cannot supply one is not offerable however the flag
 * reads. `task.status` is the whole of `REVERSIBLE_OPERATIONS` today, and a
 * `commit_unknown` notice has no after-revision to compare against, so neither can be
 * built into a body. The server's compare-and-set stays authoritative either way: this
 * only decides whether a control appears.
 */
export function mutationUndoTarget(notice: MutationNotice): MutationUndoTarget | null {
  if (!notice.undoable) return null
  if (notice.operation !== TASK_STATUS_OPERATION) return null
  if (notice.commit_state !== COMMITTED) return null
  const after = notice.after_revision
  const restoredStatus = notice.status_before
  if (after === null || restoredStatus === null) return null
  if (restoredStatus === notice.status_after) return null
  return {
    noticeId: notice.notice_id,
    entityUid: notice.entity_uid,
    revision: after,
    restoredStatus,
  }
}

export const MUTATION_UNDO_UNREADABLE = 'mutation_undo_unreadable'
export const MUTATION_UNDO_NOT_AVAILABLE = 'mutation_undo_not_available'
export const MUTATION_UNDO_REFUSED = 'mutation_undo_refused'

export class MutationUndoError extends Error {
  readonly code: string
  readonly cause: unknown

  constructor(code: string, cause?: unknown) {
    super(describeMutationUndoFailure(code))
    this.name = 'MutationUndoError'
    this.code = code
    this.cause = cause
  }
}

/**
 * Every sentence this surface can put on screen.
 *
 * Null-prototype, for the reason the sibling Capture tables have one: a backend code of
 * `constructor`, `toString` or `__proto__` must not resolve through `Object.prototype`
 * into an inherited function instead of the one fixed fallback sentence.
 */
export const MUTATION_UNDO_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    revision_conflict:
      'This task changed after this entry was recorded, so nothing was undone. Refresh recent changes to see where it stands.',
    not_found: 'This change can no longer be undone, so nothing was changed.',
    idempotency_conflict:
      'A different request already used this attempt, so nothing was undone.',
    invalid_request: 'The workspace refused this request, so nothing was undone.',
    invalid_body: 'The workspace refused this request, so nothing was undone.',
    revision_exhausted:
      'This task has no revision left to record an undo, so nothing was changed.',
    [MUTATION_UNDO_NOT_AVAILABLE]: 'This change cannot be undone.',
    [MUTATION_UNDO_UNREADABLE]:
      'Work Stack could not confirm whether this change was undone. Send the same request again to settle it without repeating the change.',
  }),
)

export const MUTATION_UNDO_FALLBACK_MESSAGE =
  'This change could not be undone, and nothing was changed.'

/** Own properties only: no backend code can reach an inherited `Object.prototype` member. */
function hasClosedMessage(code: string): boolean {
  return Object.hasOwn(MUTATION_UNDO_MESSAGES, code)
}

export function describeMutationUndoFailure(code: string): string {
  const message = hasClosedMessage(code) ? MUTATION_UNDO_MESSAGES[code] : undefined
  // Whatever a lookup produced, only one of this module's own literal sentences returns.
  return typeof message === 'string' ? message : MUTATION_UNDO_FALLBACK_MESSAGE
}

/**
 * `refused` and `conflict` are determinate: the owner answered, and it undid nothing.
 * `ambiguous` is the unknown outcome — the same intent may be retried under its own key,
 * and nothing may be announced as undone.
 */
export type MutationUndoFailureKind = 'conflict' | 'refused' | 'ambiguous'

export interface MutationUndoFailure {
  kind: MutationUndoFailureKind
  code: string
  message: string
}

function failure(kind: MutationUndoFailureKind, code: string): MutationUndoFailure {
  return { kind, code, message: describeMutationUndoFailure(code) }
}

/**
 * Maps any thrown value onto the closed copy table and one of the three outcomes.
 *
 * A 5xx is ambiguous, not a refusal: the taxonomy that produces this route's determinate
 * answers ends at 409, so a server-error status — the shape an unavailable owner takes on
 * a route that did reach a socket — is an outcome this client cannot claim to know. A
 * failed fetch is the same unknown, reported in the same words.
 */
export function classifyMutationUndoFailure(error: unknown): MutationUndoFailure {
  if (error instanceof MutationUndoError) {
    return failure(error.code === MUTATION_UNDO_UNREADABLE ? 'ambiguous' : 'refused', error.code)
  }
  if (error instanceof ApiError) {
    if (error.status >= 500) return failure('ambiguous', MUTATION_UNDO_UNREADABLE)
    if (error.code === 'revision_conflict') return failure('conflict', 'revision_conflict')
    return failure('refused', hasClosedMessage(error.code) ? error.code : MUTATION_UNDO_REFUSED)
  }
  return failure('ambiguous', MUTATION_UNDO_UNREADABLE)
}

export const MUTATION_NOTICE_LIST_REFUSED =
  'Work Stack could not read recent changes for this workspace.'
export const MUTATION_NOTICE_LIST_UNREACHABLE =
  'Work Stack could not reach the workspace owner, so recent changes are not shown.'

/**
 * The read side has no ambiguity to report — nothing was written either way — so it needs
 * only to separate a workspace that answered from one that could not be reached. Backend
 * prose stays off the screen here too.
 */
export function describeMutationNoticeListFailure(error: unknown): string {
  if (error instanceof ApiError && error.status < 500) return MUTATION_NOTICE_LIST_REFUSED
  return MUTATION_NOTICE_LIST_UNREACHABLE
}

export interface MutationUndoOutcome {
  task: Task
}

/**
 * Does this 200 prove the compensation that was requested, or merely parse?
 *
 * Status 200 says the owner answered; it does not say what it answered about. The three
 * clauses are the identity of the intent that was sent:
 *
 * - the projection is the Task the notice named, not another one;
 * - it now carries the status the notice recorded as the before-image — the compensation
 *   this reader asked for, and not some later state the workspace happens to be in;
 * - its revision is exactly the revision this request carried, plus one. An older row is
 *   a pre-Undo read and a newer one has moved past the write we asked for.
 *
 * A replayed attempt answers with the same stored body, so it satisfies the same clauses
 * without a second write — which is why the VALUE of `meta.replayed` changes nothing here.
 */
function recognizesCompensation(target: MutationUndoTarget, task: Task): boolean {
  if (task.uid !== target.entityUid) return false
  if (task.status !== target.restoredStatus) return false
  return Number.isSafeInteger(task.revision) && task.revision === target.revision + 1
}

/**
 * Does the owner's receipt say it compensated THIS notice?
 *
 * The projection alone cannot say so. A Task at the right status and revision is evidence
 * about the entity, not about which recorded change was reversed to get there, and the
 * owner publishes that separately: `meta.undone_notice_id` is the notice it acted on and
 * `meta.replayed` is whether this answer is the stored one. Both must be there and exact.
 *
 * The strict envelope above has already refused a malformed or extra-keyed body, so what
 * remains here is the identity clause — a receipt naming another notice is an answer to
 * another request, and this client may not report it as this row's undo.
 */
function admitsUndoReceipt(target: MutationUndoTarget, meta: unknown): boolean {
  const parsed = mutationUndoMetaSchema.safeParse(meta)
  return parsed.success && parsed.data.undone_notice_id === target.noticeId
}

/**
 * POST one explicit Undo of one recorded notice.
 *
 * The caller is expected to have taken its own synchronous lock before awaiting this: the
 * CSRF preflight inside the shared transport is a real await, and two clicks that both
 * reached it would be two POSTs. This route also declines the transport's network retry
 * and its 403 resend, so only the reader's explicit retry sends a second request.
 */
export async function undoMutationNotice(
  target: MutationUndoTarget,
  idempotencyKey: string,
): Promise<MutationUndoOutcome> {
  if (!target.noticeId || !target.entityUid || !idempotencyKey) {
    throw new MutationUndoError(MUTATION_UNDO_NOT_AVAILABLE)
  }
  const parsed = mutationUndoRequestSchema.safeParse({ revision: target.revision })
  // A malformed intent never reaches the network, and never counts as ambiguous:
  // no request was made, so nothing can have been written.
  if (!parsed.success) throw new MutationUndoError(MUTATION_UNDO_NOT_AVAILABLE, parsed.error)

  const receipt: MutateReceipt = {}
  let task: Task
  try {
    task = await mutateData(
      mutationNoticeUndoPath(target.noticeId),
      'POST',
      parsed.data,
      taskSchema,
      idempotencyKey,
      true,
      { receipt, singleAttempt: true, strictEnvelope: mutationUndoEnvelopeSchema },
    )
  } catch (error) {
    // A determinate refusal keeps its own code. Everything else — a failed fetch, an
    // unreadable 2xx body, a projection that does not parse — happened around a request
    // that may well have committed.
    if (error instanceof ApiError) throw error
    throw new MutationUndoError(MUTATION_UNDO_UNREADABLE, error)
  }
  // The contract publishes one success status and one only. The shared transport accepts
  // every `Response.ok`, so this route checks its own: a 201/202 means something other
  // than "this compensation is settled".
  if (receipt.status !== 200) throw new MutationUndoError(MUTATION_UNDO_UNREADABLE)
  // The owner's receipt first: it is the only thing that names the notice this answer
  // settled. A body that omits it, or names another one, is an outcome nobody knows.
  if (!admitsUndoReceipt(target, receipt.meta)) throw new MutationUndoError(MUTATION_UNDO_UNREADABLE)
  if (!recognizesCompensation(target, task)) throw new MutationUndoError(MUTATION_UNDO_UNREADABLE)
  return { task }
}
