import type { Task } from '../../domain/types'

interface TaskRelationshipsSectionProps {
  childTasks: Task[]
  dependentTasks: Task[]
  dependencyTasks: Task[]
  disabled: boolean
  onOpenTask: (taskId: string) => void
  parentTask: Task | null
}

export function TaskRelationshipsSection({
  childTasks,
  dependentTasks,
  dependencyTasks,
  disabled,
  onOpenTask,
  parentTask,
}: TaskRelationshipsSectionProps) {
  const relationships = [
    ...(parentTask ? [{ direction: 'Parent', task: parentTask }] : []),
    ...dependencyTasks.map((task) => ({ direction: 'Dependency', task })),
    ...childTasks.map((task) => ({ direction: 'Child', task })),
    ...dependentTasks.map((task) => ({ direction: 'Dependent', task })),
  ]
  if (!relationships.length) return null

  return (
    <section className="drawer-section">
      <h3>Task relationships</h3>
      <div className="task-relationship-links">
        {relationships.map(({ direction, task }) => (
          <button
            aria-label={`Open ${direction.toLowerCase()} ${task.id}`}
            disabled={disabled}
            key={`${direction}:${task.id}`}
            onClick={() => onOpenTask(task.id)}
            type="button"
          ><span>{direction}</span><strong>{task.id}</strong><small>{task.title}</small></button>
        ))}
      </div>
    </section>
  )
}
