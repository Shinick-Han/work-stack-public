import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { Dialog } from '../../components/Dialog'
import { Button, Pill } from '../../components/Primitives'
import { capturePacketSchema, findForbiddenCaptureKey } from '../../domain/schemas'
import type { CapturePacket } from '../../domain/types'

export const MAX_CAPTURE_PACKET_BYTES = 64 * 1024

interface CaptureImportDialogProps {
  open: boolean
  pending: boolean
  serverError: string | null
  onClose: () => void
  onSubmit: (packet: CapturePacket) => void
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

export function CaptureImportDialog({
  onClose,
  onSubmit,
  open,
  pending,
  serverError,
}: CaptureImportDialogProps) {
  const [text, setText] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setText('')
      setValidationError(null)
    }
  }, [open])

  const parsed = useMemo(() => {
    if (!text.trim()) return null
    try {
      return parseCapturePacketText(text)
    } catch {
      return null
    }
  }, [text])

  const loadFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    if (file.size > MAX_CAPTURE_PACKET_BYTES) {
      setValidationError('Packet exceeds the 64 KiB limit.')
      return
    }
    const nextText = await file.text()
    setText(nextText)
    setValidationError(null)
    event.target.value = ''
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    try {
      const packet = parseCapturePacketText(text)
      setValidationError(null)
      onSubmit(packet)
    } catch (error) {
      setValidationError(formatValidationError(error))
    }
  }

  return (
    <Dialog
      description="Paste or choose a sanitized Capture Packet v1. Raw mail, chat, recipients, and attachments are rejected."
      footer={<><Button disabled={pending} onClick={onClose} variant="ghost">Cancel</Button><Button disabled={pending || !text.trim()} form="capture-import-form" icon="upload" type="submit" variant="primary">{pending ? 'Importing…' : 'Import packet'}</Button></>}
      onClose={onClose}
      open={open}
      size="large"
      title="Import context"
    >
      <form className="capture-import" id="capture-import-form" onSubmit={submit}>
        <div className="privacy-note">
          <span className="privacy-note__mark">SSOT</span>
          <div><strong>Microsoft 365 remains the source of truth.</strong><p>Work Stack stores only sanitized context and opaque source references. Manual packets must declare <code>capture_mode: "manual"</code>; this importer never fabricates model or tool-trace claims.</p></div>
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
            onChange={(event) => {
              setText(event.target.value)
              setValidationError(null)
            }}
            placeholder={'{\n  "schema_version": "1.0",\n  "source_key": "sha256:…",\n  …\n}'}
            rows={13}
            spellCheck={false}
            value={text}
          />
        </label>
        <p className="field-help" id="capture-import-help">The server independently verifies hashes, provenance, prohibited keys, and content-leakage rules.</p>
        {parsed ? (
          <div className="packet-preview">
            <div><Pill tone="neutral">Manual</Pill><span>{parsed.source.provider} · {parsed.source.resource_type}</span></div>
            <strong>{parsed.source.display_title}</strong>
            <p>{parsed.normalized.summary}</p>
          </div>
        ) : null}
        {validationError || serverError ? <p className="inline-error" role="alert">{validationError ?? serverError}</p> : null}
      </form>
    </Dialog>
  )
}
