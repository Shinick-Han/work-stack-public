/**
 * Editor state and issued-response checks for one scoped knowledge request.
 *
 * This module is the arithmetic behind `KnowledgeRequestDialog`. It holds no React
 * state, performs no I/O and reads no clock: every decision it makes is a pure function
 * of values the caller hands it, so the same inputs answer the same way in a test, in a
 * review and on screen.
 *
 * What it deliberately does not do:
 *
 * - **Mint identity or authority.** A draft carries no `request_id`, no `schema`, no
 *   `requested_at` and no `expires_at`. Those are the issuer's to assign, and a locally
 *   invented one would look exactly like a real one to a reader. The draft is the part a
 *   user actually reviewed; everything else on the wire comes back from the server.
 * - **Grant scope.** `granted` corpus options arrive from the caller's own trusted
 *   state. A selection naming an alias that is not offered is refused here rather than
 *   quietly forwarded, but that refusal is a courtesy to the user — the issuer stays the
 *   authority, and passing these checks authorises nothing.
 * - **Attach Task detail.** A binding is identity only: workspace, and optionally the
 *   Task trio. No title, detail, note or body is ever placed in a draft. A Task title
 *   may seed the *editable* query text, which the user then reads and can change; that
 *   is a prefill the user owns, not an automatic attachment.
 * - **Echo values into diagnostics.** Every refusal below carries a stable code and
 *   authored copy naming, at most, a field of the closed schema. No submitted query, no
 *   corpus alias, no identifier and no timestamp is interpolated into a message.
 *
 * The bounds restated here mirror `contracts/knowledge-request-v1.md` and
 * `workstack/knowledge_request.py`. They are restated, not imported: the frontend cannot
 * depend on the Python package, and a client-side bound is a nicety for the person
 * typing, never the enforcement. The server re-validates everything.
 */

import { parseRfc3339Instant, rfc3339WindowIsValid } from './knowledgeRequestTime'

export const KNOWLEDGE_REQUEST_SCHEMA = 'workstack.knowledge-request.v1'

export const KNOWLEDGE_REQUEST_PURPOSES = [
  'find_context',
  'extract_actions',
  'refresh_capture',
] as const

export type KnowledgeRequestPurpose = (typeof KNOWLEDGE_REQUEST_PURPOSES)[number]

/** Authored, user-facing copy for the purpose allowlist. Never a provider or tool name. */
export const KNOWLEDGE_REQUEST_PURPOSE_COPY: Record<
  KnowledgeRequestPurpose,
  { label: string; help: string }
> = {
  find_context: {
    label: 'Find context',
    help: 'Look for background that explains this work.',
  },
  extract_actions: {
    label: 'Extract actions',
    help: 'Look for commitments and next steps already written down.',
  },
  refresh_capture: {
    label: 'Refresh a capture',
    help: 'Re-read a source that was captured before, to see what changed.',
  },
}

export const MAX_QUERY_CHARS = 1000
export const MIN_CORPUS_REFS = 1
export const MAX_CORPUS_REFS = 8
export const MAX_CORPUS_REF_CHARS = 64
export const MIN_RESULT_LIMIT = 1
export const MAX_RESULT_LIMIT = 10
/** A request is active for at most five minutes; the issuer decides the exact window. */
export const MAX_ACTIVE_SECONDS = 300

const MAX_TASK_REVISION = Number.MAX_SAFE_INTEGER
const CORPUS_REF_RE = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const NIL_UUID = '00000000-0000-0000-0000-000000000000'
const TASK_ID_RE = /^T-\d{4,}$/

/** The Task the caller says is open, at the revision it actually read. */
export interface KnowledgeRequestTaskRef {
  uid: string
  id: string
  revision: number
  /** Display text only. May seed the editable query; never travels in the draft. */
  title?: string | null
}

/**
 * One corpus the caller's trusted state says this user already holds.
 *
 * `alias` is the nonsecret registry label that goes on the wire. `label` is display copy
 * the server authored for a reader; it is not derived from a path, host, share or
 * connection string, and this module never builds one from the alias.
 */
export interface KnowledgeCorpusOption {
  alias: string
  label: string
  description?: string | null
}

export interface KnowledgeWorkspaceBinding {
  workspace_uid: string
}

export interface KnowledgeTaskBinding extends KnowledgeWorkspaceBinding {
  task_uid: string
  task_id: string
  task_revision: number
}

