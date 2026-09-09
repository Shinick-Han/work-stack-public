import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { classifyKnowledgeCaptureImportFailure, importKnowledgeCaptures } from '../../api/knowledgeCapture'
import { MAX_KNOWLEDGE_IMPORT_BYTES, KnowledgeCaptureImportError } from '../../domain/schemaKnowledgeCapture'
import type { KnowledgeCaptureImportOutcome } from '../../domain/schemaKnowledgeCapture'
import { Pill } from '../../components/Primitives'
import {
  parseKnowledgeImportText,
  previewKnowledgeImport,
  type KnowledgeImportEnvelope,
  type KnowledgeImportPreview,
} from './knowledgeCaptureImport'
import './KnowledgeCaptureImportForm.css'

export interface KnowledgeImportStatus {
  pending: boolean
  retrySame: boolean
  canSubmit: boolean
  blockedNewSearch: boolean
}

export function knowledgeImportFrozen(status: Pick<KnowledgeImportStatus, 'pending' | 'retrySame'>) {
  return status.pending || status.retrySame
}

function releaseIdleExecutePrefill(
  appliedPrefillRef: { current: string | null },
  admittedRef: { current: KnowledgeImportEnvelope | null },
  resetCopied: () => void,
) {
  if (appliedPrefillRef.current === null) return
  appliedPrefillRef.current = null
  admittedRef.current = null
  resetCopied()
}

interface KnowledgeCaptureImportFormProps {
  formId: string
  locked: boolean
  open: boolean
  onImported: (outcome: KnowledgeCaptureImportOutcome) => void | Promise<void>
  onStatusChange: (status: KnowledgeImportStatus) => void
  prefill?: KnowledgeImportEnvelope | null
}

function knowledgeErrorMessage(caught: unknown): string {
  return caught instanceof KnowledgeCaptureImportError
    ? caught.message
    : new KnowledgeCaptureImportError('invalid_json').message
}

async function loadKnowledgeFile(
  event: ChangeEvent<HTMLInputElement>,
  blocked: boolean,
  onText: (text: string) => void,
  onEnvelope: (envelope: KnowledgeImportEnvelope) => void,
  onError: (message: string) => void,
  clearPreview: () => void,
) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || blocked) return
  if (file.size > MAX_KNOWLEDGE_IMPORT_BYTES) {
    clearPreview()
    onError(new KnowledgeCaptureImportError('import_too_large').message)
    return
  }
  const next = await file.text()
  onText(next)
  try {
    onEnvelope(parseKnowledgeImportText(next))
  } catch (caught) {
    clearPreview()
    onError(knowledgeErrorMessage(caught))
  }
}

interface KnowledgeTextActions {
  setBlockedNewSearch: (value: boolean) => void
  setError: (value: string | null) => void
  setPreview: (value: KnowledgeImportPreview | null) => void
  setText: (value: string) => void
}

/** The envelope fields a dropped handoff and a closed window both return to. */
function clearAdmittedKnowledgeText(actions: KnowledgeTextActions) {
  actions.setText('')
  actions.setPreview(null)
  actions.setError(null)
  actions.setBlockedNewSearch(false)
}

/**
 * Whether this window is showing a result the user's own execution handed over, and
 * whether they have opened the envelope behind it.
 *
 * Both are latched, not derived from the preview. A frozen retry that outlives its
 * handoff keeps the layout it was reviewed in, and an envelope the reader is midway
 * through editing never folds away the moment it starts parsing. The window closing is
 * the only thing that resets them on its own.
 */
function useExecuteReviewLayout(open: boolean) {
  const [review, setReview] = useState(false)
  const [rawOpen, setRawOpen] = useState(false)
  const leaveReview = () => {
    setReview(false)
    setRawOpen(false)
  }
  useEffect(() => {
    if (!open) {
      setReview(false)
      setRawOpen(false)
    }
  }, [open])
  return {
    leaveReview,
    rawOpen,
    review,
    setRawOpen,
    // A result arrived. Its envelope folds away only when something readable took its
    // place; a refused one opens where the reader can see the refusal and correct it.
    enterReview: (readable: boolean) => {
      setReview(true)
      setRawOpen(!readable)
    },
  }
}

