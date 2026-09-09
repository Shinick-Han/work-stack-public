import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from 'react'
import { Dialog } from '../../components/Dialog'
import { Button, EmptyState, LoadingBlock, Pill } from '../../components/Primitives'
import {
  fetchKnowledgeConnections,
  issueKnowledgeRequest,
  replaceKnowledgeConnections,
  type KnowledgeConnection,
  type KnowledgeConnectionPolicy,
  type KnowledgeConnectionPolicyResult,
  type KnowledgeIssueBody,
} from '../../api/knowledge'
import {
  KnowledgeConnectionSettings,
  type KnowledgePolicyReview,
} from './KnowledgeConnectionSettings'
import {
  buildConnectionPolicyBody,
  connectionCorpusOptions,
  connectionPolicyRows,
  EMPTY_CONNECTION_ROW,
  type ConnectionPolicyRow,
} from './knowledgeConnectionPolicy'
import { KnowledgeIssueError, KnowledgeRequestDialog } from './KnowledgeRequestDialog'
import { KnowledgeStorageUsage } from './KnowledgeStorageUsage'
import type { KnowledgeLaunchSeed } from './knowledgeLaunchSeed'
import {
  asIssueError,
  isUnknownOutcome,
  refusalCode,
  refusalCopy,
  settledIssueOutcome,
} from './knowledgeLauncherRefusals'
import {
  IDLE_POLICY,
  isAuthoritative,
  usageIsOutdated,
  type PolicyState,
} from './knowledgePolicyState'
import type { KnowledgeRequestDraft } from './knowledgeRequestDraft'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'
import './KnowledgeRequestLauncher.css'

/**
 * The Inbox entry point for one scoped knowledge request.
 *
 * This is the parent `KNOWLEDGE-REQUEST-UI.md` said the editor was waiting for. It owns
 * the three things the editor deliberately refuses to own: the transport, the scope it
 * offers, and the intent identity a retry is allowed to reuse.
 *
 * - **The scope is the server's.** `corpusOptions` is built from the corpora the *stored
 *   owner policy* returned for the connection the user picked, and from nothing else.
 *   This screen never invents an alias, never merges two connections' grants and never
 *   offers a corpus the read did not return.
 * - **The identity is per logical attempt.** One `intent_id` is minted for one body. An
 *   unchanged retry of a failed or ambiguous attempt reuses it, so the ledger recognises
 *   the retry instead of authorising a second request. Any edit is a different body and
 *   mints a fresh one. A lapsed request is never renewed silently: the user has to press
 *   an explicit control that discards the old intent.
 * - **The server is the authority.** Nothing here mints `request_id`, `requested_at`,
 *   `expires_at`, `schema` or `scope`, and nothing here decides whether a corpus is
 *   granted. The policy read is state to display, the policy write is compare-and-set,
 *   and a lost compare-and-set re-reads rather than overwriting.
 * - **What is not known is said to be unknown.** Only the issuer's own closed refusal is
 *   reported as "not issued"; a lost response, an unreadable success or an answer about
 *   another revision leaves the outcome unconfirmed. Only a *successful* read establishes
 *   what the policy holds — a failed one leaves the last read on screen marked stale, with
 *   writes and new requests held until an authoritative read succeeds. Nothing re-reads or
 *   re-sends itself: every recovery here is a control the user presses.
 * - **A stale answer never lands.** Every asynchronous settlement is checked against a
 *   generation counter that a workspace change, a refetch and a policy write all advance,
 *   and against the connection alias and policy revision the reply itself reports. A
 *   receipt already on screen is dropped by remounting the editor under a key derived
 *   from the workspace, the Capture context if there is one, the connection and the
 *   policy revision.
 * - **No credential, no provider, no Task write.** There is no endpoint, token, path or
 *   account anywhere in this flow; no provider is contacted; the binding is workspace
 *   identity only and no Task is read, attached or modified.
 * - **A seed starts a search; it does not refresh anything.** An optional
 *   `KnowledgeLaunchSeed` lets a saved Capture offer starting values — an editable
 *   question, a neutral purpose and a connection alias to *check* against a policy read.
 *   It never binds the Capture to the request, never guarantees the same document is
 *   searched and never records a link back; the request stays workspace-only and the
 *   Capture it came from is neither read again nor changed.
 * - **Nothing is persisted.** No query, body, intent or receipt reaches `localStorage`,
 *   the address bar, a log or an analytics call. The policy write does not publish to the
 *   cross-tab planning bus either — it is not a planning change.
 */