export type KnowledgeRequestBinding = KnowledgeWorkspaceBinding | KnowledgeTaskBinding

/**
 * Exactly what the user reviewed and asked to be issued.
 *
 * This is not an HTTP body and not a wire schema. It is the argument of the `onIssue`
 * callback: a plain object handed to a parent that owns the real transport. The parent
 * is free to translate it, and must not treat it as a document that has been authorised.
 */
export interface KnowledgeRequestDraft {
  binding: KnowledgeRequestBinding
  purpose: KnowledgeRequestPurpose
  query: string
  corpus_refs: string[]
  result_limit: number
}

/** The document the issuer actually minted, as `KnowledgeRequest v1` defines it. */
export interface IssuedKnowledgeRequest {
  schema: typeof KNOWLEDGE_REQUEST_SCHEMA
  request_id: string
  binding: KnowledgeRequestBinding
  purpose: KnowledgeRequestPurpose
  query: string
  corpus_refs: string[]
  result_limit: number
  requested_at: string
  expires_at: string
}

/** The raw editor values, before any of them are known to be usable. */
export interface KnowledgeRequestEditorInput {
  workspaceUid: string
  task?: KnowledgeRequestTaskRef | null
  purpose: string
  query: string
  selectedAliases: readonly string[]
  /** Kept as typed text so an unusable limit refuses instead of being coerced. */
  resultLimit: string
  options: readonly KnowledgeCorpusOption[]
}

export type KnowledgeDraftRefusalCode =
  | 'invalid_workspace'
  | 'invalid_task_binding'
  | 'invalid_purpose'
  | 'invalid_query'
  | 'query_too_long'
  | 'no_corpus_selected'
  | 'too_many_corpora'
  | 'duplicate_corpus_ref'
  | 'corpus_not_offered'
  | 'invalid_result_limit'

export type KnowledgeDraftResult =
  | { ok: true; draft: KnowledgeRequestDraft }
  | { ok: false; code: KnowledgeDraftRefusalCode; message: string }

const DRAFT_REFUSAL_COPY: Record<KnowledgeDraftRefusalCode, string> = {
  invalid_workspace:
    'This screen has no active workspace to ask on behalf of. Reopen it from a workspace and try again.',
  invalid_task_binding:
    'The open Task was read without a complete identity, so a request cannot be bound to it. Reload the Task and try again.',
  invalid_purpose: 'Choose one of the listed purposes before generating a request.',
  invalid_query:
    'Write a question in plain text. A request needs a query, and it may not contain control characters.',
  query_too_long: `Shorten the question to ${MAX_QUERY_CHARS} characters or fewer.`,
  no_corpus_selected: 'Select at least one corpus to search.',
  too_many_corpora: `Select no more than ${MAX_CORPUS_REFS} corpora.`,
  duplicate_corpus_ref: 'The same corpus is selected twice. Select each one once.',
  corpus_not_offered:
    'A selected corpus is not in the list this workspace is allowed to search. Clear it and choose from the list.',
  invalid_result_limit: `Ask for between ${MIN_RESULT_LIMIT} and ${MAX_RESULT_LIMIT} results, as a whole number.`,
}

function refuse(code: KnowledgeDraftRefusalCode): KnowledgeDraftResult {
  return { code, message: DRAFT_REFUSAL_COPY[code], ok: false }
}

function isCanonicalUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_RE.test(value) && value !== NIL_UUID
}

/** The one definition of "control character" this query grammar refuses. */
export function isKnowledgeQueryControlCode(code: number) {
  return code < 32 || code === 127 || (code >= 0x80 && code <= 0x9f)
}

function hasControlCharacter(value: string) {
  for (const character of value) {
    if (isKnowledgeQueryControlCode(character.codePointAt(0) ?? 0)) return true
  }
  return false
}

/** Unicode code points, matching Python `len` on the knowledge-request query. */
export function knowledgeRequestQueryChars(value: string) {
  return Array.from(value).length
}

/** Keeps whole code points so a max-length clip cannot split a surrogate pair. */
export function clipKnowledgeRequestQuery(value: string, maxChars = MAX_QUERY_CHARS) {
  const chars = Array.from(value)
  if (chars.length <= maxChars) return value
  return chars.slice(0, maxChars).join('')
}

function isExactInteger(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= minimum && value <= maximum
}