/** Typed input: preview what parses, and show no preview for what does not. */
function admitTypedKnowledgeText(value: string, frozen: boolean, actions: KnowledgeTextActions) {
  if (frozen) return
  actions.setBlockedNewSearch(false)
  actions.setText(value)
  actions.setError(null)
  try {
    actions.setPreview(previewKnowledgeImport(parseKnowledgeImportText(value)))
  } catch {
    actions.setPreview(null)
  }
}

/**
 * Copy an execute prefill into the field, or show why it was not admitted.
 *
 * Returns whether a readable preview now stands in for the envelope text. A prefill this
 * form refuses has nothing to read, so the caller opens the raw area rather than
 * collapsing an empty field behind a disclosure the user would have to find.
 */
function applyExecutePrefill(prefill: KnowledgeImportEnvelope, actions: KnowledgeTextActions): boolean {
  try {
    const admitted = parseKnowledgeImportText(JSON.stringify(prefill))
    actions.setText(JSON.stringify(admitted, null, 2))
    actions.setPreview(previewKnowledgeImport(admitted))
    actions.setError(null)
    actions.setBlockedNewSearch(false)
    return true
  } catch (caught) {
    actions.setPreview(null)
    actions.setError(knowledgeErrorMessage(caught))
    return false
  }
}

/** The envelope a submit will send, or null once its refusal is on screen. */
function admittedSubmission(
  text: string,
  onPreview: (value: KnowledgeImportPreview | null) => void,
  onError: (message: string) => void,
): KnowledgeImportEnvelope | null {
  try {
    return parseKnowledgeImportText(text)
  } catch (caught) {
    onPreview(null)
    onError(knowledgeErrorMessage(caught))
    return null
  }
}

interface KnowledgeImportActions {
  clearAdmitted: () => void
  setBlockedNewSearch: (value: boolean) => void
  setError: (value: string | null) => void
  setPending: (value: boolean) => void
  setRetrySame: (value: boolean) => void
}

/**
 * One import attempt and its outcome classification. There is no retry here:
 * an unknown outcome freezes the same envelope for the user to send again.
 */
async function runKnowledgeImport(
  envelope: KnowledgeImportEnvelope,
  onImported: (outcome: KnowledgeCaptureImportOutcome) => void | Promise<void>,
  actions: KnowledgeImportActions,
) {
  try {
    const outcome = await importKnowledgeCaptures(envelope)
    actions.clearAdmitted()
    actions.setRetrySame(false)
    await onImported(outcome)
  } catch (caught) {
    const failure = classifyKnowledgeCaptureImportFailure(caught)
    actions.setError(failure.message)
    actions.setRetrySame(failure.kind === 'pending_unknown')
    actions.setBlockedNewSearch(failure.kind === 'definitive')
  } finally {
    actions.setPending(false)
  }
}

function KnowledgeImportPreviewList({ preview }: { preview: KnowledgeImportPreview }) {
  return (
    <div className="knowledge-import-preview">
      {preview.items.map((item) => (
        <div className="knowledge-import-preview__item" key={item.itemId}>
          <div>
            <Pill tone="neutral">Knowledge result</Pill>
            <span className="knowledge-import-preview__meta">{item.evidenceCount} evidence · {item.retrievalLevel}</span>
          </div>
          <strong>{item.title}</strong>
          <p>{item.summary}</p>
          <p className="knowledge-import-preview__meta">
            Retrieval score {item.retrievalScore} — not a correctness probability
            {item.truncated ? <small>Some evidence was truncated.</small> : null}
          </p>
        </div>
      ))}
    </div>
  )
}

/**
 * The paste surface: a file chooser, the envelope itself, and what import means.
 *
 * It is the same markup in both layouts. A review reached from an execution keeps it
 * behind a disclosure because the reader already has the result rendered above it; a
 * hand-pasted import keeps it in the open because it *is* the task. Neither layout
 * gets its own copy of the textarea, so the field's value, disabled state and
 * `aria-describedby` target cannot drift apart between them.
 */