const LAUNCH_LABEL = 'Search knowledge'
const CHOOSER_TITLE = 'Search a knowledge connection'
const CHOOSER_DESCRIPTION =
  'Pick one connection the owner policy grants. Its corpora are the only scope a request can name, and the server issues the request itself.'
const NOT_CONFIGURED =
  'No knowledge connection is configured for this workspace yet. Add one — an alias, the upstream workspace ID it stands for, and the corpus aliases it grants — and the search becomes available.'
const CONNECTION_NOTE =
  'A connection is a label the owner registered. Work Stack stores no endpoint, folder, account or token for it and contacts no provider.'
const READ_FAILED =
  'The connection policy could not be read. Nothing was changed. Try reading it again.'
const SAVE_FAILED = 'The connection policy was not saved. The roster on the server is unchanged.'
const SAVE_UNKNOWN =
  'The server did not confirm the save, so the outcome is unknown. The roster below has been re-read from the server rather than sent again — read it and save again only if it is still not what you want.'
const SAVE_UNKNOWN_UNREAD =
  'The server did not confirm the save and could not be re-read afterwards, so neither the outcome of the save nor the policy the server now holds is known. Nothing was sent again. Read the policy again before deciding what to do.'
const CAS_REFUSED =
  'The save was refused because the policy revision it was written against is no longer the current one. Nothing here was written.'
const CAS_UNREAD =
  'Another owner replaced the policy, so nothing here was written — and the re-read that would show you their version did not answer. What the server holds now is not known. Read the policy again.'
const STALE_POLICY =
  'This is the last policy that was read successfully. A later read did not answer, so whether the server still holds it is not known. Requests and policy saves are held until a read succeeds.'
const STALE_POLICY_EMPTY =
  'The last successful read showed no connection, but a later read did not answer, so what the server holds now is not known. Read the policy again before configuring one.'
const POLICY_UNKNOWN_WRITE =
  'The policy the server holds is not known right now, so there is no revision to save against. Nothing was sent. Read the policy again, then save.'
const ISSUE_STALE =
  'That answer arrived for a connection, workspace or policy revision this screen is no longer showing, so it was not displayed.'
const NO_CONNECTION_SELECTED = 'No connection is selected, so there is nothing to ask.'
const POLICY_NOT_CURRENT = 'The current connection policy is not known, so nothing was asked.'
const FRESH_REQUEST_LABEL = 'Start a fresh request'
const REREAD_LABEL = 'Read the policy again'

interface SettingsState {
  error: string | null
  notice: string | null
  open: boolean
  /** What a follow-up read established, including that it established nothing. */
  review: KnowledgePolicyReview | null
  rows: ConnectionPolicyRow[]
  saving: boolean
}

interface IssueNotice {
  fresh: boolean
  text: string
}

const CLOSED_SETTINGS: SettingsState = {
  error: null,
  notice: null,
  open: false,
  review: null,
  rows: [],
  saving: false,
}

/**
 * The identity of "this exact request body". Two attempts that agree on every one of
 * these are the same logical attempt and must reuse one `intent_id`.
 */
function bodyIdentity(
  workspaceUid: string,
  alias: string,
  policyRevision: number,
  draft: KnowledgeRequestDraft,
): string {
  return JSON.stringify([
    workspaceUid,
    alias,
    policyRevision,
    draft.binding,
    draft.purpose,
    draft.query,
    [...draft.corpus_refs],
    draft.result_limit,
  ])
}

function mintIntentId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  throw new Error('This browser cannot mint a request intent identifier.')
}

