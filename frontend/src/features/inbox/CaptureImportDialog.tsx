import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { Dialog } from '../../components/Dialog'
import { Button, Pill } from '../../components/Primitives'
import { capturePacketSchema, findForbiddenCaptureKey } from '../../domain/schemas'
import type { KnowledgeCaptureImportOutcome } from '../../domain/schemaKnowledgeCapture'
import type { CapturePacket } from '../../domain/types'
import { KnowledgeCaptureImportForm, knowledgeImportFrozen, type KnowledgeImportStatus } from './KnowledgeCaptureImportForm'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'
import './KnowledgeCaptureImportForm.css'

export const MAX_CAPTURE_PACKET_BYTES = 64 * 1024

interface CaptureImportDialogProps {
  open: boolean
  pending: boolean
  serverError: string | null
  onClose: () => void
  onSubmit: (packet: CapturePacket) => void
  onKnowledgeImported?: (outcome: KnowledgeCaptureImportOutcome) => void | Promise<void>
  onKnowledgeStatusChange?: (status: KnowledgeImportStatus) => void
  prefill?: KnowledgeImportEnvelope | null
}

function formatValidationError(error: unknown) {
  if (error && typeof error === 'object' && 'issues' in error) {
    const issues = (error as { issues: Array<{ path: PropertyKey[]; message: string }> }).issues
    return issues.slice(0, 3).map((issue) => `${issue.path.join('.') || 'packet'}: ${issue.message}`).join(' · ')
  }
  return error instanceof Error ? error.message : 'This packet is not valid JSON.'
}

export function parseCapturePacketValue(value: unknown): CapturePacket {
  const forbiddenPath = findForbiddenCaptureKey(value)
  if (forbiddenPath) throw new Error(`Raw-content field is not allowed: ${forbiddenPath}`)
  return capturePacketSchema.parse(value)
}

export function parseCapturePacketText(text: string): CapturePacket {
  const byteLength = new TextEncoder().encode(text).byteLength
  if (byteLength > MAX_CAPTURE_PACKET_BYTES) throw new Error('Packet exceeds the 64 KiB limit.')
  const value = JSON.parse(text) as unknown
  const packet = parseCapturePacketValue(value)
  if (packet.provenance.capture_mode !== 'manual') {
    throw new Error('Use the guided Microsoft 365 agent result importer for OOB provenance.')
  }
  return packet
}

/** The manual tab's live preview: whatever parses, and nothing otherwise. */
function manualPacketPreview(mode: 'manual' | 'knowledge', text: string): CapturePacket | null {
  if (mode !== 'manual' || !text.trim()) return null
  try {
    return parseCapturePacketText(text)
  } catch {
    return null
  }
}

/** Submit the typed packet, or leave its refusal on screen and send nothing. */
function submitManualPacket(
  text: string,
  onSubmit: (packet: CapturePacket) => void,
  onError: (message: string | null) => void,
) {
  try {
    const packet = parseCapturePacketText(text)
    onError(null)
    onSubmit(packet)
  } catch (error) {
    onError(formatValidationError(error))
  }
}

const idleKnowledgeStatus: KnowledgeImportStatus = {
  pending: false,
  retrySame: false,
  canSubmit: false,
  blockedNewSearch: false,
}

async function loadManualPacketFile(
  event: ChangeEvent<HTMLInputElement>,
  onText: (text: string) => void,
  onError: (message: string | null) => void,
) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  if (file.size > MAX_CAPTURE_PACKET_BYTES) {
    onError('Packet exceeds the 64 KiB limit.')
    return
  }
  onText(await file.text())
  onError(null)
}

function ImportDialogFooter({
  busy,
  canSubmitKnowledge,
  close,
  hasManualText,
  knowledgeBusy,
  mode,
  pending,
  retrySame,
}: {
  busy: boolean
  canSubmitKnowledge: boolean
  close: () => void
  hasManualText: boolean
  knowledgeBusy: boolean
  mode: 'manual' | 'knowledge'
  pending: boolean
  retrySame: boolean
}) {
  const knowledgeLabel = retrySame ? 'Retry same import' : knowledgeBusy ? 'Importing…' : 'Import into Inbox'
  return (
    <>
      <Button disabled={busy} onClick={close} variant="ghost">Cancel</Button>
      {mode === 'manual' ? (
        <Button disabled={pending || !hasManualText} form="capture-import-form" icon="upload" type="submit" variant="primary">
          {pending ? 'Importing…' : 'Import packet'}
        </Button>
      ) : (
        <Button disabled={busy || !canSubmitKnowledge} form="knowledge-import-form" icon="upload" type="submit" variant="primary">
          {knowledgeLabel}
        </Button>
      )}
    </>
  )
}

