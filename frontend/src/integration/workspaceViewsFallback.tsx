import type { WorkspaceViewsProps } from 'features/workspace/views'
import { EmptyState, Pill } from '../components/Primitives'
import type { TaskStatus } from '../domain/types'
import { statusLabels } from '../utils/format'

/**
 * Branch-isolation fallback only. Vite resolves `features/workspace/views` to Agent C's
 * implementation as soon as that directory exists on the integration branch.
 */
export function WorkspaceViews({
  objectiveId,
  onSelectTask,
  priority,
  search,
  selectedTaskId,
  status,
  tasks,
  view,
}: WorkspaceViewsProps) {
  const normalizedSearch = search.trim().toLowerCase()
  const visible = tasks.filter((task) => {
    const haystack = [task.id, task.title, task.detail, ...task.tags].join(' ').toLowerCase()
    return (
      (!normalizedSearch || haystack.includes(normalizedSearch)) &&
      (status === 'all' || task.status === status) &&
      (priority === 'all' || task.priority === priority) &&
      (objectiveId === 'all' || task.objective_ids.includes(objectiveId))
    )
  })

  if (!visible.length) {
    return (
      <EmptyState icon="search" title="No work matches these filters">
        Clear a filter or search for a different task.
      </EmptyState>
    )
  }

  return (
    <div className={`view-fallback view-fallback--${view}`} data-testid="workspace-views-fallback">
      {visible.map((task) => (
        <button
          className={`view-fallback__task ${selectedTaskId === task.id ? 'is-selected' : ''}`}
          key={task.id}
          onClick={() => onSelectTask(task.id)}
          type="button"
        >
          <span className={`priority-mark priority-mark--${task.priority.toLowerCase()}`} />
          <span>
            <small>{task.id}</small>
            <strong>{task.title}</strong>
          </span>
          <Pill tone={task.status}>{statusLabels[task.status as TaskStatus]}</Pill>
        </button>
      ))}
    </div>
  )
}
