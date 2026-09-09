import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type FormEvent,
  type MutableRefObject,
  type RefObject,
  type SetStateAction,
} from 'react'
import { Dialog } from '../../components/Dialog'
import { Button, Pill } from '../../components/Primitives'
import { copyTextToClipboard } from '../../utils/clipboard'
import { formatDateTime } from '../../utils/format'
import {
  buildKnowledgeRequestDraft,
  clipKnowledgeRequestQuery,
  isKnowledgeRequestExpired,
  knowledgeRequestExpiresAtMs,
  knowledgeRequestIdentity,
  knowledgeRequestQueryChars,
  knowledgeRequestReceiptJson,
  validateIssuedKnowledgeRequest,
  KNOWLEDGE_REQUEST_PURPOSE_COPY,
  KNOWLEDGE_REQUEST_PURPOSES,
  MAX_CORPUS_REFS,
  MAX_QUERY_CHARS,
  MAX_RESULT_LIMIT,
  MIN_RESULT_LIMIT,
  type IssuedKnowledgeRequest,
  type KnowledgeCorpusOption,
  type KnowledgeRequestDraft,
  type KnowledgeRequestEditorInput,
  type KnowledgeRequestTaskRef,
} from './knowledgeRequestDraft'
import { KnowledgeExecutionPanel } from './KnowledgeExecutionPanel'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'
import type { KnowledgeLaunchSeed } from './knowledgeLaunchSeed'
import './KnowledgeRequestDialog.css'

/**
 * Editor for one scoped knowledge request, from an empty question to a receipt the user
 * can carry out of band.
 *
 * The shape of the flow is the point. The user reads and edits the exact query and the
 * exact scope; nothing is sent until they press Generate; the *server* mints the request
 * and the receipt shown here is the server's own document, checked against what the user
 * reviewed before a single character of it reaches the clipboard.
 *
 * Deliberate boundaries:
 *
 * - **No transport of its own.** There is no fetch, no route, no provider branch and no
 *   endpoint in this file. `onIssue` is a callback the parent implements. It is *not* an
 *   HTTP schema: the parent may send anything it likes, and its return value is treated
 *   as untrusted until `validateIssuedKnowledgeRequest` says otherwise.
 * - **No minted identity.** This component never invents a `request_id`, a
 *   `requested_at`, an `expires_at` or a `schema`. A locally built document would be
 *   indistinguishable on screen from an issued one, so none is ever built.
 * - **No self-granted scope.** `corpusOptions` and `task` are the parent's trusted
 *   state. This screen offers what it is given, refuses a selection outside that list as
 *   a courtesy to the user, and never treats its own checks as authorisation.
 * - **No automatic Task content.** The binding is identity only. A Task title may
 *   prefill the editable query, which the user then reads and can rewrite; Task detail,
 *   notes and context are never attached, and there is no control here that would.
 * - **No frozen editor.** The question and the scope stay editable while a request is in
 *   flight. An edit makes it a different question, so it retires the flight instead of
 *   being blocked by it, and the answer that arrives for the old wording is dropped.
 * - **No opener and no markup.** Nothing here builds a URL, a path or a command, and no
 *   supplied string is rendered as HTML.
 * - **No persistence.** The receipt is on screen and nowhere else: not in storage, not
 *   in the address bar, not in a log and not in an analytics call.
 *
 * Wiring status: this is a callable component with no parent in the product yet, because
 * the issuing API and the nonsecret corpus registry it depends on do not exist. See
 * `KNOWLEDGE-REQUEST-UI.md` for what a parent still has to supply.
 */

const DIALOG_DESCRIPTION =
  'Write the question and choose which corpora may be searched. Nothing is sent until you generate the request, and the server issues the request itself.'
const TASK_DETAIL_NOTE =
  'Only identity is bound: workspace, and the Task if one is open. The Task title, detail and notes are never attached to a request.'
const WORKSPACE_ONLY_NOTE =
  'No Task is open, so this asks on behalf of the workspace alone. That is a complete request.'
const NO_CORPUS_NOTE =
  'No corpus is available to this workspace yet, so there is nothing a request could search. An owner has to grant one before a request can be generated.'
const REVIEW_NOTE = 'This is exactly what will be sent when you generate the request.'
const QUERY_NOTE =
  'The question is sent as plain text and is never run as a command, a path or an address.'
