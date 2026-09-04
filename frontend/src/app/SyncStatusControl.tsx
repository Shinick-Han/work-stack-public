import { useEffect, useRef, useState } from 'react'
import type { SyncStatus, WorkspaceRebindPreview } from '../domain/types'
import { Button, IconButton } from '../components/Primitives'
import { ApiError } from '../api/client'

interface SyncStatusControlProps {
  error: unknown
  isFetching: boolean
  onRefresh: () => void
  onReview: () => void
  onConfigureSsot?: () => void
  status?: SyncStatus
}

const reviewStates = new Set(['external-change-detected', 'invalid', 'external-change-invalid', 'stale'])

export function isSyncUnavailable(error: unknown) {
  return error instanceof ApiError && error.status === 404
}

export function isSyncWriteBlocked(status?: SyncStatus, error?: unknown) {
  if (error && !isSyncUnavailable(error)) return true
  return Boolean(status && [
    'external-change-detected',
    'invalid',
    'external-change-invalid',
    'disconnected',
    'stale',
  ].includes(status.state))
}

function statusCopy(status: SyncStatus | undefined, isFetching: boolean, error: unknown) {
  if (error && !isSyncUnavailable(error)) return { label: 'Disconnected · writes blocked', tone: 'danger', action: 'Reconnect' }
  if (!status) return { label: 'Secure local', tone: 'ok', action: '' }
  if (isFetching || status.state === 'refreshing' || status.state.startsWith('agent-update')) {
    return { label: status.state.startsWith('agent-update') ? 'Agent update' : 'Refreshing', tone: 'progress', action: '' }
  }
  if (status.rebind_available) return { label: 'Different workspace identity · review required', tone: 'danger', action: 'Review workspace identity' }
  if (status.state === 'in-sync') return { label: 'SSOT in sync', tone: 'ok', action: '' }
  if (status.state === 'external-change-detected') return { label: 'SSOT update ready', tone: 'warning', action: 'Sync / review SSOT changes' }
  if (status.state === 'invalid' || status.state === 'external-change-invalid') return { label: 'SSOT conflict · writes blocked', tone: 'danger', action: 'Review SSOT conflict' }
  if (status.state === 'stale') return { label: 'SSOT review stale · writes blocked', tone: 'warning', action: 'Review stale SSOT state' }
  return { label: 'Disconnected · writes blocked', tone: 'danger', action: 'Reconnect' }
}

export function SyncStatusControl({ error, isFetching, onConfigureSsot, onRefresh, onReview, status }: SyncStatusControlProps) {
  const copy = statusCopy(status, isFetching, error)
  const available = Boolean(status) || Boolean(error && !isSyncUnavailable(error))
  const actionable = available && copy.action.length > 0 && !isFetching
  const run = () => {
    if (actionable) {
      if (reviewStates.has(status?.state ?? '')) onReview()
      else onRefresh()
      return
    }
    onConfigureSsot?.()
  }
  const configure = !actionable && Boolean(onConfigureSsot)

  return (
    <button
      aria-label={actionable ? copy.action : configure ? `Configure SSOT connections · ${copy.label}` : copy.label}
      className={`sync-status sync-status--${copy.tone}`}
      disabled={!actionable && !configure}
      onClick={run}
      type="button"
    >
      <span className="sync-status__dot" />
      <span>{copy.label}</span>
    </button>
  )
}

function dialogTitle(status: SyncStatus) {
  if (status.rebind_available) return 'Review different workspace identity'
  if (status.state === 'external-change-detected') return 'Review SSOT update'
  if (status.state === 'stale') return 'Refresh stale SSOT review'
  if (status.state === 'invalid' || status.state === 'external-change-invalid') return 'Resolve SSOT conflict'
  return 'Review SSOT state'
}

function candidateLabel(status: SyncStatus) {
  if (status.rebind_available) return 'Different workspace identity'
  if (status.state === 'external-change-detected') return 'Detected external candidate'
  if (status.state === 'stale') return 'Expired candidate review'
  return 'Rejected external candidate'
}

function candidateRevision(status: SyncStatus) {
  if (status.rebind_available) return 'Explicit review required'
  if (status.state === 'stale') return 'Refresh required'
  if (status.state === 'invalid' || status.state === 'external-change-invalid') return 'Validation failed'
  return status.manifest_digest ? `Revision ${status.manifest_digest.slice(7, 19)}` : 'Revision unavailable'
}

function digestLabel(status: SyncStatus) {
  if (status.rebind_available) return 'Candidate workspace digest'
  return status.state === 'external-change-detected' ? 'Candidate digest' : 'Reported manifest digest'
}

function dialogIntroduction(status: SyncStatus) {
  if (status.rebind_available) return 'Review is read-only. The configured path now identifies a different workspace; nothing changes until you explicitly confirm reconnection.'
  return 'Review is read-only. Nothing is copied, merged, or overwritten until you explicitly accept the currently validated candidate.'
}

function RevisionComparison({ status }: { status: SyncStatus }) {
  const fileSummary = status.changed_files.length === 1 ? '1 file differs' : `${status.changed_files.length} files differ`
  return (
    <section aria-label="SSOT revision comparison" className="sync-review__comparison">
      <article>
        <span>Accepted baseline</span>
        <strong>Generation {status.generation}</strong>
        <small>Current Work Stack planning baseline</small>
      </article>
      <span aria-hidden="true" className="sync-review__comparison-arrow">→</span>
      <article>
        <span>{candidateLabel(status)}</span>
        <strong>{candidateRevision(status)}</strong>
        <small>{fileSummary}</small>
      </article>
    </section>
  )
}