function ImportKindTabs({
  busy,
  mode,
  onMode,
}: {
  busy: boolean
  mode: 'manual' | 'knowledge'
  onMode: (mode: 'manual' | 'knowledge') => void
}) {
  return (
    <div className="capture-import-mode" role="tablist" aria-label="Import kind">
      <Button
        aria-selected={mode === 'manual'}
        disabled={busy}
        onClick={() => { if (!busy) onMode('manual') }}
        role="tab"
        variant={mode === 'manual' ? 'secondary' : 'ghost'}
      >
        Manual packet
      </Button>
      <Button
        aria-selected={mode === 'knowledge'}
        disabled={busy}
        onClick={() => { if (!busy) onMode('knowledge') }}
        role="tab"
        variant={mode === 'knowledge' ? 'secondary' : 'ghost'}
      >
        Knowledge result
      </Button>
    </div>
  )
}

interface ImportDialogPanesProps {
  busy: boolean
  loadFile: (event: ChangeEvent<HTMLInputElement>) => void
  mode: 'manual' | 'knowledge'
  onKnowledgeImported: (outcome: KnowledgeCaptureImportOutcome) => void | Promise<void>
  onManualSubmit: (event: FormEvent) => void
  onManualText: (value: string) => void
  onMode: (mode: 'manual' | 'knowledge') => void
  onStatusChange: (status: KnowledgeImportStatus) => void
  open: boolean
  packet: CapturePacket | null
  pending: boolean
  prefill: KnowledgeImportEnvelope | null
  serverError: string | null
  text: string
  validationError: string | null
}

interface ImportDialogViewProps extends ImportDialogPanesProps {
  close: () => void
  knowledgeBusy: boolean
  knowledgeStatus: KnowledgeImportStatus
}

/** The dialog frame: title, footer and the panes. It decides nothing either. */
function ImportDialogView(props: ImportDialogViewProps) {
  return (
    <Dialog
      description="Paste a sanitized Capture Packet v1 or a frozen knowledge result envelope. Raw mail, chat, recipients, attachments, and credentials are rejected."
      footer={(
        <ImportDialogFooter
          busy={props.busy}
          canSubmitKnowledge={props.knowledgeStatus.canSubmit}
          close={props.close}
          hasManualText={Boolean(props.text.trim())}
          knowledgeBusy={props.knowledgeBusy}
          mode={props.mode}
          pending={props.pending}
          retrySame={props.knowledgeStatus.retrySame}
        />
      )}
      onClose={props.close}
      open={props.open}
      size="large"
      title="Import context"
    >
      <ImportDialogPanes {...props} />
    </Dialog>
  )
}

/** The dialog's tabs and the pane the selected tab owns. It decides nothing. */
function ImportDialogPanes(props: ImportDialogPanesProps) {
  return (
    <>
      <ImportKindTabs busy={props.busy} mode={props.mode} onMode={props.onMode} />
      {props.mode === 'manual' ? (
        <ManualPacketForm
          loadFile={props.loadFile}
          onSubmit={props.onManualSubmit}
          onText={props.onManualText}
          packet={props.packet}
          serverError={props.serverError}
          text={props.text}
          validationError={props.validationError}
        />
      ) : (
        <KnowledgeCaptureImportForm
          formId="knowledge-import-form"
          locked={props.pending}
          open={props.open}
          onImported={props.onKnowledgeImported}
          onStatusChange={props.onStatusChange}
          prefill={props.prefill}
        />
      )}
    </>
  )
}

