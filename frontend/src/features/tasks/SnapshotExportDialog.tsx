import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Dialog } from '../../components/Dialog'
import { Button, LoadingBlock } from '../../components/Primitives'
import { getErrorMessage } from '../../utils/format'

interface SnapshotExportDialogProps {
  open: boolean
  taskId: string
  onClose: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

export function SnapshotExportDialog({
  onClose,
  onNotice,
  open,
  taskId,
}: SnapshotExportDialogProps) {
  const [confirmed, setConfirmed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const previewQuery = useQuery({
    queryKey: ['task-snapshot', taskId],
    queryFn: () => api.getTaskSnapshot(taskId),
    enabled: open,
    staleTime: 0,
  })

  useEffect(() => {
    if (!open) return
    setConfirmed(false)
    setSaving(false)
    setError(null)
  }, [open, taskId])

  const close = () => {
    if (!saving) onClose()
  }

  const save = async () => {
    const preview = previewQuery.data
    if (!preview || !confirmed || saving) return
    setSaving(true)
    setError(null)
    try {
      const delivered = await api.downloadTaskSnapshot(
        taskId,
        preview.snapshot.revision,
        preview.digest,
      )
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
      onNotice('Snapshot download started. Work Stack remains unchanged.', 'success')
      onClose()
    } catch (cause) {
      setError(getErrorMessage(cause))
      setSaving(false)
    }
  }

  const preview = previewQuery.data
  return (
    <Dialog
      description="Review the exact planning text before creating a user-carried file."
      footer={(
        <>
          <Button disabled={saving} onClick={close} variant="ghost">Cancel</Button>
          <Button
            disabled={!preview || !confirmed || saving}
            icon="arrowUpRight"
            onClick={() => { void save() }}
            variant="primary"
          >{saving ? 'Preparing file…' : 'Save snapshot file'}</Button>
        </>
      )}
      onClose={close}
      open={open}
      size="large"
      title="Export to Conduit"
    >
      {previewQuery.isPending ? <LoadingBlock label="Validating committed task snapshot…" /> : null}
      {previewQuery.isError ? (
        <p className="inline-error" role="alert">{getErrorMessage(previewQuery.error)}</p>
      ) : null}
      {preview ? (
        <div className="snapshot-review">
          <section className="snapshot-review__notice" aria-label="Snapshot boundaries">
            <strong>This is a snapshot, not a live link.</strong>
            <p>Conduit receives a copy. Import does not update Work Stack, and execution must be confirmed in Conduit.</p>
          </section>
          <section className="snapshot-review__content" aria-label="Exact exported content">
            <div>
              <span>Exact title</span>
              <p>{preview.snapshot.title}</p>
            </div>
            <div>
              <span>Exact detail</span>
              <pre>{preview.snapshot.detail || '(empty)'}</pre>
            </div>
          </section>
          <section className="snapshot-review__meta" aria-label="Snapshot metadata">
            <span>Revision {preview.snapshot.revision}</span>
            <span>{preview.snapshot.planning_status}</span>
            <span>{preview.snapshot.planning_priority}</span>
            <code>{preview.digest}</code>
          </section>
          <p className="snapshot-review__omissions">
            Objectives, dependencies, subtasks, notes, and tags are omitted from this v1 file.
          </p>
          <label className="snapshot-review__confirmation">
            <input
              checked={confirmed}
              disabled={saving}
              onChange={(event) => setConfirmed(event.target.checked)}
              type="checkbox"
            />
            <span>I reviewed the exact title and detail and understand the omissions.</span>
          </label>
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </div>
      ) : null}
    </Dialog>
  )
}