/**
 * Builds the binding from the caller's own state. The Task trio is all-or-nothing: a
 * partial Task identity is refused rather than completed from whatever this screen
 * happens to know, and no Task is a valid workspace-only request rather than an error.
 */
function buildBinding(input: KnowledgeRequestEditorInput): KnowledgeDraftResult | KnowledgeRequestBinding {
  if (!isCanonicalUuid(input.workspaceUid)) return refuse('invalid_workspace')
  const task = input.task
  if (!task) return { workspace_uid: input.workspaceUid }
  const usable =
    isCanonicalUuid(task.uid) &&
    typeof task.id === 'string' &&
    TASK_ID_RE.test(task.id.toUpperCase()) &&
    isExactInteger(task.revision, 0, MAX_TASK_REVISION)
  if (!usable) return refuse('invalid_task_binding')
  return {
    task_id: task.id.toUpperCase(),
    task_revision: task.revision,
    task_uid: task.uid,
    workspace_uid: input.workspaceUid,
  }
}

function checkQuery(value: string): KnowledgeDraftRefusalCode | null {
  const query = value.trim()
  if (!query) return 'invalid_query'
  if (knowledgeRequestQueryChars(query) > MAX_QUERY_CHARS) return 'query_too_long'
  if (hasControlCharacter(query)) return 'invalid_query'
  return null
}

function checkSelection(
  selected: readonly string[],
  options: readonly KnowledgeCorpusOption[],
): KnowledgeDraftRefusalCode | null {
  if (selected.length < MIN_CORPUS_REFS) return 'no_corpus_selected'
  if (selected.length > MAX_CORPUS_REFS) return 'too_many_corpora'
  const offered = new Set(options.map((option) => option.alias))
  const seen = new Set<string>()
  for (const alias of selected) {
    if (seen.has(alias)) return 'duplicate_corpus_ref'
    seen.add(alias)
    if (!offered.has(alias)) return 'corpus_not_offered'
  }
  return null
}

function checkLimit(value: string): number | null {
  const trimmed = value.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const limit = Number(trimmed)
  if (!isExactInteger(limit, MIN_RESULT_LIMIT, MAX_RESULT_LIMIT)) return null
  return limit
}

/**
 * Turns the editor's raw values into the draft the user asked to have issued, or into a
 * single refusal explaining what to fix. Refusing here keeps an obviously unusable
 * request off the wire; it never stands in for the issuer's own validation.
 */
export function buildKnowledgeRequestDraft(input: KnowledgeRequestEditorInput): KnowledgeDraftResult {
  const binding = buildBinding(input)
  if ('ok' in binding) return binding
  if (!(KNOWLEDGE_REQUEST_PURPOSES as readonly string[]).includes(input.purpose)) {
    return refuse('invalid_purpose')
  }
  const queryProblem = checkQuery(input.query)
  if (queryProblem) return refuse(queryProblem)
  const selectionProblem = checkSelection(input.selectedAliases, input.options)
  if (selectionProblem) return refuse(selectionProblem)
  const limit = checkLimit(input.resultLimit)
  if (limit === null) return refuse('invalid_result_limit')
  const draft: KnowledgeRequestDraft = {
    binding,
    corpus_refs: [...input.selectedAliases],
    purpose: input.purpose as KnowledgeRequestPurpose,
    query: input.query.trim(),
    result_limit: limit,
  }
  return { draft, ok: true }
}

/**
 * A stable identity for "the request currently on screen".
 *
 * Any change a reader would consider a different question — a different workspace, a
 * different Task revision, a different scope, purpose, wording or limit — produces a
 * different key. The dialog uses it to invalidate a receipt that answered the previous
 * question, so a late reply can never be presented as an answer to this one.
 */
export function knowledgeRequestIdentity(input: KnowledgeRequestEditorInput): string {
  const task = input.task
  return JSON.stringify([
    input.workspaceUid,
    task ? [task.uid, task.id, task.revision] : null,
    input.purpose,
    input.query.trim(),
    [...new Set(input.selectedAliases)].sort(),
    input.resultLimit.trim(),
  ])
}