export interface KnowledgeRequestLauncherProps {
  /** The workspace the Inbox is showing, from the caller's own trusted projection. */
  workspaceUid: string
  /** Injected only so a test can name the intent. Production mints a UUID. */
  newIntentId?: () => string
  /** Opens the existing import preview with a validated execute proposal. */
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  /**
   * Starting values one saved Capture offers a *new* search. It renames the launch
   * control, seeds the editable question and purpose, and suggests a connection alias
   * this screen only uses once a policy read has actually returned it. It binds nothing:
   * the request stays workspace-only and no Capture is read, claimed or changed.
   */
  seed?: KnowledgeLaunchSeed | null
}

interface LauncherFlow {
  /** True only while the policy on screen is one a read actually returned. */
  authoritative: boolean
  chooserOpen: boolean
  closeChooser: () => void
  /** Closes the editor and nothing else. The review handoff opens another surface. */
  closeEditor: () => void
  connection: KnowledgeConnection | null
  editorKey: string
  editorOpen: boolean
  issue: (draft: KnowledgeRequestDraft) => Promise<unknown>
  issueNotice: IssueNotice | null
  /** What Cancel does: a seeded flow goes back to the roster rather than to nothing. */
  leaveEditor: () => void
  load: () => void
  openChooser: () => void
  policy: PolicyState
  saveSettings: () => void
  select: (alias: string) => void
  setSettings: (next: (state: SettingsState) => SettingsState) => void
  settings: SettingsState
  startFresh: () => void
  toggleSettings: () => void
}

interface LauncherRefs {
  /** Whether `policy` is a policy an actual read returned, rather than a stale one. */
  authoritative: MutableRefObject<boolean>
  generation: MutableRefObject<number>
  intent: MutableRefObject<{ id: string; key: string } | null>
  policy: MutableRefObject<KnowledgeConnectionPolicy | null>
  /** The Capture context this flow was opened from, or `null` for the plain Inbox entry. */
  seed: MutableRefObject<KnowledgeLaunchSeed | null>
  /** A seeded flow is open and its hint has not been offered a roster yet. */
  seedHint: MutableRefObject<boolean>
  selected: MutableRefObject<string | null>
  settings: MutableRefObject<SettingsState>
}

interface LauncherSetters {
  setChooserOpen: Dispatch<SetStateAction<boolean>>
  setEditorOpen: Dispatch<SetStateAction<boolean>>
  setIssueNotice: Dispatch<SetStateAction<IssueNotice | null>>
  setPolicy: Dispatch<SetStateAction<PolicyState>>
  setSelected: Dispatch<SetStateAction<string | null>>
  setSettings: Dispatch<SetStateAction<SettingsState>>
}

/**
 * What every asynchronous step reads at the moment it settles, rather than what a handler
 * captured when it was created. It is one stable object, so the callbacks built from it
 * are stable too and a settled promise is judged against the screen that exists now.
 */
interface LauncherContext {
  newIntentId: () => string
  refs: LauncherRefs
  set: LauncherSetters
  workspaceUid: string
}

/** Adopts a policy the server just answered with, dropping a grant it no longer lists. */
function applyPolicy(ctx: LauncherContext, next: KnowledgeConnectionPolicyResult) {
  // The observation is split off here and never stored as policy: it is not part of the
  // roster, nothing is written against it, and only these four validated numbers exist.
  const { occupancy = null, ...policy } = next
  ctx.set.setPolicy({ error: null, policy, stale: false, status: 'ready', usage: occupancy })
  // A read that answered settles what an earlier failed read left unknown. The caller that
  // asked for this read may still set a review of its own afterwards.
  ctx.set.setSettings((state) => (state.review ? { ...state, error: null, review: null } : state))
  if (!next.connections.some((entry) => entry.alias === ctx.refs.selected.current)) {
    ctx.set.setSelected(null)
    ctx.set.setEditorOpen(false)
  }
  applySeedHint(ctx, next)
}

