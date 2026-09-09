import { KNOWLEDGE_ALIAS_PATTERN } from '../../api/knowledge'
import type { Capture } from '../../domain/types'
import {
  KNOWLEDGE_REQUEST_PURPOSES,
  clipKnowledgeRequestQuery,
  isKnowledgeQueryControlCode,
  type KnowledgeRequestPurpose,
} from './knowledgeRequestDraft'

/**
 * The starting point one saved Capture offers a *new* knowledge search.
 *
 * This module is pure: it reads a Capture the caller already holds and returns editable
 * starting values. It performs no I/O, mints no identity and grants no scope. What it
 * deliberately refuses to do is as much of the contract as what it does:
 *
 * - **It is not a refresh.** Nothing here re-reads the document the Capture was made
 *   from, guarantees that the same document is searched, or records a link between the
 *   old Capture and whatever the new search returns. `purpose` therefore starts at
 *   `find_context`, not `refresh_capture`: the protocol cannot require exact-source
 *   refresh, so seeding that purpose would be a claim the request cannot keep. The user
 *   may still choose any listed purpose themselves.
 * - **It carries text, not authority.** The only Capture field that becomes a query is
 *   the bounded `display_title`, and it lands in the editable question the user reads
 *   before issuing. No summary, context, action item, path, URL, digest, fingerprint,
 *   provenance or identifier is placed in a seed.
 * - **The connection is a hint.** `connectionHint` is the alias the Capture's source
 *   names. It is a suggestion for the launcher to check against a policy the server
 *   actually returned, never a grant, and no corpus is inferred from it — `container_ref`
 *   is a request identifier, not a corpus.
 * - **Nothing is bound to the Capture.** The issued request stays workspace-only. This
 *   seed never becomes a `capture_id`, a Task binding or a stored lineage claim.
 */

/** The action a Capture drawer offers, and the sentence that says what it does. */
export const UPDATED_CONTEXT_LAUNCH_LABEL = 'Search for updated context'
export const UPDATED_CONTEXT_ENTRY_NOTE =
  'Start a new search for your review. This capture stays unchanged.'

export interface KnowledgeLaunchSeed {
  /**
   * Local identity of the context this seed was read from: workspace, Capture and the
   * Capture revision. A launcher compares it to drop a flow started under another one.
   * It is screen state only — it is never sent, stored or shown.
   */
  contextKey: string
  /** The alias the Capture's source names, when it could be one at all. Never authority. */
  connectionHint: string | null
  /** The label the seeded entry renders instead of the plain Inbox search. */
  launchLabel: string
  /** Where the purpose select starts. Always the neutral one. */
  purpose: KnowledgeRequestPurpose
  /** Editable starting question. May be empty; the user writes their own either way. */
  query: string
}

/**
 * The title as a question a person can read and edit.
 *
 * A stored `display_title` is untrusted text: an import or an adapter put it there. The
 * knowledge-request grammar refuses control characters, so each one becomes a space
 * rather than an unusable prefill or a silent truncation, and the existing clip keeps
 * whole code points at the query bound. A title that is nothing but control characters
 * and spaces sanitizes to empty, and empty is left empty — the editor asks the user for
 * a question instead of inventing one.
 */
export function sanitizeKnowledgeSeedQuery(title: unknown): string {
  if (typeof title !== 'string') return ''
  let text = ''
  for (const character of title) {
    text += isKnowledgeQueryControlCode(character.codePointAt(0) ?? 0) ? ' ' : character
  }
  return clipKnowledgeRequestQuery(text.trim())
}

/** The alias only when it is spelled like one. A malformed ref is no hint at all. */
function aliasHint(value: unknown): string | null {
  return typeof value === 'string' && KNOWLEDGE_ALIAS_PATTERN.test(value) ? value : null
}

export function knowledgeLaunchSeedFromCapture(
  capture: Capture,
  workspaceUid: string,
): KnowledgeLaunchSeed {
  return {
    connectionHint: aliasHint(capture.source.connection_ref),
    contextKey: JSON.stringify([workspaceUid, capture.id, capture.revision]),
    launchLabel: UPDATED_CONTEXT_LAUNCH_LABEL,
    purpose: KNOWLEDGE_REQUEST_PURPOSES[0],
    query: sanitizeKnowledgeSeedQuery(capture.source.display_title),
  }
}
