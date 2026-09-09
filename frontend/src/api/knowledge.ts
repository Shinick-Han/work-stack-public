import { z } from 'zod'
import { getData, mutateData, type ResponseReceipt } from './transport'

/**
 * The owner knowledge surface, exactly as `contracts/knowledge-request-http-v1.md`
 * publishes it. Three canonical routes and nothing else.
 *
 * Deliberate boundaries, all of them the contract's own:
 *
 * - **No provider is contacted.** Every function here talks to the loopback server on a
 *   same-origin path. An alias is a nonsecret registry label; nothing in this module
 *   resolves one to an endpoint, and there is no field anywhere below for an endpoint,
 *   a token, a credential, a local path or a provider name.
 * - **No `Idempotency-Key`.** Both POST routes refuse one (`unsupported_idempotency_key`)
 *   because that mechanism caches the whole response body, and an issued response
 *   contains the user's query. Idempotence comes from the caller's `intent_id`.
 * - **No forked transport.** CSRF, `Origin`, `Content-Type`, credentials and the error
 *   envelope are `api/transport.ts`'s, unedited. This module supplies a path, a body and
 *   a schema.
 * - **No minted authority.** The issue body has no `request_id`, `requested_at`,
 *   `expires_at`, `schema`, `scope`, `provider` or `authority` field, so a caller cannot
 *   assert one. The server derives every one of them.
 *
 * The wire literals below are *restated* from the contract rather than imported from the
 * editor helper: the frontend cannot depend on the Python package, and an API schema that
 * mirrors a published contract is the right place for the contract's own spelling. Every
 * response is parsed by a closed schema, so a server that grew a field cannot widen a
 * screen by accident.
 */

export const KNOWLEDGE_CONNECTIONS_PATH = '/api/v1/knowledge/connections'
export const KNOWLEDGE_REQUESTS_PATH = '/api/v1/knowledge/requests'

/** `contracts/knowledge-owner-ledger-v1.md`: the alias and corpus-ref grammar. */
export const KNOWLEDGE_ALIAS_PATTERN = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/
export const KNOWLEDGE_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const TASK_ID_PATTERN = /^T-\d{4,}$/

/** `workstack.knowledge-request.v1`, the one schema this surface issues. */
export const KNOWLEDGE_REQUEST_SCHEMA_ID = 'workstack.knowledge-request.v1'
/** The released purpose allowlist. Never a provider or a tool name. */
export const KNOWLEDGE_REQUEST_PURPOSE_IDS = [
  'find_context',
  'extract_actions',
  'refresh_capture',
] as const

const alias = z.string().regex(KNOWLEDGE_ALIAS_PATTERN)
const canonicalUuid = z.string().regex(KNOWLEDGE_UUID_PATTERN)
const wholeNumber = z.number().int().min(0)

/**
 * One granted connection, as the server projects it by name. `scope` is a server
 * constant with exactly one legal value, so a project-level grant cannot even be read.
 */
export const knowledgeConnectionSchema = z.strictObject({
  alias,
  upstream_workspace_uid: canonicalUuid,
  corpus_refs: z.array(alias),
  scope: z.literal('workspace'),
})

export const knowledgeConnectionPolicySchema = z.strictObject({
  policy_revision: wholeNumber,
  connections: z.array(knowledgeConnectionSchema),
})

const workspaceBindingSchema = z.strictObject({ workspace_uid: canonicalUuid })
const taskBindingSchema = z.strictObject({
  workspace_uid: canonicalUuid,
  task_uid: canonicalUuid,
  task_id: z.string().regex(TASK_ID_PATTERN),
  task_revision: wholeNumber,
})

/**
 * The issued wire document. This is `data` verbatim — what the user carries out of band —
 * so it is parsed closed and handed on unchanged. The editor re-checks it against the
 * draft the user actually reviewed; passing this schema authorises nothing.
 */
export const issuedKnowledgeRequestSchema = z.strictObject({
  schema: z.literal(KNOWLEDGE_REQUEST_SCHEMA_ID),
  request_id: canonicalUuid,
  binding: z.union([taskBindingSchema, workspaceBindingSchema]),
  purpose: z.enum(KNOWLEDGE_REQUEST_PURPOSE_IDS),
  query: z.string().min(1),
  corpus_refs: z.array(alias).min(1),
  result_limit: z.number().int().min(1),
  requested_at: z.string().min(1),
  expires_at: z.string().min(1),
})

/**
 * The bounded ledger facts beside the document. `state` is the ledger record's own state
 * and is read as text: a released state this build has not heard of must still be
 * reportable, not a parse failure that hides an issued request.
 */
export const knowledgeIssueMetaSchema = z.strictObject({
  replayed: z.boolean(),
  state: z.string().min(1),
  connection_alias: alias,
  policy_revision: wholeNumber,
})

/**
 * The optional capacity block `R50-KNOWLEDGE-OCCUPANCY-CONTRACT.md` publishes beside the
 * policy, in envelope `meta`. It is an observation, not part of the policy: the wire
 * `data` schema above is unchanged and stays closed.
 *
 * Read closed and cross-checked, because a number that is not both well formed and
 * internally consistent is not an observation at all. A count above its own bound, a
 * bound of zero, a fraction, a NaN or an extra key means this server is not speaking the
 * published block, and the honest projection of that is *unavailable* — never zero, which
 * would read as "plenty of room", and never a failure, because the policy beside it is
 * perfectly valid and the owner still needs it.
 */