/**
 * The Capture's own connection alias, used only once the server has said it is still
 * granted. The roster this checks is the one the read just returned, so a revoked,
 * renamed or unknown alias simply selects nothing and the owner's list stays on screen.
 * No corpus is implied: the editor opens with an empty scope the user picks themselves.
 *
 * The hint is offered to the first roster of each opened seeded flow and then spent, so a
 * later read — a policy save, an explicit re-read — never reopens the editor behind the
 * owner, and a choice the user already made is never overwritten.
 */
function applySeedHint(ctx: LauncherContext, next: KnowledgeConnectionPolicyResult) {
  if (!ctx.refs.seedHint.current) return
  ctx.refs.seedHint.current = false
  const hint = ctx.refs.seed.current?.connectionHint ?? null
  if (hint === null || ctx.refs.selected.current !== null) return
  if (!next.connections.some((entry) => entry.alias === hint)) return
  ctx.set.setSelected(hint)
  ctx.set.setChooserOpen(false)
  ctx.set.setEditorOpen(true)
}

/**
 * A read that did not answer. The last successful read, if there is one, stays on screen
 * *marked stale* — it is not evidence of what the server holds now, and it is certainly
 * not evidence of an empty roster. Nothing is asked or written until a read succeeds.
 */
function markPolicyUnread(ctx: LauncherContext, error: unknown) {
  ctx.set.setEditorOpen(false)
  ctx.set.setPolicy((state) => ({
    error: refusalCopy(error, READ_FAILED),
    policy: state.policy,
    stale: true,
    status: state.policy ? 'ready' : 'error',
    // Kept, not discarded: it is still what the last successful read saw, and the region
    // says so. Zeroing it here would invent room the server never reported.
    usage: state.usage,
  }))
}

async function loadPolicy(ctx: LauncherContext) {
  const token = ++ctx.refs.generation.current
  ctx.set.setPolicy((state) => ({ ...state, error: null, status: 'loading' }))
  try {
    const next = await fetchKnowledgeConnections()
    if (ctx.refs.generation.current !== token) return
    applyPolicy(ctx, next)
  } catch (error) {
    if (ctx.refs.generation.current !== token) return
    markPolicyUnread(ctx, error)
  }
}

/**
 * Re-reads what the server holds. It is never a replay of a write of unknown outcome, and
 * a read that fails returns `unknown` rather than an absence a caller could read as empty.
 */
async function refetchPolicy(ctx: LauncherContext, token: number): Promise<KnowledgePolicyReview> {
  try {
    const next = await fetchKnowledgeConnections()
    if (ctx.refs.generation.current === token) applyPolicy(ctx, next)
    return { connections: next.connections, kind: 'held' }
  } catch (error) {
    if (ctx.refs.generation.current === token) markPolicyUnread(ctx, error)
    return { kind: 'unknown' }
  }
}

function settleIssueFailure(ctx: LauncherContext, error: unknown) {
  const settled = settledIssueOutcome(error)
  if (!settled) return
  ctx.set.setEditorOpen(false)
  ctx.set.setChooserOpen(true)
  ctx.set.setIssueNotice({ fresh: settled.fresh, text: refusalCopy(error, READ_FAILED) })
  if (settled.refetch) void loadPolicy(ctx)
}

/** One `intent_id` per logical attempt. An unchanged body reuses the one already minted. */
function intentIdFor(ctx: LauncherContext, key: string) {
  const held = ctx.refs.intent.current
  if (held?.key === key) return held.id
  const id = ctx.newIntentId()
  ctx.refs.intent.current = { id, key }
  return id
}

