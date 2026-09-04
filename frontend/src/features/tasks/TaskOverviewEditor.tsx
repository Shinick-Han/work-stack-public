import { Icon } from '../../components/Icon'
import { DateInput } from '../../components/DateInput'
import type { ReactNode } from 'react'
import {
  TASK_PRIORITIES,
  TASK_STATUSES,
  type Objective,
  type Task,
  type WorkspaceProjection,
} from '../../domain/types'
import { getObjectiveTitle, priorityLabels, statusLabels } from '../../utils/format'
import { TaskKeyResultEditor } from './TaskKeyResultEditor'
import type { EditableTaskField, EditableTaskPatch } from './taskDrawerModel'

interface TaskOverviewEditorProps {
  availableDependencyTasks: Task[]
  availableParentTasks: Task[]
  draft: Task
  isSaving: boolean
  onDraftChange: (draft: Task) => void
  onMarkDirty: (field: EditableTaskField) => void
  onSave: (patch: EditableTaskPatch) => void
  onTagTextChange: (value: string) => void
  relationshipSection?: ReactNode
  tagText: string
  workspace: WorkspaceProjection
}


interface AlignmentSectionProps {
  draft: Task
  isSaving: boolean
  objectives: Objective[]
  onDraftChange: (draft: Task) => void
  onSave: (patch: EditableTaskPatch) => void
}

function AlignmentSection({ draft, isSaving, objectives, onDraftChange, onSave }: AlignmentSectionProps) {
  return (
    <section className="drawer-section">
      <h3>Alignment</h3>
      <div className="objective-checks">
        {objectives.map((objective) => {
          const checked = draft.objective_ids.includes(objective.id)
          const blocked = checked
            && (draft.key_result_refs ?? []).some((ref) => ref.objective_id === objective.id)
          return (
            <label key={objective.id}>
              <input
                checked={checked}
                disabled={isSaving || blocked}
                onChange={() => {
                  if (blocked) return
                  const objective_ids = checked
                    ? draft.objective_ids.filter((id) => id !== objective.id)
                    : [...draft.objective_ids, objective.id]
                  onDraftChange({ ...draft, objective_ids })
                  onSave({ objective_ids })
                }}
                type="checkbox"
              />
              <span><strong>{objective.id}</strong>{getObjectiveTitle(objective)}</span>
              {blocked ? (
                <small className="objective-checks__blocked">Unlink its outcomes first</small>
              ) : null}
            </label>
          )
        })}
      </div>
      <TaskKeyResultEditor
        draft={draft}
        isSaving={isSaving}
        objectives={objectives}
        onDraftChange={onDraftChange}
        onSave={onSave}
      />
    </section>
  )
}

export function TaskOverviewEditor({
  availableDependencyTasks,
  availableParentTasks,
  draft,
  isSaving,
  onDraftChange,
  onMarkDirty,
  onSave,
  onTagTextChange,
  relationshipSection,
  tagText,
  workspace,
}: TaskOverviewEditorProps) {
  return (
    <>
      <section className="drawer-section">
        <h3>Properties</h3>
        <div className="property-grid">
          <label><span>Status</span><select disabled={isSaving} onChange={(event) => { const status = event.target.value as Task['status']; onDraftChange({ ...draft, status }); onSave({ status }) }} value={draft.status}>{TASK_STATUSES.map((status) => <option key={status} value={status}>{statusLabels[status]}</option>)}</select></label>
          <label><span>Priority</span><select disabled={isSaving} onChange={(event) => { const priority = event.target.value as Task['priority']; onDraftChange({ ...draft, priority }); onSave({ priority }) }} value={draft.priority}>{TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{priority} · {priorityLabels[priority]}</option>)}</select></label>
          <DateInput className="date-input-control--property" label="Plan for" disabled={isSaving} onBlur={(value) => onSave({ scheduled: value || null })} onChange={(value) => { onMarkDirty('scheduled'); onDraftChange({ ...draft, scheduled: value || null }) }} value={draft.scheduled ?? ''} />
          <DateInput className="date-input-control--property" label="Due" disabled={isSaving} onBlur={(value) => onSave({ due: value || null })} onChange={(value) => { onMarkDirty('due'); onDraftChange({ ...draft, due: value || null }) }} value={draft.due ?? ''} />
          <label><span>Estimate <small>minutes</small></span><input disabled={isSaving} max={1440} min={1} onBlur={() => onSave({ estimate_minutes: draft.estimate_minutes ?? null })} onChange={(event) => { onMarkDirty('estimate_minutes'); onDraftChange({ ...draft, estimate_minutes: event.target.value ? Number(event.target.value) : null }) }} type="number" value={draft.estimate_minutes ?? ''} /></label>
          <label><span>Parent</span><select disabled={isSaving} onChange={(event) => { const parent_id = event.target.value || null; onDraftChange({ ...draft, parent_id }); onSave({ parent_id }) }} value={draft.parent_id ?? ''}><option value="">No parent</option>{availableParentTasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
        </div>
      </section>

      <section className="drawer-section">
        <h3>Definition of done</h3>
        <label>
          <span className="sr-only">Definition of done</span>
          <textarea
            className="drawer-detail-input"
            disabled={isSaving}
            onBlur={() => onSave({ detail: draft.detail })}
            onChange={(event) => { onMarkDirty('detail'); onDraftChange({ ...draft, detail: event.target.value }) }}
            placeholder="Describe the outcome…"
            rows={5}
            value={draft.detail}
          />
        </label>
      </section>

      <AlignmentSection
        draft={draft}
        isSaving={isSaving}
        objectives={workspace.objectives}
        onDraftChange={onDraftChange}
        onSave={onSave}
      />

      {relationshipSection}

      <section className="drawer-section">
        <h3>Tags & dependencies</h3>
        <label className="field"><span>Tags</span><input disabled={isSaving} onBlur={() => { const tags = tagText.split(',').map((value) => value.trim()).filter(Boolean); onDraftChange({ ...draft, tags }); onSave({ tags }) }} onChange={(event) => { const value = event.target.value; onMarkDirty('tags'); onTagTextChange(value); onDraftChange({ ...draft, tags: value.split(',').map((item) => item.trim()).filter(Boolean) }) }} value={tagText} /></label>
        <div className="dependency-editor">
          <span className="dependency-editor__label">Dependencies</span>
          {draft.dependencies.length ? (
            <div className="dependency-chips">
              {draft.dependencies.map((dependencyId) => {
                const dependency = workspace.tasks.find((task) => task.id === dependencyId)
                return (
                  <button
                    aria-label={`Remove dependency ${dependencyId}`}
                    disabled={isSaving}
                    key={dependencyId}
                    onClick={() => {
                      const dependencies = draft.dependencies.filter((id) => id !== dependencyId)
                      onDraftChange({ ...draft, dependencies })
                      onSave({ dependencies })
                    }}
                    type="button"
                  ><strong>{dependencyId}</strong><span>{dependency?.title ?? 'Unavailable Task'}</span><Icon name="close" size={12} /></button>
                )
              })}
            </div>
          ) : <p className="dependency-editor__empty">No dependencies</p>}
          <label className="field"><span>Add dependency</span><select
            aria-label="Add dependency"
            disabled={isSaving || availableDependencyTasks.length === 0}
            onChange={(event) => {
              if (!event.target.value) return
              const dependencies = [...new Set([...draft.dependencies, event.target.value])].sort()
              onDraftChange({ ...draft, dependencies })
              onSave({ dependencies })
            }}
            value=""
          ><option value="">Choose a Task…</option>{availableDependencyTasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
        </div>
      </section>
    </>
  )
}