function KnowledgeImportFields({
  disabled,
  onFile,
  onText,
  text,
}: {
  disabled: boolean
  onFile: (event: ChangeEvent<HTMLInputElement>) => void
  onText: (value: string) => void
  text: string
}) {
  return (
    <>
      <label className="file-drop">
        <input accept="application/json,.json" disabled={disabled} onChange={(event) => void onFile(event)} type="file" />
        <span><strong>Choose a JSON envelope</strong><small>or paste it below · maximum 64 KiB</small></span>
      </label>
      <label className="field">
        <span>Knowledge result envelope</span>
        <textarea
          aria-describedby="knowledge-import-help"
          className="code-input"
          disabled={disabled}
          onChange={(event) => onText(event.target.value)}
          placeholder={'{\n  "schema": "workstack.knowledge-import.v1",\n  "request_id": "…",\n  "items": []\n}'}
          rows={13}
          spellCheck={false}
          value={text}
        />
      </label>
    </>
  )
}

const MANUAL_LEDE =
  'Paste the frozen knowledge result envelope issued for an existing request. If the outcome is unknown, retry this same envelope.'
const REVIEW_LEDE =
  'These are the results your search returned. Read them here, then choose Import into Inbox to save them as captures. Nothing is saved until you do.'
const IMPORT_HELP =
  'Import is explicit. Nothing is stored in this browser, and a failed or unknown outcome does not create a Task.'
const RAW_SUMMARY = 'Show the raw envelope'

function KnowledgeImportHelp() {
  return <p className="knowledge-import__help" id="knowledge-import-help">{IMPORT_HELP}</p>
}

function KnowledgeImportAlerts({ error, retrySame }: { error: string | null; retrySame: boolean }) {
  return (
    <>
      {retrySame ? (
        <p className="knowledge-import__pending" role="status">
          The import outcome may still be pending. Retry the same envelope. Do not issue a new search yet.
        </p>
      ) : null}
      {error ? <p className="inline-error" role="alert">{error}</p> : null}
    </>
  )
}

/**
 * The form's whole surface: every affordance is already-resolved state.
 *
 * Two layouts, one set of controls. `review` is true only for a result this window was
 * handed by an execution the user just ran; then the readable preview leads and the
 * envelope text moves behind a disclosure, because a person reviewing their own search
 * result should not have to read JSON to decide. A hand-pasted import is unchanged: the
 * textarea is the first thing on screen and nothing about it is collapsed.
 *
 * What never moves into the disclosure is anything that reports a problem. The pending
 * and refusal alerts stay above it in both layouts, so a refused envelope cannot be
 * hidden by the same control that hides the input, and `rawOpen` is state the caller
 * holds rather than a value derived from the preview — an envelope the user is midway
 * through editing must not fold itself away the moment it starts parsing.
 */
function KnowledgeImportFormBody({
  disabled,
  error,
  formId,
  onFile,
  onRawToggle,
  onSubmit,
  onText,
  preview,
  rawOpen,
  retrySame,
  review,
  text,
}: {
  disabled: boolean
  error: string | null
  formId: string
  onFile: (event: ChangeEvent<HTMLInputElement>) => void
  onRawToggle: (open: boolean) => void
  onSubmit: (event: FormEvent) => void
  onText: (value: string) => void
  preview: KnowledgeImportPreview | null
  rawOpen: boolean
  retrySame: boolean
  review: boolean
  text: string
}) {
  const fields = <KnowledgeImportFields disabled={disabled} onFile={onFile} onText={onText} text={text} />
  const alerts = <KnowledgeImportAlerts error={error} retrySame={retrySame} />
  if (!review) {
    return (
      <form className="knowledge-import" id={formId} onSubmit={onSubmit}>
        <p className="knowledge-import__lede">{MANUAL_LEDE}</p>
        {fields}
        <KnowledgeImportHelp />
        {alerts}
        {preview ? <KnowledgeImportPreviewList preview={preview} /> : null}
      </form>
    )
  }
  return (
    <form className="knowledge-import" id={formId} onSubmit={onSubmit}>
      <p className="knowledge-import__lede">{REVIEW_LEDE}</p>
      {preview ? <KnowledgeImportPreviewList preview={preview} /> : null}
      {alerts}
      <KnowledgeImportHelp />
      <details
        className="knowledge-import__raw"
        onToggle={(event) => onRawToggle(event.currentTarget.open)}
        open={rawOpen}
      >
        <summary>{RAW_SUMMARY}</summary>
        <div className="knowledge-import__raw-body">{fields}</div>
      </details>
    </form>
  )
}

