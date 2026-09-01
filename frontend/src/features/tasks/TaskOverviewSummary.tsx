import { Button, Pill } from '../../components/Primitives'
import type { Objective, Task } from '../../domain/types'
import { getObjectiveTitle, priorityLabels } from '../../utils/format'
import type { EditableTaskField } from './taskDrawerModel'

interface TaskOverviewSummaryProps {
  canDiscard: boolean
  canRetry: boolean
  draft: Task
  isSaving: boolean
  navigationLocked: boolean
  objectives: Objective[]
  onDiscard: () => void
  onDraftChange: (draft: Task) => void
  onInvalidTitle: () => void
  onMarkDirty: (field: EditableTaskField) => void
  onOpenObjective?: (objectiveId: string) => void
  onOpenSnapshot: () => void
  onRetry: () => void
  onSaveTitle: (title: string) => void
  saveError: string | null
}

export function TaskOverviewSummary({
  canDiscard,
  canRetry,
  draft,
  isSaving,
  navigationLocked,
  objectives,
  onDiscard,
  onDraftChange,
  onInvalidTitle,
  onMarkDirty,
  onOpenObjective,
  onOpenSnapshot,
  onRetry,
  onSaveTitle,
  saveError,
}: TaskOverviewSummaryProps) {
  return (
    <>
      <label className="drawer-title-field">
        <span className="sr-only">Task title</span>
        <textarea
          disabled={isSaving}
          onBlur={() => {
            const title = draft.title.trim()
            if (title) onSaveTitle(title)
            else onInvalidTitle()
          }}
          onChange={(event) => { onMarkDirty('title'); onDraftChange({ ...draft, title: event.target.value }) }}
          rows={2}
          value={draft.title}
        />
      </label>
      <div className="drawer-chips">
        <Pill tone={draft.priority.toLowerCase()}>{draft.priority} · {priorityLabels[draft.priority]}</Pill>
        {objectives.map((objective) => onOpenObjective ? (
          <button
            aria-label={`Open objective ${objective.id}`}
            className="objective-pill-link"
            disabled={navigationLocked}
            key={objective.id}
            onClick={() => onOpenObjective(objective.id)}
            title={getObjectiveTitle(objective)}
            type="button"
          ><Pill tone="accent">{objective.id}</Pill></button>
        ) : <Pill key={objective.id} tone="accent">{objective.id}</Pill>)}
      </div>
      <div aria-label="Task identity" className="task-identity" role="group">
        <span><strong>Stable UID</strong><code>{draft.uid}</code></span>
        <span><strong>Current version</strong><code>Revision {draft.revision}</code></span>
      </div>
      <section className="snapshot-launch">
        <div>
          <strong>Execution handoff</strong>
          <p>Review and save one immutable planning-task revision for Conduit.</p>
        </div>
        <Button
          disabled={navigationLocked}
          icon="arrowUpRight"
          onClick={onOpenSnapshot}
          variant="secondary"
        >Export to Conduit</Button>
      </section>

      {saveError ? (
        <div className="inline-error" role="alert">
          <span>{saveError}</span>
          {canRetry ? <Button onClick={onRetry} variant="ghost">Retry save</Button> : null}
          {canDiscard ? <Button onClick={onDiscard} variant="ghost">Discard unsaved changes</Button> : null}
        </div>
      ) : null}
    </>
  )
}