export type IssuedRefusalCode =
  | 'malformed_response'
  | 'unsupported_schema'
  | 'unknown_field'
  | 'missing_field'
  | 'invalid_request_id'
  | 'invalid_binding'
  | 'invalid_purpose'
  | 'invalid_query'
  | 'invalid_corpus_refs'
  | 'invalid_result_limit'
  | 'invalid_timestamp'
  | 'invalid_request_window'
  | 'binding_mismatch'
  | 'purpose_mismatch'
  | 'query_mismatch'
  | 'corpus_mismatch'
  | 'result_limit_mismatch'

export type IssuedKnowledgeRequestResult =
  | { ok: true; request: IssuedKnowledgeRequest }
  | { ok: false; code: IssuedRefusalCode; message: string }

const SHAPE_FAILURE =
  'The issuer answered with something this screen cannot read as a knowledge request. Nothing was issued that you can copy.'
const CORRESPONDENCE_FAILURE =
  'The issuer answered a different request from the one you reviewed. It has not been shown, and nothing was copied.'

const CORRESPONDENCE_CODES: ReadonlySet<IssuedRefusalCode> = new Set([
  'binding_mismatch',
  'corpus_mismatch',
  'purpose_mismatch',
  'query_mismatch',
  'result_limit_mismatch',
])

function issueRefusal(code: IssuedRefusalCode): IssuedKnowledgeRequestResult {
  // Only the code varies; the copy is authored and carries no returned value, because a
  // malformed or mismatched response is exactly the input least safe to quote back.
  return { code, message: CORRESPONDENCE_CODES.has(code) ? CORRESPONDENCE_FAILURE : SHAPE_FAILURE, ok: false }
}

const ENVELOPE_FIELDS = [
  'schema',
  'request_id',
  'binding',
  'purpose',
  'query',
  'corpus_refs',
  'result_limit',
  'requested_at',
  'expires_at',
] as const

const BINDING_WORKSPACE_ONLY = ['workspace_uid']
const BINDING_WITH_TASK = ['workspace_uid', 'task_uid', 'task_id', 'task_revision']

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function keysExactly(value: Record<string, unknown>, allowed: readonly string[]) {
  const keys = Object.keys(value)
  return keys.length === allowed.length && allowed.every((key) => key in value)
}

function checkEnvelopeKeys(value: unknown): IssuedRefusalCode | null {
  if (!isPlainObject(value)) return 'malformed_response'
  for (const key of Object.keys(value)) {
    if (!(ENVELOPE_FIELDS as readonly string[]).includes(key)) return 'unknown_field'
  }
  for (const field of ENVELOPE_FIELDS) {
    if (!(field in value)) return 'missing_field'
  }
  return null
}

function checkReturnedBinding(value: unknown): IssuedRefusalCode | null {
  if (!isPlainObject(value)) return 'invalid_binding'
  const workspaceOnly = keysExactly(value, BINDING_WORKSPACE_ONLY)
  const withTask = keysExactly(value, BINDING_WITH_TASK)
  if (!workspaceOnly && !withTask) return 'invalid_binding'
  if (!isCanonicalUuid(value.workspace_uid)) return 'invalid_binding'
  if (workspaceOnly) return null
  if (!isCanonicalUuid(value.task_uid)) return 'invalid_binding'
  if (typeof value.task_id !== 'string' || !TASK_ID_RE.test(value.task_id)) return 'invalid_binding'
  if (!isExactInteger(value.task_revision, 0, MAX_TASK_REVISION)) return 'invalid_binding'
  return null
}

function checkReturnedCorpusRefs(value: unknown): IssuedRefusalCode | null {
  if (!Array.isArray(value)) return 'invalid_corpus_refs'
  if (value.length < MIN_CORPUS_REFS || value.length > MAX_CORPUS_REFS) return 'invalid_corpus_refs'
  const seen = new Set<string>()
  for (const entry of value) {
    if (typeof entry !== 'string' || entry.length > MAX_CORPUS_REF_CHARS) return 'invalid_corpus_refs'
    if (!CORPUS_REF_RE.test(entry)) return 'invalid_corpus_refs'
    if (seen.has(entry)) return 'invalid_corpus_refs'
    seen.add(entry)
  }
  return null
}

function checkReturnedWindow(document: Record<string, unknown>): IssuedRefusalCode | null {
  const requested = parseRfc3339Instant(document.requested_at)
  const expires = parseRfc3339Instant(document.expires_at)
  if (requested === null || expires === null) return 'invalid_timestamp'
  if (!rfc3339WindowIsValid(requested, expires, MAX_ACTIVE_SECONDS)) return 'invalid_request_window'
  return null
}