async function runIssue(ctx: LauncherContext, draft: KnowledgeRequestDraft) {
  const alias = ctx.refs.selected.current
  const current = ctx.refs.policy.current
  // Refusing here is a decision this screen made before anything was sent, so it is one of
  // the few failures that truthfully *is* "not issued".
  if (!alias || !current) throw new KnowledgeIssueError('refused', NO_CONNECTION_SELECTED)
  if (!ctx.refs.authoritative.current) throw new KnowledgeIssueError('refused', POLICY_NOT_CURRENT)
  const token = ctx.refs.generation.current
  const body: KnowledgeIssueBody = {
    binding: draft.binding,
    connection_alias: alias,
    corpus_refs: [...draft.corpus_refs],
    intent_id: intentIdFor(
      ctx,
      bodyIdentity(ctx.workspaceUid, alias, current.policy_revision, draft),
    ),
    purpose: draft.purpose,
    query: draft.query,
    result_limit: draft.result_limit,
  }
  try {
    const receipt = await issueKnowledgeRequest(body)
    // The reply has to answer the connection and the revision this screen is showing,
    // not merely be well formed. A late one is dropped rather than displayed.
    if (
      ctx.refs.generation.current !== token ||
      ctx.refs.selected.current !== alias ||
      receipt.meta.connection_alias !== alias ||
      receipt.meta.policy_revision !== current.policy_revision
    ) {
      throw new KnowledgeIssueError('stale', ISSUE_STALE)
    }
    ctx.set.setIssueNotice(null)
    return receipt.request
  } catch (error) {
    // The intent is deliberately kept: an unchanged retry must reuse it.
    if (ctx.refs.generation.current === token) settleIssueFailure(ctx, error)
    // The editor is told which of the three outcomes this was, and nothing else. It is
    // never handed a server string, and an outcome nothing established stays unknown.
    throw asIssueError(error)
  }
}

/**
 * A lost compare-and-set and an unknown outcome both end the same way: re-read what the
 * server holds and put it in front of the owner. Neither one sends the body again.
 */
async function failSave(ctx: LauncherContext, error: unknown, token: number) {
  const lostCas = refusalCode(error) === 'policy_revision_changed'
  if (!lostCas && !isUnknownOutcome(error)) {
    ctx.set.setSettings((state) => ({
      ...state,
      error: refusalCopy(error, SAVE_FAILED),
      notice: null,
      saving: false,
    }))
    return
  }
  const review = await refetchPolicy(ctx, token)
  if (ctx.refs.generation.current !== token) return
  // A read that did not answer establishes nothing. It is reported as unknown — never as
  // an empty roster, and never by re-sending the body.
  const unread = review.kind === 'unknown'
  // Cause comes from what actually settled the write, never from the read: a definitive
  // compare-and-set refusal establishes that nothing was written, an unconfirmed one
  // establishes nothing at all. The review itself only reports the roster it read.
  ctx.set.setSettings((state) => ({
    ...state,
    error: unread ? (lostCas ? CAS_UNREAD : SAVE_UNKNOWN_UNREAD) : lostCas ? CAS_REFUSED : null,
    notice: unread || lostCas ? null : SAVE_UNKNOWN,
    review,
    saving: false,
  }))
}

async function runSave(ctx: LauncherContext) {
  const current = ctx.refs.policy.current
  if (!current || !ctx.refs.authoritative.current) {
    // There is no revision to compare against, so there is no safe write. Saying "0" here
    // would be claiming the server holds nothing.
    ctx.set.setSettings((state) => ({ ...state, error: POLICY_UNKNOWN_WRITE, notice: null }))
    return
  }
  const built = buildConnectionPolicyBody(ctx.refs.settings.current.rows, current.policy_revision)
  if (!built.ok) {
    ctx.set.setSettings((state) => ({ ...state, error: built.message, notice: null }))
    return
  }
  // A policy write invalidates every read and issue already in flight, and retires the
  // intent an earlier revision authorised.
  const token = ++ctx.refs.generation.current
  ctx.refs.intent.current = null
  ctx.set.setIssueNotice(null)
  ctx.set.setEditorOpen(false)
  ctx.set.setSettings((state) => ({ ...state, error: null, notice: null, saving: true }))
  try {
    const next = await replaceKnowledgeConnections(built.body)
    if (ctx.refs.generation.current !== token) return
    applyPolicy(ctx, next)
    ctx.set.setSettings(CLOSED_SETTINGS)
  } catch (error) {
    await failSave(ctx, error, token)
  }
}

