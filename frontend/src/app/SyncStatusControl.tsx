import { useEffect, useRef } from 'react'
import type { SyncStatus } from '../domain/types'
import { Button, IconButton } from '../components/Primitives'
import { ApiError } from '../api/client'

interface SyncStatusControlProps {
  error: unknown
  isFetching: boolean
  onRefresh: () => void
  onReview: () => void
  status?: SyncStatus
}

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
  if (status.state === 'in-sync') return { label: 'SSOT in sync', tone: 'ok', action: '' }
  if (status.state === 'external-change-detected') return { label: 'SSOT changed', tone: 'warning', action: 'Review SSOT changes' }
  if (status.state === 'invalid' || status.state === 'external-change-invalid') return { label: 'SSOT invalid · writes blocked', tone: 'danger', action: 'Review SSOT changes' }
  if (status.state === 'stale') return { label: 'SSOT stale · writes blocked', tone: 'warning', action: 'Review SSOT changes' }
  return { label: 'Disconnected · writes blocked', tone: 'danger', action: 'Reconnect' }
}

export function SyncStatusControl({ error, isFetching, onRefresh, onReview, status }: SyncStatusControlProps) {
  const copy = statusCopy(status, isFetching, error)
  const available = Boolean(status) || Boolean(error && !isSyncUnavailable(error))
  const actionable = available && copy.action.length > 0 && !isFetching
  const run = () => {
    if (status && ['external-change-detected', 'invalid', 'external-change-invalid', 'stale'].includes(status.state)) onReview()
    else onRefresh()
  }

  return (
    <button
      aria-label={actionable ? copy.action : copy.label}
      className={`sync-status sync-status--${copy.tone}`}
      disabled={!actionable}
      onClick={run}
      type="button"
    >
      <span className="sync-status__dot" />
      <span>{copy.label}</span>
    </button>
  )
}

export function SyncStatusDialog({ adoptError, adopting, onAdopt, onClose, onRefresh, status }: {
  adoptError: string | null
  adopting: boolean
  onAdopt: () => void
  onClose: () => void
  onRefresh: () => void
  status: SyncStatus
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    dialogRef.current?.showModal()
  }, [])

  return (
    <dialog aria-labelledby="sync-review-title" className="dialog dialog--small" onClose={onClose} ref={dialogRef}>
      <div className="dialog__surface">
        <header className="dialog__header">
          <div>
            <h2 id="sync-review-title">Review SSOT changes</h2>
            <p>This is a read-only reconciliation summary. Refreshing does not merge or overwrite either side.</p>
          </div>
          <IconButton icon="close" label="Close SSOT review" onClick={() => dialogRef.current?.close()} variant="ghost" />
        </header>
        <div className="dialog__body sync-review">
          <dl>
            <div><dt>State</dt><dd>{status.state}</dd></div>
            <div><dt>Workspace</dt><dd>{status.workspace_id}</dd></div>
            <div><dt>Generation</dt><dd>{status.generation}</dd></div>
            <div><dt>Manifest</dt><dd><code>{status.manifest_digest ?? 'Not available'}</code></dd></div>
          </dl>
          {status.reason ? <p className="sync-review__reason">{status.reason}</p> : null}
          <section>
            <h3>Changed files</h3>
            {status.changed_files.length ? (
              <ul>{status.changed_files.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
            ) : <p>No file-level summary was supplied.</p>}
          </section>
          {status.state === 'external-change-detected' ? (
            <p className="sync-review__adoption-note">Accept only after reviewing this validated file list. Adoption advances the Work Stack baseline; it does not perform a field-level merge. If this server does not support adoption, writes remain paused.</p>
          ) : null}
          {adoptError ? <p className="sync-review__error" role="alert">{adoptError} Writes remain paused.</p> : null}
        </div>
        <footer className="dialog__footer">
          <Button onClick={() => dialogRef.current?.close()} variant="ghost">Close</Button>
          <Button icon="refresh" onClick={onRefresh}>Refresh authoritative state</Button>
          {status.state === 'external-change-detected' ? (
            <Button disabled={adopting || !status.manifest_digest} onClick={onAdopt} variant="primary">
              {adopting ? 'Accepting…' : 'Accept validated SSOT changes'}
            </Button>
          ) : null}
        </footer>
      </div>
    </dialog>
  )
}
