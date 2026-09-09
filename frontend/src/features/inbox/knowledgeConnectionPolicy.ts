import {
  KNOWLEDGE_ALIAS_PATTERN,
  KNOWLEDGE_UUID_PATTERN,
  type KnowledgeConnection,
  type KnowledgeConnectionPolicyBody,
} from '../../api/knowledge'
import type { KnowledgeCorpusOption } from './knowledgeRequestDraft'

/**
 * The arithmetic behind the owner connection settings form.
 *
 * It holds no state, performs no I/O and reads no clock. Every check restates the
 * backend's own grammar from `contracts/knowledge-request-http-v1.md` so a reader is told
 * what is wrong before a round trip — the server re-validates all of it, and passing here
 * grants nothing.
 *
 * The form is deliberately narrow. A connection is an **alias**, an **upstream workspace
 * UUID** and a list of **corpus aliases**. There is no endpoint, host, share, drive, local
 * path, token or credential field, and the alias grammar cannot express one: no scheme,
 * no separator, no `user:secret@`. `scope` is absent because the server supplies it, and
 * a body has no way to spell a project-level grant.
 */

export const MAX_POLICY_CONNECTIONS = 8
export const MAX_CONNECTION_CORPUS_REFS = 8

/** One editable row. Every value is the raw text the owner typed. */
export interface ConnectionPolicyRow {
  alias: string
  upstreamWorkspaceUid: string
  corpusRefs: string
}

export const EMPTY_CONNECTION_ROW: ConnectionPolicyRow = {
  alias: '',
  corpusRefs: '',
  upstreamWorkspaceUid: '',
}

export type PolicyDraftRefusalCode =
  | 'no_connections'
  | 'too_many_connections'
  | 'invalid_alias'
  | 'duplicate_alias'
  | 'invalid_upstream_workspace_uid'
  | 'no_corpus_refs'
  | 'too_many_corpus_refs'
  | 'invalid_corpus_ref'
  | 'duplicate_corpus_ref'

const REFUSAL_COPY: Record<PolicyDraftRefusalCode, string> = {
  no_connections: 'Add at least one connection before saving.',
  too_many_connections: `Keep the roster to ${MAX_POLICY_CONNECTIONS} connections or fewer.`,
  invalid_alias:
    'A connection alias is lowercase letters, digits, dot, dash and underscore, starting and ending with a letter or digit. It is a label, never an address.',
  duplicate_alias: 'Two connections use the same alias. Give each one its own.',
  invalid_upstream_workspace_uid:
    'The upstream workspace ID has to be a canonical UUID, in lowercase.',
  no_corpus_refs: 'Give each connection at least one corpus alias.',
  too_many_corpus_refs: `Give each connection ${MAX_CONNECTION_CORPUS_REFS} corpus aliases or fewer.`,
  invalid_corpus_ref:
    'A corpus alias uses the same grammar as a connection alias. It is a label, never a path or a URL.',
  duplicate_corpus_ref: 'A connection lists the same corpus alias twice. List each one once.',
}

export type PolicyDraftResult =
  | { ok: true; body: KnowledgeConnectionPolicyBody }
  | { ok: false; code: PolicyDraftRefusalCode; message: string }

function refuse(code: PolicyDraftRefusalCode): PolicyDraftResult {
  // The code varies; the copy is authored and interpolates nothing the owner typed.
  return { code, message: REFUSAL_COPY[code], ok: false }
}

/** Splits the typed corpus list on commas and whitespace. Empty runs are dropped. */
export function parseCorpusRefs(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
}

function checkCorpusRefs(refs: readonly string[]): PolicyDraftRefusalCode | null {
  if (!refs.length) return 'no_corpus_refs'
  if (refs.length > MAX_CONNECTION_CORPUS_REFS) return 'too_many_corpus_refs'
  const seen = new Set<string>()
  for (const ref of refs) {
    if (!KNOWLEDGE_ALIAS_PATTERN.test(ref)) return 'invalid_corpus_ref'
    if (seen.has(ref)) return 'duplicate_corpus_ref'
    seen.add(ref)
  }
  return null
}

function checkRow(
  row: ConnectionPolicyRow,
  aliases: Set<string>,
  refs: readonly string[],
): PolicyDraftRefusalCode | null {
  if (!KNOWLEDGE_ALIAS_PATTERN.test(row.alias.trim())) return 'invalid_alias'
  if (aliases.has(row.alias.trim())) return 'duplicate_alias'
  if (!KNOWLEDGE_UUID_PATTERN.test(row.upstreamWorkspaceUid.trim())) {
    return 'invalid_upstream_workspace_uid'
  }
  return checkCorpusRefs(refs)
}

/**
 * Turns the typed rows into the closed policy body, or into one refusal.
 *
 * `expectedPolicyRevision` is the revision the form was *loaded* at. It is the server's
 * compare-and-set guard: if another owner replaced the roster meanwhile, the write refuses
 * and nothing is overwritten.
 */
export function buildConnectionPolicyBody(
  rows: readonly ConnectionPolicyRow[],
  expectedPolicyRevision: number,
): PolicyDraftResult {
  if (!rows.length) return refuse('no_connections')
  if (rows.length > MAX_POLICY_CONNECTIONS) return refuse('too_many_connections')
  const aliases = new Set<string>()
  const connections: KnowledgeConnectionPolicyBody['connections'] = []
  for (const row of rows) {
    const refs = parseCorpusRefs(row.corpusRefs)
    const problem = checkRow(row, aliases, refs)
    if (problem) return refuse(problem)
    aliases.add(row.alias.trim())
    connections.push({
      alias: row.alias.trim(),
      corpus_refs: refs,
      upstream_workspace_uid: row.upstreamWorkspaceUid.trim(),
    })
  }
  return { body: { connections, expected_policy_revision: expectedPolicyRevision }, ok: true }
}

/** The server's stored roster, as editable rows. Nothing is invented for a missing field. */
export function connectionPolicyRows(
  connections: readonly KnowledgeConnection[],
): ConnectionPolicyRow[] {
  return connections.map((connection) => ({
    alias: connection.alias,
    corpusRefs: connection.corpus_refs.join(', '),
    upstreamWorkspaceUid: connection.upstream_workspace_uid,
  }))
}

/**
 * The corpora one selected connection grants, in the editor's option shape.
 *
 * The label is the alias itself: the server authors no display copy for a corpus, and
 * deriving one from a path, host or share is exactly what this surface must not do.
 */
export function connectionCorpusOptions(
  connection: KnowledgeConnection | null,
): KnowledgeCorpusOption[] {
  if (!connection) return []
  return connection.corpus_refs.map((ref) => ({
    alias: ref,
    description: null,
    label: ref,
  }))
}