function toggleSettingsForm(ctx: LauncherContext) {
  ctx.set.setSettings((state) => {
    if (state.open) return CLOSED_SETTINGS
    const rows = connectionPolicyRows(ctx.refs.policy.current?.connections ?? [])
    return {
      error: null,
      notice: null,
      open: true,
      review: null,
      rows: rows.length ? rows : [{ ...EMPTY_CONNECTION_ROW }],
      saving: false,
    }
  })
}

/**
 * Back to nothing read, nothing selected and nothing asked. Advancing the generation is
 * what makes it safe: a read or an issue still in flight for the previous context settles
 * against a token that is no longer current, so it is dropped rather than displayed.
 */
function resetLauncher(ctx: LauncherContext) {
  ctx.refs.generation.current += 1
  ctx.refs.intent.current = null
  ctx.refs.seedHint.current = false
  ctx.set.setPolicy(IDLE_POLICY)
  ctx.set.setSelected(null)
  ctx.set.setEditorOpen(false)
  ctx.set.setChooserOpen(false)
  ctx.set.setIssueNotice(null)
  ctx.set.setSettings(CLOSED_SETTINGS)
}

/**
 * Closing a seeded flow ends it, so nothing it started may land afterwards. Advancing the
 * generation is what makes that true: a policy read or an issue still in flight settles
 * against a token that is no longer current, and the hint is disarmed so a roster that
 * arrives late cannot reopen the editor behind the owner. The intent is deliberately left
 * alone — an unchanged retry the user starts again must still reuse the one it minted.
 *
 * The plain Inbox entry is untouched by this: it keeps the roster it already read and
 * simply hides, exactly as it did before, so reopening it still costs no second read.
 */
function closeChooserFlow(ctx: LauncherContext) {
  if (ctx.refs.seed.current) {
    ctx.refs.generation.current += 1
    ctx.refs.seedHint.current = false
  }
  ctx.set.setChooserOpen(false)
}

