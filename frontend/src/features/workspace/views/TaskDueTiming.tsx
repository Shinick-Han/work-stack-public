import { dueReasonForDate } from '../../focus/focusModel'
import type { WorkspaceTask } from './types'

export function TaskDueTiming({ task, today }: { task: WorkspaceTask; today: string }) {
  const active = task.status === 'open' || task.status === 'started'
  const reason = active ? dueReasonForDate(task.due ?? null, today) : null
  const label = reason?.label ?? task.due ?? 'No due date'
  const tone = reason?.kind ?? (task.due ? 'scheduled' : 'none')

  return (
    <span
      aria-label={`Due for ${task.id}: ${label}`}
      className={`wsv-due wsv-due--${tone}`}
      title={task.due ? `Due ${task.due}` : 'No due date'}
    >
      {label}
    </span>
  )
}