function ManualPacketForm({
  loadFile,
  onSubmit,
  onText,
  packet,
  serverError,
  text,
  validationError,
}: {
  loadFile: (event: ChangeEvent<HTMLInputElement>) => void
  onSubmit: (event: FormEvent) => void
  onText: (value: string) => void
  packet: CapturePacket | null
  serverError: string | null
  text: string
  validationError: string | null
}) {
  return (
    <form className="capture-import" id="capture-import-form" onSubmit={onSubmit}>
      <div className="privacy-note">
        <span className="privacy-note__mark">SAFE</span>
        <div>
          <strong>Work Stack stores sanitized context only.</strong>
          <p>
            Opaque references stay opaque. Manual packets must declare <code>capture_mode: &quot;manual&quot;</code>.
            Named sources in a knowledge result are reported claims, not verified origin.
          </p>
        </div>
      </div>
      <label className="file-drop">
        <input accept="application/json,.json" onChange={(event) => void loadFile(event)} type="file" />
        <span><strong>Choose a JSON packet</strong><small>or paste it below · maximum 64 KiB</small></span>
      </label>
      <label className="field">
        <span>Capture Packet v1 JSON</span>
        <textarea
          aria-describedby="capture-import-help"
          className="code-input"
          onChange={(event) => onText(event.target.value)}
          placeholder={'{\n  "schema_version": "1.0",\n  "source_key": "sha256:…",\n  …\n}'}
          rows={13}
          spellCheck={false}
          value={text}
        />
      </label>
      <p className="field-help" id="capture-import-help">The server independently verifies hashes, provenance, prohibited keys, and content-leakage rules.</p>
      {packet ? (
        <div className="packet-preview">
          <div><Pill tone="neutral">Manual</Pill><span>{packet.source.provider} · {packet.source.resource_type}</span></div>
          <strong>{packet.source.display_title}</strong>
          <p>{packet.normalized.summary}</p>
        </div>
      ) : null}
      {validationError || serverError ? <p className="inline-error" role="alert">{validationError ?? serverError}</p> : null}
    </form>
  )
}

export function CaptureImportDialog({
  onClose,
  onKnowledgeImported,
  onKnowledgeStatusChange,
  onSubmit,
  open,
  pending,
  prefill = null,
  serverError,
}: CaptureImportDialogProps) {
  const [mode, setMode] = useState<'manual' | 'knowledge'>('manual')
  const [text, setText] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)
  const [knowledgeStatus, setKnowledgeStatus] = useState<KnowledgeImportStatus>(idleKnowledgeStatus)
  const knowledgeBusy = knowledgeStatus.pending
  const busy = pending || knowledgeBusy
  const frozen = knowledgeImportFrozen(knowledgeStatus)
  const onKnowledgeStatusChangeRef = useRef(onKnowledgeStatusChange)
  const onCloseRef = useRef(onClose)
  const busyRef = useRef(busy)
  onKnowledgeStatusChangeRef.current = onKnowledgeStatusChange
  onCloseRef.current = onClose
  busyRef.current = busy
  const modePrefillKey = useRef<string | null>(null)

  const reportKnowledgeStatus = useCallback((status: KnowledgeImportStatus) => {
    setKnowledgeStatus(status)
    onKnowledgeStatusChangeRef.current?.(status)
  }, [])

  useEffect(() => {
    if (open) return
    modePrefillKey.current = null
    setMode('manual')
    setText('')
    setValidationError(null)
    setKnowledgeStatus(idleKnowledgeStatus)
  }, [open])

  useEffect(() => {
    if (!open || frozen) return
    if (!prefill) {
      if (modePrefillKey.current === null) return
      modePrefillKey.current = null
      if (!busyRef.current) onCloseRef.current()
      return
    }
    if (modePrefillKey.current === prefill.request_id) return
    modePrefillKey.current = prefill.request_id
    setMode('knowledge')
  }, [frozen, open, prefill])

  const parsed = manualPacketPreview(mode, text)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    submitManualPacket(text, onSubmit, setValidationError)
  }

  const close = () => {
    if (busy) return
    onClose()
  }

  return (
    <ImportDialogView
      busy={busy}
      close={close}
      knowledgeBusy={knowledgeBusy}
      knowledgeStatus={knowledgeStatus}
      loadFile={(event) => void loadManualPacketFile(event, setText, setValidationError)}
      mode={mode}
      onKnowledgeImported={onKnowledgeImported ?? (async () => undefined)}
      onManualSubmit={submit}
      onManualText={(value) => { setText(value); setValidationError(null) }}
      onMode={setMode}
      onStatusChange={reportKnowledgeStatus}
      open={open}
      packet={parsed}
      pending={pending}
      prefill={prefill}
      serverError={serverError}
      text={text}
      validationError={validationError}
    />
  )
}
