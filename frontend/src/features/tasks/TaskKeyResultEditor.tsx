import type { Objective, Task, TaskKeyResultRef } from '../../domain/types'
import {
  eligibleKeyResults,
  keyResultKey,
  projectKeyResults,
  refKey,
} from '../../domain/keyResultModel'
import { Icon } from '../../components/Icon'

interface TaskKeyResultEditorProps {
  draft: Task
  isSaving: boolean
  objectives: Objective[]
  onDraftChange: (draft: Task) => void
  onSave: (patch: { key_result_refs: TaskKeyResultRef[] }) => void
}

function sortRefs(refs: TaskKeyResultRef[]) {
  return [...refs].sort((left, right) => (
    left.objective_id === right.objective_id
      ? left.key_result_id.localeCompare(right.key_result_id)
      : left.objective_id.localeCompare(right.objective_id)
  ))
}

/** Option identity uses the model's unambiguous tuple key, never a raw join. */
function optionValue(ref: TaskKeyResultRef) {
  return refKey('', ref)
}

function nodeValue(objectiveId: string, keyResultId: string) {
  return keyResultKey('', objectiveId, keyResultId)
}

export function TaskKeyResultEditor({
  draft,
  isSaving,
  objectives,
  onDraftChange,
  onSave,
}: TaskKeyResultEditorProps) {
  const current = draft.key_result_refs ?? []
  const projection = projectKeyResults({
    workspaceId: '',
    tasks: [draft],
    objectives,
  })
  const resolution = projection.tasks[0]
  const eligible = eligibleKeyResults(draft.objective_ids, objectives)
  const selected = new Set(current.map((ref) => optionValue(ref)))
  const available = eligible.filter(
    (node) => !selected.has(nodeValue(node.objectiveId, node.keyResultId)),
  )

  const emit = (refs: TaskKeyResultRef[]) => {
    const key_result_refs = sortRefs(refs)
    onDraftChange({ ...draft, key_result_refs })
    onSave({ key_result_refs })
  }

  return (
    <div className="outcome-editor">
      <span className="outcome-editor__label">Outcomes</span>
      {current.length ? (
        <div className="outcome-chips">
          {resolution.refs.map((item) => {
            const label = item.resolved
              ? projection.byKey[item.key]
              : undefined
            const value = optionValue(item.ref)
            return (
              <button
                aria-label={`Remove outcome ${item.ref.objective_id} ${item.ref.key_result_id}`}
                disabled={isSaving}
                key={value}
                onClick={() => emit(current.filter((ref) => optionValue(ref) !== value))}
                type="button"
              >
                <strong>{item.ref.objective_id} · {item.ref.key_result_id}</strong>
                <span>
                  {label ? `${label.objectiveTitle} — ${label.text}` : 'Unresolved outcome'}
                </span>
                {label && label.recordedProgress !== null
                  ? <em>{`Recorded progress ${label.recordedProgress}`}</em>
                  : null}
                <Icon name="close" size={12} />
              </button>
            )
          })}
          <button
            aria-label="Clear outcomes"
            disabled={isSaving}
            onClick={() => emit([])}
            type="button"
          >Clear outcomes</button>
        </div>
      ) : <p className="outcome-editor__empty">No outcomes linked</p>}
      <label className="field">
        <span>Add outcome</span>
        <select
          aria-label="Add outcome"
          disabled={isSaving || available.length === 0}
          onChange={(event) => {
            if (!event.target.value) return
            const node = available.find(
              (item) => nodeValue(item.objectiveId, item.keyResultId) === event.target.value,
            )
            if (!node) return
            emit([
              ...current,
              { objective_id: node.objectiveId, key_result_id: node.keyResultId },
            ])
          }}
          value=""
        >
          <option value="">Choose an outcome…</option>
          {available.map((node) => (
            <option
              key={nodeValue(node.objectiveId, node.keyResultId)}
              value={nodeValue(node.objectiveId, node.keyResultId)}
            >
              {node.objectiveId} · {node.keyResultId} — {node.text}
            </option>
          ))}
        </select>
      </label>
      {available.length === 0 ? (
        <p className="outcome-editor__empty">
          {draft.objective_ids.length === 0
            ? 'Align this Task with an Objective first to link its outcomes.'
            : 'No further uniquely resolvable outcomes are available on the aligned Objectives.'}
        </p>
      ) : null}
    </div>
  )
}
