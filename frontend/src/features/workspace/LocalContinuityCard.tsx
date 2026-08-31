import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Button, LoadingBlock } from '../../components/Primitives'
import type { StorageStatus } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'

interface LocalContinuityCardProps {
  onNotice?: (message: string, tone?: 'success' | 'error') => void
}

function readableBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

export function buildSafeSupportSummary(status: StorageStatus): string {
  return [
    'Work Stack safe support summary',
    `Product version: ${status.product_version}`,
    `Store schema: ${status.store_schema_version}`,
    'Store readiness: ready',
    `Data files: ${status.file_count}`,
    `Data size: ${readableBytes(status.total_bytes)}`,
    `Backup format: ${status.backup_format}`,
    'Restore mode: offline with app shutdown',
  ].join('\n')
}

export function LocalContinuityCard({ onNotice }: LocalContinuityCardProps) {
  const [confirmed, setConfirmed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<{ digest: string; filename: string } | null>(null)
  const statusQuery = useQuery({
    queryKey: ['storage-status'],
    queryFn: api.getStorageStatus,
    staleTime: 10_000,
  })

  const download = async () => {
    const status = statusQuery.data
    if (!status || !confirmed || saving) return
    setSaving(true)
    setError(null)
    try {
      const delivered = await api.downloadBackup(status.workspace_id)
      const objectUrl = URL.createObjectURL(delivered.blob)
      try {
        const anchor = document.createElement('a')
        anchor.href = objectUrl
        anchor.download = delivered.filename
        anchor.hidden = true
        document.body.append(anchor)
        anchor.click()
        anchor.remove()
      } finally {
        URL.revokeObjectURL(objectUrl)
      }
      setReceipt({ digest: delivered.digest, filename: delivered.filename })
      setConfirmed(false)
      onNotice?.('Verified local backup download started.', 'success')
    } catch (cause) {
      const message = getErrorMessage(cause)
      setError(message)
      onNotice?.(message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const copySupportSummary = async () => {
    const status = statusQuery.data
    if (!status) return
    setError(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable in this browser.')
      await navigator.clipboard.writeText(buildSafeSupportSummary(status))
      onNotice?.('Safe support summary copied.', 'success')
    } catch (cause) {
      const message = getErrorMessage(cause)
      setError(message)
      onNotice?.(message, 'error')
    }
  }

  return (
    <section className="workspace-action-card local-continuity-card" aria-labelledby="local-continuity-title">
      <header><span>Local continuity</span><strong id="local-continuity-title">Verify and carry your workspace</strong></header>
      {statusQuery.isPending ? <LoadingBlock label="Verifying local store…" /> : null}
      {statusQuery.isError ? <p className="form-error" role="alert">{getErrorMessage(statusQuery.error)}</p> : null}
      {statusQuery.data ? (
        <>
          <dl className="storage-status">
            <div><dt>Product</dt><dd>Work Stack {statusQuery.data.product_version}</dd></div>
            <div><dt>Store</dt><dd>Ready · schema {statusQuery.data.store_schema_version}</dd></div>
            <div><dt>Workspace</dt><dd><code>{statusQuery.data.workspace_id}</code></dd></div>
            <div><dt>Data files</dt><dd>{statusQuery.data.file_count} · {readableBytes(statusQuery.data.total_bytes)}</dd></div>
            <div><dt>Backup</dt><dd>Verified ZIP v1</dd></div>
          </dl>
          <div className="release-integrity-guidance">
            <strong>Manual, verified updates</strong>
            <p>Work Stack does not download or run updates in the background. Verify the setup file
              against its adjacent <code>.sha256</code> before running the updater; a verified update
              creates a pre-upgrade backup and preserves the configured data directory.</p>
            <Button onClick={() => { void copySupportSummary() }} variant="secondary">Copy safe support summary</Button>
          </div>
          <div className="local-continuity-card__warning">
            <strong>The backup contains your complete local Work Stack data.</strong>
            <p>Keep it private. Restore remains offline: close Work Stack before using the maintenance restore command.</p>
          </div>
          <label className="local-continuity-card__confirmation">
            <input checked={confirmed} disabled={saving} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" />
            <span>I understand this file contains the full local workspace.</span>
          </label>
          <Button disabled={!confirmed || saving} onClick={() => { void download() }} variant="secondary">
            {saving ? 'Building verified backup…' : 'Download verified backup'}
          </Button>
          {receipt ? <p className="backup-receipt"><strong>{receipt.filename}</strong><code>{receipt.digest}</code></p> : null}
          {error ? <p className="form-error" role="alert">{error}</p> : null}
        </>
      ) : null}
    </section>
  )
}