const PENDING_NOTE = 'Waiting for the server to issue this request…'
const ISSUE_FAILED =
  'The request was not issued. Nothing has been generated, and there is nothing to copy.'
const ISSUE_UNKNOWN =
  'The server was not heard from, so whether this request was issued is not known. It may be held on the server and it may not.'
const ISSUE_STALE_ANSWER =
  'The answer that arrived was about a different connection, workspace or policy revision, so it was not shown. Whether the request you reviewed was issued is not known from here.'
const UNRESOLVED_NOTE =
  'Treat that as unconfirmed rather than refused: there is no receipt to show and nothing to copy, and nothing was sent again. The question and scope below are unchanged, so generating again asks about this same request rather than a second one.'
const EXPIRED_ON_ARRIVAL =
  'The issued request had already expired by the time it arrived. Generate a fresh one.'
const COPY_FAILED =
  'The request could not be copied to the clipboard. The request below is unchanged — try copying again.'
const EXPIRED_NOTE =
  'This request has expired. It is not renewed automatically; generate a fresh one when you are ready to ask again.'
const RECEIPT_NOTE =
  'This is the request the server issued. It is shown so you can read and carry it; it is not saved to this browser.'
const WITHDRAWN_NOTE = 'No longer available to this workspace. Clear it to generate a request.'

type CopyPhase = 'idle' | 'working' | 'copied'
type PurposeKey = keyof typeof KNOWLEDGE_REQUEST_PURPOSE_COPY

/**
 * How an attempt settled when no receipt is shown. Only `refused` is a statement about
 * the server's state, and only a parent holding the issuer's own decision may say it.
 *
 * - `refused` — the issuer decided. Nothing was issued.
 * - `unknown` — a lost response, an unreadable success or any other answer the parent
 *   could not read. The ledger may already hold the request.
 * - `stale` — a well-formed answer that belongs to a connection, workspace or policy
 *   revision this screen is no longer showing. What happened to *this* attempt is
 *   unknown, and the answer that did arrive is never shown or copied.
 */
export type KnowledgeIssueOutcome = 'refused' | 'stale' | 'unknown'

/** The copy a reader sees per outcome. Every line of it is authored here. */
const ISSUE_OUTCOME_COPY: Record<KnowledgeIssueOutcome, string> = {
  refused: ISSUE_FAILED,
  stale: ISSUE_STALE_ANSWER,
  unknown: ISSUE_UNKNOWN,
}

/**
 * The bounded way a parent tells this editor how a rejected attempt settled.
 *
 * It carries a classification and nothing else that a reader ever sees: `detail` is for a
 * developer reading a stack trace, and is never rendered, because a returned or thrown
 * value is exactly the input least safe to quote back at a user. A rejection that is *not*
 * one of these is treated as `unknown`, never as a refusal — calling an unread outcome a
 * refusal is the one mistake this editor must not make.
 */
export class KnowledgeIssueError extends Error {
  readonly outcome: KnowledgeIssueOutcome

  constructor(outcome: KnowledgeIssueOutcome, detail?: string) {
    super(detail ?? ISSUE_OUTCOME_COPY[outcome])
    this.name = 'KnowledgeIssueError'
    this.outcome = outcome
  }
}

/** An unclassified rejection is unknown: nothing about it says the server refused. */
export function knowledgeIssueOutcomeOf(error: unknown): KnowledgeIssueOutcome {
  return error instanceof KnowledgeIssueError ? error.outcome : 'unknown'
}

export interface KnowledgeRequestDialogProps {
  open: boolean
  /** The active workspace, from the parent's own trusted state. */
  workspaceUid: string
  /** The open Task, at the revision the parent actually read. `null` is workspace-only. */
  task?: KnowledgeRequestTaskRef | null
  /** The corpora the server says this caller already holds. The parent owns this list. */
  corpusOptions: readonly KnowledgeCorpusOption[]
  /**
   * Hands the reviewed draft to the parent and resolves with whatever the issuer
   * returned. This is a callback contract, not a wire format: the parent owns the
   * transport, and the resolved value is checked here before it is shown.
   */
  onIssue: (draft: KnowledgeRequestDraft) => Promise<unknown>
  onClose: () => void
  /** Hands a validated execute proposal to the parent. The parent closes this dialog. */
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  /**
   * Starting values a saved Capture offered. It seeds the editable question and the
   * purpose select only; it binds nothing, and its context identity is part of the
   * session so values from another Capture never survive into this one.
   */
  seed?: KnowledgeLaunchSeed | null
  /** Injectable clock, in epoch milliseconds. Expiry is read, never moved. */
  now?: () => number
  /** Clipboard adapter. Defaults to the shared helper the rest of the app uses. */
  copyText?: (value: string, isCurrent: () => boolean) => Promise<void>
}