function useKnowledgeLauncher(props: KnowledgeRequestLauncherProps): LauncherFlow {
  const { workspaceUid } = props
  const seedKey = props.seed?.contextKey ?? ''
  const [chooserOpen, setChooserOpen] = useState(false)
  const [policy, setPolicy] = useState<PolicyState>(IDLE_POLICY)
  const [selected, setSelected] = useState<string | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [issueNotice, setIssueNotice] = useState<IssueNotice | null>(null)
  const [settings, setSettings] = useState<SettingsState>(CLOSED_SETTINGS)

  // `generation` is what every settlement is checked against. A workspace change, a
  // refetch and a policy write all advance it, so an answer for the previous state is
  // dropped instead of populating the current screen.
  const refs: LauncherRefs = {
    authoritative: useRef(false),
    generation: useRef(0),
    intent: useRef<{ id: string; key: string } | null>(null),
    policy: useRef<KnowledgeConnectionPolicy | null>(null),
    seed: useRef<KnowledgeLaunchSeed | null>(null),
    seedHint: useRef(false),
    selected: useRef<string | null>(null),
    settings: useRef<SettingsState>(CLOSED_SETTINGS),
  }
  refs.authoritative.current = isAuthoritative(policy)
  refs.policy.current = policy.policy
  refs.seed.current = props.seed ?? null
  refs.selected.current = selected
  refs.settings.current = settings

  const held = useRef<LauncherContext | null>(null)
  held.current ??= {
    newIntentId: mintIntentId,
    refs,
    set: { setChooserOpen, setEditorOpen, setIssueNotice, setPolicy, setSelected, setSettings },
    workspaceUid,
  }
  const ctx = held.current
  ctx.newIntentId = props.newIntentId ?? mintIntentId
  ctx.workspaceUid = workspaceUid

  useEffect(() => {
    // A different workspace — or a different Capture context, at a different revision —
    // is a different question entirely: nothing read, selected or issued under the
    // previous one may survive into it.
    resetLauncher(ctx)
  }, [ctx, seedKey, workspaceUid])

  const connection = policy.policy?.connections.find((entry) => entry.alias === selected) ?? null
  const authoritative = isAuthoritative(policy)
  return {
    authoritative,
    chooserOpen,
    closeChooser: useCallback(() => closeChooserFlow(ctx), [ctx]),
    closeEditor: useCallback(() => setEditorOpen(false), []),
    // Leaving a seeded editor returns to the roster it was chosen from, so the owner can
    // pick a different granted connection instead of starting the search over.
    leaveEditor: useCallback(() => { setEditorOpen(false); if (ctx.refs.seed.current) setChooserOpen(true) }, [ctx]),
    connection,
    editorKey: `${workspaceUid}:${seedKey}:${policy.policy?.policy_revision ?? -1}:${selected ?? ''}`,
    // A stale or unread policy is not scope to ask against, so the editor is not offered.
    editorOpen: editorOpen && connection !== null && authoritative,
    issue: useCallback((draft: KnowledgeRequestDraft) => runIssue(ctx, draft), [ctx]),
    issueNotice,
    load: useCallback(() => void loadPolicy(ctx), [ctx]),
    openChooser: useCallback(() => {
      setChooserOpen(true)
      setIssueNotice(null)
      // A seeded flow always reads the policy again before its hint may be used: a roster
      // this screen happens to be holding is not evidence of what the owner grants now.
      if (ctx.refs.seed.current) ctx.refs.seedHint.current = true
      if (ctx.refs.seed.current || !ctx.refs.policy.current) void loadPolicy(ctx)
    }, [ctx]),
    policy,
    saveSettings: useCallback(() => void runSave(ctx), [ctx]),
    select: useCallback((alias: string) => {
      ctx.refs.intent.current = null
      setSelected(alias)
      setIssueNotice(null)
      setChooserOpen(false)
      setEditorOpen(true)
    }, [ctx]),
    setSettings,
    settings,
    startFresh: useCallback(() => {
      // The only way an intent is discarded. A lapsed or superseded request is raised
      // again under a new one the user explicitly asks for and then reviews.
      ctx.refs.intent.current = null
      setIssueNotice(null)
      if (ctx.refs.selected.current) {
        setChooserOpen(false)
        setEditorOpen(true)
      }
    }, [ctx]),
    toggleSettings: useCallback(() => toggleSettingsForm(ctx), [ctx]),
  }
}

function ConnectionList({
  connections,
  disabled,
  onSelect,
}: {
  connections: readonly KnowledgeConnection[]
  disabled?: boolean
  onSelect: (alias: string) => void
}) {
  return (
    <ul className="knowledge-launcher__connections">
      {connections.map((entry) => (
        <li key={entry.alias}>
          <div>
            <strong>{entry.alias}</strong>
            <Pill tone="neutral">{entry.scope}</Pill>
            <small>{entry.corpus_refs.join(' · ')}</small>
          </div>
          <Button
            disabled={disabled}
            icon="search"
            onClick={() => onSelect(entry.alias)}
            variant="secondary"
          >
            Search {entry.alias}
          </Button>
        </li>
      ))}
    </ul>
  )
}

/**
 * The last policy a read returned, shown *as* the last read rather than as the current
 * one. Nothing here can be searched: the only control is the read that would settle it.
 */
function StalePolicyBody({ flow }: { flow: LauncherFlow }) {
  const connections = flow.policy.policy?.connections ?? []
  return (
    <>
      <div className="knowledge-launcher__notice" role="alert">
        <p>{connections.length ? STALE_POLICY : STALE_POLICY_EMPTY}</p>
        <Button icon="refresh" onClick={flow.load} variant="secondary">{REREAD_LABEL}</Button>
      </div>
      {connections.length ? (
        <ConnectionList connections={connections} disabled onSelect={flow.select} />
      ) : null}
    </>
  )
}