export function KnowledgeCaptureImportForm({
  formId,
  locked,
  onImported,
  onStatusChange,
  open,
  prefill = null,
}: KnowledgeCaptureImportFormProps) {
  const admittedRef = useRef<KnowledgeImportEnvelope | null>(null)
  const pendingRef = useRef(false)
  const retrySameRef = useRef(false)
  const appliedPrefillRef = useRef<string | null>(null)
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<KnowledgeImportPreview | null>(null)
  const [pending, setPending] = useState(false)
  const [retrySame, setRetrySame] = useState(false)
  const [blockedNewSearch, setBlockedNewSearch] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const layout = useExecuteReviewLayout(open)
  const canSubmit = Boolean((retrySame ? admittedRef.current : text.trim()) && !pending && !locked && !blockedNewSearch)
  pendingRef.current = pending
  retrySameRef.current = retrySame

  useEffect(() => {
    onStatusChange({ pending, retrySame, canSubmit, blockedNewSearch })
  }, [blockedNewSearch, canSubmit, onStatusChange, pending, retrySame])

  useEffect(() => {
    if (open) return
    admittedRef.current = null
    appliedPrefillRef.current = null
    clearAdmittedKnowledgeText({ setBlockedNewSearch, setError, setPreview, setText })
    setPending(false)
    setRetrySame(false)
  }, [open])

  useEffect(() => {
    if (!open || pendingRef.current || retrySameRef.current) return
    if (!prefill) {
      releaseIdleExecutePrefill(appliedPrefillRef, admittedRef, () => {
        clearAdmittedKnowledgeText({ setBlockedNewSearch, setError, setPreview, setText })
        layout.leaveReview()
      })
      return
    }
    if (appliedPrefillRef.current === prefill.request_id) return
    // Marked applied on both branches, exactly as before: a prefill this form
    // cannot admit is reported once and not retried on every render.
    appliedPrefillRef.current = prefill.request_id
    layout.enterReview(applyExecutePrefill(prefill, { setBlockedNewSearch, setError, setPreview, setText }))
  }, [open, prefill])

  const showPreview = (envelope: KnowledgeImportEnvelope) => {
    setPreview(previewKnowledgeImport(envelope))
    setError(null)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (pending || locked || blockedNewSearch) return
    // A frozen retry sends the admitted envelope; anything else is parsed now.
    const held = retrySame ? admittedRef.current : null
    const envelope = held ?? admittedSubmission(text, setPreview, setError)
    if (!envelope) return
    admittedRef.current = envelope
    setPending(true)
    setError(null)
    showPreview(envelope)
    await runKnowledgeImport(envelope, onImported, {
      clearAdmitted: () => { admittedRef.current = null },
      setBlockedNewSearch,
      setError,
      setPending,
      setRetrySame,
    })
  }

  const frozen = retrySame || pending
  const textActions = { setBlockedNewSearch, setError, setPreview, setText }
  return (
    <KnowledgeImportFormBody
      disabled={frozen || locked}
      error={error}
      formId={formId}
      onFile={(event) => void loadKnowledgeFile(event, frozen || locked, setText, showPreview, setError, () => setPreview(null))}
      onRawToggle={layout.setRawOpen}
      onSubmit={(event) => void submit(event)}
      onText={(value) => admitTypedKnowledgeText(value, frozen, textActions)}
      preview={preview}
      rawOpen={layout.rawOpen}
      retrySame={retrySame}
      review={layout.review}
      text={text}
    />
  )
}