/** Editor values, reset whenever the dialog binds to a different session. */
interface EditorValues {
  limit: string
  purpose: string
  query: string
  selected: string[]
}

interface FlowState {
  copyError: string | null
  copyPhase: CopyPhase
  error: string | null
  expired: boolean
  pending: boolean
  receipt: IssuedKnowledgeRequest | null
  /** The attempt settled without a receipt *and* without knowing what the server did. */
  unresolved: boolean
}

const IDLE_FLOW: FlowState = {
  copyError: null,
  copyPhase: 'idle',
  error: null,
  expired: false,
  pending: false,
  receipt: null,
  unresolved: false,
}

type SetFlow = Dispatch<SetStateAction<FlowState>>

/**
 * What every guard reads at the moment it runs, rather than what a handler captured when
 * it was created. `flight` and `identity` together decide whether a settled promise still
 * answers the question that is on screen now.
 */
interface FlowRefs {
  editor: MutableRefObject<KnowledgeRequestEditorInput>
  flight: MutableRefObject<number>
  identity: MutableRefObject<string>
  mounted: MutableRefObject<boolean>
  now: MutableRefObject<() => number>
  pending: MutableRefObject<boolean>
  receipt: MutableRefObject<IssuedKnowledgeRequest | null>
}

function initialValues(
  task: KnowledgeRequestTaskRef | null,
  seed: KnowledgeLaunchSeed | null,
): EditorValues {
  // A Task title, or a saved Capture's title, is a starting point the user reads and can
  // rewrite. It is the one piece of that text this screen touches, and it never leaves
  // the editable field on its own. The scope always starts empty and is never seeded.
  const query = seed ? seed.query : clipKnowledgeRequestQuery((task?.title ?? '').trim())
  const purpose = seed ? seed.purpose : KNOWLEDGE_REQUEST_PURPOSES[0]
  return { limit: '5', purpose, query, selected: [] }
}

function sessionKeyOf(
  open: boolean,
  workspaceUid: string,
  task: KnowledgeRequestTaskRef | null,
  seed: KnowledgeLaunchSeed | null,
) {
  return JSON.stringify([
    open,
    workspaceUid,
    task ? [task.uid, task.id, task.revision] : null,
    seed ? seed.contextKey : null,
  ])
}

interface BoundIssue {
  draft: KnowledgeRequestDraft
  identity: string
  token: number
}

/** One attempt's end: the value the parent resolved with, or the reason it rejected. */
type IssueSettlement =
  | { kind: 'answered'; response: unknown }
  | { kind: 'rejected'; error: unknown }

function settleIssue(refs: FlowRefs, setFlow: SetFlow, bound: BoundIssue, settled: IssueSettlement) {
  if (!refs.mounted.current) return
  if (refs.flight.current !== bound.token || refs.identity.current !== bound.identity) return
  refs.pending.current = false
  if (settled.kind === 'rejected') {
    const outcome = knowledgeIssueOutcomeOf(settled.error)
    setFlow({
      ...IDLE_FLOW,
      error: ISSUE_OUTCOME_COPY[outcome],
      unresolved: outcome !== 'refused',
    })
    return
  }
  const checked = validateIssuedKnowledgeRequest(settled.response, bound.draft)
  if (!checked.ok) {
    // Something answered, and this screen could not read it as the request under review.
    // That is not evidence the server issued nothing: the outcome stays unconfirmed.
    setFlow({ ...IDLE_FLOW, error: checked.message, unresolved: true })
    return
  }
  if (isKnowledgeRequestExpired(checked.request, refs.now.current())) {
    setFlow({ ...IDLE_FLOW, error: EXPIRED_ON_ARRIVAL })
    return
  }
  setFlow({ ...IDLE_FLOW, receipt: checked.request })
}