export const knowledgeOccupancySchema = z
  .strictObject({
    request_count: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
    request_bound: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
    encoded_bytes: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
    byte_bound: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
  })
  .refine(
    (block) =>
      block.request_count <= block.request_bound && block.encoded_bytes <= block.byte_bound,
    { message: 'occupancy reports a usage above its own bound' },
  )

export type KnowledgeOccupancy = z.infer<typeof knowledgeOccupancySchema>

/**
 * Project the occupancy block out of an envelope `meta`, or nothing.
 *
 * `safeParse` is the whole point: an old server that sends no `meta`, a server that sends
 * some other `meta`, and a server that sends a malformed occupancy all land on the same
 * answer — no observation — without an exception that a caller would have to tell apart
 * from a real transport failure. Only the four validated numbers are returned, so no
 * other key of `meta` can reach a screen.
 */
export function decodeKnowledgeOccupancy(meta: unknown): KnowledgeOccupancy | undefined {
  if (typeof meta !== 'object' || meta === null) return undefined
  const parsed = knowledgeOccupancySchema.safeParse((meta as { occupancy?: unknown }).occupancy)
  return parsed.success ? parsed.data : undefined
}

export type KnowledgeConnection = z.infer<typeof knowledgeConnectionSchema>
export type KnowledgeConnectionPolicy = z.infer<typeof knowledgeConnectionPolicySchema>
export type IssuedKnowledgeRequestWire = z.infer<typeof issuedKnowledgeRequestSchema>
export type KnowledgeIssueMeta = z.infer<typeof knowledgeIssueMetaSchema>

/**
 * What this module hands its own callers: the policy exactly as before, plus an optional
 * client-side observation. `occupancy` is absent unless the server published a valid one,
 * so a caller that never looks at it sees the object it has always seen, and no POST body
 * anywhere in this module is built from this type.
 */
export interface KnowledgeConnectionPolicyResult extends KnowledgeConnectionPolicy {
  occupancy?: KnowledgeOccupancy
}

/** The closed policy replacement body. `scope` is absent because the server owns it. */
export interface KnowledgeConnectionInput {
  alias: string
  upstream_workspace_uid: string
  corpus_refs: string[]
}

export interface KnowledgeConnectionPolicyBody {
  expected_policy_revision: number
  connections: KnowledgeConnectionInput[]
}

export type KnowledgeRequestBindingBody =
  | { workspace_uid: string }
  | { workspace_uid: string; task_uid: string; task_id: string; task_revision: number }

/** The closed issue body: exactly these seven keys, in the contract's own order. */
export interface KnowledgeIssueBody {
  intent_id: string
  connection_alias: string
  binding: KnowledgeRequestBindingBody
  query: string
  corpus_refs: string[]
  purpose: string
  result_limit: number
}

export interface IssuedKnowledgeRequestReceipt {
  request: IssuedKnowledgeRequestWire
  meta: KnowledgeIssueMeta
}

/**
 * The stored nonsecret policy and its revision. Exposes no secret by construction.
 *
 * The receipt reads the envelope `meta` of the *same* answer — there is no second
 * request and no second route — so the occupancy attached here is the one the server
 * projected from the snapshot it returned this policy from.
 */
export async function fetchKnowledgeConnections(): Promise<KnowledgeConnectionPolicyResult> {
  const receipt: ResponseReceipt = {}
  const policy = await getData(KNOWLEDGE_CONNECTIONS_PATH, knowledgeConnectionPolicySchema, receipt)
  return withOccupancy(policy, receipt.meta)
}

/** Attaches a valid observation and omits the key entirely when there is not one. */
function withOccupancy(
  policy: KnowledgeConnectionPolicy,
  meta: unknown,
): KnowledgeConnectionPolicyResult {
  const occupancy = decodeKnowledgeOccupancy(meta)
  return occupancy ? { ...policy, occupancy } : policy
}

/**
 * Replace the roster under the server's compare-and-set guard.
 *
 * No `Idempotency-Key` (the route refuses one) and therefore no automatic network
 * resend: a write whose outcome is unknown must be re-read, never replayed, because the
 * server owns the revision this body was guarded against. `receipt` is passed so an
 * unreadable 2xx surfaces as `UnreadableSuccessError` rather than as a flat failure the
 * caller would be tempted to retry. Nothing here is a planning change, so the cross-tab
 * planning bus is deliberately not published to.
 */
export async function replaceKnowledgeConnections(
  body: KnowledgeConnectionPolicyBody,
): Promise<KnowledgeConnectionPolicyResult> {
  const receipt: ResponseReceipt = {}
  const policy = await mutateData(
    KNOWLEDGE_CONNECTIONS_PATH,
    'POST',
    body,
    knowledgeConnectionPolicySchema,
    undefined,
    false,
    { receipt },
  )
  // The body above is the caller's closed CAS body, untouched: occupancy is read off the
  // answer and never sent. A malformed one cannot reach here as a throw, so a save the
  // server accepted stays accepted.
  return withOccupancy(policy, receipt.meta)
}

/**
 * Issue one scoped KnowledgeRequest against the stored policy.
 *
 * The caller owns `intent_id` and must reuse it for an unchanged retry: that is the whole
 * of this route's idempotence, and a fresh one would ask the ledger for a second
 * authorization the user never reviewed.
 */
export async function issueKnowledgeRequest(
  body: KnowledgeIssueBody,
): Promise<IssuedKnowledgeRequestReceipt> {
  const receipt: ResponseReceipt = {}
  const request = await mutateData(
    KNOWLEDGE_REQUESTS_PATH,
    'POST',
    body,
    issuedKnowledgeRequestSchema,
    undefined,
    false,
    { receipt },
  )
  return { meta: knowledgeIssueMetaSchema.parse(receipt.meta), request }
}