function checkReturnedShape(document: Record<string, unknown>): IssuedRefusalCode | null {
  if (document.schema !== KNOWLEDGE_REQUEST_SCHEMA) return 'unsupported_schema'
  if (!isCanonicalUuid(document.request_id)) return 'invalid_request_id'
  const bindingProblem = checkReturnedBinding(document.binding)
  if (bindingProblem) return bindingProblem
  if (!(KNOWLEDGE_REQUEST_PURPOSES as readonly string[]).includes(document.purpose as string)) {
    return 'invalid_purpose'
  }
  if (typeof document.query !== 'string' || checkQuery(document.query)) return 'invalid_query'
  const corpusProblem = checkReturnedCorpusRefs(document.corpus_refs)
  if (corpusProblem) return corpusProblem
  if (!isExactInteger(document.result_limit, MIN_RESULT_LIMIT, MAX_RESULT_LIMIT)) {
    return 'invalid_result_limit'
  }
  return checkReturnedWindow(document)
}

function sameBinding(returned: KnowledgeRequestBinding, submitted: KnowledgeRequestBinding) {
  const left = returned as unknown as Record<string, unknown>
  const right = submitted as unknown as Record<string, unknown>
  const keys = Object.keys(right)
  if (Object.keys(left).length !== keys.length) return false
  return keys.every((key) => left[key] === right[key])
}

function checkCorrespondence(
  document: Record<string, unknown>,
  draft: KnowledgeRequestDraft,
): IssuedRefusalCode | null {
  if (!sameBinding(document.binding as KnowledgeRequestBinding, draft.binding)) return 'binding_mismatch'
  if (document.purpose !== draft.purpose) return 'purpose_mismatch'
  if ((document.query as string).trim() !== draft.query) return 'query_mismatch'
  if (document.result_limit !== draft.result_limit) return 'result_limit_mismatch'
  const returned = [...(document.corpus_refs as string[])].sort()
  const submitted = [...draft.corpus_refs].sort()
  if (returned.length !== submitted.length) return 'corpus_mismatch'
  if (returned.some((alias, index) => alias !== submitted[index])) return 'corpus_mismatch'
  return null
}

/**
 * Accepts the issuer's answer only if it is a well-formed `KnowledgeRequest v1` *and* it
 * answers the draft the user reviewed.
 *
 * Shape alone is not enough. A syntactically perfect document bound to another Task, or
 * carrying a query the user never wrote, would put words in the user's mouth and bind
 * evidence to a state nobody read. Both families refuse, and both refuse without quoting
 * anything the issuer returned.
 */
export function validateIssuedKnowledgeRequest(
  value: unknown,
  draft: KnowledgeRequestDraft,
): IssuedKnowledgeRequestResult {
  const keyProblem = checkEnvelopeKeys(value)
  if (keyProblem) return issueRefusal(keyProblem)
  const document = value as Record<string, unknown>
  const shapeProblem = checkReturnedShape(document)
  if (shapeProblem) return issueRefusal(shapeProblem)
  const correspondenceProblem = checkCorrespondence(document, draft)
  if (correspondenceProblem) return issueRefusal(correspondenceProblem)
  return { ok: true, request: document as unknown as IssuedKnowledgeRequest }
}

/** The issuer's expiry instant in epoch milliseconds. Validated documents always parse. */
export function knowledgeRequestExpiresAtMs(request: IssuedKnowledgeRequest): number {
  return parseRfc3339Instant(request.expires_at)?.unixMs ?? 0
}

/**
 * Reads the issuer's expiry against a clock the caller supplies. It never moves the
 * window: a lapsed request needs a fresh one the user explicitly asks for.
 */
export function isKnowledgeRequestExpired(request: IssuedKnowledgeRequest, nowMs: number) {
  return nowMs >= knowledgeRequestExpiresAtMs(request)
}

/**
 * The exact document the issuer returned, serialised in the contract's field order for
 * a person to paste. Nothing local is added and no field is dropped, so what the user
 * carries out of band is what the issuer signed off on.
 */
export function knowledgeRequestReceiptJson(request: IssuedKnowledgeRequest): string {
  const ordered: Record<string, unknown> = {}
  for (const field of ENVELOPE_FIELDS) ordered[field] = request[field]
  return JSON.stringify(ordered, null, 2)
}