function startIssue(
  refs: FlowRefs,
  setFlow: SetFlow,
  onIssue: (draft: KnowledgeRequestDraft) => Promise<unknown>,
) {
  if (refs.pending.current) return
  const built = buildKnowledgeRequestDraft(refs.editor.current)
  if (!built.ok) {
    setFlow({ ...IDLE_FLOW, error: built.message })
    return
  }
  const bound: BoundIssue = {
    draft: built.draft,
    identity: refs.identity.current,
    token: ++refs.flight.current,
  }
  refs.pending.current = true
  setFlow({ ...IDLE_FLOW, pending: true })
  let issued: Promise<unknown>
  try {
    issued = Promise.resolve(onIssue(built.draft))
  } catch (error) {
    // A parent that throws instead of rejecting must not leave this dialog waiting on a
    // promise that will never settle. How it settled is still the parent's classification.
    settleIssue(refs, setFlow, bound, { error, kind: 'rejected' })
    return
  }
  void issued.then(
    (response) => settleIssue(refs, setFlow, bound, { kind: 'answered', response }),
    (error: unknown) => settleIssue(refs, setFlow, bound, { error, kind: 'rejected' }),
  )
}

function startCopy(
  refs: FlowRefs,
  setFlow: SetFlow,
  copyText: (value: string, isCurrent: () => boolean) => Promise<void>,
) {
  const request = refs.receipt.current
  if (!request) return
  const token = refs.flight.current
  const identity = refs.identity.current
  // Everything that could change after the click is re-read here, and again by the
  // clipboard helper at each point it is still able to stop.
  const current = () =>
    refs.mounted.current &&
    refs.flight.current === token &&
    refs.identity.current === identity &&
    refs.receipt.current === request &&
    !isKnowledgeRequestExpired(request, refs.now.current())
  if (isKnowledgeRequestExpired(request, refs.now.current())) {
    setFlow((flow) => (flow.receipt === request ? { ...flow, copyError: null, copyPhase: 'idle', expired: true } : flow))
    return
  }
  if (!current()) return
  setFlow((flow) => ({ ...flow, copyError: null, copyPhase: 'working' }))
  void copyText(knowledgeRequestReceiptJson(request), current).then(
    () => {
      if (!refs.mounted.current || refs.receipt.current !== request) return
      if (isKnowledgeRequestExpired(request, refs.now.current())) {
        setFlow((flow) => (flow.receipt === request ? { ...flow, copyPhase: 'idle', expired: true } : flow))
        return
      }
      if (current()) setFlow((flow) => ({ ...flow, copyError: null, copyPhase: 'copied' }))
    },
    () => {
      if (!refs.mounted.current || refs.receipt.current !== request) return
      if (isKnowledgeRequestExpired(request, refs.now.current())) {
        setFlow((flow) => (flow.receipt === request ? { ...flow, copyPhase: 'idle', expired: true } : flow))
        return
      }
      if (current()) setFlow((flow) => ({ ...flow, copyError: COPY_FAILED, copyPhase: 'idle' }))
    },
  )
}

/**
 * Marks an issued request expired against the remaining lifetime. The timeout is not a
 * hard-realtime guarantee while the tab is suspended; visibility and focus resume
 * recheck immediately. Copy also re-reads the clock and marks expired if it got there
 * first.
 */
function useExpiryWatch(
  receipt: IssuedKnowledgeRequest | null,
  now: MutableRefObject<() => number>,
  setFlow: SetFlow,
) {
  useEffect(() => {
    if (!receipt) return undefined
    let timer = 0
    const arm = () => {
      window.clearTimeout(timer)
      timer = 0
      if (isKnowledgeRequestExpired(receipt, now.current())) {
        setFlow((flow) => (flow.receipt === receipt ? { ...flow, expired: true } : flow))
        return
      }
      timer = window.setTimeout(arm, knowledgeRequestExpiresAtMs(receipt) - now.current())
    }
    arm()
    document.addEventListener('visibilitychange', arm)
    window.addEventListener('focus', arm)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', arm)
      window.removeEventListener('focus', arm)
    }
  }, [now, receipt, setFlow])
}

