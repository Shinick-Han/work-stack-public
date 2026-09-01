import { TASK_STATUSES, type Task, type TaskStatus } from './types'

export type TaskRelationshipField = 'parent_id' | 'dependencies'

export interface TaskBlocker {
  id: string
  title: string | null
  status: TaskStatus | null
}

export interface DependencyTask {
  id: string
  title: string
  status?: string | null
  dependencies?: readonly string[]
}

export type DependencyTaskIndex = ReadonlyMap<string, DependencyTask>

export function indexDependencyTasks(tasks: readonly DependencyTask[]): DependencyTaskIndex {
  return new Map(tasks.map((candidate) => [candidate.id, candidate]))
}

export function blockingDependenciesFromIndex(
  tasksById: DependencyTaskIndex,
  task: DependencyTask,
): TaskBlocker[] {
  return (task.dependencies ?? []).flatMap((dependencyId): TaskBlocker[] => {
    const dependency = tasksById.get(dependencyId)
    if (dependency?.status === 'done') return []
    return [{
      id: dependencyId,
      title: dependency?.title ?? null,
      status: dependency && TASK_STATUSES.includes(dependency.status as TaskStatus)
        ? dependency.status as TaskStatus
        : null,
    }]
  })
}

export function blockingDependencies(tasks: readonly DependencyTask[], task: DependencyTask): TaskBlocker[] {
  return blockingDependenciesFromIndex(indexDependencyTasks(tasks), task)
}

export function cyclicRelationshipCandidates(
  tasks: Task[],
  taskId: string,
  field: TaskRelationshipField,
) {
  const reverseEdges = new Map<string, string[]>()
  for (const task of tasks) {
    const targets = field === 'parent_id'
      ? task.parent_id ? [task.parent_id] : []
      : task.dependencies
    for (const target of targets) {
      reverseEdges.set(target, [...(reverseEdges.get(target) ?? []), task.id])
    }
  }

  const pending = [taskId]
  const cyclicCandidates = new Set<string>()
  while (pending.length) {
    const current = pending.pop()!
    for (const source of reverseEdges.get(current) ?? []) {
      if (cyclicCandidates.has(source)) continue
      cyclicCandidates.add(source)
      pending.push(source)
    }
  }
  cyclicCandidates.delete(taskId)
  return cyclicCandidates
}

export function relationshipWouldCycle(
  tasks: Task[],
  taskId: string,
  candidateId: string,
  field: TaskRelationshipField,
) {
  return cyclicRelationshipCandidates(tasks, taskId, field).has(candidateId)
}