function ChangedFiles({ status }: { status: SyncStatus }) {
  return (
    <section>
      <h3>Changed files</h3>
      {status.changed_files.length ? (
        <ul>{status.changed_files.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
      ) : <p>No file-level summary was supplied.</p>}
    </section>
  )
}

function ReviewGuidance({ status }: { status: SyncStatus }) {
  if (status.state === 'external-change-detected') return (
    <p className="sync-review__adoption-note">Accept only after reviewing the candidate revision and file list. Acceptance advances the Work Stack baseline; it is not a field-level merge.</p>
  )
  if (status.rebind_available) return <p className="sync-review__adoption-note">The configured path now contains a different valid workspace. Normal change acceptance cannot cross workspace identity. Verify the new identity below to create a candidate backup and establish a new runtime baseline without changing planning bytes.</p>
  return <p className="sync-review__error" role="status">This candidate cannot be accepted. Refresh or repair the SSOT first; writes remain paused.</p>
}

function AdoptionError({ error }: { error: string | null }) {
  if (!error) return null
  return <p className="sync-review__error" role="alert"><strong>The reviewed candidate was not accepted.</strong> {error}. Refresh comparison before retrying; writes remain paused.</p>
}

function WorkspaceRebindRecovery({
  error,
  onRebind,
  preview,
  rebinding,
}: {
  error: string | null
  onRebind: () => void
  preview?: WorkspaceRebindPreview
  rebinding: boolean
}) {
  const [confirmation, setConfirmation] = useState('')
  if (!preview) return (
    <section className="sync-review__rebind">
      <h3>Reconnect workspace identity</h3>
      <p>{error ?? 'Reading the content-free workspace recovery coordinate…'}</p>
    </section>
  )
  const confirmed = confirmation.trim() === preview.candidate_workspace_id
  return (
    <section className="sync-review__rebind">
      <h3>Reconnect workspace identity</h3>
      <dl>
        <div><dt>Previous identity</dt><dd><code>{preview.manifest_workspace_id}</code></dd></div>
        <div><dt>Candidate identity</dt><dd><code>{preview.candidate_workspace_id}</code></dd></div>
        <div><dt>Candidate digest</dt><dd><code>{preview.candidate_digest}</code></dd></div>
      </dl>
      <label className="field">
        <span>Type the candidate identity to confirm</span>
        <input
          autoComplete="off"
          onChange={(event) => setConfirmation(event.target.value)}
          spellCheck={false}
          value={confirmation}
        />
      </label>
      <p className="sync-review__adoption-note">Work Stack will preserve an exact candidate ZIP, quarantine-copy the old runtime manifest, and write a content-free receipt before atomically switching the runtime baseline.</p>
      {error ? <p className="sync-review__error" role="alert">{error}</p> : null}
      <Button disabled={!confirmed || rebinding} onClick={onRebind} variant="primary">
        {rebinding ? 'Reconnecting…' : 'Back up and reconnect workspace'}
      </Button>
    </section>
  )
}

export function SyncStatusDialog({ adoptError, adopting, onAdopt, onClose, onRebind = () => undefined, onRefresh, rebindError = null, rebindPreview, rebinding = false, refreshing = false, status }: {
  adoptError: string | null
  adopting: boolean
  onAdopt: () => void
  onClose: () => void
  onRebind?: () => void
  onRefresh: () => void
  rebindError?: string | null
  rebindPreview?: WorkspaceRebindPreview
  rebinding?: boolean
  refreshing?: boolean
  status: SyncStatus
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    dialogRef.current?.showModal()
  }, [])
  const canAdopt = !status.rebind_available && status.state === 'external-change-detected' && Boolean(status.manifest_digest)

  return (
    <dialog aria-labelledby="sync-review-title" className="dialog dialog--small" onClose={onClose} ref={dialogRef}>
      <div className="dialog__surface">
        <header className="dialog__header">
          <div>
            <h2 id="sync-review-title">{dialogTitle(status)}</h2>
            <p>{dialogIntroduction(status)}</p>
          </div>
          <IconButton icon="close" label="Close SSOT review" onClick={() => dialogRef.current?.close()} variant="ghost" />
        </header>
        <div className="dialog__body sync-review">
          <RevisionComparison status={status} />
          <dl>
            <div><dt>State</dt><dd>{status.state}</dd></div>
            <div><dt>Workspace</dt><dd>{status.workspace_id}</dd></div>
            <div><dt>{digestLabel(status)}</dt><dd><code>{status.manifest_digest ?? 'Not available'}</code></dd></div>
          </dl>
          {status.reason && !status.rebind_available ? <p className="sync-review__reason">{status.reason}</p> : null}
          <ChangedFiles status={status} />
          <ReviewGuidance status={status} />
          <AdoptionError error={adoptError} />
          {status.rebind_available ? (
            <WorkspaceRebindRecovery
              error={rebindError}
              onRebind={onRebind}
              preview={rebindPreview}
              rebinding={rebinding}
            />
          ) : null}
        </div>
        <footer className="dialog__footer">
          <Button onClick={() => dialogRef.current?.close()} variant="ghost">Close</Button>
          <Button disabled={refreshing} icon="refresh" onClick={onRefresh}>{refreshing ? 'Refreshing comparison…' : 'Refresh comparison'}</Button>
          {canAdopt ? (
            <Button disabled={adopting || refreshing} onClick={onAdopt} variant="primary">
              {adopting ? 'Accepting…' : 'Accept reviewed candidate'}
            </Button>
          ) : null}
        </footer>
      </div>
    </dialog>
  )
}