interface KnowledgeRequestFlow {
  copy: () => void
  queryRef: RefObject<HTMLTextAreaElement | null>
  setValues: Dispatch<SetStateAction<EditorValues>>
  state: FlowState
  submit: (event: FormEvent) => void
  toggle: (alias: string) => void
  values: EditorValues
}

function useKnowledgeRequestFlow(props: KnowledgeRequestDialogProps): KnowledgeRequestFlow {
  const { corpusOptions, onIssue, open, workspaceUid } = props
  const copyText = props.copyText ?? copyTextToClipboard
  const now = props.now ?? Date.now
  const task = props.task ?? null
  const seed = props.seed ?? null
  const [values, setValues] = useState<EditorValues>(() => initialValues(task, seed))
  const [state, setFlow] = useState<FlowState>(IDLE_FLOW)
  const queryRef = useRef<HTMLTextAreaElement | null>(null)

  const editor: KnowledgeRequestEditorInput = useMemo(
    () => ({
      options: corpusOptions,
      purpose: values.purpose,
      query: values.query,
      resultLimit: values.limit,
      selectedAliases: values.selected,
      task,
      workspaceUid,
    }),
    [corpusOptions, task, values, workspaceUid],
  )
  const identity = knowledgeRequestIdentity(editor)

  const refs: FlowRefs = {
    editor: useRef(editor),
    flight: useRef(0),
    identity: useRef(identity),
    mounted: useRef(true),
    now: useRef(now),
    pending: useRef(false),
    receipt: useRef<IssuedKnowledgeRequest | null>(null),
  }
  refs.editor.current = editor
  refs.now.current = now
  refs.receipt.current = state.receipt
  const mounted = refs.mounted

  useEffect(() => () => { mounted.current = false }, [mounted])

  const sessionKey = sessionKeyOf(open, workspaceUid, task, seed)
  useEffect(() => {
    // A different workspace, Task revision or open/closed state is a different session:
    // nothing typed or issued under the previous one may survive into it.
    refs.flight.current += 1
    refs.pending.current = false
    setValues(initialValues(task, seed))
    if (open) queryRef.current?.focus()
  }, [sessionKey])

  useEffect(() => {
    // Any edit to the question, scope, purpose or limit retires the request that answered
    // the previous wording, and cancels an issue still in flight for it.
    refs.identity.current = identity
    refs.flight.current += 1
    refs.pending.current = false
    setFlow(IDLE_FLOW)
  }, [identity])

  useExpiryWatch(state.receipt, refs.now, setFlow)

  const submit = useCallback((event: FormEvent) => {
    event.preventDefault()
    startIssue(refs, setFlow, onIssue)
  }, [onIssue])

  const copy = useCallback(() => startCopy(refs, setFlow, copyText), [copyText])

  const toggle = useCallback((alias: string) => {
    setValues((value) => ({
      ...value,
      selected: value.selected.includes(alias)
        ? value.selected.filter((entry) => entry !== alias)
        : [...value.selected, alias],
    }))
  }, [])

  return { copy, queryRef, setValues, state, submit, toggle, values }
}

function ScopeSummary({ task, workspaceUid }: { task: KnowledgeRequestTaskRef | null; workspaceUid: string }) {
  return (
    <section className="knowledge-request__scope">
      <dl>
        <div>
          <dt>Workspace</dt>
          <dd>{workspaceUid}</dd>
        </div>
        {task ? (
          <div>
            <dt>Task</dt>
            <dd>
              {task.id} · revision {task.revision}
              {task.title ? <small>{task.title}</small> : null}
            </dd>
          </div>
        ) : null}
      </dl>
      <p>{task ? TASK_DETAIL_NOTE : `${WORKSPACE_ONLY_NOTE} ${TASK_DETAIL_NOTE}`}</p>
    </section>
  )
}

interface CorpusChooserProps {
  onToggle: (alias: string) => void
  options: readonly KnowledgeCorpusOption[]
  selected: readonly string[]
}