function ChooserBody({ flow }: { flow: LauncherFlow }) {
  const { policy } = flow
  if (policy.status === 'loading') return <LoadingBlock label="Reading the connection policy…" />
  if (policy.status === 'error') {
    return (
      <EmptyState
        action={<Button icon="refresh" onClick={flow.load}>{REREAD_LABEL}</Button>}
        icon="warning"
        title="The connection policy is unavailable"
      >
        {policy.error}
      </EmptyState>
    )
  }
  if (policy.stale) return <StalePolicyBody flow={flow} />
  if (!policy.policy) return <LoadingBlock label="Reading the connection policy…" />
  if (!policy.policy.connections.length) {
    return (
      <EmptyState
        action={<Button icon="plus" onClick={flow.toggleSettings} variant="primary">Configure connections</Button>}
        icon="context"
        title="No knowledge connection yet"
      >
        {NOT_CONFIGURED}
      </EmptyState>
    )
  }
  return <ConnectionList connections={policy.policy.connections} onSelect={flow.select} />
}

function ChooserNotice({ flow }: { flow: LauncherFlow }) {
  if (!flow.issueNotice) return null
  return (
    <div className="knowledge-launcher__notice" role="alert">
      <p>{flow.issueNotice.text}</p>
      {flow.issueNotice.fresh ? (
        <Button icon="refresh" onClick={flow.startFresh} variant="secondary">
          {FRESH_REQUEST_LABEL}
        </Button>
      ) : null}
    </div>
  )
}

function ChooserDialog({ flow }: { flow: LauncherFlow }) {
  return (
    <Dialog
      description={CHOOSER_DESCRIPTION}
      footer={
        <>
          <Button onClick={flow.closeChooser} variant="ghost">Close</Button>
          <Button icon="command" onClick={flow.toggleSettings}>
            {flow.settings.open ? 'Hide connection settings' : 'Connection settings'}
          </Button>
        </>
      }
      onClose={flow.closeChooser}
      open
      size="large"
      title={CHOOSER_TITLE}
    >
      <ChooserNotice flow={flow} />
      <p className="knowledge-launcher__lede">{CONNECTION_NOTE}</p>
      <ChooserBody flow={flow} />
      <KnowledgeStorageUsage outdated={usageIsOutdated(flow.policy)} usage={flow.policy.usage} />
      {flow.settings.open ? (
        <KnowledgeConnectionSettings
          error={flow.settings.error}
          notice={flow.settings.notice}
          onCancel={flow.toggleSettings}
          onReread={flow.load}
          onRowsChange={(rows) => flow.setSettings((state) => ({ ...state, rows }))}
          onSave={flow.saveSettings}
          // `null` where a revision would be fabricated: an unread policy has no known one.
          policyRevision={flow.authoritative ? (flow.policy.policy?.policy_revision ?? null) : null}
          review={flow.settings.review}
          rows={flow.settings.rows}
          saving={flow.settings.saving}
        />
      ) : null}
    </Dialog>
  )
}

export function KnowledgeRequestLauncher(props: KnowledgeRequestLauncherProps) {
  const flow = useKnowledgeLauncher(props)
  return (
    <>
      <Button icon="search" onClick={flow.openChooser} variant="secondary">
        {props.seed ? props.seed.launchLabel : LAUNCH_LABEL}
      </Button>
      {flow.chooserOpen ? <ChooserDialog flow={flow} /> : null}
      {flow.editorOpen && flow.connection ? (
        // Remounting under this key is how a policy change, a connection change or a
        // workspace change drops a receipt that is already on screen: the editor's own
        // input identity restarts, and nothing issued under the old scope survives.
        <KnowledgeRequestDialog
          corpusOptions={connectionCorpusOptions(flow.connection)}
          key={flow.editorKey}
          onClose={flow.leaveEditor}
          onIssue={flow.issue}
          onReviewKnowledge={
            props.onReviewKnowledge
              ? (envelope) => {
                  flow.closeEditor()
                  props.onReviewKnowledge?.(envelope)
                }
              : undefined
          }
          open
          seed={props.seed ?? null}
          task={null}
          workspaceUid={props.workspaceUid}
        />
      ) : null}
    </>
  )
}
