import { ApiError, UnreadableSuccessError } from '../../api/transport'
import { KnowledgeIssueError } from './KnowledgeRequestDialog'

/**
 * How `KnowledgeRequestLauncher` reads a rejection: the authored copy for it, whether it
 * settles the attempt, and whether anything about the ledger is actually known.
 *
 * This module is the launcher's own arithmetic, lifted out of the component file so the
 * screen stays under the production file bound. It holds no state, performs no I/O and
 * renders nothing; every decision is a pure function of the error it is handed, and every
 * sentence a user can read is written here rather than echoed from the wire.
 */

/**
 * Authored copy per closed refusal code. The server's own `message` is a constant per
 * surface, but it is not echoed: every line a user reads here was written here.
 */
const REFUSAL_COPY: Record<string, string> = {
  // This line is installed with the refusal, before the re-read it triggers has answered,
  // so it may not report that read as done. The read announces its own outcome.
  corpus_not_granted:
    'The requested corpus is not granted. Review the current connection policy before starting a new request.',
  // The ledger refused the *document*, not this question: the record would not fit under
  // the stored byte limit. It is a determinate refusal — the issuer discarded its planned
  // copy — and it is not the same stop as the record-count limit below.
  document_too_large:
    'Knowledge request storage is full. Cleanup is not available in this version.',
  invalid_alias: 'A connection or corpus alias does not match the grammar the ledger accepts.',
  invalid_csrf: 'This browser session is no longer recognised. Reload Work Stack and try again.',
  knowledge_backend_unsupported:
    'This workspace runs on a storage backend that keeps no knowledge ledger, so no connection can be configured or searched here.',
  ledger_full: 'The knowledge request limit has been reached. Cleanup is not available in this version.',
  origin_required: 'This browser session is no longer recognised. Reload Work Stack and try again.',
  policy_revision_changed:
    'Another owner replaced the connection policy. Nothing was overwritten.',
  request_digest_mismatch:
    'This attempt asked a different question under an identifier that already names another one. Start a fresh request.',
  request_expired:
    'The window on this request has closed. It is never renewed automatically — start a fresh request and review it before generating.',
  task_binding_required: 'A Task is open on the server, so a workspace-only request is refused here.',
  unknown_task: 'The Task this request names is not one the server holds.',
  workspace_mismatch: 'This request named a workspace the server is not holding open.',
}

/**
 * Refusals that settle the attempt: the editor is closed and the user is told what to do,
 * rather than being left pressing Generate against a decision that will not change.
 * `fresh` offers an explicit new intent; `refetch` re-reads the policy the server holds.
 *
 * The two capacity refusals settle with `fresh: false` deliberately. A new intent is a new
 * record, and a ledger that is out of records or out of bytes has no room for it either:
 * offering "start a fresh request" would send the owner round a loop that cannot end. This
 * screen does not claim a cleanup it has no way to perform.
 */
const SETTLED_ISSUE_CODES: Record<string, { fresh: boolean; refetch: boolean }> = {
  corpus_not_granted: { fresh: true, refetch: true },
  document_too_large: { fresh: false, refetch: false },
  knowledge_backend_unsupported: { fresh: false, refetch: false },
  ledger_full: { fresh: false, refetch: false },
  policy_revision_changed: { fresh: true, refetch: true },
  request_digest_mismatch: { fresh: true, refetch: false },
  request_expired: { fresh: true, refetch: false },
  task_binding_required: { fresh: false, refetch: false },
  unknown_task: { fresh: false, refetch: false },
  workspace_mismatch: { fresh: false, refetch: false },
}

export function refusalCode(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null
}

/**
 * `code` is a string the server put on the wire, so `constructor`, `toString` or
 * `__proto__` can arrive here. Only a line this module actually authored may be shown: an
 * inherited `Object.prototype` member is a function or an object, not copy, and rendering
 * one would be both a crash and a claim nothing established.
 */
export function refusalCopy(error: unknown, fallback: string): string {
  const code = refusalCode(error)
  return (code !== null && Object.hasOwn(REFUSAL_COPY, code) && REFUSAL_COPY[code]) || fallback
}

/**
 * How this rejection settles the attempt, or `null` when it settles nothing. Own property
 * only: an inherited member would settle an attempt this table never classified, and read
 * `fresh`/`refetch` off a function rather than off a decision.
 */
export function settledIssueOutcome(error: unknown): { fresh: boolean; refetch: boolean } | null {
  const code = refusalCode(error)
  if (code === null || !Object.hasOwn(SETTLED_ISSUE_CODES, code)) return null
  return SETTLED_ISSUE_CODES[code]
}

/** An outcome the client cannot call: the write may or may not have landed. */
export function isUnknownOutcome(error: unknown): boolean {
  return error instanceof UnreadableSuccessError || !(error instanceof ApiError)
}

/**
 * How an attempt settled, from the evidence the launcher actually has. A closed refusal
 * code is the issuer's own decision, so it may be reported as "not issued". Everything
 * else — a lost response, an unreadable 2xx, a status with no code this build knows —
 * leaves the ledger's state unknown, and an answer about another revision says nothing
 * about this attempt either.
 */
export function asIssueError(error: unknown): KnowledgeIssueError {
  if (error instanceof KnowledgeIssueError) return error
  const code = refusalCode(error)
  if (code && Object.hasOwn(REFUSAL_COPY, code)) return new KnowledgeIssueError('refused', code)
  return new KnowledgeIssueError('unknown', error instanceof Error ? error.message : undefined)
}