function CorpusChooser({ onToggle, options, selected }: CorpusChooserProps) {
  const offered = new Set(options.map((option) => option.alias))
  // A selection the workspace no longer holds stays visible and clearable rather than
  // vanishing, so the refusal it causes has something the reader can act on.
  const withdrawn = selected.filter((alias) => !offered.has(alias))
  if (!options.length && !withdrawn.length) {
    return (
      <fieldset className="knowledge-request__corpora">
        <legend>Corpora to search</legend>
        <p className="knowledge-request__empty">{NO_CORPUS_NOTE}</p>
      </fieldset>
    )
  }
  return (
    <fieldset className="knowledge-request__corpora">
      <legend>Corpora to search <small>Choose 1 to {MAX_CORPUS_REFS}</small></legend>
      <ul>
        {options.map((option) => (
          <li key={option.alias}>
            <label>
              <input
                checked={selected.includes(option.alias)}
                onChange={() => onToggle(option.alias)}
                type="checkbox"
              />
              <span>
                <strong>{option.label}</strong>
                {option.description ? <small>{option.description}</small> : null}
              </span>
            </label>
          </li>
        ))}
        {withdrawn.map((alias) => (
          <li className="knowledge-request__corpus--withdrawn" key={`withdrawn:${alias}`}>
            <label>
              <input checked onChange={() => onToggle(alias)} type="checkbox" />
              <span><strong>{alias}</strong><small>{WITHDRAWN_NOTE}</small></span>
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  )
}

function ReviewBlock({ selectedLabels, values }: { selectedLabels: readonly string[]; values: EditorValues }) {
  const purpose = KNOWLEDGE_REQUEST_PURPOSE_COPY[values.purpose as PurposeKey]
  return (
    <section aria-label="Request review" className="knowledge-request__review">
      <h3>Before you generate</h3>
      <dl>
        <div>
          <dt>Question</dt>
          <dd>{values.query.trim() ? values.query.trim() : <em>Nothing written yet</em>}</dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd>{selectedLabels.length ? selectedLabels.join(' · ') : <em>No corpus selected yet</em>}</dd>
        </div>
        <div>
          <dt>Purpose</dt>
          <dd>{purpose ? purpose.label : <em>Not chosen</em>}</dd>
        </div>
        <div>
          <dt>Results</dt>
          <dd>{values.limit.trim() ? `At most ${values.limit.trim()}` : <em>Not set</em>}</dd>
        </div>
      </dl>
      <p>{REVIEW_NOTE}</p>
    </section>
  )
}

interface ReceiptProps {
  onCopy: () => void
  request: IssuedKnowledgeRequest
  state: FlowState
}

function ReceiptPanel({ onCopy, request, state }: ReceiptProps) {
  const { copyError, copyPhase, expired } = state
  return (
    <section aria-label="Issued request" className="knowledge-request__receipt">
      <div className="knowledge-request__receipt-head">
        <h3>Issued request</h3>
        <Pill tone={expired ? 'unknown' : 'verified'}>{expired ? 'Expired' : 'Active'}</Pill>
      </div>
      <dl>
        <div><dt>Request</dt><dd>{request.request_id}</dd></div>
        <div><dt>Question</dt><dd>{request.query}</dd></div>
        <div><dt>Corpora</dt><dd>{request.corpus_refs.join(' · ')}</dd></div>
        <div><dt>Expires</dt><dd>{formatDateTime(request.expires_at)}</dd></div>
      </dl>
      <div className="knowledge-request__receipt-actions">
        <Button disabled={expired || copyPhase === 'working'} icon="upload" onClick={onCopy} variant="secondary">
          {copyPhase === 'working' ? 'Copying…' : 'Copy request'}
        </Button>
        {copyPhase === 'copied' && !expired ? (
          <span className="knowledge-request__copied" role="status">Copied to the clipboard.</span>
        ) : null}
      </div>
      {copyError ? <p className="inline-error" role="alert">{copyError}</p> : null}
      <p className="knowledge-request__footnote">{expired ? EXPIRED_NOTE : RECEIPT_NOTE}</p>
    </section>
  )
}

interface RequestFormProps {
  flow: KnowledgeRequestFlow
  options: readonly KnowledgeCorpusOption[]
  task: KnowledgeRequestTaskRef | null
  workspaceUid: string
}

function RequestForm({ flow, options, task, workspaceUid }: RequestFormProps) {
  const { setValues, state, submit, toggle, values } = flow
  const selectedLabels = values.selected.map(
    (alias) => options.find((option) => option.alias === alias)?.label ?? alias,
  )
  const purposeHelp = KNOWLEDGE_REQUEST_PURPOSE_COPY[values.purpose as PurposeKey]?.help
  return (
    /* `noValidate` keeps the browser's own bubble out of the way: every refusal the user
       sees comes from `buildKnowledgeRequestDraft`, in one authored voice, while the
       min/max attributes stay for the stepper affordance they give. */
    <form className="knowledge-request" id="knowledge-request-form" noValidate onSubmit={submit}>
      <ScopeSummary task={task} workspaceUid={workspaceUid} />
      <label className="field">
        <span>Purpose</span>
        <select
          onChange={(event) => setValues((value) => ({ ...value, purpose: event.target.value }))}
          value={values.purpose}
        >
          {KNOWLEDGE_REQUEST_PURPOSES.map((purpose) => (
            <option key={purpose} value={purpose}>{KNOWLEDGE_REQUEST_PURPOSE_COPY[purpose].label}</option>
          ))}
        </select>
      </label>
      {purposeHelp ? <p className="field-help">{purposeHelp}</p> : null}
      <label className="field">
        <span>Question</span>
        <textarea
          aria-describedby="knowledge-request-query-help"
          onChange={(event) => setValues((value) => ({
            ...value,
            query: clipKnowledgeRequestQuery(event.target.value),
          }))}
          placeholder="What do you need from the knowledge base?"
          ref={flow.queryRef}
          rows={4}
          value={values.query}
        />
      </label>
      <p className="field-help" id="knowledge-request-query-help">
        {knowledgeRequestQueryChars(values.query.trim())} of {MAX_QUERY_CHARS} characters. {QUERY_NOTE}
      </p>
      <CorpusChooser onToggle={toggle} options={options} selected={values.selected} />
      <label className="field knowledge-request__limit">
        <span>Results to ask for <small>{MIN_RESULT_LIMIT} to {MAX_RESULT_LIMIT}</small></span>
        <input
          inputMode="numeric"
          max={MAX_RESULT_LIMIT}
          min={MIN_RESULT_LIMIT}
          onChange={(event) => setValues((value) => ({ ...value, limit: event.target.value }))}
          type="number"
          value={values.limit}
        />
      </label>
      <ReviewBlock selectedLabels={selectedLabels} values={values} />
      {state.error ? <p className="inline-error" role="alert">{state.error}</p> : null}
      {/* Said separately from the refusal line, because it is the opposite claim: this
          attempt has no known outcome, and the next Generate is a retry of it. */}
      {state.unresolved ? <p className="field-help">{UNRESOLVED_NOTE}</p> : null}
      {state.pending ? <p className="knowledge-request__pending" role="status">{PENDING_NOTE}</p> : null}
    </form>
  )
}

export function KnowledgeRequestDialog(props: KnowledgeRequestDialogProps) {
  const { corpusOptions, onClose, onReviewKnowledge, open, workspaceUid } = props
  const task = props.task ?? null
  const flow = useKnowledgeRequestFlow(props)
  const canGenerate = !flow.state.pending && corpusOptions.length > 0
  const now = props.now ?? Date.now
  return (
    <Dialog
      description={DIALOG_DESCRIPTION}
      footer={
        <>
          <Button onClick={onClose} variant="ghost">Cancel</Button>
          <Button
            disabled={!canGenerate}
            form="knowledge-request-form"
            icon="search"
            type="submit"
            variant="primary"
          >
            {flow.state.pending ? 'Generating…' : 'Generate request'}
          </Button>
        </>
      }
      onClose={onClose}
      open={open}
      size="large"
      title="Ask a scoped knowledge request"
    >
      <RequestForm flow={flow} options={corpusOptions} task={task} workspaceUid={workspaceUid} />
      {flow.state.receipt ? (
        <>
          <ReceiptPanel onCopy={flow.copy} request={flow.state.receipt} state={flow.state} />
          <KnowledgeExecutionPanel
            expired={flow.state.expired}
            key={`${workspaceUid}:${flow.state.receipt.request_id}`}
            now={now}
            onReview={onReviewKnowledge}
            request={flow.state.receipt}
            workspaceUid={workspaceUid}
          />
        </>
      ) : null}
    </Dialog>
  )
}
