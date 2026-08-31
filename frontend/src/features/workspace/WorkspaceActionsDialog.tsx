import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { createIdempotencyKey } from '../../api/client'
import { Dialog } from '../../components/Dialog'
import { Button } from '../../components/Primitives'
import type { WorkspaceProjection } from '../../domain/types'
import { getErrorMessage, getObjectiveTitle } from '../../utils/format'
import { LocalContinuityCard } from './LocalContinuityCard'

interface WorkspaceActionsDialogProps {
  onClose: () => void
  onCreateNote: (text: string, links: string[], idempotencyKey: string) => Promise<void>
  onNotice?: (message: string, tone?: 'success' | 'error') => void
  open: boolean
  pending: boolean
  workspace: WorkspaceProjection
}

export function WorkspaceActionsDialog({
  onClose,
  onCreateNote,
  onNotice,
  open,
  pending,
  workspace,
}: WorkspaceActionsDialogProps) {
  const [note, setNote] = useState('')
  const [links, setLinks] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const noteIntentKey = useRef<string | null>(null)

  useEffect(() => {
    if (!open) return
    setNote('')
    setLinks([])
    setError(null)
    noteIntentKey.current = null
  }, [open])

  const linkTargets = useMemo(() => [
    ...workspace.objectives.map((item) => ({ id: item.id, label: getObjectiveTitle(item), kind: 'Objective' })),
    ...workspace.tasks.map((item) => ({ id: item.id, label: item.title, kind: 'Task' })),
  ], [workspace.objectives, workspace.tasks])

  if (!open) return null

  const submitNote = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      noteIntentKey.current ??= createIdempotencyKey()
      await onCreateNote(note.trim(), links, noteIntentKey.current)
      noteIntentKey.current = null
    } catch (caught) {
      setError(getErrorMessage(caught))
    }
  }

  return (
    <Dialog
      description="Keep shared context visible in the workspace graph and verify local continuity."
      onClose={() => { if (!pending) onClose() }}
      open={open}
      size="large"
      title="Workspace actions"
    >
      <div className="workspace-actions-grid">
        <form className="workspace-action-card" onSubmit={submitNote}>
          <header><span>Context card</span><strong>Keep shared context in view</strong></header>
          <label>
            <span>Context card</span>
            <textarea
              disabled={pending}
              onChange={(event) => { noteIntentKey.current = null; setNote(event.target.value) }}
              placeholder="Decision, assumption, or context…"
              required
              rows={4}
              value={note}
            />
          </label>
          <fieldset className="workspace-link-picker">
            <legend>Connect to tasks or objectives</legend>
            {linkTargets.length ? linkTargets.map((target) => (
              <label key={target.id}>
                <input
                  checked={links.includes(target.id)}
                  disabled={pending}
                  onChange={(event) => {
                    noteIntentKey.current = null
                    setLinks((current) => event.target.checked
                      ? [...current, target.id]
                      : current.filter((id) => id !== target.id))
                  }}
                  type="checkbox"
                />
                <span><strong>{target.id} · {target.label}</strong><small>{target.kind}</small></span>
              </label>
            )) : <p>No link targets yet. The Context card can still stand alone.</p>}
          </fieldset>
          <Button disabled={pending || !note.trim()} type="submit">Add context card</Button>
        </form>
        {onNotice ? <LocalContinuityCard onNotice={onNotice} /> : null}
      </div>
      {error ? <p className="form-error" role="alert">{error}</p> : null}
    </Dialog>
  )
}
